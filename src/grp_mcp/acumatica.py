"""Async Acumatica REST client with OAuth2 (resource-owner-password grant).

One client per Instance. Handles token fetch + refresh, then exposes thin
helpers over the contract-based REST endpoint and OData (Generic Inquiries).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Instance

# refresh a little before the token actually expires
_EXPIRY_SKEW = 30.0


class AcumaticaError(RuntimeError):
    pass


class AcumaticaClient:
    def __init__(self, instance: Instance) -> None:
        self.instance = instance
        # 120s read: a cold IIS site (first hit after app-pool recycle) or a very
        # wide single row (e.g. the 200+-column FeaturesSet DAC) can exceed 60s.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0), follow_redirects=True)
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._swagger: dict | None = None
        self._token_lock = asyncio.Lock()  # serialize token fetch -> one session, not N

    async def aclose(self) -> None:
        await self.logout()
        try:
            await self._http.aclose()
        except Exception:
            pass

    async def logout(self) -> None:
        """Close the API session to free the license seat (trial = 2 seats).

        Acumatica counts each unclosed sign-in against the Max Web Services API
        Users limit, so callers must release the session when done. Uses a fresh,
        short-lived httpx client so it works even from a shutdown handler running
        on a different event loop than the one self._http was created on.
        """
        token = self._access_token
        self._access_token = None
        self._expires_at = 0.0
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as h:
                await h.post(
                    f"{self.instance.base_url.rstrip('/')}/entity/auth/logout",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            pass

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
            # surface only the OAuth error CODE/description — never the raw body,
            # which is the one response adjacent to the credential-bearing request.
            detail = ""
            try:
                err = resp.json()
                detail = err.get("error") or ""
                if err.get("error_description"):
                    detail = f"{detail}: {err['error_description']}"
            except Exception:  # noqa: BLE001 — non-JSON error body; omit it
                detail = ""
            raise AcumaticaError(
                f"OAuth token request failed ({resp.status_code})"
                + (f": {detail}" if detail else "")
                + ". Check the instance's client_id/secret + username/password."
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 3600))

    async def _auth_header(self) -> dict[str, str]:
        if not self._access_token or time.monotonic() >= self._expires_at - _EXPIRY_SKEW:
            # serialize concurrent refreshes; re-check inside the lock so only the
            # first waiter actually logs in (avoids duplicate Acumatica sessions /
            # seat exhaustion when many tool calls fire at once)
            async with self._token_lock:
                if not self._access_token or time.monotonic() >= self._expires_at - _EXPIRY_SKEW:
                    await self._fetch_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _assert_allowed_url(self, url: str) -> None:
        """Refuse to attach the ERP bearer token to any URL outside the configured
        instance — SSRF / token-leak guard.

        Two checks, because same host is not enough: a caller-supplied URL (e.g.
        poll_action's Location, a download link) that shares the host but points at
        a DIFFERENT app under the same server (http://host/OtherApp/...) would still
        receive the token under an origin-only check. So we also require the URL's
        path to fall under the instance's base_url path prefix (e.g. /2026R1). When
        the site is hosted at the domain root (empty base path) there is nothing to
        scope to, so the origin check alone applies.
        """
        u = urlparse(url)
        origin = f"{u.scheme}://{u.netloc}".lower()
        if origin != self.instance.origin:
            raise AcumaticaError(
                f"Refusing authenticated request to '{origin}': only the configured "
                f"Acumatica origin '{self.instance.origin}' is allowed. (Blocked to "
                f"prevent leaking the OAuth token to an arbitrary URL.)"
            )
        base_path = urlparse(self.instance.base_url).path.rstrip("/")
        if base_path:
            req_path = u.path.rstrip("/") or "/"
            # allow the prefix itself and anything nested under it (prefix + "/")
            if req_path != base_path and not req_path.startswith(base_path + "/"):
                raise AcumaticaError(
                    f"Refusing authenticated request to path '{u.path}': only paths under "
                    f"the configured instance base '{base_path}/' receive the token "
                    f"(same host, different app path is blocked to prevent token leakage)."
                )

    # backward-compatible alias (older internal name)
    def _assert_same_origin(self, url: str) -> None:
        self._assert_allowed_url(url)

    # ---- request plumbing ----------------------------------------------

    async def _request_raw(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue an authenticated request and return the raw httpx.Response.

        Used for binary payloads (file downloads, report PDFs) and when the caller
        needs response headers (e.g. the Location of an async report). Refreshes the
        token once on 401 and raises AcumaticaError on >=400. Rejects any URL whose
        origin is not the configured instance (so the bearer token never leaves it).
        """
        self._assert_allowed_url(url)
        headers = await self._auth_header()
        headers.update(kwargs.pop("headers", {}))
        try:
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as e:
            # httpx timeout exceptions often stringify to "" — make the cause explicit.
            raise AcumaticaError(
                f"{method} {url} -> TIMED OUT ({type(e).__name__}) after the HTTP "
                f"read limit. Common causes: cold IIS instance (first request after "
                f"an app-pool recycle/publish) or a very wide row (e.g. FeaturesSet). "
                f"A retry usually succeeds once the site is warm.") from e
        if resp.status_code == 401:  # token rejected; force one refresh + retry
            self._access_token = None
            headers.update(await self._auth_header())
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise AcumaticaError(f"{method} {url} -> {resp.status_code}: {resp.text}")
        return resp

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        resp = await self._request_raw(method, url, headers=headers, **kwargs)

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

    @staticmethod
    def _detail_ref(spec: Any) -> str | None:
        """Schema name referenced by a detail (array) field's items, if any."""
        if isinstance(spec, dict) and spec.get("type") == "array":
            ref = (spec.get("items") or {}).get("$ref")
            if ref:
                return ref.split("/")[-1]
        return None

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

    async def get_entity_schema_deep(
        self, entity: str, refresh: bool = False, max_depth: int = 3
    ) -> dict:
        """Full field tree: scalars + every detail collection expanded to ITS OWN
        scalar/detail fields, recursively (cycle-guarded, depth-capped).

        Covers what get_entity_schema only names: each detail tab's nested fields.
        Returns {entity, field_count, scalar_fields, detail_fields: {name: {item,
        scalar_fields, detail_fields}}}.
        """
        doc = await self.get_swagger(refresh=refresh)
        schemas = (doc.get("components") or {}).get("schemas") or {}
        if entity not in schemas:
            close = sorted(s for s in schemas if entity.lower() in s.lower())
            raise AcumaticaError(
                f"Entity '{entity}' not in contract schemas. Similar: {close[:10]}"
            )

        def merged(name: str) -> dict:
            node = schemas.get(name) or {}
            props: dict = dict(node.get("properties") or {})
            for part in node.get("allOf") or []:
                if "$ref" in part:
                    ref = part["$ref"].split("/")[-1]
                    if ref != "Entity":
                        props.update(merged(ref))
                else:
                    props.update(part.get("properties") or {})
            return props

        counter = {"n": 0}

        def build(name: str, depth: int, seen: frozenset) -> dict:
            scalar: list[str] = []
            details: dict = {}
            for fn, spec in merged(name).items():
                if fn in self._META_FIELDS or fn == "_workflowActions":
                    continue
                counter["n"] += 1
                if self._is_detail(spec):
                    ref = self._detail_ref(spec)
                    if ref and ref not in seen and depth < max_depth:
                        details[fn] = {"item": ref,
                                       **build(ref, depth + 1, seen | {ref})}
                    else:
                        details[fn] = {"item": ref, "scalar_fields": [],
                                       "detail_fields": {},
                                       "note": "not expanded (cycle or max_depth)"}
                else:
                    scalar.append(fn)
            return {"scalar_fields": sorted(scalar), "detail_fields": details}

        tree = build(entity, 0, frozenset({entity}))
        return {"entity": entity, "deep": True,
                "field_count": counter["n"], **tree}

    def _abs(self, url: str) -> str:
        """Resolve a server-returned relative URL to an absolute one.

        Acumatica's _links/files:put/Location values are SITE-absolute — they already
        include the instance virtual directory (e.g. "/2025R1Setup/entity/..."). Those
        must be joined to the ORIGIN (scheme://host), not base_url, or the site segment
        doubles ("/2025R1Setup/2025R1Setup/...") and 401s. A link WITHOUT the site
        segment is joined to base_url. Absolute http(s) URLs pass through.
        """
        if url.startswith("http"):
            return url
        site = urlparse(self.instance.base_url).path.rstrip("/")  # e.g. "/2025R1Setup"
        if site and url.startswith(site + "/"):
            return f"{self.instance.origin}{url}"
        return f"{self.instance.base_url.rstrip('/')}{url}"

    async def get_url(self, url: str) -> Any:
        """GET an absolute or instance-relative URL (e.g. an action's Location)."""
        return await self._request("GET", self._abs(url))

    async def get_bytes(self, url: str) -> bytes:
        """GET raw bytes from an absolute or instance-relative URL (file download)."""
        resp = await self._request_raw("GET", self._abs(url))
        return resp.content

    async def get_all(
        self, entity: str, params: dict | None = None, page_size: int = 1000,
        max_records: int | None = None,
    ) -> list:
        """Retrieve every matching record by paging with $top/$skip.

        The contract API caps a single list GET (server RowsToFetch / proxy limits),
        so large tables need paging. Issues GETs with increasing $skip until a short
        (or empty) page comes back. Honors any caller $top as the page size and any
        $filter/$select/$expand passed in params.
        """
        base = dict(params or {})
        size = int(base.pop("$top", page_size) or page_size)
        if size <= 0:
            raise AcumaticaError(f"page_size must be >= 1 (got {size})")
        size = min(size, 10000)  # guard against absurd page sizes
        out: list = []
        skip = 0
        while True:
            page = dict(base)
            page["$top"] = size
            page["$skip"] = skip
            chunk = await self.get_entity(entity, None, page)
            if not isinstance(chunk, list):
                # single object or unexpected shape -> return as-is wrapped
                return [chunk] if chunk is not None else out
            out.extend(chunk)
            if max_records is not None and len(out) >= max_records:
                return out[:max_records]
            if len(chunk) < size:  # short page = last page
                break
            skip += size
        return out

    # ---- DAC-based OData v4 (raw data access classes) ------------------

    async def list_dacs(self) -> Any:
        """OData service document: every DAC exposed via the DAC-based OData interface."""
        return await self._request(
            "GET", self.instance.dac_odata_base, params={"$format": "json"}
        )

    async def run_dac(self, dac: str, params: dict | None = None) -> Any:
        """Query one DAC through the DAC-based OData v4 interface (<dac base>/<DAC>)."""
        url = f"{self.instance.dac_odata_base}/{dac}"
        p = {"$format": "json"}
        p.update(params or {})
        return await self._request("GET", url, params=p)

    async def dac_metadata(self) -> str:
        """Fetch the DAC-based OData CSDL ($metadata) as XML text.

        Returns the EDMX/CSDL document describing every exposed DAC: its
        properties, types, key, and Nullable flag (Nullable="false" = mandatory).
        Requested as XML on purpose — this platform's OData layer raises 500 on
        JSON metadata ("only supported at platform implementing .NETStandard 2.0"),
        and does NOT take $format. This is the only reliable mandatory-field source
        for DACs (incl. single-row config DACs like GLSetup that serve no collection).
        """
        url = f"{self.instance.dac_odata_base}/$metadata"
        return await self._request("GET", url, headers={"Accept": "application/xml"})

    # ---- report entities (contract API, async) -------------------------

    async def run_report(
        self, entity: str, body: dict, poll_interval: float = 2.0, timeout: float = 180.0
    ) -> bytes:
        """Run a Report-type entity and return the rendered file bytes (usually PDF).

        Contract flow: PUT the report entity with its parameters -> 202 + Location ->
        poll the Location (202 while rendering) -> 200 returns the binary file.
        `body` is the already-wrapped request body, e.g.
        {"parameters": {"OrgBAccountID": {"value": "MPM"}}}.
        """
        poll_interval = max(0.2, float(poll_interval))  # never 0 -> tight spin
        timeout = max(poll_interval, float(timeout))

        resp = await self._request_raw(
            "PUT", self._entity_url(entity), json=body,
            headers={"Accept": "application/pdf, application/json"},
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        location = resp.headers.get("Location")
        if not location:
            raise AcumaticaError(
                f"report '{entity}' returned {resp.status_code} with no Location to poll"
            )
        if location.startswith("/"):
            location = f"{self.instance.base_url.rstrip('/')}{location}"
        waited = 0.0
        while waited < timeout:
            r = await self._request_raw(
                "GET", location, headers={"Accept": "application/pdf, application/json"}
            )
            if r.status_code == 200 and r.content:
                return r.content
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        raise AcumaticaError(f"report '{entity}' did not finish within {timeout}s")

    # ---- file attachments (files:put) ----------------------------------

    async def put_file(
        self, url: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> Any:
        """PUT raw file bytes to a record's files:put URL (absolute or relative).

        The {filename} placeholder must already be substituted in `url`.
        """
        return await self._request(
            "PUT", self._abs(url), content=content, headers={"Content-Type": content_type}
        )

    async def record_files_put_url(
        self, entity: str, record_id: str, filename: str
    ) -> str:
        """Resolve the files:put URL for a record by reading its _links."""
        rec = await self.get_entity(entity, record_id)
        link = (rec.get("_links") or {}).get("files:put") if isinstance(rec, dict) else None
        if not link:
            raise AcumaticaError(
                f"No files:put link on {entity}/{record_id} - the record may not "
                f"exist or the entity does not support file attachments."
            )
        return link.replace("{filename}", filename)

    def constructed_files_put_url(
        self, graph: str, view: str, record_id: str, filename: str
    ) -> str:
        """Build a files:put URL WITHOUT reading the record first.

        Some entities 500 on GET-by-id and can't resolve their _links — notably
        `DataProvider`, whose `Link` field carries a BQL delegate (the server
        raises CannotOptimize / NoEntitySatisfiesTheCondition on both get-by-id
        and list). When the graph type + primary view are known, the files:put
        path is deterministic and matches the server's own link template:
            {entity_base}/files/<GraphType>/<View>/<id>/<filename>
        (e.g. .../files/PX.Api.SYProviderMaint/Providers/<id>/<file>).
        """
        return (
            f"{self.instance.entity_base}/files/"
            f"{graph}/{view}/{record_id}/{filename}"
        )

    def provider_files_put_url(self, record_id: str, filename: str) -> str:
        """files:put URL for a Data Provider (SM206015) record, GET-free.

        The DataProvider contract entity 500s on read-back, so resolve its
        upload URL by template instead. Graph = PX.Api.SYProviderMaint, the
        file view = Providers.
        """
        return self.constructed_files_put_url(
            "PX.Api.SYProviderMaint", "Providers", record_id, filename
        )

    # ---- OData (Generic Inquiries) -------------------------------------

    async def run_gi(self, name: str, params: dict | None = None) -> Any:
        url = f"{self.instance.odata_base}/{name}"
        return await self._request("GET", url, params=params or {})

    async def list_generic_inquiries(self) -> Any:
        """OData service document: the Generic Inquiries exposed via OData."""
        return await self._request(
            "GET", self.instance.odata_base, params={"$format": "json"}
        )
