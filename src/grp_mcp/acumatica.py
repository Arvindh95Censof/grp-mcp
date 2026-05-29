"""Async Acumatica REST client with OAuth2 (resource-owner-password grant).

One client per Instance. Handles token fetch + refresh, then exposes thin
helpers over the contract-based REST endpoint and OData (Generic Inquiries).
"""

from __future__ import annotations

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

    async def aclose(self) -> None:
        await self._http.aclose()

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

    # ---- OData (Generic Inquiries) -------------------------------------

    async def run_gi(self, name: str, params: dict | None = None) -> Any:
        url = f"{self.instance.odata_base}/{name}"
        return await self._request("GET", url, params=params or {})
