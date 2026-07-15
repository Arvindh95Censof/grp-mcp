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

import asyncio
import copy
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from html import escape, unescape
from typing import Any

import httpx

from .acumatica import looks_like_seat_limit
from .config import Instance

# --- shared UI-plane session cache (one login per instance identity) ----------
# Each ScreenClient used to log in independently, so N concurrent ui_* calls opened N
# logins and blew the "concurrent API logins" cap (a ~2-min lockout). This caches the
# forms-auth COOKIES per instance identity so concurrent/subsequent ScreenClients reuse a
# single login instead of each minting one. Entries idle-expire; a reused cookie that the
# server has since dropped is invalidated on the next auth failure so the next call re-logs.
_SESSION_CACHE: dict[str, dict] = {}          # key -> {"cookies": httpx.Cookies, "at": ts, "kind": str}
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}  # key -> lock (serialize logins per identity)
# Reuse a cached cookie for up to 15 min. This is a LOCAL freshness bound, not the
# server's: Acumatica's forms-auth idle timeout is typically 20-60 min, so the old 240s
# meant any pause longer than 4 min (routine between agent turns) forced a logout+login
# — 2 extra serial round-trips before the tool did any work. `at` is refreshed on every
# reuse (see _ensure_login), so a continuously-used session never goes stale at all.
_SESSION_TTL = 900.0


def _session_lock(key: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(key)
    if lock is None:
        lock = _SESSION_LOCKS[key] = asyncio.Lock()
    return lock


# --- shared UI-plane /structure cache (ETag-validated) ------------------------
# /structure is the ONLY discovery endpoint on the modern plane (no slimming params
# exist: ?fields=/?parts=/?$select= are ignored, /schema + /fields + /views 404), and
# it is FAT — measured live on 25.101: AP301000 264KB, IN202500 250KB, FA303000 91KB.
# It was re-fetched on every call from ~15 call sites with no cache, because the only
# cache (_ui_meta) is per-ScreenClient and every tool builds a fresh client.
#
# The endpoint sends an ETag, so we revalidate instead of re-downloading: a conditional
# GET costs ~100ms/0 bytes vs ~280ms/270KB (measured). Entries store the etag + the
# parsed projection.
#
# CAUTION — the ETag is NOT a per-screen content hash. It is an environment stamp that
# is IDENTICAL for every screen on a tenant:
#     25.101.0153.0049$0$<user>$<tenant>$en-MY$61$$28323058
#     build            $?$db         $tenant      $locale$user$$metadata-version
# Verified live: sending AP301000's ETag to GL101000's URL returns 304. The server does
# NOT scope the validator per screen, so a cache that mixes up keys will silently serve
# the WRONG screen's structure. Hence the key includes screen_id and we only ever send
# an entry's OWN etag back to its OWN url. The user + locale ride in the stamp too, so
# the key includes the session identity (base_url|username|tenant) rather than base_url.
_STRUCT_CACHE: dict[str, dict] = {}  # key -> {"etag": str|None, "parsed": dict}
# Seconds an entry is served with NO revalidation at all. 0 (default) = always send a
# conditional GET, which is always correct. Raise it via GRP_MCP_STRUCT_TTL to trade a
# staleness window for ~100ms/call; a customization publish clears the cache regardless.
_STRUCT_TTL = float(os.environ.get("GRP_MCP_STRUCT_TTL", "0") or 0)


# --- shared UI-plane HTTP pool ------------------------------------------------
# Every ScreenClient used to build its own httpx.AsyncClient, so although the LOGIN was
# shared via _SESSION_CACHE, the connection pool was not: each of the ~60 per-tool-call
# ScreenClients paid a fresh TCP + TLS handshake (~2-3 RTT) before its first byte. These
# are pooled by session identity (so cookie scope matches _SESSION_CACHE exactly — a
# pooled client is never shared across sites/users/tenants) plus timeout (only 3 call
# sites use a non-default one, all short polls). Pooled clients outlive any single
# ScreenClient, so aclose() must NOT close them; close_http_pool() does, at shutdown.
_HTTP_POOL: dict[str, httpx.AsyncClient] = {}


def _pooled_http(key: str, timeout: float) -> httpx.AsyncClient:
    pk = f"{key}|{timeout}"
    c = _HTTP_POOL.get(pk)
    if c is None or c.is_closed:
        c = _HTTP_POOL[pk] = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    return c


async def close_http_pool() -> list[str]:
    """Close every pooled UI-plane HTTP client. For process shutdown only — closing a
    pooled client mid-session forces the next ScreenClient to re-handshake."""
    items = list(_HTTP_POOL.items())
    _HTTP_POOL.clear()
    for _, c in items:
        try:
            await c.aclose()
        except Exception:  # noqa: BLE001 — best-effort at shutdown
            pass
    return [k for k, _ in items]


def clear_struct_cache(key: str | None = None) -> list[str]:
    """Drop cached /structure entries (all, or every entry for one identity prefix).

    Call after anything that can change screen metadata — publishing/unpublishing a
    customization is the real one, since that bumps the metadata-version segment of
    the ETag for every screen at once.
    """
    if key is None:
        cleared = list(_STRUCT_CACHE)
        _STRUCT_CACHE.clear()
        return cleared
    hit = [k for k in _STRUCT_CACHE if k.startswith(key)]
    for k in hit:
        _STRUCT_CACHE.pop(k, None)
    return hit


def clear_session_cache(key: str | None = None) -> list[str]:
    """Drop cached UI-plane sessions LOCALLY (all, or one identity key). Returns keys
    cleared. NOTE: this only forgets the cookie — it does NOT end the session
    server-side, so the ASP.NET forms-auth session (and its Web Services API seat)
    lives on until idle-timeout. To free the seat NOW, use `logout_session_cache`
    (async). Kept sync for non-network callers (tests, _invalidate_session)."""
    if key is None:
        cleared = list(_SESSION_CACHE)
        _SESSION_CACHE.clear()
        return cleared
    return [key] if _SESSION_CACHE.pop(key, None) is not None else []


async def logout_session_cache(key: str | None = None) -> list[str]:
    """Server-side LOG OUT cached UI-plane sessions, then drop them — freeing the seat
    immediately instead of at idle-timeout. Returns identity keys logged out.

    Fixes the shared-session seat LEAK: a cached cookie session is marked `_shared`,
    so neither its creator nor any reuser logs it out on aclose (by design — the cache
    owns it). Previously the ONLY disposal path, clear_session_cache(), just dropped the
    local dict entry, leaving the ASP.NET forms-auth session alive server-side holding a
    'Max Web Services API Users' seat until it idle-timed-out. This posts
    /entity/auth/logout with each cached session's own cookies first, ending it now.
    Best-effort: a failed logout still drops the entry (idle-timeout is the backstop).

    key: log out one identity (base_url|user|tenant); omit for ALL cached sessions."""
    if key is None:
        items = list(_SESSION_CACHE.items())
        _SESSION_CACHE.clear()
    else:
        entry = _SESSION_CACHE.pop(key, None)
        items = [(key, entry)] if entry is not None else []
    done: list[str] = []
    for k, entry in items:
        if not entry:
            continue
        base_url = k.split("|", 1)[0].rstrip("/")  # key == base_url|username|tenant
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                         cookies=entry.get("cookies")) as c:
                # the contract logout ends the forms-auth session both login kinds share
                await c.post(f"{base_url}/entity/auth/logout")
        except Exception:  # noqa: BLE001 — best-effort; drop it regardless
            pass
        done.append(k)
    return done

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

# Export/screen_get filter conditions the screen SOAP API accepts, plus operator
# aliases. #7: the tool used to read only the key "condition" and pass its value
# through verbatim — an unknown key ("op") or an operator symbol (">=") was silently
# ignored and the condition defaulted to Equals, returning a wrong (Equals) result
# set with no warning. Normalize aliases and REJECT anything unrecognized loudly.
_FILTER_CONDITIONS = {
    "Equals", "NotEqual", "Greater", "GreaterOrEqual", "Less", "LessOrEqual",
    "Contains", "StartsWith", "EndsWith", "IsNull", "IsNotNull", "Between",
}
_CONDITION_ALIASES = {
    "=": "Equals", "==": "Equals", "eq": "Equals", "equal": "Equals", "equals": "Equals",
    "!=": "NotEqual", "<>": "NotEqual", "ne": "NotEqual", "notequal": "NotEqual",
    ">": "Greater", "gt": "Greater", "greater": "Greater",
    ">=": "GreaterOrEqual", "gte": "GreaterOrEqual", "greaterorequal": "GreaterOrEqual",
    "<": "Less", "lt": "Less", "less": "Less",
    "<=": "LessOrEqual", "lte": "LessOrEqual", "lessorequal": "LessOrEqual",
    "contains": "Contains", "startswith": "StartsWith", "endswith": "EndsWith",
    "isnull": "IsNull", "isnotnull": "IsNotNull", "between": "Between",
}


def _normalize_condition(flt: dict) -> str:
    """Resolve a filter's condition from `condition` or the `op` alias; reject
    unknown keys or unrecognized operators LOUDLY (no silent Equals fallback)."""
    unknown = set(flt) - {"field", "value", "condition", "op"}
    if unknown:
        raise ValueError(
            f"filter has unknown key(s) {sorted(unknown)}; allowed: field, value, "
            f"condition (or its alias 'op')")
    raw = flt.get("condition", flt.get("op"))
    if raw is None:
        return "Equals"
    if raw in _FILTER_CONDITIONS:
        return raw
    norm = _CONDITION_ALIASES.get(str(raw).strip().lower())
    if norm:
        return norm
    raise ValueError(
        f"unrecognized filter condition {raw!r}. Use an Acumatica condition "
        f"({', '.join(sorted(_FILTER_CONDITIONS))}) or an operator alias "
        f"(=, !=, >, >=, <, <=, contains, startswith, ...).")

# Headers the modern UI-screen protocol (/t/<Tenant>/ui/screen/<ScreenID>) expects.
_UI_HEADERS = {
    "Accept": "application/json,text/html",
    "X-Requested-With": "Fetch",
    "Content-Type": "application/json",
}


class ScreenError(RuntimeError):
    pass


def _tree_ancestor_values(ancestor_keys: list[dict] | None) -> list:
    """The bare key VALUES of a node's ancestors (root → immediate parent), in order.
    ancestor_keys items are single-field {keyField: value} dicts."""
    return [next(iter(a.values())) for a in (ancestor_keys or [])]


def _tree_active_row_context(tree_view: str, node_key: dict,
                              ancestor_keys: list[dict] | None) -> dict:
    """The `activeRowContexts` entry for selecting one TREE node (pure, unit-testable).

    node_key is the node's own {keyField: value}; ancestor_keys is its ancestor path
    (root → immediate parent). selectedNodeParentId is the IMMEDIATE parent's value
    (None for a root node).
    """
    (kf, kv) = next(iter(node_key.items()))
    anc = _tree_ancestor_values(ancestor_keys)
    return {"dataView": tree_view, "syncPosition": True, "dataKey": {kf: kv},
            "selectedNodeParentId": anc[-1] if anc else None,
            "resultType": "TreeActiveDataRow"}


def _tree_control_block(tree_view: str, node_key: dict, ancestor_keys: list[dict] | None,
                         columns: list[str], key_fields: list[str]) -> dict:
    """The `controlsParams.<tree_view>` echo a tree-node select/command needs.

    A bare `{}` isn't enough — the server can't resolve the key field's CLR type
    without the column list (proven live: "Cannot determine the param type of
    <view>"). `columns`/`key_fields` come from ui_get_structure's `grids[tree_view]`.

    `parameters` is the FULL ancestor path plus the node: [<ancestor values>, None,
    <nodeValue>] (a root node — no ancestors — uses [nodeValue, None, nodeValue]).
    A depth-2 node (e.g. a DETAIL collection under an entity) fails to select if only
    its immediate parent is sent instead of the whole chain — proven live, 2026-07-02
    (activeRowId came back null until the full [root, entity, None, detail] path was
    used).
    """
    (kf, kv) = next(iter(node_key.items()))
    anc = _tree_ancestor_values(ancestor_keys)
    parameters = [*anc, None, kv] if anc else [kv, None, kv]
    return {
        "view": tree_view, "columns": columns or [], "treeKeys": key_fields or [kf],
        "parameters": parameters,
        "hideRootNode": True, "openedLayers": 1, "dynamic": True,
        "syncPosition": True, "dataKey": {kf: kv},
        "selectedNodeParentId": anc[-1] if anc else None, "refreshColumns": False,
        "resultType": "TreeData",
    }


def _tree_context_views(view_names: list[str], tree_view: str, node_key: dict) -> dict:
    """viewsParams for a tree node's "context" views (e.g. SelectedEndpoint on SM207060).

    Omitting these still returns HTTP 200 with no error, but silently fails to
    establish server-side selection state — proven live: a payload missing only
    this was indistinguishable from success (200, no message) until the FOLLOWING
    command also silently no-op'd. Heuristic: any /structure view named
    "Selected*" other than the tree itself — holds for SM207060, unverified on
    other tree screens (see ui_select_tree_node's select_command note).
    """
    (kf, kv) = next(iter(node_key.items()))
    return {v: {"parameters": {kf: kv}} for v in view_names
            if v.startswith("Selected") and v != tree_view}


