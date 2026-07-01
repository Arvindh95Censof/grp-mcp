"""Screen-based SOAP client (the typed /Soap/<ScreenID>.asmx API).

The contract-based REST API addresses records by key and cannot write screens
that only enable an action once a parent context is loaded (popup / master-detail
/ context screens — e.g. Segment Values CS203000). The screen-based SOAP API
replays UI command sequences *as a user*, so it drives the screen WITH context.

This is a thin, dependency-free async client (pure httpx — no zeep): Login,
GetSchema, Submit, Logout. It reuses the instance's username/password/tenant.

IMPORTANT — seats: every Login holds one of the instance's "Max Web Services API
Users" seats (a trial allows only 2). Always Logout. Use `async with
ScreenClient(...) as s:` so logout runs even on error; leaking sessions yields
"API Login Limit" faults until they idle-time-out.
"""

from __future__ import annotations

import copy
import re
import uuid
import xml.etree.ElementTree as ET
from html import escape, unescape
from typing import Any

import httpx

from .config import Instance

_XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("xsi", _XSI)

_TNS = "http://www.acumatica.com/typed/"
_ENV_OPEN = (
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    f'xmlns:tns="{_TNS}" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    "<soap:Body>"
)
_ENV_CLOSE = "</soap:Body></soap:Envelope>"

# A persisted Submit returns a tiny empty <SubmitResult/> (~335 bytes); a body
# larger than this is the screen echoing its full content = the commands did not
# bind (no-bind). Used to flag suspected silent no-persist on an HTTP-200 result.
_NOBIND_LEN = 1500

# Headers the modern UI-screen protocol (/t/<Tenant>/ui/screen/<ScreenID>) expects.
_UI_HEADERS = {
    "Accept": "application/json,text/html",
    "X-Requested-With": "Fetch",
    "Content-Type": "application/json",
}


class ScreenError(RuntimeError):
    pass


