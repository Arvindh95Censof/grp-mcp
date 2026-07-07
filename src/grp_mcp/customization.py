"""Acumatica Customization Web API client (cookie-session auth).

Separate from AcumaticaClient: the /CustomizationApi/* endpoints reject OAuth
bearer tokens and require a cookie session via /entity/auth/login.

Methods map to the Customization Web API:
  getPublished, getProject, delete, import, publishBegin, publishEnd, unpublishAll

Publishing is asynchronous: call publishBegin, then poll publishEnd until the
response has "isCompleted": true. publishEnd runs the customization plug-ins, so
it must be called for publication to finish. Publishing is website-level and
affects ALL tenants on the instance.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Optional

import httpx

from .acumatica import SeatReliever, looks_like_seat_limit
from .config import Instance


class CustomizationError(RuntimeError):
    pass


class CustomizationClient:
    # Set once by server.py to _relieve_api_seats — a cookie login here holds a Web
    # Services API seat like any other, so a publish during a seat jam must be able
    # to free seats and retry instead of hard-failing.
    default_seat_reliever: Optional[SeatReliever] = None

    def __init__(self, instance: Instance) -> None:
        self.instance = instance
        self._http = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self._logged_in = False
        self._base = instance.base_url.rstrip("/")
        self.seat_reliever: Optional[SeatReliever] = None

    async def aclose(self) -> None:
        if self._logged_in:
            try:
                await self._http.post(f"{self._base}/entity/auth/logout")
            except Exception:
                pass
        await self._http.aclose()

    async def _login(self, _seat_retried: bool = False) -> None:
        if self._logged_in:
            return
        body: dict[str, Any] = {
            "name": self.instance.username,
            "password": self.instance.password,
        }
        if self.instance.tenant:
            body["company"] = self.instance.tenant
        if self.instance.branch:
            body["branch"] = self.instance.branch
        resp = await self._http.post(f"{self._base}/entity/auth/login", json=body)
        if resp.status_code not in (200, 204):
            # seat exhaustion: free the other cached sessions, retry once (parity with
            # the AcumaticaClient/ScreenClient login paths).
            if not _seat_retried and looks_like_seat_limit(resp.text, resp.status_code):
                reliever = self.seat_reliever or type(self).default_seat_reliever
                if reliever is not None:
                    try:
                        await reliever(self)
                    except Exception:  # noqa: BLE001 — best-effort; fall through
                        reliever = None
                    if reliever is not None:
                        return await self._login(_seat_retried=True)
            # surface only a structured error message — never echo the raw body of
            # the credential-bearing request's response (parity with _fetch_token).
            detail = ""
            try:
                err = resp.json()
                detail = err.get("exceptionMessage") or err.get("message") or ""
            except Exception:  # noqa: BLE001 — non-JSON body; omit it
                detail = ""
            raise CustomizationError(
                f"login failed ({resp.status_code})"
                + (f": {detail}" if detail else "")
                + ". Check the instance's username/password/tenant."
            )
        self._logged_in = True

    async def _call(self, method: str, body: dict | None = None) -> Any:
        await self._login()
        resp = await self._http.post(
            f"{self._base}/CustomizationApi/{method}",
            json=body or {},
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise CustomizationError(
                f"CustomizationApi/{method} -> {resp.status_code}: {resp.text}"
            )
        if not resp.content:
            return {"status": resp.status_code}
        ctype = resp.headers.get("Content-Type", "")
        return resp.json() if "json" in ctype else resp.text

    # ---- read ----------------------------------------------------------

    async def get_published(self) -> Any:
        return await self._call("getPublished")

    async def get_project(self, project_name: str) -> Any:
        return await self._call("getProject", {"projectName": project_name})

    # ---- write ---------------------------------------------------------

    async def delete(self, project_name: str) -> Any:
        return await self._call("delete", {"projectName": project_name})

    async def import_project(
        self,
        project_name: str,
        content_base64: str | None = None,
        project_level: int | None = None,
        is_replace_if_exists: bool = True,
        project_description: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "projectName": project_name,
            "isReplaceIfExists": is_replace_if_exists,
        }
        if content_base64 is not None:
            body["projectContentBase64"] = content_base64
        if project_level is not None:
            body["projectLevel"] = project_level
        if project_description is not None:
            body["projectDescription"] = project_description
        return await self._call("import", body)

    async def publish_begin(
        self,
        project_names: list[str],
        tenant_mode: str = "Current",
        tenant_login_names: list[str] | None = None,
        options: dict | None = None,
    ) -> Any:
        body: dict[str, Any] = {"projectNames": project_names, "tenantMode": tenant_mode}
        if tenant_login_names:
            body["tenantLoginNames"] = tenant_login_names
        if options:
            body.update(options)
        return await self._call("publishBegin", body)

    async def publish_end(self) -> Any:
        return await self._call("publishEnd")

    async def publish(
        self,
        project_names: list[str],
        tenant_mode: str = "Current",
        tenant_login_names: list[str] | None = None,
        options: dict | None = None,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
    ) -> dict:
        """Run the full async publish: begin, then poll end until complete."""
        await self.publish_begin(project_names, tenant_mode, tenant_login_names, options)
        waited = 0.0
        last: Any = None
        while waited < timeout:
            last = await self.publish_end()
            if isinstance(last, dict) and last.get("isCompleted"):
                return {
                    "completed": True,
                    "failed": bool(last.get("isFailed")),
                    "result": last,
                }
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        return {"completed": False, "failed": None, "result": last, "timeout": timeout}

    async def unpublish_all(
        self, tenant_mode: str = "Current", tenant_login_names: list[str] | None = None
    ) -> Any:
        body: dict[str, Any] = {"tenantMode": tenant_mode}
        if tenant_login_names:
            body["tenantLoginNames"] = tenant_login_names
        return await self._call("unpublishAll", body)


def encode_zip(path: str) -> str:
    """Read a customization .zip and return base64 (for import_project)."""
    data = Path(path).read_bytes()
    return base64.b64encode(data).decode("ascii")