def _leaf(class_name: str | None) -> str | None:
    """Leaf class name of a dotted/nested .NET type, lowercased (pure, unit-testable).

    "Payroll.Graph.Entry.CSPYOvertimeRate" -> "cspyovertimerate";
    "Ns.Outer+Inner" -> "inner". None/empty -> None. Used to compare a selector's
    backing graph to the screen's own graph regardless of namespace differences.
    """
    if not class_name:
        return None
    return class_name.replace("+", ".").rsplit(".", 1)[-1].lower() or None


def _selector_meta(field_state: dict) -> dict | None:
    """Extract a selector (lookup) field's query metadata from its raw /structure
    fieldState, or None if the field isn't a selector (pure, unit-testable).

    Reverse-engineered live (2026-07-02, SM207060 CreateEntityView.ScreenID +
    PopulateFilterView.Container): a selector field carries what's needed to query
    its OWN grid sub-endpoint —
      • `graph` isn't given directly, but `fieldDacName` follows .NET's `Outer+Inner`
        nested-class convention and its outer part IS the grid query's graph class
        (confirmed: "PX.Api.ContractBased.UI.EntityConfigurationMaint+EntityDescription
        InsertModel" -> "PX.Api.ContractBased.UI.EntityConfigurationMaint"). Some
        selectors (Container) omit fieldDacName -> graph None here; the caller fills
        it from a sibling selector's graph (all selectors on a screen share it).
      • `fieldList`/`headerList` name the grid columns — but some selectors (Container)
        omit them; fall back to [valueField, descriptionName], which are always the
        real value + display columns (proven: Container returns mappedObject +
        displayName == its valueField + descriptionName).
    """
    if not field_state.get("selectorMode"):
        return None
    fdac = field_state.get("fieldDacName") or ""
    value_field = field_state.get("valueField")
    desc = field_state.get("descriptionName")
    columns = field_state.get("fieldList") or [c for c in (value_field, desc) if c]
    return {
        "view": field_state.get("viewName"),
        "graph": fdac.split("+")[0] if fdac else None,
        "value_field": value_field,
        "search_field": desc,
        "columns": columns,
        "headers": field_state.get("headerList") or [],
    }


# A PXSelector field's `viewName` encodes its lookup target as
#   _Cache#<OwnerDAC>_<FieldName>_<TargetDAC>+<TargetKeyField>_
# e.g. "_Cache#PX.Objects.FA.FixedAsset_ClassID_PX.Objects.FA.FAClass+assetID_"
# -> target DAC PX.Objects.FA.FAClass. The `valueField` (e.g. "assetCD") is the
# actual value column (camelCase -> the DAC's PascalCase field). This is how the
# common Acumatica selector exposes its master, distinct from the `selectorMode`
# grid-selector style (SM207060) that _selector_meta handles.
_CACHE_LOOKUP_RE = re.compile(r"^_Cache#(?P<owner>[\w.]+)_(?P<field>\w+)_(?P<target>[\w.]+)\+(?P<key>\w+)_$")


def _lookup_meta(field_state: dict) -> dict | None:
    """Extract a PXSelector field's LOOKUP MASTER (target DAC + value column) from its
    `viewName`/`valueField`, or None if the field isn't such a selector (pure,
    unit-testable). Lets a caller BULK-query the master via OData and diff locally —
    no per-value grid probing. Complements _selector_meta (the selectorMode style)."""
    m = _CACHE_LOOKUP_RE.match(field_state.get("viewName") or "")
    vf = field_state.get("valueField")
    if not m or not vf:
        return None
    target = m.group("target")
    return {
        "dac": target.rsplit(".", 1)[-1],          # OData entity name (FAClass, Branch, ...)
        "target_full": target,
        "value_field": vf[:1].upper() + vf[1:],     # camelCase -> DAC PascalCase (assetCD->AssetCD)
        "value_field_raw": vf,
    }


def _selector_grid_payload(sel: dict, field: str, data_view: str, search: str,
                            active_row_contexts: list | None = None) -> dict:
    """The POST body for a selector field's grid sub-endpoint (pure, unit-testable).

    sel: one field's `selector` metadata (from _selector_meta / get_ui_structure).
    """
    payload = {
        "view": sel["view"],
        "columns": [{"field": c} for c in sel["columns"]],
        "generateColumns": 0, "retrieveMode": 0, "pagerMode": 1, "startRow": 0,
        "searchField": sel["search_field"], "pageSize": 20,
        "preserveSortsAndFilters": False, "showNoteFiles": 1, "suppressAutoHide": True,
        "refreshFilters": False, "suppressStoredFilters": False,
        "fastFilterByAllFields": True, "fastFilter": search, "filterRows": [],
        "isRequestOwner": True, "graph": sel["graph"],
        "dataField": field, "dataView": data_view,
    }
    if active_row_contexts:
        payload["activeRowContexts"] = active_row_contexts
    return payload


