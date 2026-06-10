"""Async Acumatica REST client with OAuth2 (resource-owner-password grant).

One client per Instance. Handles token fetch + refresh, then exposes thin
helpers over the contract-based REST endpoint and OData (Generic Inquiries).
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .config import Instance

# refresh a little before the token actually expires
_EXPIRY_SKEW = 30.0


class AcumaticaError(RuntimeError):
    pass


class AcumaticaClient:
    def __init__(self, instance: Instance) -> None:
        self.instance = instance
        self._http = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._swagger: dict | None = None

    async def aclose(self) -> None:
        await self.logout()
        await self._http.aclose()

    async def logout(self) -> None:
        """Close the API session to free the license seat (trial = 2 seats).

        Acumatica counts each unclosed sign-in against the Max Web Services API
        Users limit, so callers must release the session when done.
        """
        if not self._access_token:
            return
        try:
            await self._http.post(
                f"{self.instance.base_url.rstrip('/')}/entity/auth/logout",
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        except Exception:
            pass
        self._access_token = None
        self._expires_at = 0.0

    # ---- auth -----------------------------------------------------------

    async def _fetch_token(self) -> None:
        inst = self.instance
        if self._refresh_token:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": inst.client_id,
                "client_secret": inst.client_secret,
            }
        else:
            data = {
                "grant_type": "password",
                "username": inst.username,
                "password": inst.password,
                "client_id": inst.client_id,
                "client_secret": inst.client_secret,
                "scope": "api offline_access",
            }
        resp = await self._http.post(
            inst.token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            # a stale refresh token fails -> drop it and retry with password
            if self._refresh_token:
                self._refresh_token = None
                await self._fetch_token()
                return
            raise AcumaticaError(
                f"OAuth token request failed ({resp.status_code}): {resp.text}"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))

    async def _auth_header(self) -> dict[str, str]:
        if not self._access_token or time.monotonic() >= self._expires_at - _EXPIRY_SKEW:
            await self._fetch_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    # ---- request plumbing ----------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = await self._auth_header()
        headers.update(kwargs.pop("headers", {}))
        headers.setdefault("Accept", "application/json")
        resp = await self._http.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:  # token rejected; force one refresh + retry
            self._access_token = None
            headers.update(await self._auth_header())
            resp = await self._http.request(method, url, headers=headers, **kwargs)

        if resp.status_code >= 400:
            raise AcumaticaError(f"{method} {url} -> {resp.status_code}: {resp.text}")

        if resp.status_code == 204 or not resp.content:
            location = resp.headers.get("Location")
            return {"status": resp.status_code, "location": location}
        ctype = resp.headers.get("Content-Type", "")
        return resp.json() if "json" in ctype else resp.text

    # ---- contract REST API ---------------------------------------------

    def _entity_url(self, entity: str, suffix: str = "") -> str:
        url = f"{self.instance.entity_base}/{entity}"
        return f"{url}/{suffix}" if suffix else url

    async def get_entity(
        self, entity: str, record_id: str | None = None, params: dict | None = None
    ) -> Any:
        return await self._request(
            "GET", self._entity_url(entity, record_id or ""), params=params or {}
        )

    async def put_entity(self, entity: str, body: dict) -> Any:
        return await self._request("PUT", self._entity_url(entity), json=body)

    async def delete_entity(self, entity: str, record_id: str) -> Any:
        return await self._request("DELETE", self._entity_url(entity, record_id))

    async def invoke_action(self, entity: str, action: str, body: dict) -> Any:
        return await self._request(
            "POST", self._entity_url(entity, action), json=body
        )

    # ---- metadata / discovery ------------------------------------------

    async def list_endpoints(self) -> Any:
        """All web service endpoints published on the instance (name/version/href)."""
        url = f"{self.instance.base_url.rstrip('/')}/entity"
        return await self._request("GET", url)

    async def get_swagger(self, refresh: bool = False) -> dict:
        """OpenAPI document for the configured endpoint (cached per client).

        The endpoint-root metadata GET ({endpoint}/{version}/) is often proxy-gated
        (401), so entity/field discovery is sourced from swagger.json instead.
        """
        if self._swagger is None or refresh:
            url = f"{self.instance.entity_base}/swagger.json"
            self._swagger = await self._request("GET", url)
        return self._swagger

    async def list_entities(self, refresh: bool = False) -> list[str]:
        """Top-level entity names exposed by the configured endpoint contract."""
        doc = await self.get_swagger(refresh=refresh)
        tops: set[str] = set()
        for path in doc.get("paths") or {}:
            m = re.match(r"/([^/{]+)", path)
            if m:
                tops.add(m.group(1))
        return sorted(tops)

    async def list_actions(self, entity: str, refresh: bool = False) -> list[str]:
        """Action names invokable on an entity (literal POST sub-paths in contract)."""
        doc = await self.get_swagger(refresh=refresh)
        prefix = f"/{entity}/"
        acts: set[str] = set()
        for path, ops in (doc.get("paths") or {}).items():
            if not path.startswith(prefix):
                continue
            seg = path[len(prefix):]
            if seg and "{" not in seg and "/" not in seg:
                if "post" in {k.lower() for k in ops}:
                    acts.add(seg)
        return sorted(acts)

    _META_FIELDS = ("id", "rowNumber", "note", "_links", "custom", "files")

    async def _merged_props(self, entity: str, refresh: bool = False) -> dict:
        """Resolve an entity's properties, merging the allOf base + detail parts."""
        doc = await self.get_swagger(refresh=refresh)
        schemas = (doc.get("components") or {}).get("schemas") or {}
        if entity not in schemas:
            close = sorted(s for s in schemas if entity.lower() in s.lower())
            raise AcumaticaError(
                f"Entity '{entity}' not in contract schemas. Similar: {close[:10]}"
            )
        node = schemas[entity]
        props: dict = dict(node.get("properties") or {})
        for part in node.get("allOf") or []:
            if "$ref" not in part:  # skip the shared base Entity ref
                props.update(part.get("properties") or {})
        return props

    @staticmethod
    def _is_detail(spec: Any) -> bool:
        """A field is detail/nested when it's an array (detail collection)."""
        return isinstance(spec, dict) and spec.get("type") == "array"

    async def detail_fields(self, entity: str, refresh: bool = False) -> set[str]:
        """Detail/nested (array) field names — omitted from list GETs by Acumatica."""
        try:
            props = await self._merged_props(entity, refresh=refresh)
        except AcumaticaError:
            return set()
        return {n for n, spec in props.items()
                if self._is_detail(spec) and n not in self._META_FIELDS}

    async def get_entity_schema(self, entity: str, refresh: bool = False) -> dict:
        """Field names for one entity, split into scalar vs detail (nested) fields.

        detail_fields are the array/nested collections Acumatica OMITS from a list
        GET — fetch them per record by key (record_id), optionally with expand=.
        """
        props = await self._merged_props(entity, refresh=refresh)
        scalar, detail = [], []
        for name, spec in props.items():
            if name in self._META_FIELDS:
                continue
            (detail if self._is_detail(spec) else scalar).append(name)
        return {
            "entity": entity,
            "field_count": len(scalar) + len(detail),
            "scalar_fields": sorted(scalar),
            "detail_fields": sorted(detail),
            "note": "detail_fields are omitted from list GETs; retrieve them by "
                    "record_id (optionally with expand=) one record at a time.",
        }

    async def get_url(self, url: str) -> Any:
        """GET an absolute or instance-relative URL (e.g. an action's Location)."""
        if url.startswith("/"):
            url = f"{self.instance.base_url.rstrip('/')}{url}"
        return await self._request("GET", url)

    # ---- OData (Generic Inquiries) -------------------------------------

    async def run_gi(self, name: str, params: dict | None = None) -> Any:
        url = f"{self.instance.odata_base}/{name}"
        return await self._request("GET", url, params=params or {})

    async def list_generic_inquiries(self) -> Any:
        """OData service document: the Generic Inquiries exposed via OData."""
        return await self._request(
            "GET", self.instance.odata_base, params={"$format": "json"}
        )