class ScreenClient:
    """One screen-based SOAP session, bound to a single screen.

    screen_id: e.g. "CS203000". The service lives at
    {base_url}/Soap/{screen_id}.asmx and Login/Logout are session-wide.
    """

    def __init__(self, instance: Instance, screen_id: str, timeout: float = 120.0) -> None:
        self.instance = instance
        self.screen_id = screen_id.upper()
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        self._logged_in = False
        self._tree: ET.Element | None = None
        self._ui_booted = False
        self._classic_used = False  # guard: don't mix classic + modern graph state

    @property
    def url(self) -> str:
        return f"{self.instance.base_url.rstrip('/')}/Soap/{self.screen_id}.asmx"

    @property
    def login_name(self) -> str:
        """Screen-API login name. Multi-tenant sites need user@Tenant."""
        u = self.instance.username
        t = self.instance.tenant
        return f"{u}@{t}" if t and "@" not in u else u

    # ---- transport ------------------------------------------------------

    async def _call(self, op: str, inner_xml: str) -> str:
        # classic SOAP op: mark it so the modern plane re-bootstraps a clean graph
        # if these get interleaved in one session (they keep separate graph state).
        self._classic_used = True
        resp = await self._http.post(
            self.url,
            content=(_ENV_OPEN + inner_xml + _ENV_CLOSE).encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": _TNS + op,
            },
        )
        text = resp.text
        if "<soap:Fault>" in text or "<faultstring>" in text:
            m = re.search(r"<faultstring>(.*?)</faultstring>", text, re.S)
            msg = re.sub(r"\s+", " ", m.group(1)).strip() if m else text[:400]
            # A PXSetupNotEnteredException means the screen's MODULE isn't configured
            # yet — GetSchema/Submit 500 until the named *Preferences/Setup* form is
            # filled in. Surface that as actionable guidance (a prerequisite to set up
            # first), not an opaque 500. The exception names the setup graph
            # (e.g. ...[PX.Objects.AR.ARSetup]) and the form ("...Preferences form").
            if "PXSetupNotEnteredException" in msg:
                setup = re.search(r"\[([\w.]+)\]", msg)
                detail = re.search(r"Error:\s*(.*?)(?: at |---|$)", msg)
                raise ScreenError(
                    f"{op} on {self.screen_id}: PREREQUISITE NOT MET — "
                    f"{(detail.group(1).strip() if detail else msg)} "
                    f"(setup graph: {setup.group(1) if setup else '?'}). "
                    f"Configure that Preferences/Setup form first, then retry."
                )
            # surface the real PX inner exception, not the SOAP wrapper boilerplate
            inner = re.search(r"PX\.\w[\w.]*Exception: ([^\n]+?)(?: at |---)", msg)
            raise ScreenError(
                f"{op} on {self.screen_id}: {inner.group(1).strip() if inner else msg}"
            )
        if resp.status_code >= 400:
            raise ScreenError(f"{op} on {self.screen_id} -> HTTP {resp.status_code}")
        return text

    # ---- session --------------------------------------------------------

    async def login(self) -> None:
        await self._call(
            "Login",
            f"<tns:Login><tns:name>{escape(self.login_name)}</tns:name>"
            f"<tns:password>{escape(self.instance.password)}</tns:password></tns:Login>",
        )
        self._logged_in = True

    async def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        try:
            await self._call("Logout", "<tns:Logout/>")
        except Exception:
            pass

    async def aclose(self) -> None:
        await self.logout()
        try:
            await self._http.aclose()
        except Exception:
            pass

    # ---- modern UI-screen plane (/ui/screen/<ScreenID>) ------------------
    #
    # Some dialog-driven actions (confirmed: GL201000 "Generate Calendar") are
    # exposed in the classic typed-SOAP schema but their server-side handler
    # isn't wired up on that endpoint — Submit returns a clean empty success
    # with zero effect. The REAL implementation lives behind the modern UI's
    # own JSON protocol at /t/<Tenant>/ui/screen/<ScreenID>, which the browser
    # itself calls. Reverse-engineered live (2026-07-01): it shares the SAME
    # cookie session as the classic SOAP Login above (same ASP.NET app), so no
    # separate auth is needed — just reuse self._http after login().
    #
    # Protocol shape (JSON POST, Content-Type: application/json):
    #   bootstrap:  {"isFirstRequest": true, "data": [], ...}                (once)
    #   set field:  {"data": [{"viewName": V, "fieldName": F, "value": val,
    #                           "rowId": "", "changeType": 5}], ...}
    #   fire cmd:   {"command": [{"name": cmd}], "data": [], ...}
    #     -> 200 if it just executes, OR
    #     -> 302 {"redirects":[{"settings":{"type":"openDialog","viewName":V}}]}
    #        meaning a confirmation dialog would open client-side; answer it:
    #   confirm:    {"command": [{"name": cmd}], "data": [],
    #                "dialogCallback": {"dialogResult": 1, "validateInput": false,
    #                                   "viewName": V}, ...}
    # dialogResult follows the public PX.Data.WebDialogResult enum: None=0,
    # OK=1, Cancel=2, Abort=3, Retry=4, Ignore=5, Yes=6, No=7.

    @property
    def ui_url(self) -> str:
        return f"{self.instance.base_url.rstrip('/')}/t/{self.instance.tenant}/ui/screen/{self.screen_id}"

    @staticmethod
    def _ui_error(resp: httpx.Response) -> str | None:
        """Parse a modern UI-screen response into a human error string, or None if OK.

        The plane carries structured errors: a `messages[]` array of typed
        messages (messageType error/warning/info), and for setup/validation
        faults a `{type, title, detail}` envelope (e.g. type=SetupNotEntered when
        the screen's module isn't configured). We surface those instead of a raw
        truncated body.

        A 200 is a FAILURE only if it carries an explicit `messageType:"error"`
        message — a warning/info message (or one with no type) on an otherwise-OK
        200 is NOT an error (avoids false positives on informational notices). A
        >=400 always surfaces (all its messages, else its type/title, else body).
        """
        j = None
        try:
            j = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON body
            j = None
        failed = resp.status_code >= 400
        if isinstance(j, dict):
            if j.get("type") == "SetupNotEntered":
                return ("PREREQUISITE NOT MET — this screen's module is not configured "
                        "yet (SetupNotEntered). Configure its Preferences/Setup form first.")
            msgs = j.get("messages") or []
            # on a 200, only explicit error-type messages count; on a failure, surface all.
            picked = [m["message"] for m in msgs if m.get("message") and (
                failed or str(m.get("messageType", "")).lower() == "error")]
            if picked:
                return "; ".join(picked)
            if failed:
                return f"{j.get('type') or 'Error'}: {j.get('detail') or j.get('title') or resp.text[:200]}"
        if failed:
            return f"HTTP {resp.status_code}: {resp.text[:300]}"
        return None

    async def ui_bootstrap(self, views: list[str] | None = None) -> None:
        """Load the modern-UI graph, populating the given views from the DB.

        Pass the views you'll EDIT so their existing record loads: else a Save
        validates a half-empty record and fails on the untouched required fields
        (e.g. editing one GL-preference checkbox with the record unloaded →
        "'Retained Earnings Account' cannot be empty"). For a pure dialog/process
        action with no field edits, views can be empty.

        NOTE: intentionally does NOT send `clearSession:true` — that resets the
        whole graph including the company/branch + selected-record context, which
        breaks process actions that depend on it (e.g. GL201000 generateYears →
        "Select a company..."). Cross-plane isolation is handled by the one-plane-
        per-session rule instead (each tool uses only classic OR only modern),
        with a best-effort re-bootstrap in _ui_post if the two ever interleave.
        """
        vp = {v: {} for v in (views or [])}
        await self._http.post(
            self.ui_url,
            json={"isFirstRequest": True, "data": [], "controlsParams": {},
                  "activeRowContexts": [], "viewsParams": vp},
            headers=_UI_HEADERS,
        )
        self._ui_booted = True
        self._classic_used = False

    async def _ui_post(self, payload: dict) -> httpx.Response:
        # Ensure a graph exists (fallback bootstrap). Re-bootstrap if a classic SOAP
        # op ran since (the planes keep separate graph state — interleaving them in
        # one session can collide, e.g. a 409 on Save). Callers editing an existing
        # record should call ui_bootstrap([views]) FIRST so the record loads.
        if not self._ui_booted or self._classic_used:
            await self.ui_bootstrap()
        return await self._http.post(self.ui_url, json=payload, headers=_UI_HEADERS)

    async def get_ui_structure(self) -> dict:
        """Read the modern UI-screen `/structure` — the schema/metadata endpoint.

        The modern-plane analog of get_schema(): returns the screen's views +
        fields (type, required, readonly, enabled, and ENUM allowed-values), the
        action inventory (enabled/visible/confirmation message), and grid key
        fields. Use it to discover what ui_set_field/ui_command can drive on any
        screen — no browser capture needed. Read-only GET (stateless, no bootstrap).
        """
        resp = await self._http.get(self.ui_url + "/structure", headers={"Accept": "application/json"})
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"get_ui_structure {self.screen_id}: {err}")
        d = resp.json()
        views: dict[str, list] = {}
        for vname, fields in (d.get("fieldStates") or {}).items():
            if not isinstance(fields, list):
                continue
            out = []
            for f in fields:
                st = f.get("fieldState") or {}
                opts = st.get("options")
                out.append({
                    "field": f.get("fieldName"),
                    "label": st.get("text"),
                    "type": st.get("typeName"),
                    "required": bool(st.get("required")),
                    "readonly": bool(st.get("readOnly")),
                    "enabled": st.get("enabled", True) is not False,
                    "options": ([{"value": o.get("value"), "text": o.get("text")} for o in opts]
                                if opts else None),
                })
            views[vname] = out
        actions = [
            {"name": name, "label": st.get("text"),
             "enabled": st.get("enabled", True) is not False,
             "visible": st.get("visible", True) is not False,
             "confirm": st.get("confirmationMessage")}
            for name, st in (d.get("actionStates") or {}).items() if isinstance(st, dict)
        ]
        grids = {
            cname: {"key_fields": cd.get("dataKeyNames"),
                    "columns": [c.get("field") for c in (cd.get("columns") or []) if isinstance(c, dict)]}
            for cname, cd in (d.get("controlsData") or {}).items()
            if isinstance(cd, dict) and cd.get("dataKeyNames")
        }
        return {"screen_id": self.screen_id, "primary_dac": d.get("primaryDacName"),
                "views": views, "actions": actions, "grids": grids}

    async def ui_set_field(self, view: str, field: str, value: str) -> None:
        """Set one field via the modern UI-screen protocol (see class docstring above).

        Value formats: strings/enums = the raw code (for enums use the option
        `value`, not its display text — see get_ui_structure); booleans = "true"/
        "false". The set lands in the graph working state; a following ui_command
        ("Save" or a screen action) commits it. Do NOT interleave with classic
        get_schema/export/submit on the same session (separate graph state).
        """
        resp = await self._ui_post({
            "data": [{"viewName": view, "fieldName": field, "value": str(value),
                       "rowId": "", "changeType": 5}],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_set_field {view}.{field} on {self.screen_id}: {err}")

    async def ui_command(self, name: str) -> dict:
        """Fire a modern UI-screen command; auto-answers OK if it opens a dialog.

        Field values set via ui_set_field() beforehand persist server-side in the
        session and don't need to be resent here. `name` is the internal command
        (from get_ui_structure `actions`), e.g. "Save", "generateYears". A 302
        `openDialog` reply is auto-confirmed (WebDialogResult.OK). Raises with the
        parsed `messages[]` on a business/validation error.
        """
        resp = await self._ui_post({
            "command": [{"name": name}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        if resp.status_code == 302:
            body = resp.json()
            view = None
            for r in body.get("redirects", []):
                settings = r.get("settings", {})
                if settings.get("type") == "openDialog":
                    view = settings.get("viewName")
                    break
            resp = await self._ui_post({
                "command": [{"name": name}], "data": [],
                "dialogCallback": {"dialogResult": 1, "validateInput": False, "viewName": view},
                "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
            })
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_command {name} on {self.screen_id}: {err}")
        return resp.json()

    # ---- modern-plane GRID editing (existing-row update) ----------------
    #
    # Editing an EXISTING grid row is NOT possible on the classic screen-SOAP
    # plane (RowNumber doesn't move the cursor — see _spec_to_command). The
    # modern UI does it through controlsParams.<grid>.changes.modified[], which
    # the browser was captured sending (2026-07-01, GL202500). The row is
    # matched server-side by the KEY field(s) included in `values` — omit the key
    # and the server treats it as an INSERT of a blank row ("<field> cannot be
    # empty"). The `columns` list + pager fields must be echoed back or the Save
    # returns a clean 200 that persists NOTHING (proven: minimal payload no-ops).

    async def ui_grid_read(self, grid_view: str, parent: dict | None = None) -> dict:
        """Fresh grid read via the modern plane (clearSession → live DB rows).

        Returns {columns, rows, key_names, quick_filter_fields}. `rows` items are
        {id, cells:{Field:{value,...}}}. clearSession forces a DB reload so stale
        graph-session state isn't returned.

        parent (MASTER-DETAIL): {"view": <primaryView>, "key": {keyField: value}}.
        A detail grid only populates under its selected header, so when `parent` is
        given the master is navigated first (its key set on the primary view via a
        changeType:5 field-set) and the CHILD grid is co-requested in the SAME graph
        state. The master stays current on this session, so a following grid write
        targets it (and the child's parent-link id is auto-filled server-side).
        parent=None → a top-level grid. (Proven: CA202000 CashAccount→ETDetails.)
        """
        if parent:
            pv = parent["view"]
            await self._http.post(self.ui_url, json={
                "isFirstRequest": True, "data": [], "controlsParams": {},
                "activeRowContexts": [], "viewsParams": {pv: {}}, "clearSession": True,
            }, headers=_UI_HEADERS)
            (pf, pval), = parent["key"].items()
            resp = await self._http.post(self.ui_url, json={
                "data": [{"viewName": pv, "fieldName": pf, "value": str(pval),
                          "rowId": "", "changeType": 5}],
                "controlsParams": {}, "activeRowContexts": [],
                "viewsParams": {pv: {}, grid_view: {}},
            }, headers=_UI_HEADERS)
        else:
            resp = await self._http.post(self.ui_url, json={
                "isFirstRequest": True, "data": [], "controlsParams": {},
                "activeRowContexts": [], "viewsParams": {grid_view: {}}, "clearSession": True,
            }, headers=_UI_HEADERS)
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_grid_read {grid_view} on {self.screen_id}: {err}")
        cd = ((resp.json().get("controlsData") or {}).get(grid_view)) or {}
        self._ui_booted, self._classic_used = True, False
        return {"columns": cd.get("columns"), "rows": cd.get("rows") or [],
                "key_names": cd.get("dataKeyNames") or [],
                "quick_filter_fields": cd.get("quickFilterFields") or []}

    @staticmethod
    def _cell_val(row: dict, field: str):
        return (row.get("cells") or {}).get(field, {}).get("value")

    @classmethod
    def _cell_key(cls, row: dict, field: str):
        """Raw key value of a cell — a lookup/selector cell holds {id,text}; use id."""
        v = cls._cell_val(row, field)
        return v.get("id") if isinstance(v, dict) else v

    @staticmethod
    def _kv(d: dict) -> list:
        """{field: value} -> [{"field","value"}] (bools kept, everything else str)."""
        return [{"field": k, "value": (v if isinstance(v, bool) else str(v))}
                for k, v in d.items()]

    def _locate_row(self, rows: list, key: dict):
        """(index, row) of the row whose key cells match every key field, else (None, None)."""
        for i, row in enumerate(rows):
            if all(str(self._cell_key(row, k) or "").strip() == str(v).strip()
                   for k, v in key.items()):
                return i, row
        return None, None

    def _full_key(self, row: dict, key_names: list) -> dict:
        """The row's COMPLETE key from its cells — incl. the parent-linkage id for a
        detail row (e.g. {CashAccountID: 994, EntryTypeID: 'BANKCHG'}). Falls back to
        empty if the grid exposes no dataKeyNames."""
        return {kn: self._cell_key(row, kn) for kn in (key_names or [])}

    def _grid_ctrl(self, grid_view: str, g: dict, changes: dict, key: dict | None) -> dict:
        """controlsParams.<grid> block for a Save. The columns + pager fields MUST be
        echoed or the Save persists nothing (a minimal payload returns a clean 200
        no-op — proven)."""
        ctrl = {
            "view": grid_view, "columns": g["columns"],
            "generateColumns": 0, "retrieveMode": 0, "pagerMode": 1, "startRow": 0,
            "pageIndex": 0, "pageSize": max(len(g["rows"]) + 1, 1),
            "preserveSortsAndFilters": True, "syncPosition": True,
            "refreshFilters": False, "suppressStoredFilters": False,
            "fastFilterByAllFields": True, "fastFilter": "",
            "filterID": "00000000-0000-0000-0000-000000000000",
            "quickFilterFields": g["quick_filter_fields"],
            "changes": changes, "isRequestOwner": False, "resultType": "GridData",
        }
        if key is not None:
            ctrl["dataKey"] = key
        return ctrl

    async def _grid_save(self, grid_view: str, g: dict, changes: dict,
                          dataKey: dict | None, op: str, parent: dict | None = None) -> dict:
        """POST a grid Save (changes = {modified|inserted|deleted: [...]}) and raise on error.

        dataKey present (update/delete) → sent as the ctrl dataKey (+ an
        activeRowContexts entry for a top-level grid); None (insert) → no dataKey.
        parent (master-detail) → the master view is re-listed in viewsParams so the
        Save keeps the header context; activeRowContexts stays empty (the loaded
        master, not a row-context, anchors the child)."""
        ctrl = self._grid_ctrl(grid_view, g, changes, dataKey)
        views = {grid_view: {}}
        if parent:
            views[parent["view"]] = {}
        payload = {
            "command": [{"name": "Save"}], "data": [],
            "controlsParams": {grid_view: ctrl},
            "activeRowContexts": ([{"dataView": grid_view, "syncPosition": True,
                                     "dataKey": dataKey, "resultType": "GridActiveDataRow"}]
                                   if (dataKey is not None and not parent) else []),
            "viewsParams": views,
        }
        resp = await self._http.post(self.ui_url, json=payload, headers=_UI_HEADERS)
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"{op} {grid_view}{dataKey or ''} on {self.screen_id}: {err}")
        return resp.json()

    async def ui_update_grid_row(self, grid_view: str, key: dict, values: dict,
                                 parent: dict | None = None) -> dict:
        """Update ONE existing grid row in place, matched by its key field(s).

        key:    {keyField: value} — the child-identifying key (for a detail grid the
                parent-linkage id is resolved from the row automatically).
        values: {field: newValue} cells to change. The full key is re-sent in the
                row's `values` so the server UPDATES (not inserts). Idempotent.
        parent: {"view", "key"} to target a detail grid under a header (see
                ui_grid_read). None = top-level grid.
        """
        g = await self.ui_grid_read(grid_view, parent)
        idx, row = self._locate_row(g["rows"], key)
        if row is None:
            raise ScreenError(f"ui_update_grid_row: no row in {grid_view} matches key {key}")
        full = self._full_key(row, g["key_names"]) or key
        change = {"id": row.get("id"), "index": idx, "values": self._kv(full) + self._kv(values)}
        return await self._grid_save(grid_view, g, {"modified": [change]}, full,
                                     "ui_update_grid_row", parent)

    async def ui_insert_grid_row(self, grid_view: str, values: dict,
                                 parent: dict | None = None) -> dict:
        """Append a NEW grid row. `values` MUST include the grid's key field(s) plus
        any other required columns (e.g. GL202500 needs AccountCD + Type + Description).
        For a detail grid (parent set) the parent-linkage id is auto-filled server-side,
        so `values` needs only the child fields. A client rowId is generated."""
        g = await self.ui_grid_read(grid_view, parent)
        change = {"id": str(uuid.uuid4()), "index": len(g["rows"]), "values": self._kv(values)}
        return await self._grid_save(grid_view, g, {"inserted": [change]}, None,
                                     "ui_insert_grid_row", parent)

    async def ui_delete_grid_row(self, grid_view: str, key: dict,
                                 parent: dict | None = None) -> dict:
        """Delete an existing grid row matched by its key field(s). The full key (incl.
        the parent-linkage id for a detail row) is sent inside the deleted row's
        `values` — required, else the delete no-ops. parent targets a detail grid."""
        g = await self.ui_grid_read(grid_view, parent)
        idx, row = self._locate_row(g["rows"], key)
        if row is None:
            raise ScreenError(f"ui_delete_grid_row: no row in {grid_view} matches key {key}")
        full = self._full_key(row, g["key_names"]) or key
        change = {"id": row.get("id"), "index": idx, "values": self._kv(full)}
        return await self._grid_save(grid_view, g, {"deleted": [change]}, full,
                                     "ui_delete_grid_row", parent)

    async def __aenter__(self) -> "ScreenClient":
        await self.login()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- operations -----------------------------------------------------

    async def get_schema_xml(self) -> str:
        return await self._call("GetSchema", "<tns:GetSchema/>")

    async def get_schema(self) -> dict:
        """Parse GetSchema into {container: {friendly_field: {object, field}}}.

        The schema's field descriptors carry the exact ObjectName + FieldName the
        Submit engine expects (e.g. Segment.DimensionID, Values.Value) plus the
        per-container service commands (NewRow/Key/DeleteRow). This is what you
        feed back into submit().
        """
        xml = await self.get_schema_xml()
        containers: dict[str, dict] = {}
        # each top-level container is <Name>...<DisplayName>..</DisplayName>...</Name>
        for cm in re.finditer(r"<(\w+)><DisplayName>(.*?)</DisplayName>(.*?)</\1>", xml, re.S):
            cname, _disp, body = cm.group(1), cm.group(2), cm.group(3)
            fields: dict[str, dict] = {}
            for fm in re.finditer(
                r"<(\w+)><FieldName>([^<]*)</FieldName><ObjectName>([^<]*)</ObjectName>",
                body,
            ):
                friendly, field, obj = fm.group(1), fm.group(2), fm.group(3)
                if friendly in ("ServiceCommands",):
                    continue
                fields.setdefault(friendly, {"object": obj, "field": field})
            if fields:
                containers[cname] = fields
        return {"screen_id": self.screen_id, "containers": containers}

    # ---- schema tree (for descriptor-based commands) -------------------

    async def _ensure_tree(self) -> ET.Element:
        """Fetch + parse GetSchema into an element tree (cached per session).

        The tree's container elements hold each field's FULL descriptor —
        FieldName, ObjectName, Value, Commit, and crucially the LinkedCommand
        navigation chain. Building Submit commands by cloning these descriptors
        (and overwriting the value) replays the chain, which is what actually
        loads/navigates the record. Bare hand-built commands omit the chain and
        silently no-op (Submit returns ok but nothing persists).
        """
        if self._tree is None:
            xml = await self.get_schema_xml()
            m = re.search(r"<GetSchemaResult>(.*)</GetSchemaResult>", xml, re.S)
            inner = m.group(1) if m else xml
            self._tree = ET.fromstring(
                f'<root xmlns:xsi="{_XSI}">{inner}</root>'
            )
        return self._tree

    def _find_field(self, name: str) -> ET.Element:
        """Locate a field/action descriptor by friendly name.

        `name` is the schema's friendly element name (e.g. "CustomerID",
        "AccountName", "Save"); use "Container.Field" to disambiguate when the
        same friendly name appears in more than one container.
        """
        root = self._tree
        if "." in name:
            cont, fname = name.split(".", 1)
            c = root.find(cont)
            el = c.find(fname) if c is not None else None
            if el is None:
                raise ScreenError(f"field {name!r} not found in schema")
            return copy.deepcopy(el)
        matches = []
        for cont in list(root):
            for child in list(cont):
                if child.tag in ("ServiceCommands", "DisplayName"):
                    continue
                if child.tag == name:
                    matches.append((cont.tag, child))
        if not matches:
            raise ScreenError(f"field {name!r} not found in any container")
        if len(matches) > 1:
            where = ", ".join(f"{c}.{name}" for c, _ in matches)
            raise ScreenError(f"field {name!r} is ambiguous — qualify it: {where}")
        return copy.deepcopy(matches[0][1])

    def _service(self, container: str, which: str) -> ET.Element:
        """Find a service command (NewRow/DeleteRow/...) under a container."""
        root = self._tree
        c = root.find(container)
        sc = c.find("ServiceCommands") if c is not None else None
        el = sc.find(which) if sc is not None else None
        if el is None:
            raise ScreenError(
                f"service command {which!r} not found under {container!r}"
            )
        return copy.deepcopy(el)

    def _has_service(self, container: str, which: str) -> bool:
        """True if `container` exposes the named service command (e.g. DialogAnswer)."""
        root = self._tree
        c = root.find(container)
        sc = c.find("ServiceCommands") if c is not None else None
        return sc is not None and sc.find(which) is not None

    def _primary_container(self) -> str | None:
        """The first non-meta container in the schema tree (the screen's main view)."""
        for cont in list(self._tree):
            if cont.tag not in ("Actions",):
                return cont.tag
        return None

    @staticmethod
    def _referenced_containers(commands: list[dict]) -> list[str]:
        """Containers named by command specs (the bit before '.' on set/key, or the
        whole value on new_row/delete_row/answer). Order-preserving, de-duped."""
        seen: list[str] = []
        for c in commands:
            cont = None
            if "new_row" in c:
                cont = c["new_row"]
            elif "delete_row" in c:
                cont = c["delete_row"]
            elif "answer" in c:
                cont = c["answer"]
            elif "set" in c and "." in c["set"]:
                cont = c["set"].split(".", 1)[0]
            elif "key" in c and "." in c["key"]:
                cont = c["key"].split(".", 1)[0]
            if cont and cont not in seen:
                seen.append(cont)
        return seen

    @staticmethod
    def _wrap(el: ET.Element, xsi_type: str, value: str | None) -> str:
        if value is not None:
            v = el.find("Value")
            if v is None:
                v = ET.SubElement(el, "Value")
            v.text = str(value)
        kids = "".join(ET.tostring(c, encoding="unicode") for c in el)
        return (
            f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" '
            f'xsi:type="{xsi_type}">{kids}</Command>'
        )

    def _spec_to_command(self, c: dict) -> str:
        """Turn one ergonomic command spec into descriptor-based command XML.

        Specs:
          {"set": "<FriendlyName>", "to": <value>}  set a field (navigates if key)
          {"action": "<FriendlyName>"}              click a button (e.g. "Save")
          {"new_row": "<Container>"}                add a detail row
          {"delete_row": "<Container>"}             delete the current detail row
          {"answer": "<Container>", "to": "Yes"}    answer a pop-up dialog
        """
        if "key" in c:
            # bare Key command (flat FieldName/ObjectName/Value) — selects an
            # existing parent record. Some screens (e.g. CS203000's segment
            # selector) navigate via Key, not via a descriptor-Value set.
            el = self._find_field(c["key"])
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            return (
                f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" xsi:type="Key">'
                f"<FieldName>{escape(fld)}</FieldName>"
                f"<ObjectName>{escape(obj)}</ObjectName>"
                f"<Value>{escape(str(c.get('to', '')))}</Value></Command>"
            )
        if "set" in c:
            return self._wrap(self._find_field(c["set"]), "Value", c.get("to"))
        if "action" in c:
            return self._wrap(self._find_field(c["action"]), "Action", None)
        if "row" in c:
            # DISABLED (2026-07-01): the RowNumber service command does NOT position
            # the grid cursor on this API — proven end-to-end on GL202500: a
            # {"row":8} followed by a set silently edited row 1 (10100), returning a
            # clean 335-byte "success" with no error. That is a silent WRONG-ROW
            # write (data corruption footgun), so we refuse it rather than pretend to
            # target a row. To edit an EXISTING grid row: (a) if the row is
            # key-navigable, set its key on a master screen (set_record); (b) for
            # pure detail grids, use the modern UI-screen plane (rowId-addressed) —
            # tracked separately. Appending rows (new_row) is unaffected and works.
            raise ScreenError(
                "positional row selection ({\"row\": N}) is unsupported: the "
                "screen-SOAP RowNumber command does not move the grid cursor, so a "
                "following set/delete would silently hit row 1 (wrong-row write). "
                "Edit an existing grid row via ui_update_grid_row (modern plane, "
                "addressed by key); use new_row only to APPEND."
            )
        if "new_row" in c:
            return self._wrap(self._service(c["new_row"], "NewRow"), "NewRow", None)
        if "delete_row" in c:
            return self._wrap(
                self._service(c["delete_row"], "DeleteRow"), "DeleteRow", None
            )
        if "answer" in c:
            return self._wrap(
                self._service(c["answer"], "DialogAnswer"), "Answer", c.get("to")
            )
        raise ScreenError(f"unrecognized command spec: {c!r}")

    def _answer_commands(self, commands: list[dict], answer: str) -> list[dict]:
        """Build {"answer", "to"} specs for each referenced container that exposes a
        DialogAnswer service command, falling back to the primary container.

        Many Save/Release actions raise a confirmation dialog ("Are you sure?")
        that the API surfaces as a generic fault; appending an Answer command and
        re-submitting clears it. Only containers that actually have a DialogAnswer
        get one — answering a container that has none just faults again.
        """
        conts = [c for c in self._referenced_containers(commands)
                 if self._has_service(c, "DialogAnswer")]
        if not conts:
            pc = self._primary_container()
            if pc and self._has_service(pc, "DialogAnswer"):
                conts = [pc]
        return [{"answer": c, "to": answer} for c in conts]

    async def submit(
        self,
        commands: list[dict],
        dry_run: bool = False,
        auto_answer: str | None = None,
    ) -> dict:
        """Submit an ergonomic command sequence; return parsed result.

        Commands reference the schema's friendly field/action names (from
        get_schema) — the client clones the matching descriptor (with its
        LinkedCommand navigation chain) so the record is actually loaded/edited.

        Spec shapes (see _spec_to_command): {"set","to"}, {"action"},
        {"new_row"}, {"delete_row"}, {"answer","to"}.

        dry_run=True drops the committing commands (button actions + row deletes)
        so the field SETs run but nothing persists — a safe preview that still
        surfaces field-level errors.

        auto_answer (e.g. "Yes"): if the Submit faults, re-submit once with a
        DialogAnswer appended for each referenced container that exposes one —
        clears confirmation pop-ups ("Are you sure?") that would otherwise block
        the action. Skipped under dry_run.

        Recipe — update a record: set the key field, set other fields, Save:
            [{"set":"CustomerID","to":"ABARTENDE"},
             {"set":"AccountName","to":"New Name"},
             {"action":"Save"}]
        Add a detail row: set the parent key(s), new_row the detail container,
        set the row's fields, Save.
        """
        await self._ensure_tree()
        if dry_run:
            # preview: drop the committing commands (button actions + row deletes)
            # so the field SETs run but nothing persists; surfaces field errors.
            commands = [c for c in commands if not ("action" in c or "delete_row" in c)]
        inner = "".join(self._spec_to_command(c) for c in commands)
        try:
            xml = await self._call(
                "Submit", f"<tns:Submit><tns:commands>{inner}</tns:commands></tns:Submit>"
            )
        except ScreenError as e:
            # First chance: a confirmation dialog blocked the action. Re-submit once
            # with the dialog answered, if the caller opted in and an answerable
            # container exists.
            if auto_answer and not dry_run:
                answers = self._answer_commands(commands, auto_answer)
                if answers:
                    try:
                        ai = "".join(
                            self._spec_to_command(c) for c in (commands + answers)
                        )
                        ax = await self._call(
                            "Submit",
                            f"<tns:Submit><tns:commands>{ai}</tns:commands></tns:Submit>",
                        )
                        errs = self._parse_field_errors(ax)
                        ar = {
                            "screen_id": self.screen_id,
                            "ok": not errs,
                            "answered": auto_answer,
                            "messages": [x["message"] for x in errs],
                            "field_errors": errs,
                            "raw_len": len(ax),
                        }
                        # answering a dialog and getting a big content echo is the
                        # classic false-positive (looks ok, persists nothing).
                        if not errs and len(ax) > _NOBIND_LEN:
                            ar["nobind_suspected"] = True
                            ar["warning"] = (
                                "Dialog answered but Submit returned a full-content "
                                "echo, not the small empty result of a persisted "
                                "write — likely nothing bound. Read the record back."
                            )
                        return ar
                    except ScreenError as e2:
                        e = e2  # fall through to diagnostics with the post-answer fault
            # A fatal action (Save/Delete/AutoFill) faulted — the SOAP fault only
            # carries a generic "record raised at least one error". Re-run just the
            # field SETs (no actions, so nothing commits) and read the per-field
            # IsError/Message from that Content to surface WHY.
            field_errors = []
            diag = [c for c in commands if ("set" in c or "key" in c)]
            if diag and len(diag) < len(commands):
                try:
                    di = "".join(self._spec_to_command(c) for c in diag)
                    dx = await self._call(
                        "Submit",
                        f"<tns:Submit><tns:commands>{di}</tns:commands></tns:Submit>",
                    )
                    field_errors = self._parse_field_errors(dx)
                except ScreenError:
                    pass
            return {
                "screen_id": self.screen_id,
                "ok": False,
                "error": str(e),
                "field_errors": field_errors,
                "messages": [f["message"] for f in field_errors],
            }
        errors = self._parse_field_errors(xml)
        result = {
            "screen_id": self.screen_id,
            "ok": not errors,
            "dry_run": dry_run,
            "messages": [e["message"] for e in errors],
            "field_errors": errors,
            "raw_len": len(xml),
        }
        # No-bind guard: a persisted Submit returns a tiny empty <SubmitResult/>
        # (~335 bytes). A multi-KB body is the screen re-rendering its full
        # content because the commands did NOT bind (e.g. a row that silently
        # failed to commit, or navigation that didn't take) — the API still
        # reports HTTP 200 / no field error, so without this the caller would
        # read it as success. Flag it; the caller should read back to confirm.
        if not errors and not dry_run and len(xml) > _NOBIND_LEN:
            result["nobind_suspected"] = True
            result["warning"] = (
                "Submit returned a full-content echo (not the small empty result a "
                "persisted write returns) — the commands may not have bound. Verify "
                "by reading the record back; check that navigation selected the "
                "intended record."
            )
        return result

    async def insert_rows(
        self,
        container: str,
        rows: list[dict],
        header: dict | None = None,
        save: bool = True,
        auto_answer: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Insert N grid/detail rows into `container` in one transaction.

        header: field sets applied first (the parent/context, e.g. a document key)
                — keys may be friendly or "Container.Field".
        rows:   list of {field: value}; each becomes NewRow + the field SETs. Field
                names are the schema's friendly names (qualify "Container.Field" if a
                name repeats). One Save commits them all.

        This is the master-detail / bulk-grid writer (e.g. Chart of Accounts rows,
        subaccount segments). Returns the submit() result.
        """
        cmds: list[dict] = []
        for k, v in (header or {}).items():
            cmds.append({"set": k, "to": v})
        for row in rows:
            cmds.append({"new_row": container})
            for k, v in row.items():
                cmds.append({"set": k, "to": v})
        if save:
            cmds.append({"action": "Save"})
        return await self.submit(cmds, dry_run=dry_run, auto_answer=auto_answer)

    async def set_record(
        self,
        key_field: str,
        key_value: str,
        fields: dict,
        insert: bool = False,
        save: bool = True,
        auto_answer: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create or edit ONE record on a master-style screen.

        insert=False (default): set the key field, which NAVIGATES to the existing
            record (via its descriptor's LinkedCommand chain), then set `fields` and
            Save — an in-place edit.
        insert=True: click Insert first to start a fresh record, then set the key +
            `fields` and Save — a create.

        key_field/fields use friendly schema names (qualify "Container.Field" if a
        name repeats). Returns the submit() result. For grid screens with many rows
        per Save, use insert_rows instead.
        """
        cmds: list[dict] = []
        if insert:
            cmds.append({"action": "Insert"})
        cmds.append({"set": key_field, "to": key_value})
        for k, v in fields.items():
            cmds.append({"set": k, "to": v})
        if save:
            cmds.append({"action": "Save"})
        return await self.submit(cmds, dry_run=dry_run, auto_answer=auto_answer)

    @staticmethod
    def _parse_field_errors(xml: str) -> list[dict]:
        """Extract per-field errors from a Submit Content response.

        An errored field comes back as a Value element carrying <Message> +
        <IsError>true</IsError> (the API reports field errors inside an HTTP 200,
        not as a SOAP fault). Returns [{field, object, message, level}].
        """
        out: list[dict] = []
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError:
            # fall back to a loose scan
            for m in re.findall(r"<Message>([^<]+)</Message>", xml):
                out.append({"field": None, "object": None,
                            "message": re.sub(r"\s+", " ", m).strip(), "level": None})
            return out
        for el in root.iter():
            msg = el.find("Message")
            iserr = el.find("IsError")
            if msg is not None and msg.text and (iserr is None or iserr.text == "true"):
                out.append({
                    "field": (el.findtext("FieldName") or None),
                    "object": (el.findtext("ObjectName") or None),
                    "message": re.sub(r"\s+", " ", msg.text).strip(),
                    "level": (el.findtext("ErrorLevel") or None),
                })
        return out

    async def export(
        self, fields: list[str], top: int = 10, filters: list[dict] | None = None
    ) -> dict:
        """Read current values from a screen via the Export SOAP operation.

        fields: schema friendly field names (qualify Container.Field if ambiguous)
                — the columns to return. top: max rows.
        filters: optional row filters, each {"field": "<Friendly>", "value": ...,
                "condition": "Equals"|"Contain"|"StartsWith"|"Greater"|... (default
                Equals)} — e.g. read one record by its key field.
        Returns {fields, headers, rows} where rows is a list of {header: value}.
        This is the read counterpart to submit(): the screen-based API's Export
        returns the live grid/record data (Submit alone doesn't echo it).
        """
        await self._ensure_tree()
        cols = []
        for f in fields:
            el = self._find_field(f)
            # Export columns are SIMPLE field references — FieldName + ObjectName
            # only. The full descriptor (with its LinkedCommand navigation chain)
            # confuses Export and collapses the result to one column.
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            cols.append(
                f'<Command xmlns="{_TNS}" xmlns:xsi="{_XSI}" xsi:type="Field">'
                f"<FieldName>{escape(fld)}</FieldName>"
                f"<ObjectName>{escape(obj)}</ObjectName></Command>"
            )
        fxml = ""
        for flt in filters or []:
            el = self._find_field(flt["field"])
            fld = el.findtext("FieldName") or ""
            obj = el.findtext("ObjectName") or ""
            cond = flt.get("condition", "Equals")
            val = escape(str(flt.get("value", "")))
            # Filter.Value is anyType — it MUST carry an explicit xsi:type or the
            # server fails to cast it (XmlNode[] -> String). Strings cover the
            # common key/field-match case.
            fxml += (
                f'<Filter xmlns="{_TNS}" xmlns:xsi="{_XSI}" '
                f'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
                f'<Field xsi:type="Field"><FieldName>{escape(fld)}</FieldName>'
                f"<ObjectName>{escape(obj)}</ObjectName></Field>"
                f'<Condition>{escape(cond)}</Condition>'
                f'<Value xsi:type="xsd:string">{val}</Value>'
                f"<OpenBrackets>0</OpenBrackets><CloseBrackets>0</CloseBrackets>"
                f"<Operator>And</Operator></Filter>"
            )
        inner = (
            f"<tns:Export><tns:commands>{''.join(cols)}</tns:commands>"
            f"<tns:filters>{fxml}</tns:filters><tns:topCount>{int(top)}</tns:topCount>"
            f"<tns:includeHeaders>true</tns:includeHeaders>"
            f"<tns:breakOnError>false</tns:breakOnError></tns:Export>"
        )
        xml = await self._call("Export", inner)
        rows: list[list[str]] = []
        try:
            root = ET.fromstring(xml.encode("utf-8"))
            for aos in root.iter():
                if aos.tag.split("}")[-1] == "ArrayOfString":
                    rows.append([(s.text or "") for s in list(aos)])
        except ET.ParseError:
            pass
        if not rows:
            return {"screen_id": self.screen_id, "fields": fields, "headers": [], "rows": []}
        headers = rows[0]
        return {
            "screen_id": self.screen_id,
            "fields": fields,
            "headers": headers,
            "rows": [dict(zip(headers, r)) for r in rows[1:]],
        }