class ScreenClient:
    """One screen-based SOAP session, bound to a single screen.

    screen_id: e.g. "CS203000". The service lives at
    {base_url}/Soap/{screen_id}.asmx and Login/Logout are session-wide.
    """

    # Set once by server.py to _relieve_api_seats (frees OTHER cached sessions on a
    # seat-limit fault). Lets a screen Login auto-recover from "API Login Limit".
    default_seat_reliever = None

    def __init__(self, instance: Instance, screen_id: str, timeout: float = 120.0) -> None:
        self.instance = instance
        self.screen_id = screen_id.upper()
        self._http = _pooled_http(f"{instance.base_url}|{instance.username}|{instance.tenant}",
                                  timeout)
        self.seat_reliever = None
        self._logged_in = False
        self._cookie_session = False  # True if logged in via /entity/auth/login (non-SOAP)
        self._shared = False          # True if reusing a cached shared session (don't logout)
        self._tree: ET.Element | None = None
        self._ui_booted = False
        self._classic_used = False  # guard: don't mix classic + modern graph state
        self._active_tree_row: dict | None = None  # see ui_select_tree_node
        self._active_tree_controls: dict | None = None
        self._active_tree_context_views: dict | None = None
        self._active_grid_row: dict | None = None  # see ui_select_grid_row
        self._ui_meta: dict[tuple[str, str], dict] | None = None  # (view,field)->meta cache
        self._struct: dict | None = None  # per-client /structure memo (see _STRUCT_CACHE)
        self._graph_dirty: bool | None = None   # last observed graphIsDirty (None = unknown)
        self._rejected_sets: list[dict] = []    # sets the plane silently dropped

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

    async def _call(self, op: str, inner_xml: str, _seat_retried: bool = False) -> str:
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
            # "API Login Limit": free other cached sessions, then retry this op once.
            if not _seat_retried and looks_like_seat_limit(msg):
                reliever = self.seat_reliever or type(self).default_seat_reliever
                if reliever is not None:
                    try:
                        await reliever(self)
                    except Exception:  # noqa: BLE001 — best-effort; fall through to raise
                        reliever = None
                    if reliever is not None:
                        return await self._call(op, inner_xml, _seat_retried=True)
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
            # surface the real PX inner exception, not the SOAP wrapper boilerplate.
            # Stack-frame boundary: a real frame is " at <Namespace>.<Type>...", i.e.
            # " at " followed by an UPPERCASE dotted identifier. The old ` at ` boundary
            # also matched the plain-English " at " in "...record raised at least one
            # error", truncating the message mid-sentence — so require the frame shape.
            inner = re.search(
                r"PX\.\w[\w.]*Exception: (.+?)(?: at [A-Z][\w.]*[.(]|---|\Z)", msg)
            raise ScreenError(
                f"{op} on {self.screen_id}: {inner.group(1).strip() if inner else msg}"
            )
        if resp.status_code >= 400:
            raise ScreenError(f"{op} on {self.screen_id} -> HTTP {resp.status_code}")
        return text

    # ---- session --------------------------------------------------------

    async def login(self) -> None:
        """Classic SOAP Login (establishes the screen-API session + its cookie)."""
        await self._call(
            "Login",
            f"<tns:Login><tns:name>{escape(self.login_name)}</tns:name>"
            f"<tns:password>{escape(self.instance.password)}</tns:password></tns:Login>",
        )
        self._logged_in = True
        self._cookie_session = False

    async def _cookie_login(self, _seat_retried: bool = False) -> None:
        """NON-SOAP cookie login via the contract endpoint POST /entity/auth/login.

        Returns 204 + the ASP.NET forms-auth cookie (.ASPXAUTH + session), which
        authorizes the MODERN /ui/screen/ plane exactly like the SOAP Login cookie
        does — but works on instances where the classic SOAP screen API (Login op) is
        DISABLED (e.g. csmdev: SOAP login off + OData 403, yet the browser's cookie
        route works). Proven live 2026-07-06: /entity/auth/login {name,password,company}
        -> 204 -> GET /t/<tenant>/ui/screen/PY101500/structure returned the descriptor.

        NOTE the login SHAPE differs from SOAP: the contract login takes `company`
        SEPARATELY (not name@tenant), which is required for tenants whose login name has
        spaces (e.g. 'AI MPM'). Does NOT hold a Web Services API SOAP seat.
        """
        body: dict = {"name": self.instance.username, "password": self.instance.password}
        if self.instance.tenant:
            body["company"] = self.instance.tenant
        if self.instance.branch:
            body["branch"] = self.instance.branch
        url = f"{self.instance.base_url.rstrip('/')}/entity/auth/login"
        resp = await self._http.post(url, json=body,
                                     headers={"Content-Type": "application/json"})
        if resp.status_code not in (200, 204):
            # "API Login Limit" self-heal — parity with classic _call: free the other
            # cached sessions this process holds, then retry ONCE. Without this, an
            # instance whose SOAP login is disabled (modern plane = cookie only, e.g.
            # csmdev) could never recover from a seat jam.
            if not _seat_retried and looks_like_seat_limit(resp.text):
                reliever = self.seat_reliever or type(self).default_seat_reliever
                if reliever is not None:
                    try:
                        await reliever(self)
                    except Exception:  # noqa: BLE001 — best-effort; fall through to raise
                        reliever = None
                    if reliever is not None:
                        return await self._cookie_login(_seat_retried=True)
            raise ScreenError(
                f"cookie login (/entity/auth/login) failed: HTTP {resp.status_code} "
                f"{resp.text[:200]}")
        self._logged_in = True
        self._cookie_session = True

    @property
    def _session_key(self) -> str:
        """Identity a login is shareable across (same site + user + tenant)."""
        i = self.instance
        return f"{i.base_url}|{i.username}|{i.tenant}"

    async def _ensure_login(self) -> None:
        """Establish a session for the MODERN plane, REUSING a cached per-instance login
        when one is warm (so N concurrent ScreenClients don't each mint a login and blow
        the concurrent-login cap). Serialized per identity. Tries classic SOAP Login first
        (also required for classic ops + sets the same cookie), and on failure falls back
        to the non-SOAP /entity/auth/login cookie — so modern-plane tools work even where
        the SOAP screen API is disabled. No-op if already logged in."""
        if self._logged_in:
            return
        key = self._session_key
        async with _session_lock(key):
            if self._logged_in:
                return
            cached = _SESSION_CACHE.get(key)
            if cached and (time.monotonic() - cached["at"]) < _SESSION_TTL:
                # reuse the shared cookie — no new login, no extra seat
                self._http.cookies.update(cached["cookies"])
                # Slide the freshness window forward on every reuse. The TTL exists to
                # bound how long we trust a cookie we haven't exercised; a session that
                # is actively being used has just proven itself, so a busy identity now
                # never pays the stale-path logout+login (it used to, every TTL, purely
                # because `at` was only ever set at creation).
                cached["at"] = time.monotonic()
                self._logged_in = True
                self._cookie_session = cached["kind"] == "cookie"
                self._shared = True
                return
            # A STALE cached entry (past the local TTL) is STILL a live server-side session
            # — the TTL is far shorter than Acumatica's session idle-timeout. Minting a new
            # login below and overwriting the dict entry would ORPHAN it: it keeps holding a
            # 'Max Web Services API Users' seat until idle-timeout, and with its handle gone
            # from the cache, release_sessions can never end it (the 'ghost session' seat
            # leak). So log the stale session out server-side FIRST, then re-login.
            if cached is not None:
                try:
                    await logout_session_cache(key)
                except Exception:  # noqa: BLE001 — best-effort; re-login regardless
                    pass
            try:
                await self.login()
                kind = "soap"
            except Exception:  # noqa: BLE001 — SOAP login disabled/blocked; try cookie login
                await self._cookie_login()
                kind = "cookie"
            # Cache a COPY of the cookies (survives this client's aclose) and mark this
            # client shared too, so the CREATOR doesn't log the session out on exit either
            # — the cached session is owned by the cache (TTL / release_sessions prune it).
            _SESSION_CACHE[key] = {"cookies": httpx.Cookies(self._http.cookies),
                                   "at": time.monotonic(), "kind": kind}
            self._shared = True

    def _invalidate_session(self) -> None:
        """Drop this identity's cached session — call when a reused cookie is rejected so
        the next _ensure_login re-authenticates instead of replaying a dead cookie."""
        _SESSION_CACHE.pop(self._session_key, None)

    async def logout(self) -> None:
        if not self._logged_in:
            return
        self._logged_in = False
        # A SHARED (cache-reused) session is owned by the cache, not this client — logging
        # it out would kill it for every other in-flight ScreenClient. Leave it; cached
        # sessions idle-expire server-side and are pruned by TTL / release_sessions.
        if self._shared:
            return
        try:
            if self._cookie_session:
                await self._http.post(
                    f"{self.instance.base_url.rstrip('/')}/entity/auth/logout")
            else:
                await self._call("Logout", "<tns:Logout/>")
        except Exception:
            pass

    async def aclose(self) -> None:
        # NOTE: self._http is POOLED (see _pooled_http) and shared with every other
        # ScreenClient of this identity — closing it here would tear down a pool that
        # in-flight clients are still using, and put back the per-call TLS handshake
        # this pool exists to remove. The pool is closed once, at shutdown, by
        # close_http_pool(). Only the session is released here.
        await self.logout()

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
            # An unauthenticated / expired modern-plane session doesn't 401 with a
            # clean error — the ASP.NET app answers 200/302 with a redirect body
            # pointing at Login.aspx. Without this, that body parses as "no error,
            # empty data" and the caller silently gets an empty structure/grid and
            # misattributes the cause (proven: a bare _http.get without login() read
            # as "the maintenance lockout is blocking the plane", 2026-07-02).
            redir = j.get("redirect")
            if isinstance(redir, str) and "Login.aspx" in redir:
                return ("NOT AUTHENTICATED — the modern-plane session is missing or "
                        "expired (server redirected to Login.aspx). Ensure login() ran "
                        "first; through the MCP tools this is automatic.")
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

    @staticmethod
    def _notices(j: Any) -> list[dict]:
        """The NON-error messages from a modern-plane response — the yellow/blue toasts
        the browser shows top-right.

        _ui_error deliberately surfaces only messageType=="error" on an HTTP 200, because
        its return value RAISES and a warning is not a failure. The cost of that filter
        was that warnings and info were dropped entirely: "the period is closed", "the
        year is already generated" — exactly the messages that explain why a write was
        accepted with a clean 200 and then quietly did nothing. They belong on the
        RESULT, not on an exception, so they are extracted separately here.

        Errors are excluded: those already raise via _ui_error, and repeating them as a
        notice would report one failure twice.
        """
        if not isinstance(j, dict):
            return []
        out = []
        for m in (j.get("messages") or []):
            if not isinstance(m, dict) or not m.get("message"):
                continue
            mtype = str(m.get("messageType") or "info").lower()
            if mtype == "error":
                continue
            out.append({"type": mtype, "message": m["message"]})
        return out

    @classmethod
    def _annotate_notices(cls, j: Any) -> Any:
        """Tag a raw plane response with its non-error messages under `@grp.notices`.

        The `@grp.` prefix marks the key as added by this server, not returned by
        Acumatica, so it can never be mistaken for a server field.
        """
        notices = cls._notices(j)
        if notices and isinstance(j, dict):
            j["@grp.notices"] = notices
        return j

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
        await self._ensure_login()
        vp = {v: {} for v in (views or [])}
        resp = await self._http.post(
            self.ui_url,
            json={"isFirstRequest": True, "data": [], "controlsParams": {},
                  "activeRowContexts": [], "viewsParams": vp},
            headers=_UI_HEADERS,
        )
        # Record the graph's dirty state from the LOAD (a load should leave it clean).
        # ui_set_field's silent-rejection check needs a KNOWN-clean starting point —
        # see there for why it can't just assume one.
        try:
            self._graph_dirty = (resp.json() or {}).get("graphIsDirty")
        except Exception:  # noqa: BLE001 — non-JSON body; leave the state unknown
            self._graph_dirty = None
        self._ui_booted = True
        self._classic_used = False
        self._active_tree_row = None  # a fresh graph has no node selected
        self._active_tree_controls: dict | None = None
        self._active_tree_context_views: dict | None = None
        self._active_grid_row = None  # a fresh graph has no grid row selected

    async def ui_navigate_record(self, view: str, key: dict) -> None:
        """Select a SPECIFIC EXISTING record on `view` by its key field(s) — the
        modern-plane equivalent of opening a screen already scoped to one row (e.g.
        SM207060's Endpoint header: InterfaceName + GateVersion).

        ui_bootstrap alone only LOADS a view; it does not select which record. A
        screen with a single, always-current record (most Preferences/Setup forms)
        doesn't need this. A screen whose primary view is itself keyed to a specific
        record — SM207060 being the proven case — silently operates on no/wrong
        record without it (proven live, 2026-07-02: without navigating Endpoint to
        the target InterfaceName/GateVersion first, InsertNew opened a dialog fine
        but committing it failed with "The Insert button is disabled" — the graph
        never actually had a valid endpoint loaded).

        Composite keys are navigated field-by-field, in `key`'s iteration order —
        the same technique ui_grid_read uses for a master-detail parent (some
        screens only resolve on the LAST field of a composite key).
        """
        if not key:
            raise ValueError(
                f"ui_navigate_record {view} on {self.screen_id}: `key` is empty — pass the "
                f"key field(s) identifying the record to select.")
        resp = None
        for f, v in key.items():
            resp = await self._ui_post({
                "data": [{"viewName": view, "fieldName": f, "value": str(v),
                          "rowId": "", "changeType": 5}],
                "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
            })
            # Only a HARD failure is checked per-field. A composite key resolves on its
            # LAST field, so an intermediate response legitimately carries a business
            # error ("record not found") while half the key is set — raising on that
            # would break the very screens this method exists for. An HTTP >=400 is not
            # that: it's the transport/server failing, and continuing to post more
            # fields at a broken graph only buries the cause.
            if resp.status_code >= 400:
                raise ScreenError(
                    f"ui_navigate_record {view}.{f} on {self.screen_id}: "
                    f"{self._ui_error(resp)}")
        # Business errors are judged on the final response, once the whole key is set.
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_navigate_record {view} on {self.screen_id}: {err}")

    def ui_select_grid_row(self, grid_view: str, key: dict) -> None:
        """Mark an EXISTING data-grid row as the graph's CURRENT row for `grid_view`,
        so a codebehind action fired next operates on it. The modern-plane peer of
        clicking a detail-grid row (ui_select_tree_node is the tree equivalent).

        WHY: actions like SM206015 `fillSchemaFields` read the *selected* child row
        (the schema object whose fields to fill); ui_command sends an empty
        activeRowContexts, so they fault "A schema object is not selected". This stores
        a `GridActiveDataRow` activeRowContext that `_ui_post` then auto-attaches to
        every subsequent command in THIS session (the action + a trailing Save),
        exactly as the browser resends it while a row stays selected.

        grid_view: the grid's data view (from ui_get_structure `grids`, e.g. "Objects").
        key:       the row's FULL grid key (grids[grid_view].key_fields), e.g.
            {"ProviderID": <id>, "LineNbr": 1}. For a detail grid include the parent-
            link field(s); navigate the header first with ui_navigate_record so the
            right parent's rows are loaded. Sync only — no network (the context rides
            the next command). Proven live on SM206015 (2026-07-14)."""
        self._active_grid_row = {"dataView": grid_view, "syncPosition": True,
                                 "resultType": "GridActiveDataRow", "dataKey": dict(key)}

    async def _ui_post(self, payload: dict, _auth_retried: bool = False) -> httpx.Response:
        # The modern plane rides the SOAP login cookie (same ASP.NET app). If no
        # login has run this session, self-authenticate rather than silently
        # bouncing to Login.aspx (which _ui_error would now flag, but self-healing
        # is cheaper than erroring on a recoverable state).
        await self._ensure_login()
        # Ensure a graph exists (fallback bootstrap). Re-bootstrap if a classic SOAP
        # op ran since (the planes keep separate graph state — interleaving them in
        # one session can collide, e.g. a 409 on Save). Callers editing an existing
        # record should call ui_bootstrap([views]) FIRST so the record loads.
        if not self._ui_booted or self._classic_used:
            await self.ui_bootstrap()
        # A selected TREE node (ui_select_tree_node) is context every subsequent
        # command needs — BOTH activeRowContexts AND the tree's own controlsParams
        # block. The browser resends both on every request for as long as the node
        # stays "active"; a bare activeRowContexts alone still silently no-ops
        # (proven live: InsertNew stayed a no-op — graphIsDirty:false — until the
        # full EntityTree controlsParams echo was added too, SM207060, 2026-07-02).
        # Auto-attach unless the caller already named this dataView/view.
        if self._active_tree_row is not None:
            existing = payload.get("activeRowContexts") or []
            if not any(c.get("dataView") == self._active_tree_row["dataView"] for c in existing):
                payload = {**payload, "activeRowContexts": [self._active_tree_row, *existing]}
        if self._active_tree_controls is not None:
            cp = dict(payload.get("controlsParams") or {})
            for view, block in self._active_tree_controls.items():
                cp.setdefault(view, block)
            payload = {**payload, "controlsParams": cp}
        # The node's "context" views (Selected* → {parameters:{Key}}) also ride on
        # EVERY later command while a node is selected — the browser resends them on
        # the field-sets, the dialog commit, AND the final Save (proven live: without
        # them the dialog-commit stages nothing, so the trailing Save persists an
        # empty graph — SM207060 capture, 2026-07-02). setdefault: caller wins.
        if self._active_tree_context_views is not None:
            vp = dict(payload.get("viewsParams") or {})
            for view, block in self._active_tree_context_views.items():
                vp.setdefault(view, block)
            payload = {**payload, "viewsParams": vp}
        # A selected data-GRID row (ui_select_grid_row) is the "current row" of a detail
        # view — the context a codebehind action on a selected row needs (e.g. SM206015
        # `fillSchemaFields` faults "A schema object is not selected" without it). The
        # browser resends this activeRowContexts entry on every command while the row
        # stays selected; auto-attach it (+ ensure the grid view is listed in viewsParams
        # so the row resolves). Caller-supplied contexts win. Proven live 2026-07-14.
        if self._active_grid_row is not None:
            existing = payload.get("activeRowContexts") or []
            if not any(c.get("dataView") == self._active_grid_row["dataView"] for c in existing):
                payload = {**payload, "activeRowContexts": [*existing, self._active_grid_row]}
            gv = self._active_grid_row["dataView"]
            vp = dict(payload.get("viewsParams") or {})
            vp.setdefault(gv, {})
            payload = {**payload, "viewsParams": vp}
        resp = await self._http.post(self.ui_url, json=payload, headers=_UI_HEADERS)
        # If we REUSED a cached shared cookie and the server has since dropped it, the
        # reply is a Login redirect. Invalidate the stale cache entry, re-login fresh, and
        # retry once. Only for reused sessions — a fresh login that "fails auth" is a real
        # credential problem, not a stale-cookie one.
        if not _auth_retried and self._shared:
            err = self._ui_error(resp)
            if err and "not authenticated" in err.lower():
                self._invalidate_session()
                self._logged_in = False
                self._shared = False
                self._ui_booted = False
                return await self._ui_post(payload, _auth_retried=True)
        return resp

    async def ui_select_tree_node(self, tree_view: str, node_key: dict,
                                   parent_key: dict | None = None,
                                   ancestor_keys: list[dict] | None = None,
                                   select_command: str = "EnablePopulate") -> dict:
        """Make a TREE node the active row — the modern-plane equivalent of clicking it.

        The capability classic screen-SOAP and the flat-grid CRUD tools both lack:
        a hierarchical tree control (e.g. SM207060's EntityTree — Endpoint structure)
        isn't a normal data grid, so ui_insert_grid_row/etc. throw a server-side
        null-reference against it (proven live, 2026-07-02). Trees are addressed by
        `activeRowContexts` instead — reverse-engineered from a live browser capture
        of the SM207060 "Create Entity" flow.

        Once selected, the node stays the active row for every subsequent
        ui_set_field/ui_command call on THIS client (auto-attached in _ui_post) —
        mirroring how the browser keeps resending the same context while a node is
        selected. Call again with a different node_key to move the selection; call
        with node_key=None (or start a fresh ui_bootstrap) to clear it.

        tree_view:  the tree control's view name (from ui_get_structure `grids`,
            e.g. "EntityTree" on SM207060).
        node_key:   {keyField: value} identifying the node — from a row returned by
            ui_read_grid(tree_view), e.g. {"Key": "ROOT#GRPMCP"}.
        parent_key: the node's immediate PARENT key dict, or omit for a root-level
            node. Fine for a depth-1 node (child of root, e.g. an endpoint entity).
            (Distinct from ui_read_grid's `parent` — that addresses a different,
            master-detail grid; this re-selects a row WITHIN this tree.)
        ancestor_keys: the FULL ancestor path as a list of key dicts, root →
            immediate parent, for a DEEPER node (e.g. a detail collection at depth 2:
            [{"Key": root}, {"Key": entity}]). Required beyond depth 1 — the server
            rejects the selection (activeRowId comes back null) if only the immediate
            parent is sent. Takes precedence over parent_key when both are given.
        select_command: the command that establishes the selection server-side.
            "EnablePopulate" is what the browser actually fires for SM207060's
            EntityTree (captured live — it's the graph's own selection-changed
            handler, not a generic framework primitive, so it likely differs on
            OTHER tree screens). If a tree on a different screen doesn't respond,
            check that screen's `actions` (ui_get_structure) for its own
            selection-handler name and pass it here.
        """
        if ancestor_keys is None:
            ancestor_keys = [parent_key] if parent_key else []
        ctx = _tree_active_row_context(tree_view, node_key, ancestor_keys)
        # Pull the real columns/key_fields from /structure — needed for the
        # controlsParams echo below (cached and auto-attached to every later
        # command in this selection by _ui_post, alongside `ctx`).
        struct = await self.get_ui_structure()
        tree_meta = struct["grids"].get(tree_view) or {}
        block = _tree_control_block(tree_view, node_key, ancestor_keys,
                                     tree_meta.get("columns"), tree_meta.get("key_fields"))
        context_views = _tree_context_views(list(struct["views"]), tree_view, node_key)
        resp = await self._ui_post({
            "command": [{"name": select_command}], "data": [],
            "controlsParams": {tree_view: block},
            "activeRowContexts": [ctx], "viewsParams": context_views,
        })
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_select_tree_node {tree_view} on {self.screen_id}: {err}")
        self._active_tree_row = ctx
        self._active_tree_controls = {tree_view: block}
        self._active_tree_context_views = context_views
        return resp.json()

    async def ui_tree_dialog_insert(self, tree_view: str, node_key: dict,
                                     open_action: str, dialog_view: str,
                                     fields: list[dict], parent_key: dict | None = None,
                                     save: bool = True) -> dict:
        """Add a child under a TREE node via its INSERT DIALOG — select node, open the
        dialog, fill it, commit it, and (by default) Save. The end-to-end capability
        behind adding an entity to a web-service endpoint (SM207060), and the general
        shape for any "click a tree node → Insert → fill a popup → OK → Save" screen.

        Reverse-engineered from a full live browser capture (2026-07-02, SM207060
        adding DataProvider to a fresh endpoint) — the exact 5-phase sequence the UI
        actually performs, which no single ui_command call reproduces:
          1. select the tree node (establishes the active-row + Selected* context
             that every following call must carry — done via ui_select_tree_node,
             then auto-attached by _ui_post);
          2. fire `open_action` once to OPEN a blank dialog;
          3. Repaint to load `dialog_view`'s fields into the graph;
          4. set each dialog field (a SELECTOR field's value must be its resolved
             {id,text} dict — see ui_resolve_selector);
          5. fire `open_action` AGAIN with dialogCallback OK to COMMIT the dialog
             (this only STAGES the node — graphIsDirty becomes true), then a
             SEPARATE Save to PERSIST it. Missing the trailing Save was why earlier
             attempts "succeeded" (200, no error) yet nothing appeared.

        tree_view/node_key/parent_key: as ui_select_tree_node (e.g. "EntityTree",
            {"Key": "ROOT#GRPMCP"}).
        open_action: the tree's insert command (e.g. "InsertNew" on SM207060).
        dialog_view: the popup's view name (e.g. "CreateEntityView" on SM207060).
        fields:      [{"field": <name>, "value": <value-or-{id,text}>}] to fill in
            the dialog. Resolve any selector field first with ui_resolve_selector.
        save:        commit to the DB (default True). False leaves the node staged
            in the graph only — rarely wanted.

        Requires the record already navigated if the screen's primary view is keyed
        (see ui_navigate_record). Verify the result with the entity/contract API.
        """
        await self.ui_select_tree_node(tree_view, node_key, parent_key)
        # OPEN — fire once; the dialog opens (auto-attached tree context rides along).
        await self._ui_post({
            "command": [{"name": open_action}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        # REPAINT — load the dialog view's fields into the graph (the browser does
        # this before it can fill them; without it the field-sets hit nothing).
        await self._ui_post({
            "command": [{"name": "Repaint"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {dialog_view: {}},
        })
        # FILL — set each dialog field (selector values pass through as {id,text}).
        for f in fields:
            await self.ui_set_field(dialog_view, f["field"], f["value"])
        # COMMIT the dialog (dialogResult OK) — STAGES the node (graphIsDirty:true).
        commit = await self._ui_post({
            "command": [{"name": open_action}], "data": [],
            "dialogCallback": {"dialogResult": 1, "validateInput": False, "viewName": dialog_view},
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(commit)
        if err:
            raise ScreenError(f"ui_tree_dialog_insert commit on {self.screen_id}: {err}")
        result = commit.json()
        if not save:
            return result
        # PERSIST — a SEPARATE Save (the commit only staged the node in the graph).
        save_resp = await self._ui_post({
            "command": [{"name": "Save"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(save_resp)
        if err:
            raise ScreenError(f"ui_tree_dialog_insert save on {self.screen_id}: {err}")
        return save_resp.json()

    def _tree_row_by_title(self, tree_resp: dict, tree_view: str, title: str) -> dict | None:
        """Find a tree row whose Title matches `title` in a select/read response, and
        return {key_field: value} for it (or None). Matches either the full Title or,
        for a detail node whose Title is "<Collection>: <Type>[]", its collection name
        before the colon (so "CompaniesDetails" finds "CompaniesDetails: CompaniesDetail[]").
        Titles inherited from a base endpoint carry a trailing ' ↓'/'↑' marker —
        stripped before matching."""
        rows = ((tree_resp.get("controlsData") or {}).get(tree_view) or {}).get("rows") or []
        want = title.strip()
        for r in rows:
            cells = r.get("cells") or {}
            t = ((cells.get("Title") or {}).get("value") or "").strip().rstrip("↓↑").strip()
            if t == want or t.split(":", 1)[0].strip() == want:
                return {"Key": (cells.get("Key") or {}).get("value")}
        return None

    async def ui_populate_entity_fields(self, root_node_key: dict, entity_object_name: str,
                                         data_view: str, data_view_pick: dict | None = None,
                                         detail_title: str | None = None,
                                         save: bool = True) -> dict:
        """Populate an endpoint entity's FIELDS from one of its screen data views —
        SM207060's "select the entity → Populate → pick the Object → Select All → OK
        → Save" flow. Turns an entity SHELL (added by ui_tree_dialog_insert, which
        only creates the node + its detail collections) into one with real scalar
        fields exposed on the contract.

        Reverse-engineered + proven live from a full browser capture (2026-07-02,
        DataProvider ← "Provider Summary", field_count 1 → 5; detail path also
        captured 2026-07-02). Same select→open→repaint→fill→commit→Save skeleton as
        ui_tree_dialog_insert, plus wrinkles unique to this flow:
          • the node selected is the ENTITY node (not the endpoint root) — found here
            by its Title (== ObjectName) among the root's expanded children — or, with
            `detail_title`, a DETAIL-collection node one level deeper (selected with
            its full ancestor path root→entity→detail; a depth-2 node won't select
            with only its immediate parent);
          • the dialog's data-view selector (`Container`) is scoped to the SELECTED
            node — its lookup only returns that node's views, so it's resolved
            in-session with the node active (ui_resolve_selector rides the active tree
            row onto the query), not standalone; and a `SelectAll` command (tick every
            field's Populate box) fires between setting `Container` and committing.

        root_node_key:      the endpoint's root tree node, e.g. {"Key": "ROOT#GRPMCP"}.
        entity_object_name: the entity's ObjectName as shown in the tree (e.g.
            "DataProvider") — used to locate its node.
        data_view:          the data view to pull fields from, matched by its display
            name (e.g. "Provider Summary"); `data_view_pick` disambiguates if >1 match.
        detail_title:       to populate a nested DETAIL collection instead of the
            top-level entity, its collection name (e.g. "CompaniesDetails" matches the
            "CompaniesDetails: CompaniesDetail[]" node); omit for the top-level entity.
        save:               persist (default True).

        Requires the endpoint record already navigated (ui_navigate_record on
        Endpoint) and allow_write. Verify with get_entity_schema (field_count).
        """
        # 1. select ROOT — expands the tree so the entity/detail nodes (+ keys) list.
        root_resp = await self.ui_select_tree_node("EntityTree", root_node_key)
        entity_key = self._tree_row_by_title(root_resp, "EntityTree", entity_object_name)
        if entity_key is None or not entity_key.get("Key"):
            raise ScreenError(
                f"ui_populate_entity_fields: entity {entity_object_name!r} not found "
                f"under {root_node_key} on {self.screen_id}."
            )
        # 2. select the target node — the entity, or (for detail_title) a detail node
        #    one level deeper. Either way it becomes active so the Container lookup is
        #    scoped to it. The browser selects entity THEN detail; a detail needs its
        #    FULL ancestor path (root→entity) or it won't select (activeRowId null).
        await self.ui_select_tree_node("EntityTree", entity_key, parent_key=root_node_key)
        if detail_title is not None:
            detail_key = self._tree_row_by_title(root_resp, "EntityTree", detail_title)
            if detail_key is None or not detail_key.get("Key"):
                raise ScreenError(
                    f"ui_populate_entity_fields: detail {detail_title!r} not found "
                    f"under entity {entity_object_name!r} on {self.screen_id}."
                )
            await self.ui_select_tree_node("EntityTree", detail_key,
                                            ancestor_keys=[root_node_key, entity_key])
        # 3. open the Populate dialog.
        await self._ui_post({
            "command": [{"name": "PopulateFields"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        # 4. Repaint to load the filter view's fields.
        await self._ui_post({
            "command": [{"name": "Repaint"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {"PopulateFilterView": {}},
        })
        # 5. resolve + set the (entity-scoped) Container data-view selector.
        resolved = await self.ui_resolve_selector("PopulateFilterView", "Container",
                                                   data_view, data_view_pick)
        if "value" not in resolved:
            raise ScreenError(
                f"ui_populate_entity_fields: data view {data_view!r} for "
                f"{entity_object_name!r} matched {resolved['row_count']} rows "
                f"(need exactly 1) — rows: {resolved['rows']}"
            )
        await self.ui_set_field("PopulateFilterView", "Container", resolved["value"])
        # 6. SelectAll — tick every field's Populate box.
        sa = await self._ui_post({
            "command": [{"name": "SelectAll"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(sa)
        if err:
            raise ScreenError(f"ui_populate_entity_fields SelectAll on {self.screen_id}: {err}")
        # 7. commit the dialog (dialogResult OK) — stages the field mappings.
        commit = await self._ui_post({
            "command": [{"name": "PopulateFields"}], "data": [],
            "dialogCallback": {"dialogResult": 1, "validateInput": False,
                                "viewName": "PopulateFilterView"},
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(commit)
        if err:
            raise ScreenError(f"ui_populate_entity_fields commit on {self.screen_id}: {err}")
        if not save:
            return commit.json()
        # 8. Save.
        save_resp = await self._ui_post({
            "command": [{"name": "Save"}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(save_resp)
        if err:
            raise ScreenError(f"ui_populate_entity_fields save on {self.screen_id}: {err}")
        return save_resp.json()

    @property
    def _struct_key(self) -> str:
        """Cache key for this screen's /structure — see _STRUCT_CACHE on why the
        session identity (not just base_url) AND the screen_id must both be in it."""
        return f"{self._session_key}|{self.screen_id}"

    async def get_ui_structure(self, refresh: bool = False) -> dict:
        """Read the modern UI-screen `/structure` — the schema/metadata endpoint.

        The modern-plane analog of get_schema(): returns the screen's views +
        fields (type, required, readonly, enabled, and ENUM allowed-values), the
        action inventory (enabled/visible/confirmation message), and grid key
        fields. Use it to discover what ui_set_field/ui_command can drive on any
        screen — no browser capture needed. Read-only GET (stateless, no bootstrap).

        Cached twice over (see _STRUCT_CACHE): a per-client memo makes the repeat
        calls within one tool call free, and the shared cache revalidates with the
        endpoint's ETag (~100ms/0 bytes) instead of re-downloading (~280ms/270KB).
        Safe to cache precisely BECAUSE the GET is stateless — it describes screen
        metadata, not record state, which is why the server's own validator is keyed
        on build+tenant+locale+metadata-version and not on any graph state.
        refresh=True forces a full re-fetch and re-seeds both layers.
        """
        if self._struct is not None and not refresh:
            return self._struct
        key = self._struct_key
        entry = None if refresh else _STRUCT_CACHE.get(key)
        if entry is not None and _STRUCT_TTL > 0 and (
                time.monotonic() - entry["at"]) < _STRUCT_TTL:
            self._struct = entry["parsed"]
            return self._struct

        await self._ensure_login()
        url = self.ui_url + "/structure"
        headers = {"Accept": "application/json"}
        if entry and entry.get("etag"):
            # ONLY ever this entry's own etag, on this entry's own url. The server
            # 304s on an etag match without checking the screen (proven live), so
            # replaying one screen's etag at another's url would serve wrong metadata.
            headers["If-None-Match"] = entry["etag"]
        resp = await self._http.get(url, headers=headers)
        err = self._ui_error(resp)
        if err and self._shared and "not authenticated" in err.lower():
            # reused cookie was dropped server-side — re-login fresh and retry once
            self._invalidate_session()
            self._logged_in = False
            self._shared = False
            await self._ensure_login()
            resp = await self._http.get(url, headers=headers)
            err = self._ui_error(resp)
        if err:
            raise ScreenError(f"get_ui_structure {self.screen_id}: {err}")
        if resp.status_code == 304:
            if entry is not None:  # validated: our copy is still current
                entry["at"] = time.monotonic()
                self._struct = entry["parsed"]
                return self._struct
            # 304 with nothing cached (entry evicted mid-flight) — no body to parse,
            # so re-ask unconditionally rather than returning something wrong.
            return await self.get_ui_structure(refresh=True)
        parsed = self._parse_structure(resp.json())
        _STRUCT_CACHE[key] = {"etag": resp.headers.get("ETag"), "parsed": parsed,
                              "at": time.monotonic()}
        self._struct = parsed
        return parsed

    def _parse_structure(self, d: dict) -> dict:
        """Project the raw /structure descriptor into the compact shape callers use."""
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
                    "selector": _selector_meta(st),
                    "lookup": _lookup_meta(st),
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
                    "dac": cd.get("dataDacName"),
                    "columns": [c.get("field") for c in (cd.get("columns") or []) if isinstance(c, dict)]}
            for cname, cd in (d.get("controlsData") or {}).items()
            if isinstance(cd, dict) and cd.get("dataKeyNames")
        }
        # Screen graph class: the grid sub-endpoint each selector queries needs it,
        # but some selectors omit fieldDacName. All selectors on a screen share the
        # same graph, so take the first one that DOES carry it as the screen default.
        screen_graph = None
        for fields in views.values():
            for f in fields:
                g = (f.get("selector") or {}).get("graph")
                if g:
                    screen_graph = g
                    break
            if screen_graph:
                break
        return {"screen_id": self.screen_id, "primary_dac": d.get("primaryDacName"),
                "screen_graph": screen_graph, "views": views, "actions": actions, "grids": grids}

    async def probe_required_selectors(self, struct: dict) -> dict:
        """Source-free prereq discovery: for each REQUIRED + enabled selector field
        on this screen, query its lookup grid (blank search) and report whether the
        source table has ANY candidate rows. A required selector whose source is
        EMPTY is a hard prerequisite — that field can never be set until the screen
        feeding it is populated first. Metadata alone can't tell you this; only
        actually hitting the lookup can. Reuses the live session (one seat).

        Returns {gaps, satisfiable, supply, probe_errors} — see screen_prereqs.
        """
        gaps, satisfiable, supply, probe_errors = [], [], [], []
        # A selector whose backing graph IS the screen's own graph/primary DAC is the
        # record's OWN key (you create it HERE) — not a foreign-key prerequisite you
        # populate on another screen. Compare on the leaf class name (namespaces vary).
        own = {_leaf(struct.get("screen_graph")), _leaf(struct.get("primary_dac"))} - {None}
        jobs: list[tuple[dict, dict]] = []  # (entry, field meta) for the selectors to probe
        for vname, fields in (struct.get("views") or {}).items():
            for f in fields:
                if not (f.get("required") and f.get("enabled") and not f.get("readonly")):
                    continue
                fname, label = f.get("field"), f.get("label")
                entry = {"view": vname, "field": fname, "label": label}
                if f.get("selector"):
                    jobs.append((entry, f))
                elif f.get("options"):
                    supply.append({**entry, "kind": "enum",
                                   "allowed": [o.get("value") for o in f["options"]]})
                else:
                    supply.append({**entry, "kind": "scalar",
                                   "type": f.get("type")})

        # Probe the required selectors CONCURRENTLY: each is an independent read-only
        # query against its own lookup grid, with no graph state involved, so nothing
        # here needs serializing. Bounded so a wide screen doesn't burst connections.
        sem = asyncio.Semaphore(4)

        async def _probe(entry: dict, f: dict) -> tuple[dict, dict, dict | None, str | None]:
            async with sem:
                try:
                    # selector_probe, NOT ui_resolve_selector: it takes the struct we
                    # already hold, where ui_resolve_selector re-fetches /structure per
                    # field. Same payload builder and same rows, so this is a drop-in.
                    return entry, f, await self.selector_probe(
                        struct, entry["view"], entry["field"], ""), None
                except ScreenError as e:
                    return entry, f, None, str(e)

        # gather preserves input order, so the report stays deterministic.
        for entry, f, r, perr in await asyncio.gather(*(_probe(e, f) for e, f in jobs)):
            if perr is not None:
                probe_errors.append({**entry, "probe_error": perr})
                continue
            sel_graph = (f["selector"] or {}).get("graph")
            n = len((r or {}).get("rows") or [])
            self_key = _leaf(sel_graph) in own
            if n and not self_key:
                satisfiable.append({**entry, "kind": "selector", "candidates": n})
            elif self_key:
                # own key: you're creating this record's key value here, not
                # sourcing it elsewhere. Not a prereq regardless of row_count.
                supply.append({**entry, "kind": "new_key",
                               "existing": n, "selector_graph": sel_graph})
            else:
                gaps.append({**entry, "kind": "empty_selector_source",
                             "selector_graph": sel_graph,
                             "reason": "required lookup has NO candidate rows — populate "
                                       "its source screen before this field can be set."})
        return {"gaps": gaps, "satisfiable": satisfiable, "supply": supply,
                "probe_errors": probe_errors}

    async def ui_coerce_validate(self, sets: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
        """Modern-plane write safety for a list of {view?, field, value} sets — the
        peer of the classic submit()'s _validate_sets, closing the SAME silent-failure
        gap on this plane. For each set, using the modern /structure metadata:
          • resolve a missing `view` by field name (unique across views, else flagged
            ambiguous) — friendly single-name addressing;
          • COERCE an enum's display text to its option value (accept "Reversed" OR
            "R" — passing the label used to silently no-op);
          • FLAG a read-only/disabled field or an invalid enum value (both are
            accepted by the plane with a clean 200 and silently dropped).
        Returns (normalized_sets, issues, notes). Best-effort: a field not present in
        the metadata is passed through untouched (downstream existence-check catches a
        real typo). Zero extra calls — _ui_field_meta is cached."""
        meta = await self._ui_field_meta()
        by_field: dict[str, list] = {}
        for (v, f), m in meta.items():
            by_field.setdefault(f, []).append((v, m))
        norm: list[dict] = []
        issues: list[dict] = []
        notes: list[str] = []
        for s in sets:
            field, view, val = s["field"], s.get("view"), s.get("value")
            if not view:  # friendly single-name: resolve the view
                cands = by_field.get(field, [])
                if len(cands) == 1:
                    view = cands[0][0]
                    notes.append(f"{field} -> {view}.{field}")
                elif len(cands) > 1:
                    issues.append({"field": field, "value": val,
                                   "problem": f"ambiguous field — qualify with a view; "
                                   f"present in {sorted(c[0] for c in cands)}"})
                    norm.append({"view": view, "field": field, "value": val})
                    continue
            m = meta.get((view, field)) if view else None
            if m:
                if m.get("readonly") or m.get("enabled") is False:
                    issues.append({"field": f"{view}.{field}", "value": val,
                                   "problem": "read-only / not writable — accepted by the "
                                   "plane but silently ignored"})
                    norm.append({"view": view, "field": field, "value": val})
                    continue
                opts = m.get("options")
                if opts and val is not None and not isinstance(val, bool):
                    sval = str(val)
                    if not any(sval == str(o.get("value")) for o in opts):
                        match = next((o for o in opts
                                      if sval.lower() == str(o.get("text")).lower()), None)
                        if match:
                            notes.append(f"{view}.{field}: coerced label {val!r} -> "
                                         f"value {match.get('value')!r}")
                            val = match.get("value")
                        else:
                            issues.append({"field": f"{view}.{field}", "value": val,
                                           "problem": "not a valid option (would silently "
                                           "no-op)", "allowed": opts})
            norm.append({"view": view, "field": field, "value": val})
        return norm, issues, notes

    async def ui_set_field(self, view: str, field: str, value) -> None:
        """Set one field via the modern UI-screen protocol (see class docstring above).

        Value formats: strings/enums = the raw code (for enums use the option
        `value`, not its display text — see get_ui_structure); booleans = "true"/
        "false"; a SELECTOR field (see ui_resolve_selector) takes its resolved
        `{"id": ..., "text": ...}` dict UNCHANGED — stringifying it is wrong (proven
        live, 2026-07-02: CreateEntityView.ScreenID needs the object form, not its
        plain ScreenIDValue string). The set lands in the graph working state; a
        following ui_command ("Save" or a screen action) commits it. Do NOT
        interleave with classic get_schema/export/submit on the same session
        (separate graph state).
        """
        v = value if isinstance(value, (dict, bool)) else str(value)
        was_dirty = self._graph_dirty
        resp = await self._ui_post({
            "data": [{"viewName": view, "fieldName": field, "value": v,
                       "rowId": "", "changeType": 5}],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_set_field {view}.{field} on {self.screen_id}: {err}")
        try:
            now_dirty = (resp.json() or {}).get("graphIsDirty")
        except Exception:  # noqa: BLE001 — non-JSON body; state unknown
            now_dirty = None
        self._graph_dirty = now_dirty if now_dirty is not None else was_dirty
        # SILENT-REJECTION NET — a NARROW one. Read the coverage note before trusting it.
        #
        # This plane reports NOTHING when it refuses a value: no messages, no fieldStates,
        # no error — just a clean 200. The only signal is graphIsDirty, readable in one
        # direction only:
        #   clean -> still clean  = the set was REFUSED (nothing staged).
        #   clean -> dirty        = the value landed. NOT proof it is valid.
        #
        # MEASURED COVERAGE (live, 2026-07-15, 5 screens / 4 graphs). It fires on ONE:
        #   GL101000 FiscalYearSetup.BegFinYear = "NOT-A-DATE"  -> clean  (REFUSED)  <-- only hit
        #   AP301000 Document.DocDate           = "NOT-A-DATE"  -> dirty  (accepted!)
        #   GL301000 BatchModule.DateEntered    = "NOT-A-DATE"  -> dirty  (accepted!)
        #   AP101000 Setup.PastDue00            = "abc" (Int16) -> dirty  (accepted!)
        #   CS101500 commonsetup.DecPlQty       = "abc" (Int16) -> dirty  (accepted!)
        #   AP101000 Setup.<read-only field>    = write         -> dirty  (accepted!)
        # So this is NOT a general validity guard and not even general to dates —
        # BegFinYear happens to have a validating setter. Most bad values sail through as
        # clean->dirty and this net never sees them. It is a cheap true-positive catcher,
        # nothing more; do not read "no rejected_fields" as "the values were good".
        # The read-only/bad-enum class is ui_coerce_validate's job (metadata-based), which
        # in turn cannot catch an unparseable date. The two are complementary and BOTH
        # partial.
        #
        # No false positives observed across those 5 screens. "No change" is specifically
        # NOT one: setting a field to its own current value still returns dirty=True
        # (verified against the record's live value on GL101000 and AP101000).
        # LIMIT: once the graph is dirty it stays dirty, so a refusal AFTER the first
        # successful set is invisible here. Only flag on a KNOWN-clean graph — was_dirty
        # None means we never observed it, and guessing would cry wolf.
        if was_dirty is False and now_dirty is False:
            self._rejected_sets.append({
                "view": view, "field": field, "value": v,
                "reason": "the screen silently REFUSED this value — the graph stayed "
                          "clean, so nothing was staged. The value was NOT written.",
            })

    async def ui_resolve_selector(self, view: str, field: str, search: str,
                                   pick: dict | None = None) -> dict:
        """Resolve a lookup/selector FORM field to its `{id, text}` value — the modern
        plane's equivalent of clicking the magnifier, typing a search, and picking a
        row (e.g. CreateEntityView.ScreenID on SM207060). No browser capture needed
        per field: a selector's /structure fieldState carries everything required to
        query its OWN grid sub-endpoint, so this generalizes to ANY selector field on
        ANY screen (reverse-engineered + proven live, 2026-07-02).

        search: free-text match against the field's own search column (its `text`,
            e.g. a screen's Title).
        pick:   optional {column: value} to disambiguate when `search` alone matches
            multiple rows — Acumatica routinely has duplicate titles across modules
            (e.g. "Companies" matches both a Generic Inquiry, CS1015PL — NOT a valid
            entity source — and the real maintenance screen, CS101500). ALWAYS check
            `rows` before trusting `value` when more than one comes back.

        Returns {view, field, search, row_count, rows, value?}. `value` (the
        {id,text} ready for ui_set_field) is present only when exactly one row
        matches — otherwise inspect `rows` and re-call with `pick`.
        """
        struct = await self.get_ui_structure()
        fmeta = next((f for f in struct["views"].get(view, []) if f["field"] == field), None)
        if fmeta is None:
            raise ScreenError(f"ui_resolve_selector: {view}.{field} not found on {self.screen_id}")
        sel = fmeta.get("selector")
        if not sel:
            raise ScreenError(
                f"ui_resolve_selector: {view}.{field} on {self.screen_id} is not a "
                f"selector field — set it directly via ui_set_field instead."
            )
        # Some selectors omit their own graph (no fieldDacName) — fall back to the
        # screen graph (shared across all its selectors). Without a graph the grid
        # query runs but returns UNFILTERED rows (proven live: Container returned all
        # 8 views instead of the 1 matched).
        if not sel.get("graph"):
            sel = {**sel, "graph": struct.get("screen_graph")}
        # This sub-endpoint doesn't take the tenant-prefixed ui_url (404s there,
        # proven live) — always the bare base_url form, unlike every other modern-
        # plane call in this class.
        grid_url = self.instance.base_url.rstrip("/") + f"/ui/screen/{self.screen_id}/grid"
        active_rows = [self._active_tree_row] if self._active_tree_row is not None else None
        payload = _selector_grid_payload(sel, field, view, search, active_rows)
        resp = await self._http.post(grid_url, json=payload, headers=_UI_HEADERS)
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_resolve_selector {view}.{field} on {self.screen_id}: {err}")
        body = resp.json()
        cols = sel["columns"]
        rows = [{c: (r.get("cells") or {}).get(c, {}).get("value") for c in cols}
                for r in (body.get("rows") or [])]
        if pick:
            rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in pick.items())]
        result = {"view": view, "field": field, "search": search,
                  "row_count": len(rows), "rows": rows}
        if len(rows) == 1:
            vf, df = sel["value_field"], sel["search_field"]
            result["value"] = {"id": rows[0].get(vf), "text": rows[0].get(df)}
        return result

    async def selector_probe(self, struct: dict, view: str, field: str,
                             search: str) -> dict | None:
        """Query a selector's lookup grid using a PREFETCHED /structure — the fast,
        loopable core of ui_resolve_selector (which re-fetches /structure every call,
        fatal when probing hundreds of values). Returns {value_field, rows:[{col:val}]}
        or None if the field isn't a selector on this screen. `search` is the
        server-side fastFilter (the value you're validating).

        Used by validate_import_setup to check each distinct source value against its
        field's live master without a browser or a curated FK map.
        """
        fmeta = next((f for f in struct["views"].get(view, []) if f["field"] == field), None)
        sel = fmeta.get("selector") if fmeta else None
        if not sel:
            return None
        if not sel.get("graph"):
            sel = {**sel, "graph": struct.get("screen_graph")}
        grid_url = self.instance.base_url.rstrip("/") + f"/ui/screen/{self.screen_id}/grid"
        active_rows = [self._active_tree_row] if self._active_tree_row is not None else None
        payload = _selector_grid_payload(sel, field, view, search, active_rows)
        resp = await self._http.post(grid_url, json=payload, headers=_UI_HEADERS)
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"selector_probe {view}.{field} on {self.screen_id}: {err}")
        cols = sel["columns"]
        rows = [{c: (r.get("cells") or {}).get(c, {}).get("value") for c in cols}
                for r in (resp.json().get("rows") or [])]
        return {"value_field": sel["value_field"], "rows": rows}

    # WebDialogResult values accepted as a dialog answer (public PX.Data enum).
    _DIALOG_ANSWERS = {"ok": 1, "cancel": 2, "yes": 6, "no": 7}

    async def ui_command(self, name: str, answer: str = "ok") -> dict:
        """Fire a modern UI-screen command; answers a confirmation dialog if one opens.

        Field values set via ui_set_field() beforehand persist server-side in the
        session and don't need to be resent here. `name` is the internal command
        (from get_ui_structure `actions`), e.g. "Save", "generateYears".

        answer: how to respond if the command opens a 302 `openDialog` confirmation —
        "ok" (default; WebDialogResult.OK), "yes", "no", "cancel", or "none" to NOT
        answer: the command returns {dialog_open: true, dialog_view} so the caller
        can inspect what the screen is asking before committing. Raises with the
        parsed `messages[]` on a business/validation error.
        """
        ans = (answer or "ok").lower()
        if ans != "none" and ans not in self._DIALOG_ANSWERS:
            raise ScreenError(
                f"ui_command: unknown dialog answer {answer!r} — "
                f"use one of {sorted(self._DIALOG_ANSWERS)} or 'none'")
        resp = await self._ui_post({
            "command": [{"name": name}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
        })
        if resp.status_code == 302:
            body = resp.json()
            view = None
            for r in body.get("redirects", []):
                settings = r.get("settings", {})
                # openDialog is a panel; openMessageBox is a yes/no confirm (e.g.
                # SM206025 insertFrom's "provider differs — continue?"). Both are
                # answered the same way (dialogCallback), so catch both.
                if settings.get("type") in ("openDialog", "openMessageBox"):
                    view = settings.get("viewName")
                    break
            if ans == "none":
                return {"dialog_open": True, "dialog_view": view, "command": name,
                        "note": "confirmation dialog NOT answered (answer='none') — "
                                "re-fire with answer='ok'/'yes'/... to commit"}
            resp = await self._ui_post({
                "command": [{"name": name}], "data": [],
                "dialogCallback": {"dialogResult": self._DIALOG_ANSWERS[ans],
                                    "validateInput": False, "viewName": view},
                "controlsParams": {}, "activeRowContexts": [], "viewsParams": {},
            })
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_command {name} on {self.screen_id}: {err}")
        # A command that "succeeds" but was actually declined explains itself in a
        # WARNING toast, not an error — carry it back instead of dropping it.
        return self._annotate_notices(resp.json())

    @staticmethod
    def _is_processing(j: dict) -> bool:
        """True if the response says a long-running process is still in flight — a
        `longRun`/`processing` redirect, or a LongRunData block flagged in-progress.
        Conservative: an unrecognized shape returns False (don't hang on it)."""
        for r in (j.get("redirects") or []):
            if (r.get("settings") or {}).get("type") in ("longRun", "processing"):
                return True
        lr = (j.get("controlsData") or {}).get("LongRunData") or {}
        if isinstance(lr, dict):
            if lr.get("isInProgress") or lr.get("running") is True:
                return True
            st = str(lr.get("status") or "").lower()
            if st in ("inprocess", "running", "inprogress"):
                return True
        return False

    @staticmethod
    def _process_summary(j: dict) -> dict:
        """Extract the process outcome: the per-row ProcessingResultData (if any) plus
        any messages, so the caller sees WHAT the process did."""
        cd = j.get("controlsData") or {}
        return {
            "processing_result": cd.get("ProcessingResultData") or None,
            "messages": [m.get("message") for m in (j.get("messages") or []) if m.get("message")],
        }

    async def ui_run_process(self, action: str, set_fields: list[dict] | None = None,
                             load_views: list[str] | None = None,
                             poll_interval: float = 3.0, timeout: float = 45.0) -> dict:
        """Fire a PROCESS action (Process / ProcessAll / a mass-action) and drive it to
        completion on the modern plane.

        A small or empty batch finishes SYNCHRONOUSLY in the one call (verified live:
        GL503000 ProcessAll -> 200 inline). A genuinely long-running batch opens a
        processing dialog; this then polls it (via actionCloseProcessing) until it
        settles or `timeout` (best-effort — kept under the MCP request limit; a very
        long process may still need a re-poll). set_fields set the process FILTER first
        (e.g. Action/FromYear/ToYear on GL503000). Any pre-process confirmation dialog
        is auto-answered OK. Returns {ok, action, result:{processing_result, messages}}.
        """
        struct = await self.get_ui_structure()
        valid = {a["name"] for a in struct["actions"]}
        if action not in valid:
            raise ScreenError(
                f"ui_run_process: unknown action {action!r} on {self.screen_id}. "
                f"Available: {sorted(valid)}")
        views = set(load_views or []) | {f["view"] for f in (set_fields or [])}
        primary = next(iter(struct["views"]), None)
        if primary:
            views.add(primary)
        await self.ui_bootstrap(sorted(v for v in views if v))
        for f in (set_fields or []):
            await self.ui_set_field(f["view"], f["field"], f["value"])
        resp = await self._ui_post({"command": [{"name": action}], "data": [],
            "controlsParams": {}, "activeRowContexts": [], "viewsParams": {}})
        if resp.status_code == 302:  # a confirm dialog before processing -> answer OK
            body = resp.json()
            view = next(((r.get("settings") or {}).get("viewName")
                         for r in body.get("redirects", [])
                         if (r.get("settings") or {}).get("type") == "openDialog"), None)
            resp = await self._ui_post({"command": [{"name": action}], "data": [],
                "dialogCallback": {"dialogResult": 1, "validateInput": False, "viewName": view},
                "controlsParams": {}, "activeRowContexts": [], "viewsParams": {}})
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_run_process {action} on {self.screen_id}: {err}")
        j = resp.json()
        waited = 0.0
        # Back off from a short first wait up to poll_interval, instead of sleeping the
        # full interval before the FIRST poll. Most process actions here finish in well
        # under a second, and the old fixed 3s meant every one of them cost 3s of pure
        # sleep. The cap keeps a genuinely long process at the same steady-state poll
        # rate (and the same request count) as before.
        delay = min(0.25, poll_interval)
        while waited < timeout and self._is_processing(j):
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * 2, poll_interval)
            rp = await self._ui_post({"command": [{"name": "actionCloseProcessing"}],
                "data": [], "controlsParams": {}, "activeRowContexts": [], "viewsParams": {}})
            if self._ui_error(rp):
                break
            j = rp.json()
        return {"screen_id": self.screen_id, "action": action,
                "ok": not self._is_processing(j),
                "still_processing": self._is_processing(j),
                "result": self._process_summary(j)}

    async def ui_grid_row_action(self, grid_view: str, row_key: dict, action: str,
                                  parent: dict | None = None, confirm: bool = True) -> dict:
        """Select an EXISTING grid row by key, then fire a screen-level ACTION on it.

        Closes the one capability the classic SOAP plane structurally lacks: it can
        navigate to a keyed MASTER record but cannot select an arbitrary existing
        GRID row by key, so a "click this row, then hit a toolbar button" flow
        (SM203520 Restore Snapshot; any process-a-selected-row screen) is
        impossible there. The modern plane addresses a row via activeRowContexts
        (GridActiveDataRow), which is what this drives.

        grid_view: the grid container/view (from get_ui_structure `grids`, e.g.
            "Snapshots" on SM203520).
        row_key:   {keyField: value} identifying the row (key fields from
            get_ui_structure grids[grid_view].key_fields, e.g. {"SnapshotID": ...}).
        action:    the internal command to fire with that row active (from
            get_ui_structure `actions`, e.g. "importSnapshotCommand").
        parent:    MASTER-DETAIL / tenant-scoped screens — {"view", "key"} to load
            the header record first (e.g. SM203520 {"view":"Companies",
            "key":{"CompanyID":3}}); the grid + row are then addressed under it.
            None for a top-level grid.
        confirm:   auto-answer a 302 openDialog with OK (WebDialogResult.OK). Set
            False to leave a confirmation dialog UN-answered (the action then only
            opens the dialog and does NOT commit) — a safe "arm without firing".

        Returns {ok, grid_view, row_key, action, status, dialog_view?, redirect?,
        graph_is_dirty, messages}. `status` is "committed" (action ran / dialog
        answered), "dialog_open" (confirm=False, dialog left open), or "redirected"
        (server answered with a goTo — e.g. Restore redirects to SM203510 to run/
        monitor; NOT an error, but not a synchronous completion either — verify the
        downstream effect). Raises ScreenError on an explicit business/validation
        error. Requires allow_write for a committing action.

        PRECONDITION (KB-first policy): consult kb-mcp for the screen first.
        """
        # 1. load the header (and the grid) so the row is addressable.
        if parent:
            await self.ui_grid_read(grid_view, parent)
        else:
            await self.ui_bootstrap([grid_view])
        active = [{"dataView": grid_view, "syncPosition": True,
                    "dataKey": row_key, "resultType": "GridActiveDataRow"}]
        views = {grid_view: {}}
        if parent:
            views[parent["view"]] = {}
        # 2. fire the action with the row active.
        resp = await self._http.post(self.ui_url, json={
            "command": [{"name": action}], "data": [],
            "controlsParams": {}, "activeRowContexts": active, "viewsParams": views,
        }, headers=_UI_HEADERS)
        dialog_view = None
        status = "committed"
        if resp.status_code == 302:
            body = resp.json()
            goto = None
            for r in body.get("redirects", []):
                s = r.get("settings", {})
                if s.get("type") == "openDialog":
                    dialog_view = s.get("viewName")
                elif s.get("type") == "goTo":
                    goto = r.get("url")
            if dialog_view and not confirm:
                return {"ok": True, "grid_view": grid_view, "row_key": row_key,
                        "action": action, "status": "dialog_open",
                        "dialog_view": dialog_view, "graph_is_dirty": None,
                        "messages": []}
            if dialog_view:
                # answer OK — the real commit; keep the row active + dialogCallback.
                resp = await self._http.post(self.ui_url, json={
                    "command": [{"name": action}], "data": [],
                    "dialogCallback": {"dialogResult": 1, "validateInput": False,
                                        "viewName": dialog_view},
                    "controlsParams": {}, "activeRowContexts": active, "viewsParams": views,
                }, headers=_UI_HEADERS)
            elif goto:
                # a bare goTo (no dialog) — the action handed off to another screen.
                err = self._ui_error(resp)
                if err:
                    raise ScreenError(f"ui_grid_row_action {action} on {self.screen_id}: {err}")
                return {"ok": True, "grid_view": grid_view, "row_key": row_key,
                        "action": action, "status": "redirected", "redirect": goto,
                        "graph_is_dirty": None, "messages": []}
        err = self._ui_error(resp)
        if err:
            raise ScreenError(f"ui_grid_row_action {action} on {self.screen_id}: {err}")
        # a committing action can STILL answer with a post-commit goTo (e.g. Restore
        # → SM203510 to run/monitor). Surface that honestly rather than as a plain OK.
        redirect = None
        j = {}
        try:
            j = resp.json()
        except Exception:  # noqa: BLE001
            j = {}
        if resp.status_code == 302:
            for r in j.get("redirects", []):
                if r.get("settings", {}).get("type") == "goTo":
                    redirect = r.get("url")
                    status = "redirected"
        return {"ok": True, "grid_view": grid_view, "row_key": row_key,
                "action": action, "status": status, "redirect": redirect,
                "dialog_view": dialog_view,
                "graph_is_dirty": j.get("graphIsDirty"),
                "messages": [m.get("message") for m in (j.get("messages") or [])]}

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
        await self._ensure_login()
        if parent:
            pv = parent["view"]
            await self._http.post(self.ui_url, json={
                "isFirstRequest": True, "data": [], "controlsParams": {},
                "activeRowContexts": [], "viewsParams": {pv: {}}, "clearSession": True,
            }, headers=_UI_HEADERS)
            # composite header keys (e.g. SM207060 InterfaceName+GateVersion) are
            # navigated field-by-field, like the browser commits them; the child
            # grid is co-requested only on the LAST set, once the record is current.
            key_items = list(parent["key"].items())
            resp = None
            for i, (pf, pval) in enumerate(key_items):
                last = i == len(key_items) - 1
                resp = await self._http.post(self.ui_url, json={
                    "data": [{"viewName": pv, "fieldName": pf, "value": str(pval),
                              "rowId": "", "changeType": 5}],
                    "controlsParams": {}, "activeRowContexts": [],
                    "viewsParams": ({pv: {}, grid_view: {}} if last else {pv: {}}),
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

    @staticmethod
    def _key_mangle_norm(s: Any) -> str:
        """Normalize a key the way a field's key input-mask does when it rejects
        punctuation: every non-alphanumeric char -> space, runs of space collapsed,
        trimmed. Used ONLY to RECOGNIZE a server-mangled key (sent 'KK.' persisted as
        'KK'), never to alter what we send."""
        return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]", " ", str(s))).strip()

    @classmethod
    def _is_altered_key(cls, sent: Any, stored: Any) -> bool:
        """True if `stored` looks like a silently-altered form of `sent` — the two
        SILENT key transforms proven on this platform's key fields:
          • punctuation replaced with spaces (classic plane: 'A. SELERA'->'A  SELERA')
          • right-truncation at the field length (modern plane: 11-char 'ZZ.TEST/GRD'
            ->'ZZ.TEST/GR')
        Identical values (post-strip) are NOT 'altered'. Order matters: exact is
        checked by the caller first."""
        s, t = str(sent).strip(), str(stored).strip()
        if not t or s == t:
            return False
        ns, nt = cls._key_mangle_norm(s), cls._key_mangle_norm(t)
        if ns == nt:
            return True                     # punctuation -> space
        if s.startswith(t):
            return True                     # right-truncated
        if nt and ns.startswith(nt):
            return True                     # punctuation + truncation
        return False

    async def _verify_stored_key(self, grid_view: str, g: dict, sent_values: dict,
                                 save_resp: Any, parent: dict | None) -> dict | None:
        """After an insert, confirm the row persisted under the EXACT key sent.

        Acumatica can silently ALTER a key field on save — two transforms proven live:
        punctuation replaced with spaces (classic plane: CS205010 BuildingCD
        'A. SELERA'->'A  SELERA') and right-truncation at the field length (modern
        plane: 'ZZ.TEST/GRD'->'ZZ.TEST/GR'). Either way a later lookup/import by the
        ORIGINAL key misses. Returns {warning, sent_key, stored_key} if the stored key
        differs, else None. Best-effort: prefers the Save response's echoed grid rows
        (free); falls back to one fresh read only if the response carried none; never
        raises."""
        key_names = g.get("key_names") or []
        sent_key = {k: sent_values[k] for k in key_names if k in sent_values}
        if not sent_key:
            return None
        rows = (((save_resp or {}).get("controlsData") or {}).get(grid_view) or {}).get("rows") \
            if isinstance(save_resp, dict) else None
        if not rows:
            try:
                rows = (await self.ui_grid_read(grid_view, parent)).get("rows")
            except Exception:  # noqa: BLE001 — verification is best-effort, never block
                return None
        if not rows:
            return None
        # exact (post-strip) key present -> stored as sent, no alteration
        idx, _ = self._locate_row(rows, sent_key)
        if idx is not None:
            return None
        # else find the row whose key is a silently-altered form of what we sent
        for row in rows:
            stored = {k: (self._cell_key(row, k) or "") for k in sent_key}
            matched = all(str(stored[k]).strip() == str(v).strip()
                          or self._is_altered_key(v, stored[k])
                          for k, v in sent_key.items())
            altered = any(self._is_altered_key(v, stored[k]) for k, v in sent_key.items())
            if matched and altered:
                return {
                    "warning": "the row persisted under a DIFFERENT key than you sent — "
                    "the screen silently altered a key field on save (punctuation "
                    "replaced with spaces, or the value truncated at the field length). "
                    "Reference the STORED key in later lookups, updates, deletes, and "
                    "imports.",
                    "sent_key": sent_key,
                    "stored_key": {k: stored[k] for k in sent_key},
                }
        return None

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
        # Grid Saves are the classic silent-no-op surface: a clean 200 whose warning
        # toast is the only clue the rows didn't take. Carry it on the result.
        return self._annotate_notices(resp.json())

    @staticmethod
    def _parse_grid_cols(columns: list | None) -> dict[str, dict]:
        """Grid column list -> {field: {readonly, options}}. `allowUpdate` is the
        per-cell read-only signal (False = read-only); `valueItems.items` are the
        enum allowed-values [{value,text}] (same shape as a form field's options)."""
        meta: dict[str, dict] = {}
        for c in (columns or []):
            f = c.get("field")
            if not f:
                continue
            vi = c.get("valueItems")
            items = vi.get("items") if isinstance(vi, dict) else None
            meta[f] = {
                "readonly": c.get("allowUpdate") is False,
                "options": ([{"value": o.get("value"), "text": o.get("text")} for o in items]
                            if items else None),
            }
        return meta

    async def _grid_col_meta(self, grid_view: str, grid_read: dict) -> dict[str, dict]:
        """Per-column meta for grid_view: from the grid read's columns (has valueItems),
        falling back to the /structure controlsData columns when the grid is EMPTY (a
        grid read of 0 rows returns 0 columns — proven live). {} if neither yields a
        column list, in which case the caller skips validation (never blocks)."""
        meta = self._parse_grid_cols(grid_read.get("columns"))
        if meta:
            return meta
        try:
            r = await self._http.get(self.ui_url + "/structure",
                                     headers={"Accept": "application/json"})
            cols = ((r.json().get("controlsData") or {}).get(grid_view) or {}).get("columns")
            return self._parse_grid_cols(cols)
        except Exception:  # noqa: BLE001 — best-effort; never block a write on this
            return {}

    @staticmethod
    def _grid_validate_coerce(cmeta: dict, values: dict) -> tuple[dict, list[dict]]:
        """Grid-cell peer of ui_coerce_validate. For each value whose column is known:
        flag a read-only cell, coerce an enum display-label to its option value, or
        flag an invalid enum (with the allowed list). A value whose column ISN'T in
        cmeta is passed through untouched (column-completeness varies by screen — a
        spurious block on a real column is worse than missing a typo). Returns
        (coerced_values, issues)."""
        out: dict = {}
        issues: list[dict] = []
        for k, v in values.items():
            m = cmeta.get(k)
            if not m:
                out[k] = v
                continue
            if m.get("readonly"):
                issues.append({"field": k, "value": v,
                               "problem": "read-only cell (allowUpdate=false) — accepted "
                               "by the plane but silently ignored"})
                out[k] = v
                continue
            opts = m.get("options")
            if opts and v is not None and not isinstance(v, bool):
                sv = str(v)
                if any(sv == str(o.get("value")) for o in opts):
                    out[k] = v
                else:
                    match = next((o for o in opts
                                  if sv.lower() == str(o.get("text")).lower()), None)
                    if match:
                        out[k] = match.get("value")
                    else:
                        issues.append({"field": k, "value": v,
                                       "problem": "not a valid option (would silently "
                                       "no-op)", "allowed": opts})
                        out[k] = v
            else:
                out[k] = v
        return out, issues

    async def _grid_write_guard(self, grid_view: str, g: dict, values: dict,
                                op: str, skip_validation: bool) -> tuple[dict, dict | None]:
        """Run grid-cell validation/coercion. Returns (coerced_values, refusal) — if
        `refusal` is non-None the caller must return it (ok:false) instead of writing."""
        if skip_validation:
            return values, None
        cmeta = await self._grid_col_meta(grid_view, g)
        if not cmeta:
            return values, None  # no column shape -> skip, never block
        coerced, issues = self._grid_validate_coerce(cmeta, values)
        if issues:
            return values, {
                "screen_id": self.screen_id, "grid_view": grid_view, "ok": False,
                "validation_errors": issues,
                "messages": [f"{i['field']}: {i['problem']}" for i in issues],
                "note": f"Refused {op} — these cells would be silently ignored by the "
                        "modern plane (read-only or invalid enum). Fix the value(s), or "
                        "pass skip_validation=true to override."}
        return coerced, None

    async def ui_update_grid_row(self, grid_view: str, key: dict, values: dict,
                                 parent: dict | None = None,
                                 skip_validation: bool = False) -> dict:
        """Update ONE existing grid row in place, matched by its key field(s).

        key:    {keyField: value} — the child-identifying key (for a detail grid the
                parent-linkage id is resolved from the row automatically).
        values: {field: newValue} cells to change. The full key is re-sent in the
                row's `values` so the server UPDATES (not inserts). Idempotent.
        parent: {"view", "key"} to target a detail grid under a header (see
                ui_grid_read). None = top-level grid.
        """
        g = await self.ui_grid_read(grid_view, parent)
        values, refusal = await self._grid_write_guard(grid_view, g, values,
                                                       "ui_update_grid_row", skip_validation)
        if refusal:
            return refusal
        idx, row = self._locate_row(g["rows"], key)
        if row is None:
            raise ScreenError(f"ui_update_grid_row: no row in {grid_view} matches key {key}")
        full = self._full_key(row, g["key_names"]) or key
        change = {"id": row.get("id"), "index": idx, "values": self._kv(full) + self._kv(values)}
        return await self._grid_save(grid_view, g, {"modified": [change]}, full,
                                     "ui_update_grid_row", parent)

    async def ui_update_grid_rows(self, grid_view: str, updates: list[dict],
                                  parent: dict | None = None,
                                  skip_validation: bool = False,
                                  chunk_size: int = 500) -> dict:
        """Update MANY existing grid rows — ONE grid read + ONE Save per chunk.

        The bulk peer of ui_update_grid_row, which re-reads the ENTIRE grid to
        resolve one row's id+index: N rows cost N full reads, and on a big grid
        (6977 prepared-import rows, ~1.6 MB a read) that is minutes of wall-clock
        and enough concurrent load to blow the MCP request timeout. changes.modified
        is already a LIST server-side, so batching is the fix: locate every target
        row in one read, commit the batch in one Save.

        updates:    [{"key": {keyField: value}, "values": {field: newValue}}, ...]
        chunk_size: rows per Save.

        The grid is read ONCE up front. A Save echoes the grid back with fresh row
        ids (the same echo _verify_stored_key relies on), so each chunk re-seeds the
        next chunk's row map from its own Save response instead of paying another
        full read — the read was the dominant cost, not the Save (that 6977-row grid
        was 70 chunks x ~1.6 MB = ~112 MB of pure re-reading). A fresh read is still
        issued if a Save echoes no usable rows, so correctness never depends on the
        echo being present.

        A row whose key matches nothing is collected in `not_found` and a row whose
        cells fail validation in `validation_errors`; neither aborts the run (the
        rest still commit), mirroring screen_bulk_load's per-row isolation.
        Returns {ok, total, updated, chunks, not_found, validation_errors, notices?}.
        `notices` carries any WARNING/INFO toast a chunk's Save returned (tagged with
        its chunk) — those are not errors, but they are how the screen says it ignored
        what you sent, so `updated` counts rows SENT, not rows the screen kept.
        """
        if not updates:
            return {"ok": True, "total": 0, "updated": 0, "chunks": 0,
                    "not_found": [], "validation_errors": []}
        not_found: list[dict] = []
        refusals: list[dict] = []
        notices: list[dict] = []
        updated = chunks = 0
        step = max(int(chunk_size), 1)
        g = await self.ui_grid_read(grid_view, parent)
        # column meta is per-grid, not per-row (and not per-chunk) — resolve once.
        cmeta = {} if skip_validation else await self._grid_col_meta(grid_view, g)
        for start in range(0, len(updates), step):
            batch = updates[start:start + step]
            changes: list[dict] = []
            last_full: dict | None = None
            for u in batch:
                key, vals = u["key"], u["values"]
                if cmeta:
                    vals, issues = self._grid_validate_coerce(cmeta, vals)
                    if issues:
                        refusals.append({"key": key, "validation_errors": issues})
                        continue
                idx, row = self._locate_row(g["rows"], key)
                if row is None:
                    not_found.append(key)
                    continue
                full = self._full_key(row, g["key_names"]) or key
                changes.append({"id": row.get("id"), "index": idx,
                                "values": self._kv(full) + self._kv(vals)})
                last_full = full
            if not changes:
                continue
            # dataKey mirrors the browser: the row the cursor ends on after the edits.
            save_resp = await self._grid_save(grid_view, g, {"modified": changes}, last_full,
                                              "ui_update_grid_rows", parent)
            updated += len(changes)
            chunks += 1
            # A warning toast on a chunk explains an accepted-but-ignored Save; keep it
            # per-chunk so it can be tied back to which rows it applied to.
            for n in self._notices(save_resp):
                notices.append({"chunk": chunks, **n})
            if start + step >= len(updates):
                break  # last chunk — nothing left to re-map
            g = await self._regrid(grid_view, g, save_resp, parent)
        out = {"ok": not refusals and not not_found, "total": len(updates),
               "updated": updated, "chunks": chunks,
               "not_found": not_found, "validation_errors": refusals}
        if notices:
            out["notices"] = notices
            out["note"] = ("The screen returned warning/info messages — `updated` counts "
                           "rows SENT, not rows the screen necessarily kept. Read them.")
        return out

    async def _regrid(self, grid_view: str, g: dict, save_resp: Any,
                      parent: dict | None) -> dict:
        """Row map for the NEXT chunk: reuse the Save's echoed rows when they look like
        a full-grid echo, else re-read.

        A Save echoes controlsData.<grid>.rows with fresh ids (pageSize is set to the
        whole grid in _grid_ctrl), which makes the re-read redundant. The guard is the
        row COUNT: an update-only Save cannot shrink the grid, so an echo with fewer
        rows than we had is a partial/delta echo, not the full grid — reusing it would
        silently lose rows from the map and report them `not_found`. That falls back to
        a real read, i.e. exactly the old behaviour.
        """
        echoed = None
        if isinstance(save_resp, dict):
            echoed = (((save_resp.get("controlsData") or {}).get(grid_view)) or {}).get("rows")
        if echoed and len(echoed) >= len(g.get("rows") or []):
            return {**g, "rows": echoed}
        return await self.ui_grid_read(grid_view, parent)

    async def ui_insert_grid_row(self, grid_view: str, values: dict,
                                 parent: dict | None = None,
                                 skip_validation: bool = False) -> dict:
        """Append a NEW grid row. `values` MUST include the grid's key field(s) plus
        any other required columns (e.g. GL202500 needs AccountCD + Type + Description).
        For a detail grid (parent set) the parent-linkage id is auto-filled server-side,
        so `values` needs only the child fields. A client rowId is generated.

        Cell writes are validated/coerced like form fields (read-only + invalid-enum
        refused, enum label->value coerced) when the grid's column meta is available;
        skip_validation=true bypasses."""
        g = await self.ui_grid_read(grid_view, parent)
        values, refusal = await self._grid_write_guard(grid_view, g, values,
                                                       "ui_insert_grid_row", skip_validation)
        if refusal:
            return refusal
        change = {"id": str(uuid.uuid4()), "index": len(g["rows"]), "values": self._kv(values)}
        res = await self._grid_save(grid_view, g, {"inserted": [change]}, None,
                                    "ui_insert_grid_row", parent)
        # Key-mangle guard: a key field can be silently normalized on save (e.g.
        # CS205010 BuildingCD turns '.' '/' '*' into spaces), so the row persists
        # under a DIFFERENT key than sent and a later lookup/import by the original
        # key misses. Flag it here — the first time — instead of N rows later.
        warn = await self._verify_stored_key(grid_view, g, values, res, parent)
        if warn and isinstance(res, dict):
            res.setdefault("warnings", []).append(warn)
            res["key_mangled"] = True
        return res

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
        await self._ensure_login()
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
        # Parse the schema as a TREE, not by regex. The old regex required each
        # container to open with a paired <DisplayName>…</DisplayName>, so a container
        # with an empty/self-closing/absent DisplayName — an "unnamed" SUMMARY/header
        # container (e.g. PY302000's PayCodeCD/Description/Type header) — was dropped
        # ENTIRELY, leaving the record un-navigable. A tree walk keys every direct
        # child that holds field descriptors, named or not (falling back to _Summary).
        tree = await self._ensure_tree()

        def _local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        containers: dict[str, dict] = {}
        for cont in tree:
            cname = _local(cont.tag)
            # skip the action container (its children are toolbar actions, not data
            # fields — use {"action": ...} in submit) + schema-level scalars.
            if cname in ("ServiceCommands", "DisplayName", "Actions"):
                continue
            fields: dict[str, dict] = {}
            for fld in cont:
                fname = _local(fld.tag)
                if fname in ("DisplayName", "ServiceCommands"):
                    continue
                field = fld.findtext("FieldName")
                obj = fld.findtext("ObjectName")
                if field is not None and obj is not None:
                    fields.setdefault(fname, {"object": obj, "field": field})
            if fields:
                # an unnamed container has a real XML tag anyway; only fall back if blank
                containers.setdefault(cname or "_Summary", fields)
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

    async def _ui_field_meta(self) -> dict[tuple[str, str], dict]:
        """(view, field) -> {readonly, enabled, options, required} from the modern
        /structure. Cached per session; best-effort (returns {} if the modern plane
        can't read this screen, e.g. a SetupNotEntered/unlicensed screen)."""
        if self._ui_meta is None:
            self._ui_meta = {}
            try:
                st = await self.get_ui_structure()
                for view, fields in (st.get("views") or {}).items():
                    for f in fields:
                        self._ui_meta[(view, f["field"])] = f
            except Exception:
                self._ui_meta = {}  # cache the miss; never block a write on this
        return self._ui_meta

    async def classify_writable(self, field_names: list[str]) -> tuple[list[str], list[str]]:
        """Split friendly field names into (writable, readonly) using modern-plane
        field metadata. A read-only field here is typically a ROLLUP/parent (e.g. the
        CS100000 'StandardFinancials' feature) that the platform toggles automatically
        when a child is enabled — setting it directly is refused/no-op. Fields that
        can't be positively identified are treated as WRITABLE (never silently dropped)."""
        try:
            await self._ensure_tree()
        except Exception:  # noqa: BLE001 — if the tree won't load, don't classify
            return list(field_names), []
        meta = await self._ui_field_meta()
        if not meta:
            return list(field_names), []
        writable, readonly = [], []
        for f in field_names:
            try:
                el = self._find_field(f)
                m = meta.get((el.findtext("ObjectName"), el.findtext("FieldName")))
            except ScreenError:
                m = None
            if m and (m.get("readonly") or m.get("enabled") is False):
                readonly.append(f)
            else:
                writable.append(f)
        return writable, readonly

    async def _validate_sets(self, commands: list[dict]) -> list[dict]:
        """Best-effort pre-write validation of {set} commands against modern-plane
        field metadata. Catches two silent-corruption classes: writing a read-only
        field (accepted, ignored, ok:true) and an invalid enum value (accepted,
        silently no-op/defaulted, ok:true). Only flags fields POSITIVELY identified
        in the metadata — an unmappable field (grid column, modern plane unreachable)
        is skipped, never falsely flagged. Returns a list of issue dicts."""
        meta = await self._ui_field_meta()
        if not meta:
            return []
        issues: list[dict] = []
        for c in commands:
            if "set" not in c or "key" in c:
                continue
            val = c.get("to")
            try:
                el = self._find_field(c["set"])
            except ScreenError:
                continue
            fld, obj = el.findtext("FieldName"), el.findtext("ObjectName")
            m = meta.get((obj, fld))
            if not m:
                continue  # grid column / unmapped — skip, don't guess
            if m.get("readonly") or m.get("enabled") is False:
                issues.append({"field": c["set"], "value": val,
                               "problem": "read-only / not writable — the write is "
                               "accepted by SOAP but silently ignored"})
                continue
            opts = m.get("options")
            if opts and val is not None:
                sval = str(val)
                ok = any(sval == str(o.get("value")) or sval.lower() == str(o.get("text")).lower()
                         for o in opts)
                if not ok:
                    issues.append({"field": c["set"], "value": val,
                                   "problem": "not a valid option (SOAP would silently "
                                   "keep the current/default value)",
                                   "allowed": [{"value": o.get("value"), "text": o.get("text")}
                                               for o in opts]})
        return issues

    async def submit(
        self,
        commands: list[dict],
        dry_run: bool = False,
        auto_answer: str | None = None,
        skip_validation: bool = False,
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
        # Pre-write guard (#1 enum / #2 read-only): SOAP accepts a read-only or
        # invalid-enum SET with ok:true and silently drops it. Validate against the
        # modern-plane field metadata and FAIL LOUD instead of corrupting silently.
        # Best-effort: only fires when the field is positively identified; pass
        # skip_validation=True to bypass. Skipped under dry_run (already non-committing).
        if not dry_run and not skip_validation:
            issues = await self._validate_sets(commands)
            if issues:
                return {
                    "screen_id": self.screen_id, "ok": False,
                    "validation_errors": issues,
                    "messages": [f"{i['field']}: {i['problem']}" for i in issues],
                    "note": "Refused to submit — these SETs would be silently ignored by "
                            "SOAP (ok:true but no change). Fix the value(s) or pass "
                            "skip_validation=true to override.",
                }
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
            out = {
                "screen_id": self.screen_id,
                "ok": False,
                "error": str(e),
                "field_errors": field_errors,
                "messages": [f["message"] for f in field_errors],
            }
            # #4b: an insert/Save fault whose per-field diagnostic came back empty
            # (e.g. "Inserting 'X' record raised at least one error") leaves the caller
            # with nothing actionable. Best-effort: list the screen's REQUIRED fields
            # (modern plane) and which the caller did set, so the missing one is
            # obvious without a second round of manual discovery.
            if not field_errors:
                try:
                    meta = await self._ui_field_meta()
                    if meta:
                        req = sorted(f"{v}.{fld}" for (v, fld), m in meta.items()
                                     if m.get("required"))
                        set_objs = {self._find_field(c["set"]).findtext("FieldName")
                                    for c in commands if "set" in c}
                        if req:
                            out["required_fields"] = req
                            out["fields_you_set"] = sorted(x for x in set_objs if x)
                            out["hint"] = ("The SOAP fault carries no field detail. "
                                           "A required field is likely unset — compare "
                                           "required_fields against fields_you_set.")
                except Exception:
                    pass
            return out
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
        """Insert N grid/detail rows into `container`, ONE Submit per row.

        header: field sets applied first (the parent/context, e.g. a document key)
                — keys may be friendly or "Container.Field". Applied once, before
                the row loop, in its own Submit.
        rows:   list of {field: value}; each row gets its own NewRow + field SETs +
                Save, submitted independently. Field names are the schema's friendly
                names (qualify "Container.Field" if a name repeats).

        This is the master-detail / bulk-grid writer (e.g. Chart of Accounts rows,
        subaccount segments).

        FIXED 2026-07-13: previously bundled every row's NewRow+Set into ONE Submit
        envelope. The screen-SOAP command stream carries no explicit row-index on a
        Value command — it relies entirely on the server's "current row after the
        last NewRow" state, which does not reliably hold across multiple NewRows in
        one Submit. Proven live on CS205010 (Buildings grid): a 2-row batched insert
        left field values shifted onto the wrong BuildingCD, and a dry_run's
        NewRow/Set commands (dry_run only drops the Save, not the row-add/field-set)
        left the graph dirty for the NEXT call to inherit, corrupting an unrelated
        later Save too. One Submit per row eliminates both: each row is now fully
        isolated (matches the already-safe pattern in screen_bulk_load and the
        modern-plane ui_insert_grid_row).

        Returns {ok, row_count, succeeded, failed, results:[{index, ok, ...}], and
        (for back-compat with single-Submit callers) messages/field_errors merged
        across all rows}.
        """
        header_cmds = [{"set": k, "to": v} for k, v in (header or {}).items()]
        if header_cmds:
            hres = await self.submit(header_cmds, dry_run=dry_run, auto_answer=auto_answer)
            if not hres.get("ok"):
                hres["note"] = "header field-set failed — no rows were attempted"
                return hres
        results: list[dict] = []
        for i, row in enumerate(rows):
            cmds = [{"new_row": container}]
            for k, v in row.items():
                cmds.append({"set": k, "to": v})
            if save:
                cmds.append({"action": "Save"})
            r = await self.submit(cmds, dry_run=dry_run, auto_answer=auto_answer)
            r["index"] = i
            results.append(r)
        all_ok = all(r.get("ok") for r in results)
        merged_messages = [m for r in results for m in (r.get("messages") or [])]
        merged_field_errors = [fe for r in results for fe in (r.get("field_errors") or [])]
        return {
            "screen_id": self.screen_id,
            "ok": all_ok,
            "dry_run": dry_run,
            "row_count": len(rows),
            "succeeded": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "results": results,
            "messages": merged_messages,
            "field_errors": merged_field_errors,
        }

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
            cond = _normalize_condition(flt)  # #7: alias ops, reject unknown loudly
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
