"""grp-mcp MCP server.

Exposes Acumatica's contract-based REST API as MCP tools. All tools accept an
optional `instance` argument selecting a configured connection; when omitted the
default instance is used.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .acumatica import AcumaticaClient, AcumaticaError
from .config import Config, Instance, load_config, save_config
from .customization import CustomizationClient, encode_zip
from .loaders import map_row, read_rows
from .screen import ScreenClient, ScreenError

_KB_FIRST_POLICY = (
    "TOOL SELECTION: this server has ~77 tools across FOUR Acumatica planes (contract "
    "REST, DAC/GI OData, classic screen SOAP, modern UI-JSON). If you're unsure which "
    "tool/plane fits your task, call `guide` first (or guide(topic=...)); for one "
    "screen call screen_capabilities(screen_id); for financial-foundation setup call "
    "get_setup_guidance. Don't guess a plane.\n\n"
    "KB-FIRST CRUD POLICY (mandatory). Before ANY create/update/delete on an "
    "Acumatica screen or entity with this server — i.e. before calling "
    "create_or_update_entity, delete_entity, load_from_excel, invoke_action, "
    "attach_file, set_note, screen_submit, screen_insert_rows, screen_record, "
    "set_segment_value, create_segmented_key, create_ledger, chart_of_accounts, "
    "create_financial_calendar, enable_features, run_import_scenario, "
    "ui_screen_action, ui_insert_grid_row, ui_update_grid_row, ui_delete_grid_row, "
    "or any other write — FIRST consult the Acumatica knowledge base (the kb-mcp server: "
    "search_kb, then read_kb_file) for that screen/entity and the specific action. "
    "Read its PREREQUISITES, dependent screens, required fields, validation rules, "
    "and ordering constraints; verify each prerequisite exists in the instance "
    "(run_dac_odata / screen_get / get_entity / setup_readiness) and set up any "
    "missing one first (recursively). Do not drive a screen cold. Exempt (reads "
    "only): get_entity, count_entity, list_* (list_entities/list_actions/etc.), "
    "screen_get, screen_get_schema, screen_preflight, run_generic_inquiry, "
    "run_dac_odata, run_report, ui_get_structure, ui_read_grid, ui_resolve_selector, "
    "screen_capabilities, ui_preflight, setup_readiness, get_setup_guidance, guide, whoami. NOTE: "
    "run_import_scenario is NOT a read — despite the run_ prefix it WRITES data "
    "(executes an import scenario), so it requires the KB-first check like any other "
    "write. This exists "
    "because Acumatica screens have hard dependencies the screen won't surface "
    "until a write fails with a generic/misleading error (e.g. CS203000 Segment "
    "Values requires the key to exist on CS202000 with a Validate=ON segment, and "
    "a segmented key must be torn down children-first or it orphans)."
)

mcp = FastMCP("grp-mcp", instructions=_KB_FIRST_POLICY)

_config: Config | None = None
_clients: dict[str, AcumaticaClient] = {}
_setup_map_cache: dict | None = None
# background publish jobs: job-id -> live state dict (updated by a driver task).
# Lets publish_customization return before the site recompile finishes (which
# outlasts the MCP request timeout) while the publish still completes server-side.
_publish_jobs: dict[str, dict] = {}
# background bulk-load jobs: job-id -> live state. A large load_from_excel (sequential
# PUTs) outlasts the MCP request window, so it runs in a background task and reports
# progress + a resume offset here, mirroring _publish_jobs.
_load_jobs: dict[str, dict] = {}


def _load_job_view(state: dict) -> dict:
    """Serializable snapshot of a bulk-load job (drops the Task handle)."""
    done = bool(state.get("completed"))
    err = state.get("error")
    return {
        "job": state["job"],
        "entity": state["entity"],
        "status": "completed" if done else ("error" if err else "in_progress"),
        "total": state["total"],
        "processed": state["processed"],
        "succeeded": state["succeeded"],
        "failed": state["failed"],
        "next_offset": state["next_offset"],
        "completed": done,
        "errors": state["errors"][:50],
        "error": err,
        "note": None if (done or err) else (
            f"Load running in background: {state['processed']}/{state['total']} rows. "
            f"Poll load_status(job={state['job']!r}) until status != in_progress. "
            f"To resume after an interruption, re-run load_from_excel with "
            f"offset={state['next_offset']}."
        ),
    }


async def _drive_load(
    state: dict, client: "AcumaticaClient", entity: str, mapped: list[dict],
    base_offset: int, stop_on_error: bool,
) -> None:
    """Sequential PUT loop shared by the sync + background load paths. Updates `state`
    as it goes so load_status reflects live progress and a resume offset."""
    for i, fields in enumerate(mapped):
        try:
            await client.put_entity(entity, _wrap_fields(fields))
            state["succeeded"] += 1
        except Exception as e:  # noqa: BLE001 — record per-row, keep going
            state["failed"] += 1
            # spreadsheet row = header(1) + base_offset + (i+1)
            state["errors"].append(
                {"row": 1 + base_offset + i + 1, "error": str(e)[:300], "fields": fields})
            if stop_on_error:
                # leave next_offset AT this row so a resume retries it
                state["processed"] += 1
                state["next_offset"] = base_offset + i
                state["completed"] = True
                return
        state["processed"] += 1
        state["next_offset"] = base_offset + i + 1
    state["completed"] = True


def _publish_job_view(state: dict) -> dict:
    """Serializable snapshot of a publish job's live state (drops the Task handle)."""
    done = bool(state.get("completed"))
    err = state.get("error")
    return {
        "job": state["job"],
        "project_names": state["project_names"],
        "status": "completed" if done else ("error" if err else "in_progress"),
        "phase": state.get("phase"),
        "completed": done,
        "failed": state.get("failed"),
        "result": state.get("result"),
        "error": err,
        "note": None if (done or err) else (
            f"{'publishBegin still running (cold site can take >60s)' if state.get('phase') == 'begin' else 'Site recompile still running server-side'}"
            f" — it WILL finish on its own; do NOT re-publish. Poll "
            f"publish_status(job={state['job']!r}) until status != in_progress."
        ),
    }


def _setup_map() -> dict:
    """Load the bundled foundation setup map (documented layer). Cached."""
    global _setup_map_cache
    if _setup_map_cache is None:
        from pathlib import Path

        p = Path(__file__).resolve().parent / "setup_map.json"
        _setup_map_cache = json.loads(p.read_text(encoding="utf-8"))
    return _setup_map_cache


def _cfg() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _client(instance: str | None, endpoint: str | None = None) -> AcumaticaClient:
    """Cached contract-REST client; `endpoint` = '<Name>/<Version>' overrides the
    instance's configured endpoint for this client (e.g. 'grp_mcp/25.200.001')."""
    cfg = _cfg()
    name = instance or cfg.default
    key = f"{name}@{endpoint}" if endpoint else name
    if key not in _clients:
        inst = cfg.get(name)
        if endpoint:
            ep_name, _, ep_ver = endpoint.partition("/")
            if not ep_name or not ep_ver:
                raise ValueError(
                    f"endpoint must be '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001'), got {endpoint!r}")
            inst = inst.model_copy(update={"endpoint_name": ep_name, "endpoint_version": ep_ver})
        _clients[key] = AcumaticaClient(inst)
    return _clients[key]


async def _relieve_api_seats(exclude: object | None = None) -> None:
    """Free API seats by logging out cached contract sessions (except `exclude`).

    Wired as the default seat-reliever on both client types: when a request faults
    with "API Login Limit" (all 'Max Web Services API Users' seats in use — a trial
    has 2), the client calls this to log out the OTHER cached sessions this process
    holds, then retries once. `exclude` is the client mid-request (never logged out).
    ScreenClient/CustomizationClient sessions are context-managed and self-release, so
    the persistent seat holders are the cached contract clients in `_clients`.
    """
    for name, client in list(_clients.items()):
        if client is exclude:
            continue
        _clients.pop(name, None)
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — best-effort seat relief
            pass


# Both client types self-recover from a seat-limit fault via this reliever (retry once).
AcumaticaClient.default_seat_reliever = staticmethod(_relieve_api_seats)
ScreenClient.default_seat_reliever = staticmethod(_relieve_api_seats)


@asynccontextmanager
async def _customization(instance: str | None):
    """Short-lived cookie session for one Customization API operation.

    Not cached: opened per call and logged out on exit so it never holds an API
    license seat at idle (trial license = only 2 Web Services API Users).
    """
    cfg = _cfg()
    client = CustomizationClient(cfg.get(instance or cfg.default))
    try:
        yield client
    finally:
        await client.aclose()


def _require_publish(instance: str | None) -> None:
    """Block Customization API write ops unless the instance opted in."""
    cfg = _cfg()
    name = instance or cfg.default
    if not cfg.get(name).allow_publish:
        raise PermissionError(
            f"Publishing is disabled for instance '{name}'. Set \"allow_publish\": true "
            f"in its connections.json profile to permit publish/import/unpublish. "
            f"Note: publishing is website-level and affects ALL tenants on the instance."
        )


def _require_write(instance: str | None) -> None:
    """Block record-mutating tools unless the instance opted in (allow_write)."""
    cfg = _cfg()
    name = instance or cfg.default
    if not cfg.get(name).allow_write:
        raise PermissionError(
            f"Writes are disabled for instance '{name}'. Set \"allow_write\": true in "
            f"its connections.json profile to permit create/update, load, actions, "
            f"import-scenario, notes, and attachments. (Default is read-only.)"
        )


# Screen/UI actions that DELETE data — held to the stricter allow_delete gate (not
# just allow_write) so neither the classic screen SOAP nor the modern /ui/screen/
# plane can sidestep it. Covers a record Delete AND detail-row deletes.
_DESTRUCTIVE_ACTIONS = frozenset({"Delete", "DeleteRow", "DeleteDetail", "DeleteAll"})


def _require_delete(instance: str | None) -> None:
    """Block record deletes unless the instance opted in (allow_delete)."""
    cfg = _cfg()
    name = instance or cfg.default
    if not cfg.get(name).allow_delete:
        raise PermissionError(
            f"Deletes are disabled for instance '{name}'. Set \"allow_delete\": true "
            f"in its connections.json profile to permit delete_entity."
        )


def _require_admin(op: str) -> None:
    """Gate config-file MUTATIONS that PERSIST to connections.json (which stores
    credentials) behind an explicit opt-in env var — a separate, higher-trust gate
    than the per-instance write gates, since these edit the connector's own config
    (add/remove a profile, change the persisted default) rather than ERP data.

    Session-only variants (persist=False) are NOT gated — they don't touch disk.
    Set GRP_MCP_ALLOW_ADMIN=1 to permit persisting config changes.
    """
    allowed = os.environ.get("GRP_MCP_ALLOW_ADMIN", "").strip().lower() in ("1", "true", "yes")
    if not allowed:
        raise PermissionError(
            f"Refusing to persist a config change ({op}): writing connections.json "
            f"(which holds credentials) requires the GRP_MCP_ALLOW_ADMIN=1 environment "
            f"variable. Either set it to manage profiles, or call this with persist=false "
            f"for a session-only change. (Guards against an agent silently rewriting your "
            f"credential file.)"
        )


def _require_range(name: str, value: Any, lo: float, hi: float) -> None:
    """Validate a numeric argument is within [lo, hi]; raise ValueError otherwise."""
    if value is None:
        return
    if not isinstance(value, (int, float)) or value < lo or value > hi:
        raise ValueError(f"{name} must be a number in [{lo}, {hi}] (got {value!r})")


def _resolve_roots(roots: list[str]) -> list:
    from pathlib import Path

    return [Path(r).expanduser().resolve() for r in roots]


def _within(path, roots: list) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _check_read_path(path: str, instance: str | None):
    """Validate a file to be READ: inside read_roots (if set) and under the size cap."""
    from pathlib import Path

    cfg = _cfg()
    inst = cfg.get(instance or cfg.default)
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    roots = _resolve_roots(inst.read_roots)
    if roots and not _within(p, roots):
        raise PermissionError(
            f"Reading '{p}' is not allowed. Permitted read_roots: {[str(r) for r in roots]}."
        )
    size = p.stat().st_size
    if inst.max_file_bytes and size > inst.max_file_bytes:
        raise PermissionError(
            f"file is {size} bytes, exceeds max_file_bytes={inst.max_file_bytes}."
        )
    return p


def _check_write_path(path: str, instance: str | None):
    """Validate a path to be WRITTEN: its parent is inside write_roots (if set)."""
    from pathlib import Path

    cfg = _cfg()
    inst = cfg.get(instance or cfg.default)
    p = Path(path).expanduser().resolve()
    roots = _resolve_roots(inst.write_roots)
    if roots and not _within(p.parent, roots) and not _within(p, roots):
        raise PermissionError(
            f"Writing to '{p}' is not allowed. Permitted write_roots: {[str(r) for r in roots]}."
        )
    return p


def _shutdown_clients() -> None:
    """Best-effort: log out cached API sessions on process exit to free seats.

    Runs on a fresh event loop (the server's is gone by atexit); logout() uses its
    own short-lived httpx client so it works there. Guarded so exit never crashes.
    """
    if not _clients:
        return

    async def _close_all() -> None:
        for c in list(_clients.values()):
            try:
                await c.aclose()
            except Exception:
                pass

    try:
        import asyncio

        asyncio.run(_close_all())
    except Exception:
        pass


def _wrap(value: Any) -> Any:
    """Recursively convert plain values into Acumatica's {"value": ...} envelope.

    - scalar              -> {"value": scalar}
    - {"value": x}        -> passed through (already a wrapped scalar)
    - nested/linked dict  -> each field recursively wrapped
      (e.g. {"MainContact": {"Email": "x", "Address": {"City": "KL"}}})
    - list of rows        -> each dict row recursively wrapped (detail collection)

    This makes nested objects (vendor/customer address & contact, sub-details)
    work without the caller hand-wrapping every leaf as {"value": ...}.
    """
    if isinstance(value, dict):
        # an already-wrapped scalar envelope: {"value": ...} (nothing else) -> keep
        if set(value.keys()) <= {"value"}:
            return value
        # a nested / linked object or detail row -> recurse into its fields, but keep
        # `id` and `delete` UNWRAPPED. `id` is the row/record identifier (wrapping it
        # makes Acumatica reject the body / fail to match the row). `delete` is the
        # row-level delete flag on a detail line ({"NoteID": "...", "delete": true});
        # it must stay a bare boolean, not {"value": true}, or the row isn't removed.
        return {k: (v if k in ("id", "delete") else _wrap(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap(row) if isinstance(row, dict) else row for row in value]
    return {"value": value}


def _wrap_fields(fields: dict) -> dict:
    return {k: _wrap(v) for k, v in fields.items()}


@mcp.tool()
def list_instances() -> dict:
    """List configured Acumatica profiles (no secrets) + which one is active.

    The active profile is used by any tool called without an `instance` arg.
    Switch it with set_active_instance; add/remove with add_instance/remove_instance.
    """
    cfg = _cfg()
    # map tenant -> profiles sharing it (>1 = a same-tenant collision; instance-less
    # calls to any of them route to the active profile, which is easy to miss).
    by_tenant: dict[str, list[str]] = {}
    for n, i in cfg.instances.items():
        if i.tenant:
            by_tenant.setdefault(i.tenant, []).append(n)
    collisions = {t: names for t, names in by_tenant.items() if len(names) > 1}
    return {
        "active": cfg.default,
        "source_path": cfg.source_path,
        "tenant_collisions": collisions or None,
        "instances": [
            {
                "name": n,
                "base_url": i.base_url,
                "endpoint": f"{i.endpoint_name}/{i.endpoint_version}",
                "tenant": i.tenant,
                "active": n == cfg.default,
                "session_only": n in cfg.session_only,
                "shares_tenant_with": [x for x in by_tenant.get(i.tenant, []) if x != n] or None,
                "gates": {
                    "write": i.allow_write,
                    "delete": i.allow_delete,
                    "publish": i.allow_publish,
                },
            }
            for n, i in cfg.instances.items()
        ],
    }


@mcp.tool()
def add_instance(
    name: str,
    base_url: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    endpoint_name: str = "Default",
    endpoint_version: str = "24.200.001",
    tenant: str = "",
    branch: str = "",
    allow_write: bool = False,
    allow_delete: bool = False,
    allow_publish: bool = False,
    read_roots: list[str] | None = None,
    write_roots: list[str] | None = None,
    set_active: bool = False,
    persist: bool = True,
) -> dict:
    """Add (or replace) a connection profile and save it to connections.json.

    name: the profile key you'll pass as `instance` (e.g. "financenew").
    base_url + OAuth (client_id/client_secret/username/password): the target
        instance and a Connected Application registered there.
    Gates default to read-only (allow_write/allow_delete/allow_publish off) and the
    filesystem sandbox (read_roots/write_roots) is unset (unrestricted) unless given.
    set_active=true makes it the default profile. persist=true writes connections.json
    (the file is gitignored) — and, because that file holds credentials, persisting
    requires the GRP_MCP_ALLOW_ADMIN=1 env var (an admin gate separate from the ERP
    write gates); persist=false is a session-only add that needs no gate. Returns the
    updated profile list (no secrets).

    Needs a registered Connected Application on the target instance (Integration ->
    Connected Applications, Resource Owner Password Credentials flow).
    """
    cfg = _cfg()
    inst = Instance(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
        username=username,
        password=password,
        endpoint_name=endpoint_name,
        endpoint_version=endpoint_version,
        tenant=tenant,
        branch=branch,
        allow_write=allow_write,
        allow_delete=allow_delete,
        allow_publish=allow_publish,
        read_roots=read_roots or [],
        write_roots=write_roots or [],
    )
    if persist:
        _require_admin("add_instance persist")
    existed = name in cfg.instances
    # same-tenant collision: another profile shares this tenant. A tool called WITHOUT
    # instance= routes to cfg.default, so if this add isn't made active the read/write
    # silently lands on the OTHER same-tenant profile — surface that up front.
    collisions = [n for n, i in cfg.instances.items()
                  if n != name and i.tenant == inst.tenant and inst.tenant]
    cfg.instances[name] = inst
    if persist:
        cfg.session_only.discard(name)
    else:
        cfg.session_only.add(name)
    _clients.pop(name, None)  # drop any stale cached client for this name
    if set_active or len(cfg.instances) == 1:
        cfg.default = name
    saved = save_config(cfg) if persist else None
    is_active = cfg.default == name
    routing = (
        f"active — instance-less tool calls now route here."
        if is_active else
        f"NOT active (active={cfg.default!r}). Pass instance={name!r} to every call, "
        f"or set_active_instance({name!r}), or re-add with set_active=true — otherwise "
        f"calls without instance= go to {cfg.default!r}."
    )
    return {
        "added": name,
        "replaced": existed,
        "active": cfg.default,
        "session_only": not persist,
        "persisted_to": saved,
        "routing": routing,
        "same_tenant_collision": (
            f"tenant {inst.tenant!r} is also used by {collisions} — without instance= "
            f"(or making {name!r} active) same-tenant calls hit the active profile, not "
            f"this one." if collisions else None
        ),
        "instances": list_instances()["instances"],
    }


@mcp.tool()
def set_active_instance(name: str, persist: bool = False) -> dict:
    """Select which profile tools use by default (when called without `instance`).

    persist=true also writes the choice as "default" in connections.json so it
    survives a restart (requires the GRP_MCP_ALLOW_ADMIN=1 env var, since it edits the
    credential file); otherwise it's an ungated session-only switch.
    """
    cfg = _cfg()
    if name not in cfg.instances:
        raise KeyError(f"Unknown profile '{name}'. Configured: {', '.join(cfg.instances)}")
    if persist:
        _require_admin("set_active_instance persist")
    cfg.default = name
    saved = save_config(cfg) if persist else None
    return {"active": name, "persisted": bool(saved), "persisted_to": saved}


@mcp.tool()
def remove_instance(name: str, persist: bool = True) -> dict:
    """Remove a connection profile (and drop its cached session).

    If it was the active profile, the active switches to another remaining profile.
    persist=true updates connections.json (requires the GRP_MCP_ALLOW_ADMIN=1 env var,
    since it edits the credential file); persist=false is a session-only removal.
    Refuses to remove the last profile.
    """
    cfg = _cfg()
    if name not in cfg.instances:
        raise KeyError(f"Unknown profile '{name}'. Configured: {', '.join(cfg.instances)}")
    if persist:
        _require_admin("remove_instance persist")
    if len(cfg.instances) == 1:
        raise ValueError("refusing to remove the only configured profile.")
    del cfg.instances[name]
    cfg.session_only.discard(name)
    _clients.pop(name, None)
    if cfg.default == name:
        cfg.default = next(iter(cfg.instances))
    saved = save_config(cfg) if persist else None
    return {
        "removed": name,
        "active": cfg.default,
        "persisted_to": saved,
        "instances": list(cfg.instances),
    }


@mcp.tool()
async def test_connection(instance: str | None = None) -> dict:
    """Verify a profile's credentials: fetch an OAuth token and read the contract.

    Returns ok=true + the entity count on success, or ok=false + the error. Use
    after add_instance to confirm the profile works before relying on it.
    """
    cfg = _cfg()
    name = instance or cfg.default
    try:
        ents = await _client(instance).list_entities()
        return {"instance": name, "ok": True, "entity_count": len(ents)}
    except Exception as e:
        return {"instance": name, "ok": False, "error": str(e)[:400]}


@mcp.tool()
async def reload_config(instance: str | None = None) -> dict:
    """Reload connections.json from disk WITHOUT restarting the server.

    Use this after editing profiles in the config UI (grp-mcp-ui) or the file by
    hand — the server normally reads config only at startup, so this applies the
    changes (new/edited profiles, active selection, gates) to the live connector.
    Closes all cached API sessions first (frees license seats); they re-auth on
    next use. Returns the refreshed active profile + list.

    Session-only profiles (added with add_instance persist=false) are PRESERVED
    across the reload — a disk reload no longer silently drops an in-memory add.
    Disk always wins on a name conflict (the on-disk profile is authoritative).
    """
    global _config
    old = _config
    # snapshot session-only adds so a disk reload doesn't drop them
    preserved: dict[str, Instance] = {}
    if old is not None:
        preserved = {n: old.instances[n] for n in old.session_only if n in old.instances}
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
    _config = load_config()
    readded = []
    for n, inst in preserved.items():
        if n not in _config.instances:  # disk wins on conflict
            _config.instances[n] = inst
            _config.session_only.add(n)
            readded.append(n)
    out = list_instances()
    out["preserved_session_only"] = readded or None
    return out


@mcp.tool()
async def list_endpoints(instance: str | None = None) -> Any:
    """List all web service endpoints published on the instance.

    Returns each endpoint's name, version, and href (e.g. Default, GRPSetup,
    MANUFACTURING). Independent of the instance's configured endpoint.
    """
    return await _client(instance).list_endpoints()


@mcp.tool()
async def list_entities(refresh: bool = False, endpoint: str | None = None,
                        instance: str | None = None) -> Any:
    """List the top-level entities exposed by the instance's configured endpoint.

    Uses endpoint_name/endpoint_version from connections.json. Source: the
    endpoint's swagger.json (the metadata-root GET is often proxy-gated 401).
    Set refresh=true to bypass the per-session cache.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001') to hit a
    different endpoint than the configured one — no config change needed.
    """
    return await _client(instance, endpoint).list_entities(refresh=refresh)


@mcp.tool()
async def get_entity_schema(
    entity: str,
    refresh: bool = False,
    deep: bool = False,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """List the fields of one entity in the configured endpoint contract.

    entity: e.g. "Customer", "Project", "SalesOrder". Returns field names +
    count (from swagger.json). Use before create_or_update_entity to know which
    fields exist on the screen.

    deep=true returns the FULL tree: scalars + every detail collection (tab)
    expanded to its own nested fields, recursively (cycle-guarded). Use this to
    see every field inputtable via the API, including detail/tab fields, in one
    call. Pass these nested arrays back to create_or_update_entity to set details.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001').
    """
    client = _client(instance, endpoint)
    if deep:
        return await client.get_entity_schema_deep(entity, refresh=refresh)
    return await client.get_entity_schema(entity, refresh=refresh)


@mcp.tool()
async def get_endpoint_definition(
    endpoint_name: str,
    endpoint_version: str,
    expand: str = "EntityTree,EntityProperties",
    instance: str | None = None,
) -> Any:
    """Read an endpoint's contract definition from SM207060 (read-only).

    Returns the endpoint record with its entity tree / properties expanded, so you
    can see how a contract is built before extending it. Key = name + version.
    """
    rid = f"{endpoint_name}/{endpoint_version}"
    return await _client(instance).get_entity(
        "WebServiceEndpoints", rid, {"$expand": expand}
    )


# NOT a registered MCP tool (deliberately un-decorated): a PUT to
# WebServiceEndpoints is a verified no-op, so exposing it only invited an agent to
# "extend an endpoint" and silently achieve nothing. Superseded by ui_tree_dialog_insert
# (+ ui_populate_endpoint_entity_fields), which drive the real SM207060 wizard via the
# modern UI-screen plane. Kept as an importable reference documenting why REST can't.
async def extend_endpoint(
    endpoint_name: str,
    endpoint_version: str,
    create_entities: list[dict] | None = None,
    fields: list[dict] | None = None,
    entity_properties: list[dict] | None = None,
    extend_current_endpoint: list[dict] | None = None,
    instance: str | None = None,
) -> Any:
    """[NOT A TOOL — DOES NOT WORK over REST] Attempt to add entities to a contract
    via the WebServiceEndpoints entity.

    Verified on csmdev 2025R1: a PUT here is a NO-OP. WebServiceEndpoints (SM207060)
    is a STATEFUL WIZARD form, not a CRUD entity -- CreateEntity/EntityProperties are
    transient working-views (empty on read), the EntityTree encodes internal screen
    node IDs the form generates, and the create/extend ops (Insert, ExtendEntity,
    PopulateFields, Save) are container actions whose parameters aren't in the
    contract and which need live form state that doesn't survive stateless calls.

    To actually extend an endpoint, use the modern-plane tools that drive the real
    SM207060 wizard:
      - ui_tree_dialog_insert            (add an entity / detail collection), then
      - ui_populate_endpoint_entity_fields (expose its scalar fields).
    Or a customization project: import_customization + publish_customization.

    Reading a contract works fine: use get_endpoint_definition. This function is no
    longer registered as an MCP tool (it was a trap: a clean success that changed
    nothing); it remains only as importable reference.
    """
    _require_publish(instance)
    body: dict = {
        "EndpointName": endpoint_name,
        "EndpointVersion": endpoint_version,
    }
    if create_entities:
        body["CreateEntity"] = create_entities
    if fields:
        body["Fields"] = fields
    if entity_properties:
        body["EntityProperties"] = entity_properties
    if extend_current_endpoint:
        body["ExtendCurrentEndpoint"] = extend_current_endpoint
    return await _client(instance).put_entity(
        "WebServiceEndpoints", _wrap_fields(body)
    )


@mcp.tool()
async def get_entity(
    entity: str,
    record_id: str | None = None,
    filter: str | None = None,
    select: str | None = None,
    expand: str | None = None,
    top: int | None = None,
    skip: int | None = None,
    custom: str | None = None,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """Retrieve one or many records of a top-level entity.

    entity: endpoint entity name, e.g. "Customer", "SalesOrder", "Bill".
    record_id: fetch a single record by its key/id; omit to list.
    filter/select/expand/top/skip: OData-style query options (contract API $filter,
            $skip for paging the next page of a large list, etc).
    custom: $custom param to pull fields NOT in the contract (unexposed elements /
            user-defined fields), format "<View>.<Field>" comma-separated.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001') to read an
            entity that only exists on a non-default endpoint.

    For pulling an ENTIRE large table, prefer fetch_all_entities (auto-pages).
    """
    _require_range("top", top, 1, 100000)
    _require_range("skip", skip, 0, 100000000)
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if top:
        params["$top"] = top
    if skip:
        params["$skip"] = skip
    if custom:
        params["$custom"] = custom

    client = _client(instance, endpoint)
    try:
        result = await client.get_entity(entity, record_id, params)
    except AcumaticaError as e:
        # Some entities expose views with BQL delegates (e.g. ImportScenarios,
        # VendorClass, WebServiceEndpoints). A list GET with $select/$expand on
        # those 500s with "Optimization cannot be performed" / "key was not
        # present". Retry once without $select (the usual culprit), then without
        # $expand, and flag what was dropped instead of failing outright.
        msg = str(e)
        retryable = record_id is None and (select or expand) and any(
            s in msg for s in (
                "Optimization cannot be performed",
                "key was not present in the dictionary",
                "has BQL delegate",
            )
        )
        if not retryable:
            raise
        dropped: list[str] = []
        retry = dict(params)
        if "$select" in retry:
            retry.pop("$select"); dropped.append("$select")
        try:
            result = await client.get_entity(entity, record_id, retry)
        except AcumaticaError:
            if "$expand" in retry:
                retry.pop("$expand"); dropped.append("$expand")
            result = await client.get_entity(entity, record_id, retry)
        # Fail closed on the dropped $select: the server returned every field, but
        # the caller asked for a narrower projection. Re-apply it locally so fields
        # the caller deliberately excluded are NOT handed back.
        projected = False
        if "$select" in dropped and select:
            keep = {c.split("(", 1)[0].strip() for c in select.split(",") if c.strip()}
            keep |= {"id", "rowNumber"}  # keep record identity

            def _proj(rec: Any) -> Any:
                if not isinstance(rec, dict):
                    return rec
                return {k: v for k, v in rec.items() if k in keep}

            result = [_proj(r) for r in result] if isinstance(result, list) else _proj(result)
            projected = True
        return {
            "_warning": (
                f"'{entity}' has BQL-delegate views that break optimized list "
                f"queries; retried after dropping {dropped}. "
                + ("$select was re-applied locally so excluded fields are not returned; "
                   if projected else "")
                + ("$expand could not be honored (nested data absent). "
                   if "$expand" in dropped else "")
                + f"Fetch a single record by key for full options. "
                f"Original error: {msg[:200]}"
            ),
            "result": result,
        }

    # Active guard: a LIST GET (no record_id) cannot return detail/nested fields.
    # If the caller asked for any via $expand or $select, flag it — the data for
    # those fields is silently absent and must be fetched per record by key.
    if record_id is None and (expand or select):
        details = await client.detail_fields(entity)
        if details:
            requested = {p.split("(", 1)[0].strip()
                         for raw in ((expand or ""), (select or ""))
                         for p in raw.split(",") if p.strip()}
            flagged = sorted(requested & details)
            if flagged:
                return {
                    "_warning": (
                        f"List GET on '{entity}' cannot return detail fields "
                        f"{flagged} - Acumatica omits nested collections from list "
                        f"queries. Their values are NOT in this result. To get "
                        f"them, fetch one record by key: "
                        f"get_entity('{entity}', record_id=<key>, expand='"
                        f"{','.join(flagged)}')."
                    ),
                    "result": result,
                }
    return result


@mcp.tool()
async def fetch_all_entities(
    entity: str,
    filter: str | None = None,
    select: str | None = None,
    expand: str | None = None,
    page_size: int = 1000,
    max_records: int | None = None,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """Retrieve ALL records of an entity, auto-paging with $top/$skip.

    The contract API caps a single list GET, so a plain get_entity can silently
    return only the first page of a big table. This loops $skip until the last
    (short) page, concatenating results.

    page_size: rows per request ($top). max_records: hard cap to stop early
    (None = no cap). Use filter/select to scope/shrink. Returns {count, records}.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001').
    """
    _require_range("page_size", page_size, 1, 10000)
    _require_range("max_records", max_records, 1, 100000000)
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    rows = await _client(instance, endpoint).get_all(
        entity, params, page_size=page_size, max_records=max_records
    )
    return {"entity": entity, "count": len(rows), "records": rows}


@mcp.tool()
async def create_or_update_entity(
    entity: str,
    fields: dict,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """Create or update a record (PUT). Acumatica upserts by key fields.

    entity: e.g. "Customer", "SalesOrder".
    fields: plain field->value map; scalars are auto-wrapped. Detail lines go in
            a list, e.g. {"OrderType": "SO", "CustomerID": "ABC",
                          "Details": [{"InventoryID": "ITEM1", "OrderQty": 2}]}.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001') to write
            an entity that only exists on a non-default endpoint.

    Requires the instance's "allow_write": true (default is read-only).

    Detail-collection echo quirk (auto-corrected): Acumatica's PUT response
    echoes a nested detail collection you just wrote as `[]` even when it
    persisted correctly (proven on TaxReportingSettings.ReportingGroups,
    Tax.TaxSchedule, TaxCategory.Details, TaxZone.ApplicableTaxes — all `[]` on
    write, all present on read-back). When that happens here, this tool
    automatically re-fetches the record by id with those fields expanded and
    patches the real values into the result — so what you get back is always
    the true persisted state, not a misleading empty array. If that re-fetch
    itself fails (rare), the suspect keys are still `[]` but the result carries
    an `_unverified_details` list naming them — verify those manually with
    get_entity(..., expand=...) before trusting them.

    Two more real gotchas on nested detail arrays (proven on TaxReportingSettings.
    ReportingGroups): (1) a detail array ALWAYS APPENDS, never upserts-by-content —
    resending identical detail data creates a duplicate row every time; to update
    or remove an EXISTING row you must include its own `id` (from a prior
    get_entity fetch): `{"id": <id>, ...changed fields...}` to update, or
    `{"id": <id>, "delete": true}` to remove (id/delete stay bare, never
    {"value":...}-wrapped). (2) That `id` is NOT stable across separate requests —
    two consecutive fetches of the same record can return different ids for the
    same rows — but it DOES remain valid for an action issued immediately after
    the fetch that produced it. Fetch, then act right away; never cache a detail
    row's id across a later, separate call.
    """
    _require_write(instance)
    client = _client(instance, endpoint)
    result = await client.put_entity(entity, _wrap_fields(fields))
    if isinstance(result, dict):
        empty_details = [
            k for k, v in fields.items()
            if isinstance(v, list) and v
            and isinstance(result.get(k), list) and not result[k]
        ]
        record_id = result.get("id")
        if empty_details and record_id:
            try:
                fresh = await client.get_entity(
                    entity, record_id, {"$expand": ",".join(empty_details)}
                )
                still_empty = []
                for k in empty_details:
                    if isinstance(fresh, dict) and fresh.get(k):
                        result[k] = fresh[k]
                    else:
                        still_empty.append(k)
                if still_empty:
                    result["_unverified_details"] = still_empty
            except Exception:
                result["_unverified_details"] = empty_details
    return result


@mcp.tool()
async def attach_file(
    entity: str,
    record_id: str,
    file_path: str,
    filename: str | None = None,
    content_type: str | None = None,
    instance: str | None = None,
) -> Any:
    """Upload a file and attach it to an existing record (the files:put API).

    entity:    the entity the record belongs to, e.g. "DataProvider", "Vendor".
    record_id: the record's id/GUID (from a create_or_update_entity / get_entity
               response `id` or `_links.self`).
    file_path: local path to the file to upload (CSV, XLSX, PDF, ...).
    filename:  name to store it as (defaults to the file's basename).
    content_type: MIME type (auto-guessed from the extension if omitted).

    Use this to put the Pentaho CSV onto a Data Provider, or attach source
    documents to a record, entirely via API.

    Requires "allow_write": true. The file must be within the instance's read_roots
    (if configured) and under max_file_bytes.
    """
    import mimetypes

    _require_write(instance)
    p = _check_read_path(file_path, instance)
    name = filename or p.name
    ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    client = _client(instance)
    url = await client.record_files_put_url(entity, record_id, name)
    content = p.read_bytes()
    await client.put_file(url, content, ctype)
    return {
        "attached": name,
        "bytes": len(content),
        "content_type": ctype,
        "entity": entity,
        "record_id": record_id,
    }


@mcp.tool()
async def attach_file_to_provider(
    record_id: str,
    file_path: str,
    filename: str | None = None,
    content_type: str | None = None,
    instance: str | None = None,
) -> Any:
    """Attach a source file to a Data Provider (SM206015) record — GET-free.

    Use this instead of attach_file for Data Providers: the `DataProvider`
    contract entity 500s on read-back (its `Link` field has a BQL delegate), so
    the normal _links resolution fails. This builds the files:put URL by
    template (.../files/PX.Api.SYProviderMaint/Providers/<id>/<file>) and PUTs
    directly — no GET on the broken entity.

    record_id: the provider's GUID, as returned by setup_data_provider's `id`
               (also visible in the URL of its read-back error).
    file_path: the .xlsx/.csv to upload (must be within the instance read_roots).
    filename:  stored name (defaults to the file's basename).

    Requires "allow_write": true. Returns the upload URL + byte count.
    """
    import mimetypes

    _require_write(instance)
    p = _check_read_path(file_path, instance)
    name = filename or p.name
    ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    client = _client(instance)
    url = client.provider_files_put_url(record_id, name)
    content = p.read_bytes()
    await client.put_file(url, content, ctype)
    return {
        "attached": name,
        "bytes": len(content),
        "content_type": ctype,
        "record_id": record_id,
        "url": url,
    }


@mcp.tool()
async def screen_get_schema(screen_id: str, instance: str | None = None) -> Any:
    """Discover a screen's command schema via the screen-based SOAP API.

    Returns {containers: {<Container>: {<Field>: {object, field}}}} — the exact
    ObjectName + FieldName the Submit engine expects for each field. Use this to
    build the `commands` for screen_submit.

    This is the entry point for writing screens the contract REST API can't —
    context/popup/master-detail screens (e.g. Segment Values CS203000), whose
    insert actions only enable once a parent record is loaded. Read-only; opens
    and closes its own SOAP session (no API seat held at idle).
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.get_schema()


@mcp.tool()
async def ui_get_structure(screen_id: str, instance: str | None = None) -> Any:
    """Discover a screen via the MODERN UI-screen API (/ui/screen/<ID>/structure).

    The richer, modern-plane counterpart to screen_get_schema. Returns:
      views:   {ViewName: [{field, label, type, required, readonly, enabled,
                            options, selector}]} — `options` lists a fixed-enum
               field's allowed values as [{value, text}] (SET the `value`, not
               `text`). `selector` is non-null on a LOOKUP field (e.g. SM207060
               CreateEntityView.ScreenID) — pass it to ui_resolve_selector rather
               than guessing a raw value; setting a selector field's plain text
               directly does not work (proven live).
      actions: [{name, label, enabled, visible, confirm}] — the live action
               inventory; `confirm` is the dialog text an action will pop.
      grids:   {GridName: {key_fields, columns}} — key_fields are the row-key
               fields for addressing a specific grid row.

    Use this to drive ANY screen with ui_screen_action without a browser capture:
    read the views/fields (and enum options) here, then set + act. Reflects the
    LIVE graph + workflow state (what's required/enabled right now), not just DB
    nullability — a stronger preflight than screen_preflight.

    Read-only GET. Works on any Modern-UI screen; a screen whose module isn't
    configured returns a clear "PREREQUISITE NOT MET" (SetupNotEntered) error, and
    an unlicensed module is access-denied — both distinguish "not set up here" from
    a real failure. (KB: Modern UI web API controllers; JSON protocol.)
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.get_ui_structure()


@mcp.tool()
async def ui_preflight(screen_id: str, set_fields: list[dict],
                       instance: str | None = None) -> Any:
    """Dry-run a modern-plane write: validate + coerce set_fields WITHOUT writing.

    The read-only preview for ui_screen_action — runs the same write-safety pass
    (against the live /structure) and tells you, before you commit, exactly what
    would happen:
      • `issues`      — read-only fields and invalid enum values that would be
                        silently dropped (each with the `allowed` option list), and
                        ambiguous field names needing a `view`;
      • `coercions`   — enum display-text auto-normalized to its option value;
      • `normalized`  — the set_fields as they would actually be applied (views
                        resolved, values coerced);
      • `ok`          — true only if there are no issues.
    set_fields: [{"view"?: <ViewName>, "field": <FieldName>, "value": <value>}] —
        `view` optional when the field name is unique across the screen's views.
    Read-only (no graph mutation, no API seat held). Use it to check a payload,
    then pass the same set_fields to ui_screen_action.
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        normalized, issues, coercions = await s.ui_coerce_validate(set_fields)
    return {"screen_id": screen_id.upper(), "ok": not issues,
            "issues": issues, "coercions": coercions, "normalized": normalized}


@mcp.tool()
async def screen_capabilities(screen_id: str, instance: str | None = None) -> Any:
    """Recommend WHICH plane/tool to drive a screen with — the router for "use JSON
    or SOAP when needed" so you don't discover the right plane by trial-and-error.

    Probes the modern `/structure` (views, grids, selectors, action inventory) and
    derives, per operation shape, the tool to reach for and why. Encodes the
    plane-by-shape decision rule (also in setup_map.json cross_cutting_rules):

      - EDIT a keyed master record / bulk-append detail rows  -> classic SOAP
        (screen_record / screen_insert_rows): simple, frees the API seat, and the
        ONLY plane that works during a maintenance lockout (auth is per-call, not
        the cookie/forms session the modern plane rides).
      - SELECT an existing grid row / tree node, THEN act      -> modern
        (ui_grid_row_action / ui_select_tree_node + ui_screen_action): SOAP
        cannot address an existing grid row by key.
      - DIALOG / process action (Generate Calendar, Restore)   -> modern
        (ui_screen_action / ui_grid_row_action): the classic SOAP handler for
        these is often a silent no-op.
      - a field marked `selector`                              -> ui_resolve_selector
        first (setting a selector's plain text does not bind).
      - an entity already on the endpoint contract             -> REST
        (create_or_update_entity / get_entity): no screen driving needed.

    Returns {screen_id, primary_dac, grids, actions, selector_fields,
    recommendations:[{operation, plane, tool, why}]}. Read-only GET. Advisory: the
    documented rule is stable, but always confirm the live required/enabled state
    with ui_get_structure before a write. (KB-first policy still applies.)
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        struct = await s.get_ui_structure()
    grids = struct.get("grids") or {}
    actions = [a["name"] for a in struct.get("actions") or []]
    selectors = sorted({f["field"] for v in (struct.get("views") or {}).values()
                        for f in v if f.get("selector")})
    recs = []
    if grids:
        recs.append({"operation": "select an existing grid row then run an action on it",
                     "plane": "modern", "tool": "ui_grid_row_action",
                     "why": "SOAP cannot address an existing grid row by key; the modern "
                            "plane selects it via activeRowContexts."})
        recs.append({"operation": "read grid rows (live DB state)",
                     "plane": "modern", "tool": "ui_read_grid",
                     "why": "reflects the live grid the write tools see; clearSession forces a DB reload."})
        recs.append({"operation": "edit an existing grid row in place",
                     "plane": "modern", "tool": "ui_update_grid_row",
                     "why": "row addressed by key; SOAP RowNumber does not move the cursor."})
        recs.append({"operation": "append detail/grid rows in bulk",
                     "plane": "SOAP", "tool": "screen_insert_rows",
                     "why": "one Save commits N new rows; simplest for pure appends."})
    recs.append({"operation": "edit a keyed master record (set fields + Save)",
                 "plane": "SOAP", "tool": "screen_record",
                 "why": "simple, idempotent, frees the API seat, and works during a maintenance lockout."})
    recs.append({"operation": "fire a dialog / process action",
                 "plane": "modern", "tool": "ui_screen_action (or ui_grid_row_action if row-scoped)",
                 "why": "the classic SOAP handler for many dialog actions is a silent no-op; "
                        "the real implementation is the modern JSON protocol."})
    if selectors:
        recs.append({"operation": f"set a selector/lookup field ({', '.join(selectors)})",
                     "plane": "modern", "tool": "ui_resolve_selector (then pass its value)",
                     "why": "setting a selector field's plain text does not bind."})
    return {"screen_id": screen_id.upper(), "primary_dac": struct.get("primary_dac"),
            "grids": {g: gd.get("key_fields") for g, gd in grids.items()},
            "actions": actions, "selector_fields": selectors, "recommendations": recs}


@mcp.tool()
async def ui_resolve_selector(
    screen_id: str,
    view: str,
    field: str,
    search: str,
    pick: dict | None = None,
    instance: str | None = None,
) -> Any:
    """Resolve a lookup/selector FORM field to its `{id, text}` value.

    The modern-plane equivalent of clicking a field's magnifier, typing a search,
    and picking a row — needed before ui_screen_action can set a SELECTOR field
    (per ui_get_structure's `selector` marker; e.g. SM207060 CreateEntityView's
    ScreenID). Setting a selector field's plain text directly does not work.
    No browser capture needed per field — a selector's own /structure metadata
    carries everything needed to query it, so this works on ANY selector field
    on ANY screen (reverse-engineered + proven live, 2026-07-02).

    search: free-text match against the field's own search column (its display
        text, e.g. a screen's Title).
    pick:   optional {column: value} to disambiguate when `search` alone matches
        multiple rows — Acumatica routinely has duplicate titles across modules
        (e.g. "Companies" matches both a Generic Inquiry, CS1015PL — NOT usable as
        an entity source — and the real maintenance screen, CS101500). ALWAYS
        check `rows` before trusting `value` when more than one row comes back;
        picking the wrong one fails a downstream entity-add silently.

    Returns {view, field, search, row_count, rows, value?}. `value` (ready to pass
    straight into ui_screen_action's set_fields) is present only when exactly one
    row matches. Read-only (no gate) — this only queries, never sets anything.

    Example — resolve then set (two calls, same screen; see ui_screen_action for
    why selection state needs tree_select on the SAME call as the set/action):
        r = ui_resolve_selector("SM207060", "CreateEntityView", "ScreenID",
                                 search="Companies", pick={"screenID": "CS101500"})
        ui_screen_action("SM207060", action="InsertNew",
            tree_select={"view": "EntityTree", "key": {"Key": "ROOT#GRPMCP"}},
            set_fields=[{"view": "CreateEntityView", "field": "ObjectName", "value": "Companies"},
                        {"view": "CreateEntityView", "field": "ScreenID", "value": r["value"]}])
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.ui_resolve_selector(view, field, search, pick)


@mcp.tool()
async def ui_screen_action(
    screen_id: str,
    action: str,
    set_fields: list[dict] | None = None,
    tree_select: dict | None = None,
    record_key: dict | None = None,
    skip_validation: bool = False,
    verify: bool = False,
    instance: str | None = None,
) -> Any:
    """Drive a screen via the MODERN UI-screen API — set fields, then fire an action.

    The general driver for the modern plane. Use it for screens/actions the classic
    screen SOAP engine can't reach: notably dialog-driven actions whose classic tag
    is a silent no-op (e.g. GL201000 "Generate Calendar"), and plain record edits
    (set fields + action="Save"). Reuses the same login session as the rest of the
    engine — no browser, no separate auth.

    WRITE SAFETY (parity with screen_submit): before firing, each set_field is
    checked against the screen's /structure metadata — a read-only field or an
    invalid enum value is REFUSED up front (returns ok:false + validation_errors)
    instead of being accepted with a clean 200 and silently dropped. An enum's
    DISPLAY TEXT is auto-coerced to its option value (pass "Reversed" OR "R"). The
    `view` may be omitted when the field name is unique across the screen's views
    (it's resolved for you). skip_validation=true bypasses; verify=true re-reads the
    screen after the action and reports whether the graph still shows unsaved changes.

    set_fields:  optional list of {"view": <ViewName>, "field": <FieldName>,
        "value": <value>} — from ui_get_structure. `view` optional if the field name
        is unambiguous. For enum fields pass the option value OR its display text
        (auto-coerced); booleans are "true"/"false".
    tree_select: optional {"view": <TreeView>, "key": {keyField: value},
        "parent_key": {keyField: value} (omit for a root-level node)} — selects a
        node in a TREE control (e.g. SM207060's EntityTree) before set_fields/action
        run, the modern-plane equivalent of clicking it. Trees aren't normal data
        grids (ui_insert_grid_row etc. throw a null-reference against one); an
        action like "InsertNew" that depends on a selected tree node silently
        no-ops without this. `key`/`parent_key` come from ui_read_grid(tree_view)
        rows. Selection stays active for set_fields + action in THIS call only
        (each ui_screen_action call is its own fresh session).
    record_key:  optional {"view": <ViewName>, "key": {keyField: value}} — selects
        a SPECIFIC EXISTING record before tree_select/set_fields/action run. Needed
        whenever the screen's PRIMARY view is itself keyed to one record instead of
        being a single always-current one (e.g. SM207060's Endpoint header —
        InterfaceName + GateVersion identify WHICH endpoint you're editing).
        Omitting this on such a screen doesn't error — the dialog can still open —
        but committing later fails opaquely ("The Insert button is disabled", proven
        live) because the graph never actually loaded a valid record. Most
        Preferences/Setup screens don't need this (nothing to select).
    action:      the internal command to fire after setting (from ui_get_structure
        `actions`), e.g. "Save" to commit a record edit, or a screen action like
        "generateYears". If the action opens a confirmation dialog it's
        auto-answered OK.

    Business/validation errors surface as clear messages (the screen's own
    `messages[]`), not opaque HTTP codes. PRECONDITION (KB-first policy): consult
    kb-mcp for the screen's prerequisites first — an unconfigured module returns
    "PREREQUISITE NOT MET". Requires allow_write; a DESTRUCTIVE action (Delete,
    ...) additionally requires allow_delete. Only FORM-view fields are supported;
    grid-cell edits aren't yet (no per-row addressing). Verify the write with
    ui_get_structure, screen_get, or run_dac_odata.

    Example — generate financial periods (what generate_master_calendar does):
        ui_screen_action("GL201000", action="generateYears",
            set_fields=[{"view":"GenerateParams","field":"FromYear","value":"2026"},
                        {"view":"GenerateParams","field":"ToYear","value":"2026"}])
    Example — edit a record: set fields, then Save:
        ui_screen_action("GL102000", action="Save",
            set_fields=[{"view":"GLSetupRecord","field":"ConsolidatedPosting","value":"true"}])
    Example — insert a node under a selected TREE row (SM207060 Endpoint Structure —
    record_key selects WHICH endpoint; tree_select then selects its root node):
        ui_screen_action("SM207060", action="InsertNew",
            record_key={"view":"Endpoint","key":{"InterfaceName":"GRPMCP","GateVersion":"25.200.001"}},
            tree_select={"view":"EntityTree","key":{"Key":"ROOT#GRPMCP"}})
    """
    _require_write(instance)
    # A destructive action deletes data — hold it to the stricter allow_delete gate,
    # so the modern plane can't sidestep it (parity with delete_entity).
    if action in _DESTRUCTIVE_ACTIONS:
        _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        # Preflight against /structure: unknown actions AND unknown fields both
        # silently no-op in this protocol (the server ignores them and returns 200),
        # which would look like a bogus success. Validate up front and fail loudly.
        struct = await s.get_ui_structure()
        valid_actions = {a["name"] for a in struct["actions"]}
        if action not in valid_actions:
            raise ScreenError(
                f"ui_screen_action: unknown action {action!r} on {screen_id.upper()}. "
                f"Available: {sorted(valid_actions)}"
            )
        # Write safety (modern-plane parity with screen_submit): resolve friendly
        # single-name fields, coerce enum labels -> values, and REFUSE a read-only /
        # invalid-enum set that the plane would silently drop. Uses the /structure
        # already fetched — zero extra calls.
        coercions: list[str] = []
        if set_fields and not skip_validation:
            set_fields, issues, coercions = await s.ui_coerce_validate(set_fields)
            if issues:
                return {"screen_id": screen_id.upper(), "action": action, "ok": False,
                        "validation_errors": issues,
                        "messages": [f"{i['field']}: {i['problem']}" for i in issues],
                        "note": "Refused — these set_fields would be silently ignored by the "
                                "modern plane (read-only or invalid enum). Fix the value(s), "
                                "or pass skip_validation=true to override."}
        valid_fields = {(v, f["field"]) for v, fs in struct["views"].items() for f in fs}
        grid_cols = {(g, c) for g, gd in struct["grids"].items() for c in (gd.get("columns") or [])}
        if record_key and record_key["view"] not in struct["views"]:
            raise ScreenError(
                f"ui_screen_action: unknown view {record_key['view']!r} on "
                f"{screen_id.upper()} (record_key). Views: {sorted(struct['views'])}"
            )
        for f in (set_fields or []):
            if (f["view"], f["field"]) in valid_fields:
                continue
            if (f["view"], f["field"]) in grid_cols:
                raise ScreenError(
                    f"ui_screen_action: {f['view']}.{f['field']} is a GRID column; "
                    f"this tool sets FORM-view fields only. To EDIT an existing grid "
                    f"row use ui_update_grid_row (per-row addressing by key); to APPEND "
                    f"a row use screen_submit new_row."
                )
            avail = sorted(x["field"] for x in struct["views"].get(f["view"], []))
            raise ScreenError(
                f"ui_screen_action: unknown field {f['view']}.{f['field']} on "
                f"{screen_id.upper()}. Fields in view {f['view']!r}: {avail or '(view not found)'}"
            )
        # Load the views we'll edit (so a Save validates a full record) PLUS the
        # primary view (first in /structure) — it carries the record/company
        # context an action needs (e.g. GL201000 generateYears faults "Select a
        # company" if FiscalYear, the primary view, isn't loaded).
        primary = next(iter(struct["views"]), None)
        load = {f["view"] for f in (set_fields or [])} | ({primary} if primary else set())
        if record_key:
            load.add(record_key["view"])
        await s.ui_bootstrap(sorted(load))
        if record_key:
            await s.ui_navigate_record(record_key["view"], record_key["key"])
        if tree_select:
            await s.ui_select_tree_node(tree_select["view"], tree_select["key"],
                                         tree_select.get("parent_key"))
        for f in (set_fields or []):
            await s.ui_set_field(f["view"], f["field"], f["value"])
        result = await s.ui_command(action)
        # Honest persistence signal: the plane echoes graphIsDirty. After a Save it
        # should be False; still-True means the commit didn't take (a silent no-op the
        # HTTP 200 hides). Best-effort verify=true re-reads /structure to confirm the
        # graph settled. (ui_command already raised on any explicit error message.)
        dirty = result.get("graphIsDirty") if isinstance(result, dict) else None
        verified = None
        if verify:
            try:
                after = await s.get_ui_structure()
                verified = {"reread_ok": True, "actions": len(after.get("actions", []))}
            except Exception as e:  # noqa: BLE001
                verified = {"reread_ok": False, "error": str(e)[:200]}
    ok = not (action == "Save" and dirty is True)
    out = {"screen_id": screen_id.upper(), "action": action, "set_fields": set_fields or [],
           "record_key": record_key, "tree_select": tree_select, "ok": ok, "raw": result}
    if coercions:
        out["coercions"] = coercions
    if dirty is not None:
        out["graph_is_dirty"] = dirty
    if not ok:
        out["warning"] = ("Action 'Save' returned graphIsDirty=true — the change may NOT "
                          "have persisted (silent no-op). Read the record back to confirm.")
    if verified is not None:
        out["verified"] = verified
    return out


@mcp.tool()
async def ui_lookup(
    screen_id: str,
    view: str,
    field: str,
    search: str = "",
    pick: dict | None = None,
    instance: str | None = None,
) -> Any:
    """Search a reference table behind a SELECTOR/lookup field — the modern plane's
    magnifier as a general-purpose lookup.

    Where ui_resolve_selector resolves ONE field to a single {id,text} for a write,
    this returns ALL matching rows (with every lookup column) so you can BROWSE/SEARCH
    reference data through any screen's selector — customers, vendors, accounts,
    screens, terms, tax IDs, whatever a given selector points at — without needing a
    DAC/GI route for it.

    screen_id/view/field: a selector field (from ui_get_structure, where the field's
        `selector` marker is non-null). e.g. GL202500 grid selectors, or any form's
        lookup.
    search: free-text filter against the lookup's search column (blank = first page).
    pick:   optional {column: value} to further filter the returned rows.
    Returns {view, field, search, row_count, rows}. Read-only (no gate, no seat held).
    To then SET the value on a write, use ui_resolve_selector (single) + ui_screen_action.
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        res = await s.ui_resolve_selector(view, field, search, pick)
    return {"screen_id": screen_id.upper(), "view": view, "field": field,
            "search": search, "row_count": res.get("row_count"), "rows": res.get("rows")}


@mcp.tool()
async def ui_run_process(
    screen_id: str,
    action: str,
    set_fields: list[dict] | None = None,
    poll_seconds: float = 45.0,
    instance: str | None = None,
) -> Any:
    """Run a PROCESS screen action (Process / ProcessAll / a mass-action) to completion.

    The modern-plane driver for processing screens — period Open/Close (GL503000),
    posting, allocations run, revaluation, any "Process"/"Process All". It sets the
    process FILTER, fires the action, auto-answers a confirmation dialog, and — for a
    genuinely long-running batch — polls the processing dialog to completion.

    Most batches on a normal-sized dataset finish SYNCHRONOUSLY in one call (verified
    live: GL503000 ProcessAll). A large batch opens a progress dialog that this polls
    up to `poll_seconds` (kept under the MCP request limit; if it returns
    still_processing=true the process is still running server-side — re-call or check
    the downstream effect). PRECONDITION (KB-first): consult kb-mcp for the screen's
    prerequisites. Requires allow_write; a destructive process action additionally
    requires allow_delete.

    action:     the process command from ui_get_structure `actions` (e.g. "ProcessAll",
        "Process"). set_fields: [{"view","field","value"}] for the filter (e.g.
        GL503000 {"view":"Filter","field":"Action","value":"Open"} + FromYear/ToYear).
    Returns {ok, action, still_processing, result:{processing_result, messages}}. Verify
    the effect with run_dac_odata / screen_get.

    Example — open financial periods (what manage_financial_periods does):
        ui_run_process("GL503000", "ProcessAll",
            set_fields=[{"view":"Filter","field":"Action","value":"Open"},
                        {"view":"Filter","field":"FromYear","value":"2026"},
                        {"view":"Filter","field":"ToYear","value":"2026"}])
    """
    _require_write(instance)
    if action in _DESTRUCTIVE_ACTIONS:
        _require_delete(instance)
    _require_range("poll_seconds", poll_seconds, 0, 120)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.ui_run_process(action, set_fields=set_fields, timeout=float(poll_seconds))


@mcp.tool()
async def ui_grid_row_action(
    screen_id: str,
    grid_view: str,
    row_key: dict,
    action: str,
    parent: dict | None = None,
    confirm: bool = True,
    instance: str | None = None,
) -> Any:
    """Select an EXISTING grid row by key, then fire a screen-level ACTION on it —
    the "click a row in the grid, then hit a toolbar button" flow.

    Closes the one thing the classic screen-SOAP plane structurally CANNOT do: it
    navigates to a keyed MASTER record fine, but cannot select an arbitrary
    existing GRID row by key, so a process-the-selected-row action is impossible
    there (proven live 2026-07-02: SM203520 Restore Snapshot faulted "A snapshot is
    not selected" via SOAP because the Snapshots row could not be made active). The
    modern plane addresses the row via activeRowContexts, which this drives.

    grid_view: the grid container/view (from ui_get_structure `grids`, e.g.
        "Snapshots" on SM203520).
    row_key:   {keyField: value} identifying the row (keys from ui_get_structure
        grids[grid_view].key_fields, e.g. {"SnapshotID": "459edf6a-..."}).
    action:    the internal command to fire with that row active (from
        ui_get_structure `actions`, e.g. "importSnapshotCommand").
    parent:    tenant-scoped / master-detail screens — {"view", "key"} to load the
        header first (e.g. SM203520 {"view":"Companies","key":{"CompanyID":3}} to
        target the SalesDemo tenant). Omit for a top-level grid.
    confirm:   auto-answer a confirmation dialog with OK (default True). False =
        "arm without firing": the action opens its dialog but is NOT committed
        (status "dialog_open") — a safe dry-run for a destructive action.

    Returns {ok, status, ...}. status is "committed" (ran / dialog answered),
    "dialog_open" (confirm=False), or "redirected" (server answered with a goTo —
    e.g. Restore hands off to SM203510 to run/monitor; that is NOT a synchronous
    completion, so verify the downstream effect yourself). Validates grid_view +
    action against /structure first (both silently no-op if wrong on this
    protocol). Requires allow_write for a committing action.

    PRECONDITION (KB-first policy): consult kb-mcp for the screen first.

    Example — restore a snapshot into the SalesDemo tenant on SM203520:
        ui_grid_row_action("SM203520", grid_view="Snapshots",
            row_key={"SnapshotID": "459edf6a-70e3-4d88-ae5d-235b761e34c9"},
            action="importSnapshotCommand",
            parent={"view": "Companies", "key": {"CompanyID": 3}})
    """
    if confirm:
        _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        struct = await s.get_ui_structure()
        valid_actions = {a["name"] for a in struct["actions"]}
        if action not in valid_actions:
            raise ScreenError(
                f"ui_grid_row_action: unknown action {action!r} on {screen_id.upper()}. "
                f"Available: {sorted(valid_actions)}"
            )
        if grid_view not in struct["grids"]:
            raise ScreenError(
                f"ui_grid_row_action: unknown grid {grid_view!r} on {screen_id.upper()}. "
                f"Grids: {sorted(struct['grids'])}"
            )
        if parent and parent.get("view") not in struct["views"]:
            raise ScreenError(
                f"ui_grid_row_action: unknown parent view {parent.get('view')!r} on "
                f"{screen_id.upper()}. Views: {sorted(struct['views'])}"
            )
        result = await s.ui_grid_row_action(grid_view, row_key, action,
                                            parent=parent, confirm=confirm)
    return {"screen_id": screen_id.upper(), **result}


@mcp.tool()
async def ui_tree_dialog_insert(
    screen_id: str,
    tree_view: str,
    node_key: dict,
    open_action: str,
    dialog_view: str,
    fields: list[dict],
    parent_key: dict | None = None,
    record_key: dict | None = None,
    save: bool = True,
    instance: str | None = None,
) -> Any:
    """Add a child under a TREE node via its INSERT DIALOG — the full "click a tree
    node → Insert → fill a popup → OK → Save" flow that no single ui_screen_action
    call reproduces. The end-to-end capability behind adding an entity to a
    web-service endpoint (SM207060); generalizes to any tree+insert-dialog screen.

    Reverse-engineered + proven live from a full browser capture (2026-07-02): the
    UI performs a 5-phase sequence — select node, OPEN the dialog, Repaint to load
    its fields, FILL them, then COMMIT the dialog (which only STAGES the node) plus
    a SEPARATE Save to PERSIST. This tool runs all of it in one session.

    tree_view/node_key/parent_key: identify the tree + node to insert under (from
        ui_read_grid; e.g. "EntityTree", {"Key": "ROOT#GRPMCP"}). parent_key omitted
        for a root-level node.
    open_action: the tree's insert command (from ui_get_structure `actions`; e.g.
        "InsertNew" on SM207060).
    dialog_view: the popup view name (e.g. "CreateEntityView" on SM207060).
    fields:      [{"field": <name>, "value": <value>}] to fill the dialog. For a
        SELECTOR field (per ui_get_structure's `selector` marker; e.g. ScreenID)
        resolve it FIRST with ui_resolve_selector and pass its `value` ({id,text})
        here unchanged. A required-looking field the server fills itself at commit
        (e.g. SM207060's EntityType, resolved from ScreenID) can be omitted.
    record_key:  {"view": <ViewName>, "key": {...}} if the screen's primary view is
        keyed to a specific record (SM207060's Endpoint: InterfaceName+GateVersion)
        — REQUIRED there, else the commit fails "Insert button is disabled".
    save:        persist to the DB (default True).

    Requires allow_write. Verify the result with get_entity_schema/list_entities
    (contract) — not just the tool's own response.

    PRECONDITION (KB-first policy): consult kb-mcp for the screen first.

    Example — add the Companies entity to endpoint GRPMCP on SM207060:
        r = ui_resolve_selector("SM207060", "CreateEntityView", "ScreenID",
                                 search="Companies", pick={"screenID": "CS101500"})
        ui_tree_dialog_insert("SM207060", tree_view="EntityTree",
            node_key={"Key": "ROOT#GRPMCP"}, open_action="InsertNew",
            dialog_view="CreateEntityView",
            record_key={"view": "Endpoint",
                        "key": {"InterfaceName": "GRPMCP", "GateVersion": "25.200.001"}},
            fields=[{"field": "ObjectName", "value": "Companies"},
                    {"field": "ScreenID", "value": r["value"]}])
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        struct = await s.get_ui_structure()
        if dialog_view not in struct["views"]:
            raise ScreenError(
                f"ui_tree_dialog_insert: unknown dialog_view {dialog_view!r} on "
                f"{screen_id.upper()}. Views: {sorted(struct['views'])}"
            )
        if open_action not in {a["name"] for a in struct["actions"]}:
            raise ScreenError(
                f"ui_tree_dialog_insert: unknown open_action {open_action!r} on "
                f"{screen_id.upper()}. Actions: {sorted(a['name'] for a in struct['actions'])}"
            )
        load = {dialog_view} | ({record_key["view"]} if record_key else set())
        primary = next(iter(struct["views"]), None)
        if primary:
            load.add(primary)
        await s.ui_bootstrap(sorted(load))
        if record_key:
            await s.ui_navigate_record(record_key["view"], record_key["key"])
        result = await s.ui_tree_dialog_insert(
            tree_view, node_key, open_action, dialog_view, fields,
            parent_key=parent_key, save=save)
    return {"screen_id": screen_id.upper(), "tree_view": tree_view, "node_key": node_key,
            "dialog_view": dialog_view, "fields": fields, "saved": save, "ok": True,
            "graph_is_dirty": result.get("graphIsDirty")}


@mcp.tool()
async def ui_populate_endpoint_entity_fields(
    endpoint_name: str,
    endpoint_version: str,
    entity_object_name: str,
    data_view: str,
    data_view_pick: dict | None = None,
    detail_title: str | None = None,
    save: bool = True,
    instance: str | None = None,
) -> Any:
    """Populate a web-service-endpoint entity's FIELDS from one of its screen data
    views (SM207060's "select entity → Populate → pick Object → Select All → OK →
    Save"). `ui_tree_dialog_insert` adds an entity SHELL; this fills in the scalar
    fields of the picked view so they show on the contract.

    Proven live (2026-07-02): ImportScenarios ← "Scenario Summary" took field_count
    1 → 20 (Name, Provider, SyncType, …); GenInquiry ← "Data Sources" 1 → 7.

    endpoint_name/endpoint_version: the endpoint the entity lives on (e.g. "GRPMCP",
        "25.200.001") — its header record is navigated first (required, else the
        Populate context is wrong).
    entity_object_name: the entity's ObjectName as shown in the tree (e.g.
        "DataProvider") — used to locate its node under the root.
    data_view:      the data view to pull fields from, matched by display name (e.g.
        "Provider Summary"). The lookup is scoped to the SELECTED node's views,
        resolved in-session automatically.
    data_view_pick: optional {column: value} to disambiguate if >1 view matches
        (data-view display names can repeat, e.g. two "Details").
    detail_title:   to populate a nested DETAIL COLLECTION instead of the top-level
        entity, its collection name (e.g. "CompaniesDetails" for the
        "CompaniesDetails: CompaniesDetail[]" node). The detail node is selected with
        its full ancestor path (root→entity→detail) — a depth-2 node the plain tree
        selector previously couldn't reach (fixed 2026-07-02). Omit for the entity.
    save:           persist (default True).

    Requires allow_write. Verify with get_entity_schema (field_count jumps).
    Adds fields from ONE view; call again per view for a multi-view entity/detail.

    Example:
        ui_populate_endpoint_entity_fields("GRPMCP", "25.200.001",
            entity_object_name="DataProvider", data_view="Provider Summary")
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, "SM207060") as s:
        struct = await s.get_ui_structure()
        primary = next(iter(struct["views"]), None)
        load = {"PopulateFilterView", "Endpoint"} | ({primary} if primary else set())
        await s.ui_bootstrap(sorted(load))
        await s.ui_navigate_record("Endpoint", {"InterfaceName": endpoint_name,
                                                 "GateVersion": endpoint_version})
        result = await s.ui_populate_entity_fields(
            root_node_key={"Key": f"ROOT#{endpoint_name}"},
            entity_object_name=entity_object_name,
            data_view=data_view, data_view_pick=data_view_pick,
            detail_title=detail_title, save=save)
    return {"endpoint": f"{endpoint_name}/{endpoint_version}", "entity": entity_object_name,
            "detail_title": detail_title, "data_view": data_view, "saved": save, "ok": True,
            "graph_is_dirty": result.get("graphIsDirty")}


@mcp.tool()
async def ui_read_grid(
    screen_id: str,
    grid_view: str,
    fields: list[str] | None = None,
    top: int | None = None,
    parent: dict | None = None,
    fallback_dac: str | None = None,
    instance: str | None = None,
) -> Any:
    """Read GRID rows via the MODERN UI-screen plane (the read peer of the grid CRUD).

    Reads fresh from the DB (clearSession) and returns each row flattened to
    {field: value} plus its `_rowId`. Unlike screen_get (classic Export) /
    run_dac_odata (raw DB), this reflects the LIVE grid — the same rows/cells the
    modern write tools see.

    grid_view: the grid container/view (from ui_get_structure `grids`, e.g.
        "AccountRecords" on GL202500).
    fields:    optional list of columns to return (default: all bound cells).
    top:       optional row cap.
    parent:    MASTER-DETAIL — {"view": <primaryView>, "key": {keyField: value}} to
        read a CHILD grid under a header record (e.g. CA202000 entry-type details of
        one cash account: parent={"view":"CashAccount","key":{"CashAccountCD":"10200"}},
        grid_view="ETDetails"). Omit for a top-level grid.
    fallback_dac: TREE grids (e.g. EP204061 "Folders", the Company Tree) load only the
        visible/root level over the modern plane, so this returns just the root. Pass
        the backing DAC name (e.g. "EPCompanyTree") and, when the grid yields <=1 row,
        the FULL backing table is read via DAC OData and returned under `dac_fallback`
        (with the parent-link + sort columns the flat grid read omits).

    Returns {grid_view, key_names, columns, row_count, rows:[{...cells, _rowId}]}.
    Read-only (no gate).
    """
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        g = await s.ui_grid_read(grid_view, parent)
    col_fields = [c.get("field") for c in (g["columns"] or []) if isinstance(c, dict) and c.get("field")]
    out = []
    for r in g["rows"]:
        cells = r.get("cells") or {}
        if fields:
            row = {f: cells.get(f, {}).get("value") for f in fields}
        else:
            row = {f: c.get("value") for f, c in cells.items()
                   if isinstance(c, dict) and "value" in c}
        row["_rowId"] = r.get("id")
        out.append(row)
        if top and len(out) >= top:
            break
    result = {"screen_id": screen_id.upper(), "grid_view": grid_view,
              "key_names": g["key_names"], "columns": col_fields,
              "row_count": len(out), "rows": out}
    # A tree grid only returns its visible/root level over the modern plane — when the
    # caller names the backing DAC and we got <=1 row, read the full table instead.
    if fallback_dac and len(out) <= 1:
        try:
            dac = await run_dac_odata(fallback_dac, instance=instance)
            result["dac_fallback"] = {
                "dac": fallback_dac,
                "row_count": len(dac.get("value", [])),
                "rows": dac.get("value", []),
                "note": "Grid returned <=1 row (a collapsed tree only loads the root over "
                        "the modern plane). These are the full backing-table rows via DAC "
                        "OData, including parent-link/sort columns the grid read omits.",
            }
        except Exception as e:  # noqa: BLE001 — fallback is best-effort
            result["dac_fallback"] = {"dac": fallback_dac, "error": str(e)[:200]}
    return result


@mcp.tool()
async def ui_update_grid_row(
    screen_id: str,
    grid_view: str,
    key: dict,
    values: dict,
    parent: dict | None = None,
    skip_validation: bool = False,
    instance: str | None = None,
) -> Any:
    """Edit ONE existing GRID row in place, on the MODERN UI-screen plane.

    The capability the classic screen SOAP engine lacks: change a cell of an
    EXISTING detail/grid row. (Classic positional selection is inert — a {"row":N}
    there silently hits row 1, so it now hard-errors.) This drives the modern
    plane's `controlsParams.<grid>.changes.modified` channel, reverse-engineered
    from a live browser capture (GL202500, 2026-07-01). No browser, same session.

    grid_view: the grid container/view (from ui_get_structure `grids`, e.g.
        "AccountRecords" on GL202500 Chart of Accounts).
    key:    {keyField: value} identifying the row — MUST be the grid's live key
        (ui_get_structure grids[...].key_fields), e.g. {"AccountCD": "40000"}. The
        server matches the existing row by this key (that's why it updates instead
        of inserting a blank row).
    values: {field: newValue} cells to change; booleans as true/false.
    parent: MASTER-DETAIL — {"view", "key"} to target a CHILD grid under a header
        record (see ui_read_grid). For a detail grid pass only the child key; the
        parent-linkage id is resolved automatically. Omit for a top-level grid.

    Reads the grid fresh (clearSession → live DB) to resolve the row's id+index,
    then Saves. Idempotent. Requires allow_write. Verify with run_dac_odata /
    screen_get. To APPEND a new row use ui_insert_grid_row; for FORM-view (non-
    grid) field edits use ui_screen_action.

    CELL VALIDATION: each cell is checked against the grid's live column metadata —
    a read-only cell (allowUpdate=false) or an invalid enum is REFUSED (ok:false +
    validation_errors) rather than silently dropped, and an enum's display label is
    coerced to its stored value. Best-effort (skipped only when the grid exposes no
    column shape); skip_validation=true bypasses.

    Example — rename a GL account's description:
        ui_update_grid_row("GL202500", "AccountRecords",
            key={"AccountCD": "40000"}, values={"Description": "Sales Revenue"})
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        res = await s.ui_update_grid_row(grid_view, key, values, parent, skip_validation)
    if isinstance(res, dict) and res.get("ok") is False:
        return res  # validation refusal — surface it instead of a bogus success
    return {"screen_id": screen_id.upper(), "grid_view": grid_view,
            "key": key, "values": values, "parent": parent, "ok": True}


@mcp.tool()
async def ui_insert_grid_row(
    screen_id: str,
    grid_view: str,
    values: dict,
    parent: dict | None = None,
    skip_validation: bool = False,
    instance: str | None = None,
) -> Any:
    """Append a NEW row to a GRID on the MODERN UI-screen plane.

    Drives the modern plane's `controlsParams.<grid>.changes.inserted` channel
    (reverse-engineered live on GL202500). A client rowId is generated for you.

    grid_view: the grid container/view (from ui_get_structure `grids`).
    values:    {field: value} for the new row — MUST include the grid's KEY
        field(s) plus any other REQUIRED columns, or the Save fails validation
        (e.g. GL202500 needs AccountCD + Type + Description). Enum fields take the
        stored code (Type "E"=Expense); booleans as true/false.
    parent:    MASTER-DETAIL — {"view", "key"} to append into a CHILD grid under a
        header (see ui_read_grid). The parent-linkage id is auto-filled server-side,
        so `values` needs only the child fields. Omit for a top-level grid.

    Requires allow_write. Verify with run_dac_odata / screen_get.

    CELL VALIDATION: cells are checked against the grid's live column metadata — a
    read-only cell or invalid enum is REFUSED (ok:false + validation_errors) instead
    of silently dropped, and an enum's display label is coerced to its stored value
    (so Type "Expense" and "E" both work). skip_validation=true bypasses.

    Examples:
        ui_insert_grid_row("GL202500", "AccountRecords",
            values={"AccountCD": "40100", "Type": "I", "Description": "Service Revenue"})
        ui_insert_grid_row("CA202000", "ETDetails", values={"EntryTypeID": "BANKCHG"},
            parent={"view": "CashAccount", "key": {"CashAccountCD": "10200"}})
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        res = await s.ui_insert_grid_row(grid_view, values, parent, skip_validation)
    if isinstance(res, dict) and res.get("ok") is False:
        return res  # validation refusal — surface it instead of a bogus success
    return {"screen_id": screen_id.upper(), "grid_view": grid_view,
            "values": values, "parent": parent, "ok": True}


@mcp.tool()
async def ui_delete_grid_row(
    screen_id: str,
    grid_view: str,
    key: dict,
    parent: dict | None = None,
    instance: str | None = None,
) -> Any:
    """Delete an existing GRID row (matched by key) on the MODERN UI-screen plane.

    Drives `controlsParams.<grid>.changes.deleted` (the full key is sent inside the
    row's values — required, else the delete silently no-ops). Reads the grid
    fresh to resolve the row, then Saves.

    grid_view: the grid container/view (from ui_get_structure `grids`).
    key:       {keyField: value} identifying the row, e.g. {"AccountCD": "40100"}
        (for a detail grid pass only the child key; the parent id is resolved).
    parent:    MASTER-DETAIL — {"view", "key"} to delete from a CHILD grid under a
        header (see ui_read_grid). Omit for a top-level grid.

    DESTRUCTIVE — requires allow_delete (stricter than allow_write). Some rows
    can't be deleted once referenced (e.g. a posted GL account) — the screen's own
    validation surfaces as a clear error. Verify with run_dac_odata.
    """
    _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        await s.ui_delete_grid_row(grid_view, key, parent)
    return {"screen_id": screen_id.upper(), "grid_view": grid_view,
            "key": key, "parent": parent, "ok": True}


@mcp.tool()
async def screen_submit(
    screen_id: str,
    commands: list[dict],
    dry_run: bool = False,
    auto_answer: str | None = None,
    skip_validation: bool = False,
    instance: str | None = None,
) -> Any:
    """Drive a screen via the screen-based SOAP API — writes screens REST can't.

    PRECONDITION (KB-first policy): before this write, consult kb-mcp (search_kb /
    read_kb_file) for this screen's prerequisites, dependent screens, and validation
    rules, and verify each prerequisite exists — Acumatica screens have hard
    dependencies they won't surface until a write fails. See the server instructions.

    dry_run=True previews: it drops the committing commands (button actions like
    Save + row deletes) so the field SETs run but nothing persists, and still
    returns any field-level errors. Use it to validate a sequence before writing.

    auto_answer (e.g. "Yes"): if the Submit faults, retry once with a confirmation
    dialog answered — clears "Are you sure?" pop-ups that block Save/Release on
    some screens. Only applied to containers that actually expose a dialog.

    Replays a UI command sequence *as a user*, so it works on context screens
    the contract REST API refuses (insert enabled only with a parent loaded).
    Commands reference the schema's FRIENDLY field/action names (from
    screen_get_schema) — the client clones the matching descriptor, which
    carries the LinkedCommand navigation chain that actually loads/edits the
    record (bare field-name commands silently no-op). Spec shapes:
        {"set": "<FriendlyName>", "to": <value>}   set a field (navigates if key)
        {"action": "<FriendlyName>"}               click a button (e.g. "Save")
        {"new_row": "<Container>"}                 add a detail row
        {"delete_row": "<Container>"}              delete the current detail row
        {"answer": "<Container>", "to": "Yes"}     answer a pop-up dialog
    Use "Container.Field" for `set` when a friendly name repeats across
    containers. Friendly names + containers come from screen_get_schema.

    Recipe — update a record: set the key field, set other fields, Save:
        [{"set":"CustomerID","to":"ABARTENDE"},
         {"set":"AccountName","to":"New Name"},
         {"action":"Save"}]
    Add a detail row (master-detail/context screen): set the parent key(s),
    new_row the detail container, set the row's fields, Save.

    Field-level errors are returned in `messages` (the API reports them inside a
    200, not as a fault). Requires "allow_write": true; a sequence containing a
    `delete_row` (unless dry_run) additionally requires "allow_delete": true, so
    the screen plane can't sidestep the delete gate. Opens/closes its own SOAP
    session so it never holds an API seat at idle (trial = 2 seats — always frees).

    PRE-WRITE VALIDATION: before submitting, each `set` is checked against the
    screen's modern-plane metadata — a read-only field or an invalid enum value is
    rejected up front (returns ok:false + `validation_errors`) rather than being
    accepted by SOAP with ok:true and silently dropped. Best-effort (only fires when
    the field is identified); pass skip_validation=true to bypass.
    """
    _require_write(instance)
    # A delete_row OR a record-level Delete action destroys data — hold it to the
    # stricter allow_delete gate (dry_run drops committing commands, never deletes).
    if not dry_run and any(
        "delete_row" in c or c.get("action") in _DESTRUCTIVE_ACTIONS
        for c in commands if isinstance(c, dict)
    ):
        _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.submit(commands, dry_run=dry_run, auto_answer=auto_answer,
                              skip_validation=skip_validation)


@mcp.tool()
async def screen_insert_rows(
    screen_id: str,
    container: str,
    rows: list[dict],
    header: dict | None = None,
    save: bool = True,
    auto_answer: str | None = None,
    dry_run: bool = False,
    instance: str | None = None,
) -> Any:
    """Insert many grid/detail rows into one container in a single transaction.

    The master-detail / bulk-grid writer on top of the screen-based SOAP engine —
    use it for Chart of Accounts rows, subaccount segments, GL batch lines, any
    screen where one Save commits N rows.

    container: the grid container friendly name (from screen_get_schema), e.g.
               "AccountRecords" on GL202500.
    rows:      list of {field: value}; each row becomes NewRow + the field SETs.
               Field names are friendly (qualify "Container.Field" if a name
               repeats across containers).
    header:    optional field sets applied once before the rows (a parent key /
               document context).
    save:      add a final Save (set False to chain more work first).
    auto_answer: answer a confirmation dialog raised by Save (e.g. "Yes").
    dry_run:   preview — runs the SETs, drops Save, surfaces field errors.

    Example — add two GL accounts (GL202500):
        screen_insert_rows("GL202500", "AccountRecords", [
          {"Account":"10100","Type":"Asset","AccountClass":"CASH","Description":"Cash"},
          {"Account":"40100","Type":"Income","Description":"Sales"}])
    Requires allow_write. Opens/closes its own SOAP session (frees the API seat).
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.insert_rows(
            container, rows, header=header, save=save,
            auto_answer=auto_answer, dry_run=dry_run,
        )


@mcp.tool()
async def screen_record(
    screen_id: str,
    key_field: str,
    key_value: str,
    fields: dict,
    insert: bool = False,
    save: bool = True,
    auto_answer: str | None = None,
    dry_run: bool = False,
    instance: str | None = None,
) -> Any:
    """Create or edit ONE record on a master-style screen (idempotent setup helper).

    insert=False (default): set the key field, which NAVIGATES to the existing
        record, then apply `fields` and Save — an in-place edit. Re-runnable.
    insert=True: click Insert to start a fresh record, then set the key + `fields`
        and Save — a create.

    key_field/fields use friendly schema names (from screen_get_schema; qualify
    "Container.Field" if a name repeats). For grids with many rows per Save use
    screen_insert_rows instead.

    Example — set a GL ledger's description (edit existing):
        screen_record("GL201500","LedgerID","ACTUAL",{"Description":"Actual Ledger"})
    Requires allow_write. Opens/closes its own SOAP session (frees the API seat).
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.set_record(
            key_field, key_value, fields, insert=insert, save=save,
            auto_answer=auto_answer, dry_run=dry_run,
        )


@mcp.tool()
async def screen_get(
    screen_id: str,
    fields: list[str],
    top: int = 10,
    filters: list[dict] | None = None,
    instance: str | None = None,
) -> Any:
    """Read current values from a screen via the screen-based SOAP Export op.

    The read counterpart to screen_submit — returns the live record/grid data
    that Submit alone doesn't echo. Useful to confirm a write, or to read screens
    the contract REST/DAC routes can't (config singletons, context grids).

    fields: schema friendly field names = the columns to return (qualify
            "Container.Field" if a name repeats; see screen_get_schema). top: max
            rows. Returns {headers, rows:[{header: value}, ...]}.

    filters: [{"field": "<Friendly>", "value": ..., "condition"/"op": ...}]. The
            condition is an Acumatica name (Equals, NotEqual, Greater, GreaterOrEqual,
            Less, LessOrEqual, Contains, StartsWith, EndsWith, Between, IsNull,
            IsNotNull) OR an operator alias via `op` (=, !=, >, >=, <, <=, contains,
            startswith, ...). Default = Equals. An unrecognized condition or unknown
            key is REJECTED with an error (it used to silently fall back to Equals and
            return the wrong rows). Example: [{"field":"AccountRecords.Account",
            "op":">=","value":"300000"}].

    Example — read the financial calendar periods (GL101000):
        screen_get("GL101000", ["Periods.PeriodNbr","Periods.StartDate","Periods.Description"])
    Read-only; opens/closes its own SOAP session (no API seat held at idle).
    """
    _require_range("top", top, 1, 5000)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, screen_id) as s:
        return await s.export(fields, top=int(top), filters=filters)


@mcp.tool()
async def release_sessions(instance: str | None = None) -> Any:
    """Log out cached API sessions to free Web Service API license seats.

    Each instance's contract-REST client keeps a logged-in session (one of the
    instance's "Max Web Services API Users" seats — a trial allows only 2). This
    logs out and drops the cached client(s) so the seat is freed immediately
    rather than at idle timeout; the next tool call transparently re-logs in.

    instance: release just that profile; omit to release ALL cached sessions.
    Use it when you hit "API Login Limit", or after a batch of work.
    """
    names = [instance] if instance else list(_clients.keys())
    released = []
    for name in names:
        client = _clients.pop(name, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
            released.append(name)
    return {"released": released, "remaining_cached": list(_clients.keys())}


@mcp.tool()
async def list_screens(query: str, top: int = 50, instance: str | None = None) -> Any:
    """Find a screen's ID by title — search the site map (for screen_get/submit).

    query: case-insensitive substring of the screen Title (e.g. "segment values",
    "financial year", "ledger"). Returns [{ScreenID, Title}] so you can feed the
    ScreenID to screen_get_schema / screen_get / screen_submit. Read-only.
    """
    _require_range("top", top, 1, 1000)
    client = _client(instance)
    res = await client.run_dac("SiteMap", {"$select": "ScreenID,Title", "$top": 5000})
    rows = res.get("value", []) if isinstance(res, dict) else []
    q = query.lower()
    hits = [
        {"ScreenID": r.get("ScreenID"), "Title": r.get("Title")}
        for r in rows
        if q in (r.get("Title") or "").lower()
    ]
    hits.sort(key=lambda h: (len(h["Title"] or ""), h["Title"] or ""))
    return {"query": query, "count": len(hits), "screens": hits[: int(top)]}


@mcp.tool()
async def whoami(instance: str | None = None) -> Any:
    """Report the active connection identity + reachability (and seat guidance).

    Returns the configured username/tenant/endpoint, whether the token + contract
    read succeed, and the count of cached sessions holding API seats. Acumatica
    exposes no clean per-seat usage over REST, so to free seats use
    release_sessions (trial = 2 seats). Read-only.
    """
    cfg = _cfg()
    name = instance or cfg.default
    inst = cfg.get(name)
    ok, detail, entity_count = True, None, None
    try:
        client = _client(instance)
        rec = await client.get_swagger()
        entity_count = len((rec.get("paths") or {})) if isinstance(rec, dict) else None
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)[:200]
    return {
        "instance": name,
        "username": inst.username,
        "login_name_screen_api": f"{inst.username}@{inst.tenant}" if inst.tenant else inst.username,
        "tenant": inst.tenant,
        "base_url": inst.base_url,
        "endpoint": f"{inst.endpoint_name}/{inst.endpoint_version}",
        "reachable": ok,
        "error": detail,
        "cached_sessions_holding_seats": list(_clients.keys()),
        "note": "Free seats with release_sessions (trial = 2 Web Services API Users).",
    }


@mcp.tool()
async def enable_features(
    features: list[str], activate: bool = False, instance: str | None = None
) -> Any:
    """Set feature flags on the Enable/Disable Features screen (CS100000).

    features: schema friendly field names from screen_get_schema('CS100000')
    (e.g. "Subaccounts", "Inventory", "InventorySubitems"). Sets each ON and
    Saves. Returns the screen_submit result.

    ROLLUP FEATURES: some feature checkboxes are read-only PARENT/rollup toggles
    (e.g. "StandardFinancials") that the platform turns on automatically once one of
    their children is enabled — you can't set them directly (SOAP refuses / no-ops).
    This detects those in the requested list, DROPS them from the SET commands (so the
    batch isn't refused), and reports them under `rollup_skipped`; the writable
    features are set + Saved as normal. If EVERY requested feature is a rollup, nothing
    is set (enable a child instead).

    Save STAGES the change (FeaturesSet gets a working row; ActivationStatus =
    "Pending Activation"). To ACTIVATE/INSTALL the staged set, call
    activate_features (the "Enable" button = the RequestValidation action), which
    recompiles the site. Set activate=True here to do both in one call.

    Read current states with run_dac_odata('FeaturesSet'). Requires allow_write.
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, "CS100000") as s:
        writable, rollup = await s.classify_writable(features)
        if not writable:
            return {
                "screen_id": "CS100000", "ok": False, "staged": None,
                "rollup_skipped": rollup,
                "note": "Every requested feature is a read-only rollup/parent toggle — "
                        "nothing to set. Enable one of their CHILD features instead; the "
                        "parent turns on automatically.",
            }
        cmds = [{"set": f, "to": "True"} for f in writable] + [{"action": "Save"}]
        staged = await s.submit(cmds)
    if rollup and isinstance(staged, dict):
        staged = {**staged, "rollup_skipped": rollup,
                  "rollup_note": f"read-only rollup features not set directly (auto-enable "
                                 f"via children): {rollup}"}
    if not activate:
        return staged
    activated = await activate_features(instance=instance)
    return {"staged": staged, "activated": activated}


@mcp.tool()
async def _activation_status(inst, poll_interval: float, budget: float) -> str | None:
    """Poll CS100000 ActivationStatus for up to `budget` seconds, tolerating the
    transient errors the site restart throws; return the last status seen (or
    "Validated" as soon as observed)."""
    elapsed, status = 0.0, None
    while True:
        try:
            async with ScreenClient(inst, "CS100000", timeout=poll_interval) as s:
                rows = (await s.export(["GeneralSettings.ActivationStatus"], top=1)).get("rows")
            status = rows[0].get("Status") if rows else None
            if status == "Validated":
                return status
        except Exception:  # noqa: BLE001 — site still recompiling; keep polling
            status = "unknown (site recompiling)"
        if elapsed >= budget:
            return status
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval


async def activate_features(wait_seconds: float = 40.0, instance: str | None = None) -> Any:
    """Activate/install the staged feature set on CS100000 (the "Enable" button) —
    NON-BLOCKING (won't hang on the recompile).

    The apply step enable_features (Save) does NOT do: it fires the "Enable" button
    (via the MODERN UI plane's `requestValidation` command — the classic SOAP
    RequestValidation action NREs on a large feature set), which validates the
    license, activates the staged features, and RECOMPILES the site (~1-3 min). Unlike a customization publish, the
    activation runs to completion SERVER-SIDE on its own — polling ActivationStatus
    only OBSERVES it. So this fires RequestValidation, watches ActivationStatus for up
    to `wait_seconds`, then returns:
      • status "completed"  — ActivationStatus reached "Validated" in time;
      • status "in_progress" — still recompiling; it WILL finish on its own. Poll
        `activate_features_status()` until activated=true (do NOT re-fire this — a
        second RequestValidation restarts the recompile).

    Activates whatever is currently staged, so set the flags (enable_features) first.
    wait_seconds clamped to [5, 120]. Requires allow_write.
    """
    _require_write(instance)
    _require_range("wait_seconds", wait_seconds, 5, 120)
    inst = _cfg().get(instance or _cfg().default)
    poll_interval = 8.0
    # Fire the Enable button via the MODERN UI-screen plane (command "requestValidation"),
    # NOT the classic SOAP RequestValidation action: on a large feature set the SOAP
    # submit replays every feature field and NREs ("Object reference not set ...
    # ProjectAccounting", proven live 2026-07-02), leaving ActivationStatus stuck at
    # "Pending Activation". The modern plane fires it as a plain command (no field
    # replay) and returns a reloadPage redirect as the recompile starts. The recompile
    # may drop this in-flight request — fine, activation proceeds server-side and we
    # observe it via ActivationStatus.
    fire: dict = {"ok": None}
    try:
        async with ScreenClient(inst, "CS100000", timeout=poll_interval + 5) as s:
            await s.ui_bootstrap()
            fire = await s.ui_command("requestValidation")
    except Exception as e:  # noqa: BLE001 — recompile commonly drops the connection
        fire = {"ok": None, "transport": str(e)[:160]}
    status = await _activation_status(inst, poll_interval, wait_seconds)
    activated = status == "Validated"
    return {
        "activated": activated,
        "activation_status": status,
        "status": "completed" if activated else "in_progress",
        "fire_result": fire,
        "note": None if activated else (
            "Feature activation/recompile is still running server-side — it finishes "
            "on its own (usually 1-3 min). Poll activate_features_status() until "
            "activated=true; do NOT re-run activate_features (that restarts the recompile)."
        ),
    }


@mcp.tool()
async def activate_features_status(instance: str | None = None) -> Any:
    """Check CS100000 feature-activation status — a single quick read (no recompile,
    no polling loop). After activate_features returns status "in_progress", poll this
    until activated=true.

    Returns {activated, activation_status}. activated is True only when
    ActivationStatus == "Validated". During the recompile the read can transiently
    fail — reported as activation_status "unknown (site recompiling)", just poll again.
    """
    inst = _cfg().get(instance or _cfg().default)
    try:
        async with ScreenClient(inst, "CS100000", timeout=15) as s:
            rows = (await s.export(["GeneralSettings.ActivationStatus"], top=1)).get("rows")
        status = rows[0].get("Status") if rows else None
    except Exception as e:  # noqa: BLE001 — site still recompiling
        return {"activated": False, "activation_status": "unknown (site recompiling)",
                "error": str(e)[:160]}
    return {"activated": status == "Validated", "activation_status": status}


@mcp.tool()
async def create_financial_calendar(
    first_year: str,
    starts_on: str | None = None,
    has_adjustment_period: bool = False,
    number_of_periods: int | None = None,
    period_type: str | None = None,
    skip_validation: bool = False,
    instance: str | None = None,
) -> Any:
    """Create the financial calendar (GL101000): set the year-start date, AutoFill, Save.

    first_year: e.g. "2026" — the fiscal year to establish.
    starts_on:  year-start date, M/D/YYYY (e.g. "1/1/2026"). Omit to default to
                Jan 1 of first_year.
    has_adjustment_period: add a year-end adjustment period (Period 13) to the pattern.
    number_of_periods: override the period count (e.g. 12 monthly). Omit for default.
    period_type: override the period type (e.g. "Month"). Omit for default.

    WHY THE START DATE, NOT THE YEAR FIELD: on 2024R2+/26.100 the "First Financial
    Year" field (FiscalYearSetup.FirstFinYear) is READ-ONLY once a calendar pattern
    exists — the year is DERIVED from the year-start date (BegFinYear). Setting
    FirstFinancialYear directly is refused (the read-only write guard rejects it, and
    SOAP would silently drop it anyway). So this drives the year off
    FinancialYearStartsOn (= BegFinYear) instead, which establishes the year on every
    build. AutoFill then generates the period rows from that date; Save commits and a
    confirmation dialog is auto-answered.

    skip_validation bypasses the pre-write read-only/enum guard (use only if a valid
    SET is being wrongly rejected). Requires allow_write. Verify with
    screen_get('GL101000', ['Periods.PeriodNbr','Periods.StartDate']).
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    start = str(starts_on) if starts_on else f"1/1/{str(first_year).strip()}"
    # Set the start date FIRST (it derives the year), then let AutoFill regenerate the
    # period rows from it, then Save. FirstFinancialYear is intentionally NOT set.
    cmds: list[dict] = [{"set": "FinancialYearStartsOn", "to": start}]
    if period_type:
        cmds.append({"set": "PeriodType", "to": str(period_type)})
    if number_of_periods is not None:
        cmds.append({"set": "NumberOfFinancialPeriods", "to": str(number_of_periods)})
    if has_adjustment_period:
        cmds.append({"set": "HasAdjustmentPeriod", "to": "True"})
    cmds.append({"action": "AutoFill"})
    cmds.append({"action": "Save"})
    async with ScreenClient(inst, "GL101000") as s:
        return await s.submit(cmds, auto_answer="Yes", skip_validation=skip_validation)


@mcp.tool()
async def delete_financial_year(year: str, instance: str | None = None) -> Any:
    """Delete ONE financial year (and its periods) from the Master Calendar (GL201000).

    Calendar teardown — the inverse of create_financial_calendar/generate_master_calendar.
    Drives the modern plane: navigate the FiscalYear record to `year`, fire the screen's
    Delete action (which COMMITS immediately — there is no separate Save; a Save after
    the delete errors "At least one period should be defined" because the year is already
    gone, proven live 2026R1).

    year: the financial year to remove, e.g. "2027".

    CAVEATS: delete LATER years before earlier ones (a year is generated from the prior
    one) — see reset_calendar for a range. A year with posted GL activity, or the only
    remaining year, may refuse to delete. To drop just a year-end ADJUSTMENT period
    (Period 13) without deleting the year, re-run create_financial_calendar with
    has_adjustment_period=false instead. Held to the allow_delete gate (destructive).
    Verify with run_dac_odata('FinPeriod', filter="FinYear eq '<year>'") — expect empty.
    """
    _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    async with ScreenClient(inst, "GL201000") as s:
        await s.ui_bootstrap(["FiscalYear"])
        await s.ui_navigate_record("FiscalYear", {"Year": str(year)})
        res = await s.ui_command("Delete")
    return {"screen_id": "GL201000", "year": str(year), "deleted": True,
            "graph_is_dirty": res.get("graphIsDirty"),
            "note": "Delete committed immediately (no Save). Verify with "
                    f"run_dac_odata('FinPeriod', filter=\"FinYear eq '{year}'\") — expect empty."}


@mcp.tool()
async def reset_calendar(
    from_year: str, to_year: str | None = None, instance: str | None = None
) -> Any:
    """Delete a RANGE of financial years from the Master Calendar (GL201000) — teardown.

    Deletes each year in [from_year, to_year] HIGHEST-FIRST (a later year is generated
    from the earlier one, so it must go first). Per-year result is reported; a year that
    refuses (posted activity, or the last remaining year) is recorded and the rest
    continue. Each delete commits immediately (see delete_financial_year).

    from_year/to_year: inclusive year range (omit to_year for a single year). To also
    reset the year PATTERN (period count / adjustment period), re-run
    create_financial_calendar afterwards. Held to the allow_delete gate (destructive).
    """
    _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    y0, y1 = int(from_year), int(to_year or from_year)
    if y1 < y0:
        raise ValueError(f"to_year ({y1}) must be >= from_year ({y0})")
    results: list[dict] = []
    async with ScreenClient(inst, "GL201000") as s:
        for y in range(y1, y0 - 1, -1):  # highest year first
            await s.ui_bootstrap(["FiscalYear"])
            try:
                await s.ui_navigate_record("FiscalYear", {"Year": str(y)})
                res = await s.ui_command("Delete")
                results.append({"year": str(y), "deleted": True,
                                "graph_is_dirty": res.get("graphIsDirty")})
            except Exception as e:  # noqa: BLE001 — record per-year, keep going
                results.append({"year": str(y), "deleted": False, "error": str(e)[:200]})
    deleted = [r["year"] for r in results if r.get("deleted")]
    return {"screen_id": "GL201000", "from_year": str(y0), "to_year": str(y1),
            "deleted_years": deleted, "results": results,
            "note": "Verify with run_dac_odata('FinPeriod'). Re-run "
                    "create_financial_calendar to rebuild the year pattern if needed."}


@mcp.tool()
async def create_ledger(
    ledger_id: str,
    description: str,
    ledger_type: str = "Actual",
    currency: str = "USD",
    instance: str | None = None,
) -> Any:
    """Create a GL ledger (GL201500): LedgerID, Description, Type, Currency, Save.

    ledger_type: "Actual" | "Reporting" | "Statistical" | "Budget". Requires a
    financial calendar to exist first (create_financial_calendar). Requires
    allow_write. Verify with screen_get('GL201500', ['LedgerRecords.LedgerID']).
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    cmds = [
        {"set": "LedgerRecords.LedgerID", "to": ledger_id},
        {"set": "Description", "to": description},
        {"set": "Type", "to": ledger_type},
        {"set": "Currency", "to": currency},
        {"action": "Save"},
    ]
    async with ScreenClient(inst, "GL201500") as s:
        return await s.submit(cmds, auto_answer="Yes")


@mcp.tool()
async def set_gl_preferences(
    retained_earnings: str,
    ytd_net_income: str,
    auto_post_on_release: bool | None = None,
    hold_batches_on_entry: bool | None = None,
    instance: str | None = None,
) -> Any:
    """Set General Ledger Preferences (GL102000) — the system accounts + posting flags.

    The GL-phase keystone: sets the two REQUIRED Chart-of-Accounts settings that
    enable posting, then optional posting/data-entry flags, then Save.

    retained_earnings: the Retained Earnings account code. MUST be an account of
        type LIABILITY (Acumatica errors otherwise — there is no Equity type).
    ytd_net_income:    the YTD Net Income account code (also Liability by convention).
    auto_post_on_release: optional — set the "Automatically Post on Release" flag
        (True simplifies batch processing: no Unposted batches).
    hold_batches_on_entry: optional — set "Hold Batches on Entry" (False = new
        batches are Balanced immediately).

    PREREQUISITE: both accounts must already exist in the Chart of Accounts
    (chart_of_accounts) with type Liability. Verify after with
    screen_get('GL102000', ['GLSetupRecord.RetainedEarningsAccount',
    'GLSetupRecord.YTDNetIncomeAccount']) or setup_readiness (gl_preferences).
    Requires allow_write. (KB: To Specify General Ledger Preferences.)
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    G = "GLSetupRecord"
    cmds: list[dict] = [
        {"set": f"{G}.YTDNetIncomeAccount", "to": ytd_net_income},
        {"set": f"{G}.RetainedEarningsAccount", "to": retained_earnings},
    ]
    if auto_post_on_release is not None:
        cmds.append({"set": f"{G}.AutomaticallyPostOnRelease",
                     "to": "True" if auto_post_on_release else "False"})
    if hold_batches_on_entry is not None:
        cmds.append({"set": f"{G}.HoldBatchesOnEntry",
                     "to": "True" if hold_batches_on_entry else "False"})
    cmds.append({"action": "Save"})
    async with ScreenClient(inst, "GL102000") as s:
        return await s.submit(cmds, auto_answer="Yes")


# Acumatica GL has exactly four account types (NO Equity — equity accounts are typed
# Liability, hence Retained Earnings/YTD-Net-Income must be Liability; see
# set_gl_preferences). Source systems often code the type as a single letter; this maps
# the common scheme. E (Equity) -> Liability by that rule. B and H are less standard —
# override per-load via chart_of_accounts(type_map=...) if your source differs.
_ACU_ACCOUNT_TYPES = {"Asset", "Liability", "Income", "Expense"}
_COA_TYPE_MAP = {
    "A": "Asset",
    "L": "Liability",
    "B": "Expense",
    "H": "Income",
    "E": "Liability",  # Equity -> Liability (no Equity type in Acumatica GL)
}


def _normalize_coa_type(value: Any, type_map: dict | None = None) -> str:
    """Resolve a source account-type code/name to an Acumatica GL type.

    Accepts an already-valid type (Asset/Liability/Income/Expense, case-insensitive)
    verbatim, or a single-letter source code mapped via type_map (defaults to
    _COA_TYPE_MAP). Raises ValueError on anything unmapped rather than silently
    passing a bad type to the screen (which would fault or mis-post).
    """
    raw = str(value).strip()
    # already a full Acumatica type? (case-insensitive) -> canonical casing
    for t in _ACU_ACCOUNT_TYPES:
        if raw.lower() == t.lower():
            return t
    m = {**_COA_TYPE_MAP, **{k.upper(): v for k, v in (type_map or {}).items()}}
    hit = m.get(raw.upper())
    if hit is None:
        raise ValueError(
            f"account type {value!r} is not a valid Acumatica type "
            f"({'/'.join(sorted(_ACU_ACCOUNT_TYPES))}) nor a known source code "
            f"({'/'.join(sorted(m))}). Pass type_map to add a mapping.")
    return hit


@mcp.tool()
async def chart_of_accounts(
    accounts: list[dict],
    save: bool = True,
    dry_run: bool = False,
    auto_answer: str | None = "Yes",
    type_map: dict | None = None,
    instance: str | None = None,
) -> Any:
    """Create Chart of Accounts rows (GL202500) in one transaction.

    accounts: list of dicts. Per account:
        account      (required) the account number/CD, e.g. "10100"
        type         (required) an Acumatica type "Asset"|"Liability"|"Income"|"Expense"
                     OR a single-letter source code auto-mapped: A->Asset, L->Liability,
                     E->Liability (Equity; Acumatica has no Equity type), B->Expense,
                     H->Income. Override/extend via the type_map arg.
        description  (required) free text
        account_class            optional account class ID (must already exist)
        post_option              optional, e.g. "Detail" | "Summary"
        active                   optional bool, defaults True
    Each becomes a NewRow + field SETs on the AccountRecords grid; one Save
    commits them all. A confirmation dialog (if any) is auto-answered "Yes".

    type_map: optional {code: AcumaticaType} to override the built-in letter map
        (e.g. {"B": "Asset"} if your source codes B as a Bank/Asset account).

    Prerequisites: the GL module enabled and a posting ledger to exist. Verify
    after with screen_get('GL202500', ['AccountRecords.Account',
    'AccountRecords.Description']). dry_run previews without saving. Requires
    allow_write.
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    rows: list[dict] = []
    for a in accounts:
        row = {
            "Account": str(a["account"]),
            "Type": _normalize_coa_type(a["type"], type_map),
            "Description": a["description"],
        }
        if a.get("account_class"):
            row["AccountClass"] = a["account_class"]
        if a.get("post_option"):
            row["PostOption"] = a["post_option"]
        row["Active"] = "True" if a.get("active", True) else "False"
        rows.append(row)
    async with ScreenClient(inst, "GL202500") as s:
        return await s.insert_rows(
            "AccountRecords", rows, save=save,
            auto_answer=auto_answer, dry_run=dry_run,
        )


@mcp.tool()
async def generate_master_calendar(
    from_year: str, to_year: str | None = None, instance: str | None = None
) -> Any:
    """Generate financial periods on the Master Financial Calendar (GL201000).

    The GL101000 financial-year pattern only defines period LENGTHS — no actual
    Period records exist until this runs. Creates them with status Inactive for
    the given year range, the prerequisite for manage_financial_periods.

    from_year: first financial year to generate periods for, e.g. "2026". Must
        already exist as a financial-year pattern (create_financial_calendar).
    to_year:   last year in the range; omit to generate just from_year.

    NOTE ON MECHANISM: the classic typed screen SOAP API exposes this action's
    tag (GenerateYears) but its handler isn't wired up there — it returns a
    clean success with zero effect (confirmed via live testing). The real
    implementation lives behind the modern UI's own JSON protocol
    (/ui/screen/GL201000), which this tool drives directly — reusing the SAME
    login session as the rest of this engine (same cookie, no separate auth).
    See ScreenClient.ui_command/ui_set_field for the reverse-engineered
    protocol details.

    Only applies when *Centralized Period Management* is the active mode (one
    calendar org-wide, the common case). Verify after with screen_get(
    'GL201000', ['Periods.FinancialPeriodID','Periods.Status']) or
    run_dac_odata('FinPeriod', filter="FinYear eq '<year>'"). Requires
    allow_write. (KB: To Generate Financial Periods for the Master Calendar.)

    RANGE: to_year > from_year generates EVERY year in the inclusive range. The
    screen's generateYears command only ever materializes a SINGLE year per fire
    (setting ToYear on the params is silently ignored on this build), so this
    loops year-by-year internally — one generateYears per year, reusing the same
    login session — and reports per-year results.
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    y0, y1 = int(from_year), int(to_year or from_year)
    if y1 < y0:
        raise ValueError(f"to_year ({y1}) must be >= from_year ({y0})")
    per_year: list[dict] = []
    async with ScreenClient(inst, "GL201000") as s:
        # Load FiscalYear so the company/calendar context is present — without it
        # generateYears faults "Select a company and create its first calendar year."
        await s.ui_bootstrap(["FiscalYear"])
        for y in range(y0, y1 + 1):
            # Set BOTH ends to the single year: ToYear is ignored on this build, so
            # a multi-year span would only ever create from_year — drive one at a time.
            await s.ui_set_field("GenerateParams", "FromYear", str(y))
            await s.ui_set_field("GenerateParams", "ToYear", str(y))
            raw = await s.ui_command("generateYears")
            per_year.append({"year": str(y), "raw": raw})
    return {
        "generated": True,
        "from_year": str(y0),
        "to_year": str(y1),
        "years": [p["year"] for p in per_year],
        "per_year": per_year,
    }


@mcp.tool()
async def manage_financial_periods(
    from_year: str,
    to_year: str | None = None,
    action: str = "Open",
    company: str | None = None,
    reopen_in_subledgers: bool | None = None,
    instance: str | None = None,
) -> Any:
    """Bulk period action on Manage Financial Periods (GL503000) — Open by default.

    Drives the screen's "Process All" flow (no per-period checkbox selection
    needed): set the filter, then process every matching period in one shot.

    from_year/to_year: financial year range to act on (required — this can
        touch every period in range, so the scope is explicit rather than
        defaulting to "every period that ever existed"). Omit to_year to
        target just from_year.
    action:   "Open" | "Close" | "Lock" | "Unlock" | "Reopen" | "Deactivate".
    company:  optional — restrict to one company/branch; omit for the
        logged-in tenant's default.
    reopen_in_subledgers: optional bool, meaningful only for action="Reopen"
        (maps to ReopenFinancialPeriodsInAllModules on the filter).

    PREREQUISITE: periods must already exist for the range with a status this
    action can act on (Open needs Inactive, Close needs Open, etc.) —
    generate_master_calendar creates them Inactive. Verify after with
    screen_get('GL503000', ['FinPeriods.FinancialPeriodID','FinPeriods.Status'])
    or setup_readiness (open_periods). Requires allow_write. (KB: Opening
    Financial Periods — Process Activity.)
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    to_year = str(to_year or from_year)
    cmds: list[dict] = [
        {"set": "Filter_.Action", "to": action},
        {"set": "Filter_.FromYear", "to": str(from_year)},
        {"set": "Filter_.ToYear", "to": to_year},
    ]
    if company:
        cmds.append({"set": "Filter_.Company", "to": company})
    if reopen_in_subledgers is not None:
        cmds.append({"set": "Filter_.ReopenFinancialPeriodsInAllModules",
                     "to": "True" if reopen_in_subledgers else "False"})
    cmds.append({"action": "ProcessAll"})
    async with ScreenClient(inst, "GL503000") as s:
        return await s.submit(cmds, auto_answer="Yes")


@mcp.tool()
async def create_numbering_sequence(
    numbering_id: str,
    description: str,
    start_number: str = "000001",
    end_number: str = "999999",
    numbering_step: int = 1,
    warning_number: str | None = None,
    start_date: str | None = None,
    new_number_symbol: str | None = None,
    manual_numbering: bool = False,
    instance: str | None = None,
) -> Any:
    """Create a numbering sequence (CS201010) — header + one subsequence.

    Numbering sequences auto-generate IDs for documents (GL batches, invoices,
    bills, payments, transfers, allocations, schedules, …) and for auto-numbered
    segmented-key segments. A Common-Settings foundation screen — no module
    prerequisite; typically set up before the modules that reference it.

    numbering_id: unique ID, <=10 alphanumeric chars.
    description:  <=30 chars.
    start_number / end_number / warning_number: alphanumeric strings, processed
        as strings with the SAME length (<=15) and same prefix — keep leading
        zeros (e.g. "000001"). end >= start; warning (if given) >= start.
    numbering_step: increment added to the rightmost numeric portion (default 1).
    start_date:   M/D/YYYY the subsequence takes effect (optional).
    new_number_symbol: placeholder shown until a number is assigned (e.g. "<NEW>").
    manual_numbering: True = users type document numbers themselves (no auto-gen);
        the subsequence range is then irrelevant.

    Creates the header + one non-branch subsequence in a single Save. For
    branch-split subsequences (different prefix/range per branch), use screen_submit
    with extra NumberingSequenceDetails rows. Requires allow_write. Verify with
    run_dac_odata('Numbering', filter="NumberingID eq '<id>'"). (KB: Numbering
    Sequences / Use of Numbering Sequences.)
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    cmds: list[dict] = [
        {"set": "NumberingID", "to": numbering_id},
        {"set": "Description", "to": description},
    ]
    if manual_numbering:
        cmds.append({"set": "ManualNumbering", "to": "True"})
    if new_number_symbol:
        cmds.append({"set": "NewNumberSymbol", "to": new_number_symbol})
    D = "NumberingSequenceDetails"
    cmds.append({"new_row": D})
    cmds += [
        {"set": f"{D}.StartNumber", "to": str(start_number)},
        {"set": f"{D}.EndNumber", "to": str(end_number)},
        {"set": f"{D}.NumberingStep", "to": str(numbering_step)},
    ]
    if warning_number:
        cmds.append({"set": f"{D}.WarningNumber", "to": str(warning_number)})
    if start_date:
        cmds.append({"set": f"{D}.StartDate", "to": str(start_date)})
    cmds.append({"action": "Save"})
    async with ScreenClient(inst, "CS201010") as s:
        return await s.submit(cmds, auto_answer="Yes")


@mcp.tool()
async def create_segmented_key(
    key_id: str,
    description: str,
    segments: list[dict],
    lookup_mode: str | None = None,
    specific_module: str | None = None,
    numbering_id: str | None = None,
    instance: str | None = None,
) -> Any:
    """Create a segmented key on Segment Keys (CS202000) — key header + segment rows.

    This is the PREREQUISITE step for set_segment_value: a key (and its segments)
    must exist here before CS203000 can hold values. Per the KB chain: CS202000
    (this tool) -> CS203000 (set_segment_value) -> GL203000 etc.

    key_id:      the segmented key identifier (e.g. "SUBACCOUNT", "ZZFUND").
    description: key description.
    segments:    list of dicts, one per segment (at least one required). Per segment:
        length      (required) int, segment length in characters
        description optional segment label
        validate    optional bool — ON = segment holds a validated value list you
                    then populate with set_segment_value (a 1-segment key is always
                    validated). OFF = only length/mask are checked.
        edit_mask   optional "Alpha" | "Numeric" | "Alphanumeric" | "Unicode"
                    (omit = screen default)
        align       optional "Left" | "Right"
        auto_number optional bool (only ONE segment per key; requires numbering_id)
        separator   optional char shown between segments (default "-")
    lookup_mode:     optional; omit to use the screen default. A validated segment
                     needs a lookup mode that supports validation (see KB
                     'Lookup Modes for Segmented Keys').
    specific_module: optional module to scope the key to.
    numbering_id:    optional numbering sequence ID (required if any auto_number
                     segment; its length must match that segment's length).

    Verify creation against the MASTER table: run_dac_odata('Dimension',
    filter="DimensionID eq '<key_id>'") — the CS202000 picker lists Dimension, not
    Segment. (Segment/SegmentValue are the children.) Always pass >=1 segment; a key
    with none fails "Segmented key must have at least one segment".

    To DELETE a key later, tear down children-first (deleting the master alone
    orphans the children, which then can't be removed via the API): delete the
    segment values on CS203000, then the segments on CS202000 LAST-segment-first,
    then delete_row the master + Save.

    Requires allow_write. Total of all segment lengths must not exceed the key max.
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    K = "SegmentedKeyDefinition"
    S = "SegmentDefinition"
    cmds: list[dict] = [{"set": f"{K}.SegmentedKeyID", "to": key_id}]
    if lookup_mode:
        cmds.append({"set": f"{K}.LookupMode", "to": lookup_mode})
    if specific_module:
        cmds.append({"set": f"{K}.SpecificModule", "to": specific_module})
    if numbering_id:
        cmds.append({"set": f"{K}.NumberingID", "to": numbering_id})
    cmds.append({"set": f"{K}.Description", "to": description})
    for seg in segments:
        cmds.append({"new_row": S})
        if seg.get("description") is not None:
            cmds.append({"set": f"{S}.Description", "to": seg["description"]})
        cmds.append({"set": f"{S}.Length", "to": str(seg["length"])})
        if seg.get("edit_mask"):
            cmds.append({"set": f"{S}.EditMask", "to": seg["edit_mask"]})
        if seg.get("align"):
            cmds.append({"set": f"{S}.Align", "to": seg["align"]})
        if seg.get("separator"):
            cmds.append({"set": f"{S}.Separator", "to": seg["separator"]})
        if seg.get("auto_number"):
            cmds.append({"set": f"{S}.AutoNumber", "to": "True"})
        if seg.get("validate"):
            cmds.append({"set": f"{S}.Validate", "to": "True"})
    cmds.append({"action": "Save"})
    async with ScreenClient(inst, "CS202000") as s:
        return await s.submit(cmds, auto_answer="Yes")


@mcp.tool()
async def set_segment_value(
    segmented_key_id: str,
    value: str,
    description: str | None = None,
    segment_id: str = "1",
    active: bool = True,
    instance: str | None = None,
) -> Any:
    """Add a value to a segment on Segment Values (CS203000) — the one that works.

    PREREQUISITE: the segmented key + its segments must already exist on Segment
    Keys (CS202000) — CS203000 only lists keys defined there, and a segment must
    have Validate=ON to hold a value list. So the chain is CS202000 (create key,
    add segments, set Validate) -> CS203000 (this tool). See the KB
    'Segmented Identifiers' / 'Segment Values (CS203000)' for the full order.

    CS203000 IS writable via the screen-based SOAP engine. The trick is NAVIGATION:
    select the segment with a descriptor `set` on the header key (which replays the
    LinkedCommand chain), NOT a flat key command — a flat key leaves the screen on
    its default segment and the value lands in the WRONG segment. This recipe does
    it correctly: set SegmentedKeyID (+ SegmentID) -> NewRow -> set Value/Description
    /Active -> Save.

    segmented_key_id: the dimension, e.g. "ACCOUNT", "MLISTCD", "SALESPER" (the keys
                      listed on CS203000; find them with run_dac_odata('Segment')).
    value:            the segment value — must fit the segment's defined Length/mask
                      (run_dac_odata('Segment', filter="DimensionID eq '<id>'") shows
                      Length/EditMask). Too long/ill-formatted -> "failed to commit".
    segment_id:       segment number within the key (default "1"; multi-segment keys
                      like a subaccount have 2, 3, …).
    description/active: the row's label and Active flag.

    Verify with run_dac_odata('SegmentValue', filter="DimensionID eq '<id>'"). A
    persisted write returns a small response; a large `raw_len` + `nobind_suspected`
    means it didn't bind (check the value fits the segment). Requires allow_write.
    """
    _require_write(instance)
    inst = _cfg().get(instance or _cfg().default)
    cmds: list[dict] = [
        {"set": "SegmentSummary.SegmentedKeyID", "to": segmented_key_id},
    ]
    if segment_id:
        cmds.append({"set": "SegmentSummary.SegmentID", "to": str(segment_id)})
    cmds.append({"new_row": "PossibleValues"})
    cmds.append({"set": "PossibleValues.Value", "to": value})
    if description is not None:
        cmds.append({"set": "PossibleValues.Description", "to": description})
    cmds.append({"set": "PossibleValues.Active", "to": "True" if active else "False"})
    cmds.append({"action": "Save"})
    async with ScreenClient(inst, "CS203000") as s:
        return await s.submit(cmds)


@mcp.tool()
async def delete_segmented_key(key_id: str, instance: str | None = None) -> Any:
    """Delete a segmented key with the correct children-first teardown.

    Deleting the CS202000 master row alone ORPHANS its Segment/SegmentValue
    children (they then can't be navigated/removed). This does it in the order the
    framework requires:
      1. (orphan recovery) if the key has children but no `Dimension` master,
         recreate the master so the screens can navigate it again;
      2. delete every segment VALUE on CS203000;
      3. delete the SEGMENTS on CS202000;
      4. delete the master row.

    Verifies via the Dimension/Segment/SegmentValue DACs and returns the final
    state. This DELETES records, so it requires the stricter allow_delete (not
    just allow_write) — parity with delete_entity.

    LIMIT: the typed screen API can't reliably select a non-first segment row, so a
    key with MULTIPLE segments can't be fully torn down here (the last-segment-first
    rule can't be satisfied) — single-segment keys and orphan cleanup work; multi-
    segment keys are reported as partially handled (delete the extra segments in the
    CS202000 UI).
    """
    _require_delete(instance)
    inst = _cfg().get(instance or _cfg().default)
    client = _client(instance)

    async def _rows(dac: str) -> list[dict]:
        r = await client.run_dac(dac, {"$filter": f"DimensionID eq '{key_id}'"})
        return r.get("value", []) if isinstance(r, dict) else []

    steps: list[str] = []
    dims = await _rows("Dimension")
    segs = await _rows("Segment")
    vals = await _rows("SegmentValue")
    if not dims and not segs and not vals:
        return {"key_id": key_id, "ok": True, "note": "nothing to delete (key absent)"}

    # 1. orphan recovery — recreate the master so CS203000/CS202000 can navigate it
    if not dims and (segs or vals):
        async with ScreenClient(inst, "CS202000") as s:
            await s.submit([{"set": "SegmentedKeyDefinition.SegmentedKeyID", "to": key_id},
                            {"set": "SegmentedKeyDefinition.Description", "to": "(cleanup)"},
                            {"action": "Save"}], auto_answer="Yes")
        steps.append("recreated orphan master")

    # 2. delete all segment values (CS203000)
    if vals:
        async with ScreenClient(inst, "CS203000") as s:
            for v in vals:
                await s.submit([{"set": "SegmentSummary.SegmentedKeyID", "to": key_id},
                                {"key": "PossibleValues.Value", "to": v.get("Value")},
                                {"delete_row": "PossibleValues"}, {"action": "Save"}],
                               auto_answer="Yes")
        steps.append(f"deleted {len(vals)} value(s)")

    # 3 + 4. delete the segment + master (CS202000) — ONLY for single-segment keys.
    # A multi-segment key can't have its non-first segments removed via SOAP, and the
    # last-segment-first rule blocks deleting the master too; deleting the master
    # alone would re-orphan the children, so for multi-segment we STOP here (leaving
    # the key intact + navigable) and report it for UI teardown.
    res = None
    if len(segs) <= 1:
        async with ScreenClient(inst, "CS202000") as s:
            res = await s.submit(
                [{"set": "SegmentedKeyDefinition.SegmentedKeyID", "to": key_id},
                 {"delete_row": "SegmentDefinition"},
                 {"delete_row": "SegmentedKeyDefinition"}, {"action": "Save"}],
                auto_answer="Yes")
        steps.append("deleted segment + master")
    else:
        steps.append(f"left intact: {len(segs)} segments (multi-segment teardown not "
                     "supported via SOAP)")

    after = {"Dimension": len(await _rows("Dimension")),
             "Segment": len(await _rows("Segment")),
             "SegmentValue": len(await _rows("SegmentValue"))}
    fully = after == {"Dimension": 0, "Segment": 0, "SegmentValue": 0}
    return {
        "key_id": key_id,
        "ok": fully,
        "steps": steps,
        "remaining": after,
        "last_result": res,
        **({"warning": f"multi-segment key ({len(segs)} segments): cannot be deleted "
            "via SOAP (can't select a non-first segment row). Left intact and "
            "navigable — delete it in the CS202000 UI."} if len(segs) > 1 else {}),
    }


@mcp.tool()
async def screen_preflight(
    dac: str,
    provided: list[str],
    instance: str | None = None,
) -> Any:
    """Check supplied fields against a DAC's MANDATORY fields before a write.

    Reads the OData CSDL ($metadata) — the authoritative mandatory-field source
    (Nullable="false" or key = mandatory) — and reports which required fields are
    NOT in `provided`. The screen-based SOAP plane returns no field-state, so this
    is the practical preflight: catch missing required fields up front instead of
    eating a generic "record raised at least one error" fault on Save.

    dac:      the DAC entity-type name (e.g. "Account", "Ledger", "Branch").
              List them with get_dac_metadata(dac=None).
    provided: the field names you intend to set (friendly or DAC names — matched
              case-insensitively against the DAC's mandatory field names).

    Returns {dac, mandatory, provided, missing, ok}. ok=False means `missing` lists
    required fields you haven't set. Read-only. Note: container friendly names may
    differ from DAC field names — treat `missing` as a strong hint, not a hard gate.
    """
    meta = await get_dac_metadata(dac=dac, mandatory_only=True, instance=instance)
    if isinstance(meta, dict) and "error" in meta:
        return meta
    fields = meta.get(dac) or (next(iter(meta.values()), []) if meta else [])
    # Drop framework/system columns the CSDL marks non-nullable but no caller sets.
    _SYS = {"deleteddatabaserecord", "noteid", "tstamp", "createdbyid",
            "createddatetime", "lastmodifiedbyid", "lastmodifieddatetime",
            "createdbyscreenid", "lastmodifiedbyscreenid"}
    mandatory = [f["name"] for f in fields if (f["name"] or "").lower() not in _SYS]
    have = {p.lower() for p in provided}
    missing = [m for m in mandatory if m and m.lower() not in have]
    return {
        "dac": dac,
        "mandatory": mandatory,
        "provided": provided,
        "missing": missing,
        "ok": not missing,
    }


@mcp.tool()
async def load_from_excel(
    entity: str,
    path: str,
    column_map: dict | None = None,
    sheet: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    offset: int = 0,
    stop_on_error: bool = False,
    background: bool | None = None,
    instance: str | None = None,
) -> Any:
    """Bulk create/update records of an entity from an .xlsx/.csv file.

    Each data row -> one upsert (PUT, keyed by the entity's key fields). The first
    row is the header. column_map maps a header to an entity field name; omit it to
    use headers verbatim, or map a header to "" to ignore that column. Only scalar
    fields are supported (no nested detail rows).

    dry_run=True (DEFAULT): parses + maps + validates field names against the
    schema and returns a preview WITHOUT writing anything. Inspect unknown_fields
    and sample, then re-run with dry_run=false to actually load.

    offset skips the first N DATA rows (0-based) — use it to RESUME an interrupted
    load from the next_offset a prior run reported. limit caps rows processed (after
    offset). stop_on_error aborts on the first failed row (next_offset points AT that
    row so a resume retries it).

    background: a large load (sequential PUTs) can outlast the request window. When
    background is True — or None (auto) and > 150 rows remain — the load runs in a
    BACKGROUND task and returns a job id immediately; poll load_status(job) for
    progress and next_offset. Set background=false to force the inline path.
    Tip: run get_entity_schema(entity) first to get exact field names.
    """
    _require_range("limit", limit, 1, 1000000)
    _require_range("offset", offset, 0, 100000000)
    _check_read_path(path, instance)  # sandbox + size cap (read-side guard)
    if not dry_run:
        _require_write(instance)
    headers, rows = read_rows(path, sheet)
    if offset:
        rows = rows[offset:]
    if limit:
        rows = rows[:limit]
    mapped = [m for m in (map_row(r, column_map) for r in rows) if m]

    client = _client(instance)

    if dry_run:
        unknown: list[str] = []
        schema_error: str | None = None
        try:
            sch = await client.get_entity_schema(entity)
            valid = set(sch["scalar_fields"]) | set(sch["detail_fields"])
            used = {k for m in mapped for k in m}
            unknown = sorted(used - valid)
        except Exception as e:
            # fail closed: do NOT report unknown_fields=[] as if validation passed
            schema_error = str(e)[:300]
        return {
            "dry_run": True,
            "entity": entity,
            "file_headers": headers,
            "row_count": len(mapped),
            "validated": schema_error is None,
            "unknown_fields": unknown if schema_error is None else None,
            "schema_error": schema_error,
            "sample": mapped[:5],
            "sandbox": _cfg().get(instance or _cfg().default).fs_sandbox("read"),
            "note": (
                "Schema validation FAILED — could not confirm field names; fix the "
                "error and re-run before loading. No data written."
                if schema_error
                else "No data written. Resolve unknown_fields (fix column_map), "
                "then re-run with dry_run=false."
            ),
        }

    job = f"{entity}@{offset}+{len(mapped)}"
    state: dict[str, Any] = {
        "job": job, "entity": entity, "total": len(mapped),
        "processed": 0, "succeeded": 0, "failed": 0,
        "next_offset": offset, "errors": [], "completed": False, "error": None,
    }
    go_bg = background if background is not None else len(mapped) > 150
    if not go_bg:
        await _drive_load(state, client, entity, mapped, offset, stop_on_error)
        out = _load_job_view(state)
        out["dry_run"] = False
        out["background"] = False
        return out

    _load_jobs[job] = state

    async def _run() -> None:
        try:
            await _drive_load(state, client, entity, mapped, offset, stop_on_error)
        except Exception as e:  # noqa: BLE001 — record, don't crash the loop
            state["error"] = str(e)[:400]
        finally:
            state.pop("_task", None)
    state["_task"] = asyncio.create_task(_run())
    # let a fast load finish inline; otherwise return the job to poll
    waited = 0.0
    while waited < 5.0 and not state["completed"] and state["error"] is None:
        await asyncio.sleep(1.0)
        waited += 1.0
    out = _load_job_view(state)
    out["dry_run"] = False
    out["background"] = True
    return out


@mcp.tool()
async def load_status(job: str | None = None) -> Any:
    """Check a background bulk-load started by load_from_excel (in-memory, instant).

    job: the id load_from_excel returned; omit for the most recent. Returns the same
    shape (status completed | in_progress | error) with processed/succeeded/failed and
    next_offset. If it stopped early (stop_on_error or an interruption), resume with
    load_from_excel(..., offset=next_offset). State is per server session (a restart
    clears it — verify loaded rows via count_entity / get_entity instead).
    """
    if not _load_jobs:
        return {"status": "none",
                "note": "No background load started in this server session."}
    if job is None:
        job = next(reversed(_load_jobs))
    state = _load_jobs.get(job)
    if state is None:
        return {"status": "unknown", "job": job, "known_jobs": list(_load_jobs)}
    return _load_job_view(state)


@mcp.tool()
async def setup_data_provider(
    name: str,
    file_path: str,
    provider_type: str = "PX.DataSync.ExcelSYProvider",
    object_name: str = "Template",
    key_columns: list[str] | None = None,
    upload_file: bool = True,
    sheet: str | None = None,
    instance: str | None = None,
) -> dict:
    """Create AND fully configure a Data Provider (SM206015) from a data file — via API.

    Reads the file's header columns and writes the provider's schema object + field
    rows DIRECTLY, sidestepping the stateful `fillSchemaFields` screen action (which
    can't run over stateless REST because it needs a UI-selected object row). Then,
    by default, uploads the file so an import run can read it.

    name:          provider name (its key).
    file_path:     the .xlsx/.csv source (must be within the instance's read_roots).
    provider_type: the plugin class (default = Excel provider).
    object_name:   schema object/sheet name (default "Template" — the Excel provider's).
    key_columns:   header columns that are keys (default: the first column).
    upload_file:   also attach the file via files:put so prepare/import can read it.
    sheet:         worksheet name for .xlsx (default: first sheet).

    Requires "allow_write": true. Returns the provider id + the columns written.
    """
    import mimetypes

    _require_write(instance)
    p = _check_read_path(file_path, instance)
    headers, _ = read_rows(file_path, sheet)
    headers = [h for h in headers if h and str(h).strip()]
    if not headers:
        raise ValueError(f"no header columns found in {file_path}")
    keys = set(key_columns or [headers[0]])
    unknown_keys = keys - set(headers)
    if unknown_keys:
        raise ValueError(f"key_columns not in the file header: {sorted(unknown_keys)}")

    client = _client(instance)
    # 1) create the provider header
    rec = await client.put_entity(
        "DataProvider",
        _wrap_fields({"Name": name, "ProviderType": provider_type, "Active": True}),
    )
    rid = rec.get("id") if isinstance(rec, dict) else None
    # 2) write the schema object + field rows directly (no stateful action needed)
    field_rows = [
        {"ObjectName": object_name, "Field": h, "DataType": "String",
         "Key": h in keys, "Active": True}
        for h in headers
    ]
    await client.put_entity("DataProvider", _wrap_fields({
        "Name": name,
        "SchemaSourceObjects": [{"Object": object_name, "Active": True, "LineNbr": 1}],
        "SchemaSourceFields": field_rows,
    }))
    out: dict[str, Any] = {
        "provider": name,
        "id": rid,
        "object": object_name,
        "columns": headers,
        "key_columns": sorted(keys),
        "file_uploaded": False,
    }
    # 3) optionally upload the source file so an import run can read it.
    #    Use the GET-free template URL: the DataProvider entity 500s on
    #    read-back (Link field BQL delegate), so record_files_put_url would
    #    fail to resolve _links. Don't let an upload hiccup mask a created
    #    provider — surface it instead of raising.
    if upload_file and rid:
        try:
            url = client.provider_files_put_url(rid, p.name)
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            await client.put_file(url, p.read_bytes(), ctype)
            out["file_uploaded"] = True
            out["filename"] = p.name
        except Exception as e:
            out["file_upload_error"] = str(e)[:300]
            out["note"] = (
                "Provider + schema created, but file upload failed. Retry with "
                f"attach_file_to_provider(record_id='{rid}', file_path=...)."
            )
    return out


@mcp.tool()
async def delete_entity(entity: str, record_id: str, endpoint: str | None = None,
                        instance: str | None = None) -> Any:
    """Delete a record by its id (the record's key GUID or keys path).

    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001').
    Requires the instance's "allow_delete": true (default off, stricter than write).
    """
    _require_delete(instance)
    return await _client(instance, endpoint).delete_entity(entity, record_id)


@mcp.tool()
async def count_entity(
    entity: str,
    filter: str | None = None,
    select: str | None = None,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """Count records of an entity (optionally scoped by filter).

    NOTE: the contract API has no server-side $count, so this fetches matching
    rows (auto-paging with $skip so big tables aren't under-counted) and counts
    them. Pass select=<a key field> to shrink the payload, and use filter to scope.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001').
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    rows = await _client(instance, endpoint).get_all(entity, params)
    return {"entity": entity, "count": len(rows), "filter": filter or None}


@mcp.tool()
async def list_actions(entity: str, refresh: bool = False, instance: str | None = None) -> Any:
    """List the actions invokable on an entity via invoke_action (from the contract).

    e.g. SalesOrder -> ["ReopenSalesOrder", ...]. Use before invoke_action to get
    the exact action name. Set refresh=true to bypass the swagger cache.
    """
    return await _client(instance).list_actions(entity, refresh=refresh)


@mcp.tool()
async def poll_action(location: str, instance: str | None = None) -> Any:
    """Check a long-running action's status by its Location (from invoke_action).

    invoke_action returns 202 + a location for async actions. GET it here:
    204 = finished, 202 = still running. Re-call until it finishes.
    """
    return await _client(instance).get_url(location)


@mcp.tool()
async def snapshot_entity(
    entity: str,
    path: str | None = None,
    filter: str | None = None,
    expand: str | None = None,
    instance: str | None = None,
) -> Any:
    """Dump all records of an entity to a JSON file (backup before risky changes).

    Writes to `path`, or by default to <connections dir>/snapshots/<entity>_<instance>.json.
    Returns the file path + record count. Use before destructive ops (calendar
    regen, segment restructure, bulk overwrite) so you can roll back.

    Auto-pages with $skip so the snapshot captures the FULL table, not just page 1.
    The final path — whether you supplied one or it's the auto-generated default —
    must be inside the instance's write_roots (if configured).
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if expand:
        params["$expand"] = expand
    cfg = _cfg()
    name = instance or cfg.default
    if not path:
        base = os.path.dirname(os.environ.get("GRP_MCP_CONNECTIONS", "")) or os.getcwd()
        path = os.path.join(base, "snapshots", f"{entity}_{name}.json")
    _check_write_path(path, instance)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = await _client(instance).get_all(entity, params)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    n = len(data) if isinstance(data, list) else (0 if data is None else 1)
    return {"entity": entity, "instance": name, "count": n, "path": path,
            "sandbox": cfg.get(name).fs_sandbox("write")}


@mcp.tool()
async def list_generic_inquiries(instance: str | None = None) -> Any:
    """List Generic Inquiries exposed via OData (name + url) on the instance.

    Requires the instance's `tenant` set in config. Use a returned name with
    run_generic_inquiry.
    """
    return await _client(instance).list_generic_inquiries()


@mcp.tool()
async def invoke_action(
    entity: str,
    action: str,
    entity_ref: dict,
    parameters: dict | None = None,
    endpoint: str | None = None,
    instance: str | None = None,
) -> Any:
    """Invoke an action on a record (e.g. Release, ConfirmShipment).

    entity: entity the action belongs to, e.g. "SalesOrder".
    action: action name, e.g. "Release".
    entity_ref: identifies the target record, e.g. {"OrderType": "SO", "OrderNbr": "000123"}.
    parameters: optional action parameters.
    endpoint: override as '<Name>/<Version>' (e.g. 'grp_mcp/25.200.001').
    Returns 202 + a Location to poll for long-running actions.

    Requires the instance's "allow_write": true (actions mutate ERP state).
    """
    _require_write(instance)
    body = {"entity": _wrap_fields(entity_ref), "parameters": _wrap_fields(parameters or {})}
    return await _client(instance, endpoint).invoke_action(entity, action, body)


@mcp.tool()
async def run_import_scenario(
    scenario_name: str,
    do_import: bool = False,
    entity: str = "ImportByScenario",
    key_field: str = "ScenarioName",
    prepare_action: str = "prepareIBS",
    import_action: str = "importIBS",
    poll_interval: float = 3.0,
    timeout: float = 300.0,
    instance: str | None = None,
) -> Any:
    """Drive Import-by-Scenario (SM206036) end to end via API.

    Selects the scenario record, runs Prepare (stages rows from the provider),
    and optionally Import (commits to the target). Returns the record status.

    scenario_name: the scenario's Name (must already exist in SM206025).
    do_import:     False (default) = prepare only (safe, no commit); True = also import.
    entity/key_field/prepare_action/import_action: override if your endpoint names
        them differently (defaults match the GRPSetup setup: ImportByScenario +
        prepareIBS/importIBS). Find action names with list_actions(entity).

    NOTE: the provider that the scenario uses must already have its file attached
    (see attach_file). Prepare/Import run on the screen's selected scenario record.

    Requires the instance's "allow_write": true (it stages/commits records).
    """
    import asyncio

    _require_write(instance)
    _require_range("poll_interval", poll_interval, 0.2, 60)
    _require_range("timeout", timeout, 1, 3600)
    client = _client(instance)
    ref = {key_field: scenario_name}

    async def _act(action: str) -> Any:
        body = {"entity": _wrap_fields(ref), "parameters": _wrap_fields({})}
        res = await client.invoke_action(entity, action, body)
        # async action -> {status: 202, location: ...}; poll until 204/done
        if isinstance(res, dict) and res.get("location"):
            waited = 0.0
            while waited < timeout:
                st = await client.get_url(res["location"])
                if isinstance(st, dict) and st.get("status") == 204:
                    return {"action": action, "completed": True}
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            return {"action": action, "completed": False, "timeout": timeout}
        return {"action": action, "result": res}

    # select the scenario record (upsert by key) so Prepare/Import act on it
    await client.put_entity(entity, _wrap_fields(ref))
    out: dict[str, Any] = {"scenario": scenario_name, "prepare": await _act(prepare_action)}
    if do_import:
        out["import"] = await _act(import_action)
    # read back the status/result of the selected record
    try:
        rec = await client.get_entity(entity, None, {"$filter": f"{key_field} eq '{scenario_name}'", "$top": 1})
        if isinstance(rec, list) and rec:
            rec = rec[0]
        out["status"] = {
            k: (rec.get(k) or {}).get("value")
            for k in ("Status", "Result", "PreparedOn", "CompletedOn", "NumberofRecords")
            if isinstance(rec, dict)
        }
    except Exception:
        pass
    return out


@mcp.tool()
async def run_generic_inquiry(
    name: str,
    filter: str | None = None,
    top: int | None = None,
    instance: str | None = None,
) -> Any:
    """Run a Generic Inquiry via OData. `name` is the GI's exposed OData name.

    Requires the instance's `tenant` (company login) to be set in config.
    """
    params: dict[str, Any] = {"$format": "json"}
    if filter:
        params["$filter"] = filter
    if top:
        params["$top"] = top
    return await _client(instance).run_gi(name, params)


@mcp.tool()
async def list_dacs(instance: str | None = None) -> Any:
    """List every DAC (data access class) exposed via the DAC-based OData v4 interface.

    Returns the OData service document (each DAC's name + url). This reaches data
    DIRECTLY from DACs (e.g. PX.Objects.GL.GLTran) WITHOUT needing the screen on a
    web service endpoint — the complement to the contract API's endpoint-bound view.
    Requires the instance's `tenant` to be set in config. Query one with run_dac_odata.
    """
    return await _client(instance).list_dacs()


@mcp.tool()
async def run_dac_odata(
    dac: str,
    filter: str | None = None,
    select: str | None = None,
    expand: str | None = None,
    top: int | None = None,
    skip: int | None = None,
    dedup: bool = True,
    instance: str | None = None,
) -> Any:
    """Query a single DAC through the DAC-based OData v4 interface.

    dac: the DAC OData name from list_dacs (e.g. "PX_Objects_GL_GLTran", "Account").
    filter/select/expand/top/skip: OData v4 query options ($filter, $select, ...).
    Read-only. Use this to read tables/screens NOT exposed on the contract endpoint
    (the contract API only sees entities added to the endpoint). Requires `tenant`.

    dedup (default True): this platform's DAC-OData layer occasionally returns the
    SAME row more than once across internal server-side page boundaries (observed on
    FinPeriod). With dedup on, identical rows in the `value` array are collapsed
    (order preserved) and a `@grp.deduped` count is added when any were removed.
    Set dedup=false to see the raw payload verbatim.
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand
    if top:
        params["$top"] = top
    if skip:
        params["$skip"] = skip
    result = await _client(instance).run_dac(dac, params)
    if dedup and isinstance(result, dict) and isinstance(result.get("value"), list):
        rows = result["value"]
        seen: set[str] = set()
        unique: list = []
        for r in rows:
            # Signature = the full row (paging dupes are byte-identical); sort keys so
            # dict ordering never splits a true duplicate. Fall back to id() if a row
            # isn't JSON-serializable (never expected for OData scalars).
            try:
                sig = json.dumps(r, sort_keys=True, default=str)
            except Exception:  # noqa: BLE001
                sig = repr(r)
            if sig in seen:
                continue
            seen.add(sig)
            unique.append(r)
        if len(unique) != len(rows):
            result = dict(result)
            result["value"] = unique
            result["@grp.deduped"] = len(rows) - len(unique)
    return result


@mcp.tool()
async def get_dac_metadata(
    dac: str | None = None,
    mandatory_only: bool = False,
    raw: bool = False,
    instance: str | None = None,
) -> Any:
    """Read DAC field definitions from the OData CSDL ($metadata) — incl. mandatory flags.

    The authoritative source for which fields a DAC requires. Each property carries a
    Nullable flag: Nullable="false" (or a key field) = MANDATORY. Works for DACs that
    run_dac_odata cannot read, including single-row config DACs (e.g. GLSetup, the GL
    Preferences table; FinancialYear) that serve no OData collection route.

    dac: entity-type name to filter to (e.g. "Organization", "Branch", "GLSetup").
         Case-insensitive; omit to return every DAC.
    mandatory_only: if True, return only the mandatory fields (Nullable=false or key).
    raw: if True, return the raw CSDL XML text instead of the parsed map.

    Returns (parsed) {dacName: [{name, type, nullable, key, maxLength}, ...]}.
    Requires the instance's `tenant` to be set in config.
    """
    import xml.etree.ElementTree as ET

    xml_text = await _client(instance).dac_metadata()
    if raw:
        return xml_text

    root = ET.fromstring(xml_text)
    # CSDL namespaces vary by version; match by local tag name to stay version-proof.
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    want = dac.lower() if dac else None
    out: dict[str, list[dict[str, Any]]] = {}
    for et in root.iter():
        if _local(et.tag) != "EntityType":
            continue
        name = et.get("Name") or ""
        if want and name.lower() != want:
            continue
        keys: set[str | None] = set()
        for k in et:
            if _local(k.tag) != "Key":
                continue
            for pr in k:
                if _local(pr.tag) == "PropertyRef":
                    keys.add(pr.get("Name"))
        fields: list[dict[str, Any]] = []
        for prop in et:
            if _local(prop.tag) != "Property":
                continue
            pname = prop.get("Name")
            is_key = pname in keys
            # OData default Nullable is true; key fields are implicitly mandatory.
            nullable = prop.get("Nullable", "true").lower() != "false"
            mandatory = is_key or not nullable
            if mandatory_only and not mandatory:
                continue
            fields.append({
                "name": pname,
                "type": prop.get("Type"),
                "nullable": nullable,
                "key": is_key,
                "maxLength": prop.get("MaxLength"),
            })
        out[name] = fields
    if want and not out:
        return {"error": f"DAC '{dac}' not found in metadata", "available_count": "use dac=None to list all"}
    return out


# Headline module switches surfaced by setup_readiness (subset of FeaturesSet columns).
_MODULE_FLAGS = [
    "FinancialModule", "FinancialStandard", "FinancialAdvanced", "MultiCompany",
    "SubAccount", "Multicurrency", "MultipleBaseCurrencies", "ProjectModule",
    "ProjectAccounting", "DistributionModule", "Inventory", "Manufacturing",
    "CustomerModule", "ServiceManagementModule", "PayrollModule", "PlatformModule",
]

# Per-module setup checklist keyed to the Acumatica implementation guide. Each step
# is (label, DAC collection, key field) — existence is probed via DAC OData. The DAC
# names are best-effort; a name not exposed as a collection yields "exists": null.
_SETUP_CHECKLIST = [
    ("Company structure", "OrganizationModule", [
        ("Branch defined (CS102000)", "Branch", "BranchCD"),
    ]),
    ("General Ledger", "FinancialModule", [
        ("Actual ledger created (GL201500)", "Ledger", "LedgerCD"),
        ("Chart of Accounts (GL202500)", "Account", "AccountCD"),
    ]),
    ("Accounts Receivable", "FinancialStandard", [
        ("Customer class (AR201000)", "CustomerClass", "CustomerClassID"),
    ]),
    ("Accounts Payable", "FinancialStandard", [
        ("Vendor class (AP201000)", "VendorClass", "VendorClassID"),
    ]),
    ("Cash Management", "FinancialStandard", [
        ("Payment methods (CA204000)", "PaymentMethod", "PaymentMethodID"),
        ("Cash accounts (CA202000)", "CashAccount", "CashAccountCD"),
    ]),
    ("Inventory", "Inventory", [
        ("Item classes (IN201000)", "INItemClass", "ItemClassCD"),
    ]),
    ("Project Accounting", "ProjectModule", [
        ("Project created (PM301000)", "PMProject", "ContractCD"),
    ]),
]


async def _probe_exists(client, dac: str, key: str):
    """Best-effort existence check: does the DAC collection have >= 1 row?

    Returns True/False, or None when the DAC isn't readable as a collection
    (single-row config DACs and unknown names degrade to "unknown" rather than error).
    """
    try:
        res = await client.run_dac(dac, {"$top": 1, "$select": key})
        vals = res.get("value") if isinstance(res, dict) else None
        if vals is None:
            return None
        return len(vals) > 0
    except Exception:
        return None


_GUIDE = {
    "start_here": (
        "grp-mcp exposes Acumatica over FOUR planes. Don't guess — pick by task shape "
        "below, or call screen_capabilities(screen_id) for one screen, or "
        "get_setup_guidance for financial-foundation setup. Golden rules: (a) KB-FIRST "
        "before any write (search_kb/read_kb_file for the screen's prerequisites); "
        "(b) a clean ok is NOT proof — read back (run_dac_odata/screen_get/get_entity); "
        "(c) writes need allow_write, deletes allow_delete, publish allow_publish."
    ),
    "the_four_planes": {
        "contract REST (entities)": "the endpoint's typed entities — default for CRUD on "
            "anything on the endpoint. Tools: get_entity, fetch_all_entities, "
            "create_or_update_entity, delete_entity, invoke_action. endpoint='Name/Ver' "
            "overrides the configured endpoint (e.g. grp_mcp/25.200.001).",
        "DAC / GI OData (raw read)": "read tables/inquiries the endpoint doesn't expose. "
            "Tools: run_dac_odata (any DAC incl. config singletons), get_dac_metadata "
            "(mandatory-field discovery), run_generic_inquiry, list_dacs, "
            "list_generic_inquiries. Read-only.",
        "classic screen SOAP": "drive a SCREEN the REST API can't (context / master-detail "
            "/ wizard screens). Tools: screen_get, screen_get_schema, screen_submit, "
            "screen_record, screen_insert_rows, screen_preflight. Uses FRIENDLY "
            "container.field names (screen_get_schema). Enum/read-only pre-validated.",
        "modern UI-JSON": "what classic SOAP can't: dialog actions that SOAP silently "
            "no-ops (e.g. GL201000 Generate), grid-CELL edits, row-scoped actions, "
            "processes, selector lookups. Tools: ui_get_structure, ui_screen_action, "
            "ui_read_grid, ui_insert_grid_row, ui_update_grid_row, ui_delete_grid_row, "
            "ui_grid_row_action, ui_run_process, ui_lookup, ui_resolve_selector, "
            "ui_preflight, ui_tree_dialog_insert, ui_populate_endpoint_entity_fields.",
    },
    "by_task": {
        "discover what exists": ["whoami", "list_instances", "list_entities",
            "get_entity_schema", "list_screens", "screen_get_schema", "ui_get_structure",
            "list_dacs", "list_generic_inquiries", "list_actions", "screen_capabilities"],
        "read data": ["get_entity / fetch_all_entities (endpoint entity)",
            "run_dac_odata (raw DAC / config singleton)", "run_generic_inquiry (saved GI)",
            "screen_get (screen the API can't reach)", "ui_read_grid (live grid)",
            "count_entity", "run_report"],
        "write ONE record": ["create_or_update_entity (endpoint entity — DEFAULT)",
            "screen_record / screen_submit (context/master-detail screen)",
            "ui_screen_action (modern form field / dialog action)",
            "ui_preflight (dry-run validate a modern write first)"],
        "grid rows": ["screen_insert_rows (bulk append, classic)",
            "ui_insert_grid_row / ui_update_grid_row / ui_delete_grid_row (modern, "
            "key-addressed, cell-validated)", "ui_grid_row_action (select row + fire action)"],
        "run a process / mass-action": ["ui_run_process (Process/ProcessAll to completion)",
            "manage_financial_periods, generate_master_calendar (GL recipes)"],
        "financial-foundation / GL setup": ["get_setup_guidance FIRST (per-screen prereqs, "
            "required fields, order, plane, gotchas)", "setup_readiness (what's missing)",
            "enable_features + activate_features", "create_financial_calendar", "create_ledger",
            "chart_of_accounts", "create_segmented_key + set_segment_value",
            "create_numbering_sequence", "set_gl_preferences", "generate_master_calendar",
            "manage_financial_periods"],
        "lookups / reference data": ["ui_lookup (search any selector's table)",
            "ui_resolve_selector (resolve one selector field to {id,text} for a write)"],
        "web-service endpoints / customization": ["get_endpoint_definition",
            "import_customization + publish_customization (poll publish_status)",
            "list_published, unpublish_customization, export_customization",
            "ui_tree_dialog_insert + ui_populate_endpoint_entity_fields (add entity via SM207060)"],
        "import / export data": ["run_import_scenario (SM206036)", "load_from_excel",
            "setup_data_provider, setup_readiness"],
        "files / notes / attachments": ["attach_file", "download_file", "list_attachments",
            "set_note"],
        "actions": ["list_actions", "invoke_action", "poll_action (async 202)"],
        "sessions / config / seats": ["release_sessions (free API seats)", "test_connection",
            "reload_config", "set_active_instance", "add_instance", "remove_instance"],
    },
    "plane_by_shape": (
        "entity CRUD on the endpoint -> contract REST; raw table / config singleton -> "
        "run_dac_odata; saved inquiry -> run_generic_inquiry; a screen REST can't reach "
        "(context/master-detail/wizard) or a bulk-append, and the ONLY plane under a "
        "maintenance lockout -> classic SOAP (screen_*); a dialog action SOAP no-ops, a "
        "grid-CELL edit, a row-scoped action, or a process -> modern (ui_*); unsure which "
        "for a given screen -> screen_capabilities(screen_id)."
    ),
    "deeper_guides": {
        "get_setup_guidance": "per-screen financial-foundation setup map (prereqs, required "
            "fields, order, plane, verify, gotchas) + cross-cutting rules incl. the modern-"
            "UI protocol facts and SOAP write caveats.",
        "screen_capabilities": "probes one screen's /structure and recommends the plane/tool "
            "per operation shape.",
    },
}


@mcp.tool()
def guide(topic: str | None = None) -> Any:
    """START HERE — pick the right grp-mcp tool for your task (this server has ~77 tools
    across four Acumatica planes, so guessing wastes calls).

    Returns a task->tool decision map + the plane-by-shape routing rule. Read-only,
    instant (static, no API call).

    topic: narrow the answer — one of: "read", "write", "grid", "process", "setup",
        "lookup", "customization", "import", "files", "actions", "session", "discover",
        "planes". Omit for the full overview. (For a SPECIFIC screen use
        screen_capabilities(screen_id); for financial-foundation setup use
        get_setup_guidance.)
    """
    if topic:
        t = topic.strip().lower()
        aliases = {
            "read": "read data", "write": "write ONE record", "grid": "grid rows",
            "process": "run a process / mass-action",
            "setup": "financial-foundation / GL setup", "gl": "financial-foundation / GL setup",
            "lookup": "lookups / reference data", "lookups": "lookups / reference data",
            "customization": "web-service endpoints / customization",
            "endpoint": "web-service endpoints / customization",
            "import": "import / export data", "export": "import / export data",
            "files": "files / notes / attachments", "notes": "files / notes / attachments",
            "actions": "actions", "action": "actions",
            "session": "sessions / config / seats", "config": "sessions / config / seats",
            "discover": "discover what exists",
        }
        if t in ("plane", "planes"):
            return {"the_four_planes": _GUIDE["the_four_planes"],
                    "plane_by_shape": _GUIDE["plane_by_shape"]}
        key = aliases.get(t)
        if key and key in _GUIDE["by_task"]:
            return {"topic": key, "tools": _GUIDE["by_task"][key],
                    "plane_by_shape": _GUIDE["plane_by_shape"],
                    "golden_rules": _GUIDE["start_here"]}
        return {"error": f"unknown topic {topic!r}",
                "topics": sorted(set(aliases) | {"planes"}),
                "tip": "omit topic for the full overview."}
    return _GUIDE


@mcp.tool()
def get_setup_guidance(screen_id: str | None = None, area: str | None = None) -> Any:
    """Baked-in Acumatica FOUNDATION setup map — prereqs/required-fields/gotchas/order per screen.

    The documented, version-stable knowledge for standing up the financial foundation
    (System/Company -> GL -> Common -> CA -> AP/AR -> Tax -> Currency), distilled from the
    Acumatica KB + proven experience. Consult it BEFORE driving any setup screen so you know
    the prerequisites, required fields, validation gotchas, correct order, which grp-mcp
    tool/plane to use, and how to verify — instead of hitting a screen cold and eating a
    generic error. This is the machine-readable companion to the KB-first policy.

    screen_id: e.g. "CA101000" -> that screen's full guidance + the cross-cutting rules.
    area:      e.g. "CA" | "GL" | "AP" -> all screens in that area, in setup order.
    (both omitted) -> overview: scope, cross-cutting rules, canonical order, screen index.

    IMPORTANT — two layers: the returned prereqs/order/gotchas are DOCUMENTED (trust them),
    but the required-field lists and what's actually configured/licensed are INSTANCE-SPECIFIC
    — always recompute live with ui_get_structure(<screen>) / run_dac_odata before writing.
    Read-only, no API session. Scope is the financial foundation only (not Distribution/
    Projects/Manufacturing/etc. nor the transactional layer).
    """
    m = _setup_map()
    live_reminder = m["layers"]["live"]
    if screen_id:
        sid = screen_id.upper()
        sc = m["screens"].get(sid)
        if not sc:
            return {"error": f"'{sid}' is not in the foundation setup map (scope: {m['scope']})",
                    "covered_screens": sorted(m["screens"]),
                    "tip": "For screens outside the map, use ui_get_structure + screen_get_schema + KB (search_kb)."}
        return {"screen_id": sid, **sc,
                "cross_cutting_rules": m["cross_cutting_rules"], "recompute_live": live_reminder}
    if area:
        a = area.upper()
        scr = [(k, v) for k, v in m["screens"].items() if str(v.get("area", "")).upper() == a]
        if not scr:
            return {"error": f"no area '{area}'",
                    "areas": sorted({v["area"] for v in m["screens"].values()})}
        scr.sort(key=lambda kv: kv[1].get("order", 999))
        return {"area": area, "screens": [{"screen_id": k, **v} for k, v in scr],
                "recompute_live": live_reminder}
    idx = sorted(m["screens"].items(), key=lambda kv: kv[1].get("order", 999))
    return {
        "scope": m["scope"], "source": m["source"], "layers": m["layers"],
        "cross_cutting_rules": m["cross_cutting_rules"],
        "canonical_order": m["canonical_order"],
        "screens": [{"screen_id": k, "name": v["name"], "area": v["area"], "order": v["order"]}
                    for k, v in idx],
        "usage": "Call with screen_id=<ID> for a screen's full guidance, or area=<CA|GL|AP|...> for an area in order.",
    }


@mcp.tool()
async def setup_readiness(instance: str | None = None) -> Any:
    """Report an instance's setup state: enabled features + per-module config gaps.

    Reads the FeaturesSet config DAC (which modules/features are switched on) and runs
    best-effort existence probes for the prerequisite records each financial module
    needs (ledger, chart of accounts, customer/vendor classes, ...), then cross-checks
    the Acumatica implementation checklist to flag what's still missing.

    Read-only. The engine for guided/no-knowledge setup: call it to learn "where is this
    instance now, and what's the next step." Probes degrade to `exists: null` (unknown)
    for any DAC not exposed as a collection. Requires the instance's `tenant` to be set.

    NOT checked here (no REST surface — see EXTENDING_ENDPOINTS.md): the wizard actions
    (enable features CS100000, activate license SM201510, financial calendar GL101000)
    and preference VALUES (GLSetup/ARSetup/APSetup serve no readable collection route).
    """
    client = _client(instance)
    feats_raw = await client.run_dac("FeaturesSet", {"$top": 1})
    rows = feats_raw.get("value") if isinstance(feats_raw, dict) else None
    feats = rows[0] if rows else {}

    modules = {f: bool(feats.get(f)) for f in _MODULE_FLAGS if f in feats}
    enabled_features = sorted(k for k, v in feats.items() if v is True)

    checklist: list[dict[str, Any]] = []
    for module, flag, steps in _SETUP_CHECKLIST:
        feature_on = bool(feats.get(flag))
        step_out = []
        for label, dac, key in steps:
            exists = await _probe_exists(client, dac, key) if feature_on else None
            step_out.append({"step": label, "exists": exists})
        complete = feature_on and all(s["exists"] is True for s in step_out)
        checklist.append({
            "module": module,
            "feature_flag": flag,
            "feature_enabled": feature_on,
            "complete": complete,
            "steps": step_out,
        })

    gaps = [
        f"{c['module']}: {s['step']}"
        for c in checklist if c["feature_enabled"]
        for s in c["steps"] if s["exists"] is False
    ]

    # Financial calendar (GL101000) has no DAC/REST collection route — probe it via
    # the screen-based SOAP Export (the wizard plane). Best-effort: degrades to
    # exists:null if SOAP is unreachable. A calendar is the prerequisite for the GL
    # ledger, so surface it as a gap when the financial module is on but it's absent.
    calendar = {"exists": None, "checked_via": "GL101000 Export (screen SOAP)"}
    try:
        inst_obj = _cfg().get(instance or _cfg().default)
        async with ScreenClient(inst_obj, "GL101000") as sc:
            periods = await sc.export(["Periods.PeriodNbr"], top=1)
            calendar["exists"] = bool(periods.get("rows"))
    except Exception as e:  # noqa: BLE001 - readiness must never hard-fail
        calendar["error"] = str(e)[:200]
    if bool(feats.get("FinancialModule")) and calendar["exists"] is False:
        gaps.insert(0, "General Ledger: Financial calendar (GL101000)")

    # Feature ACTIVATION (are the enabled flags actually INSTALLED, or only staged?).
    # CS100000 ActivationStatus via screen Export — "Validated" = installed; "Pending
    # Activation" = saved but not applied (call activate_features). This is the gap
    # that silently blocks everything downstream.
    feature_activation = {"status": None, "installed": None,
                          "checked_via": "CS100000 Export (screen SOAP)"}
    try:
        async with ScreenClient(inst_obj, "CS100000") as sc:
            rows = (await sc.export(["GeneralSettings.ActivationStatus"], top=1)).get("rows")
        st = rows[0].get("Status") if rows else None
        feature_activation.update(status=st, installed=(st == "Validated"))
    except Exception as e:  # noqa: BLE001
        feature_activation["error"] = str(e)[:160]
    if feature_activation.get("installed") is False:
        gaps.insert(0, "Features: staged but NOT installed — ActivationStatus is "
                    f"'{feature_activation.get('status')}' (call activate_features)")

    # GL Preferences system accounts (GL102000): Retained Earnings + YTD Net Income
    # must be set before the GL master calendar can be generated / posting enabled.
    gl_preferences = {"retained_earnings": None, "ytd_net_income": None,
                      "configured": None, "checked_via": "GL102000 Export (screen SOAP)"}
    try:
        async with ScreenClient(inst_obj, "GL102000") as sc:
            rows = (await sc.export(["GLSetupRecord.RetainedEarningsAccount",
                                     "GLSetupRecord.YTDNetIncomeAccount"], top=1)).get("rows")
        if rows:
            vals = list(rows[0].values())
            re_acct = (vals[0] if len(vals) > 0 else None) or None
            ytd_acct = (vals[1] if len(vals) > 1 else None) or None
            gl_preferences.update(
                retained_earnings=re_acct, ytd_net_income=ytd_acct,
                configured=bool(re_acct) and bool(ytd_acct))
    except Exception as e:  # noqa: BLE001
        gl_preferences["error"] = str(e)[:160]
    if bool(feats.get("FinancialModule")) and gl_preferences.get("configured") is False:
        gaps.append("General Ledger: GL Preferences system accounts not set "
                    "(GL102000 Retained Earnings + YTD Net Income — GL phase)")

    # Open periods — no open period means no posting. FinPeriod is empty until the
    # master calendar is generated (GL201000) + periods opened (GL201100).
    periods = {"any_exist": None, "checked_via": "FinPeriod DAC"}
    try:
        pr = await client.run_dac("FinPeriod", {"$top": 1})
        rows = pr.get("value") if isinstance(pr, dict) else None
        periods["any_exist"] = bool(rows) if rows is not None else None
    except Exception as e:  # noqa: BLE001
        periods["error"] = str(e)[:160]
    if bool(feats.get("FinancialModule")) and periods.get("any_exist") is False:
        gaps.append("General Ledger: no financial periods generated/open "
                    "(GL201000 generate calendar → GL201100 open periods — GL phase)")

    return {
        "instance": instance or _cfg().default,
        "modules": modules,
        "enabled_features": enabled_features,
        "feature_activation": feature_activation,
        "financial_calendar": calendar,
        "gl_preferences": gl_preferences,
        "open_periods": periods,
        "checklist": checklist,
        "gaps": gaps,
        "note": "Probes are best-effort (null = unknown DAC/route or SOAP unreachable). "
                "Now also reports: feature ACTIVATION (installed vs staged), GL Preferences "
                "system accounts, and whether any financial periods exist — the GL-phase "
                "gates. Calendar/features/GL-prefs are read via screen SOAP.",
    }


@mcp.tool()
async def list_attachments(
    entity: str, record_id: str, instance: str | None = None
) -> Any:
    """List the files attached to a record (name + download href).

    Reads the record's `files` collection. Use a returned `id`/`filename` with
    download_file. (To ADD a file, use attach_file.)
    """
    rec = await _client(instance).get_entity(entity, record_id)
    files = (rec.get("files") if isinstance(rec, dict) else None) or []
    out = [
        {
            "id": f.get("id"),
            "filename": f.get("filename"),
            "href": (f.get("href") or (f.get("_links") or {}).get("self")),
        }
        for f in files
        if isinstance(f, dict)
    ]
    return {"entity": entity, "record_id": record_id, "count": len(out), "files": out}


@mcp.tool()
async def download_file(
    entity: str,
    record_id: str,
    out_path: str,
    filename: str | None = None,
    instance: str | None = None,
) -> Any:
    """Download a file attached to a record to disk.

    entity/record_id: the record. filename: which attached file to pull (defaults
    to the record's first/only attachment). out_path: where to write the bytes.
    Resolves the file's href from the record's `files` collection, then GETs the
    raw bytes. (List a record's files first with list_attachments.)

    out_path must be within the instance's write_roots (if configured).
    """
    from pathlib import Path

    dest = _check_write_path(out_path, instance)
    client = _client(instance)
    rec = await client.get_entity(entity, record_id)
    files = (rec.get("files") if isinstance(rec, dict) else None) or []
    if not files:
        raise AcumaticaError(f"no files attached to {entity}/{record_id}")
    chosen = None
    if filename:
        chosen = next(
            (f for f in files if isinstance(f, dict) and f.get("filename") == filename),
            None,
        )
        if chosen is None:
            names = [f.get("filename") for f in files if isinstance(f, dict)]
            raise AcumaticaError(f"'{filename}' not attached. Available: {names}")
    else:
        chosen = files[0]
    href = chosen.get("href") or (chosen.get("_links") or {}).get("self")
    if not href:
        raise AcumaticaError(f"attachment has no download href: {chosen}")
    data = await client.get_bytes(href)
    dest.write_bytes(data)
    return {
        "entity": entity,
        "record_id": record_id,
        "filename": chosen.get("filename"),
        "bytes": len(data),
        "path": out_path,
        "sandbox": _cfg().get(instance or _cfg().default).fs_sandbox("write"),
    }


@mcp.tool()
async def run_report(
    report_entity: str,
    out_path: str,
    parameters: dict | None = None,
    poll_interval: float = 2.0,
    timeout: float = 180.0,
    instance: str | None = None,
) -> Any:
    """Run a Report-type endpoint entity and save the rendered file (PDF) to disk.

    report_entity: the report entity's name on the configured endpoint (a Report
        entity must be added to the endpoint first — list_entities to see them).
    parameters: the report's parameters as a plain map, auto-wrapped, e.g.
        {"LedgerID": "ACTUAL", "FromPeriod": "012026", "ToPeriod": "122026"}.
    out_path: where to write the report bytes.

    Contract flow: PUT report + params -> 202 + Location -> poll until 200 -> bytes.
    out_path must be within the instance's write_roots (if configured).
    """
    _require_range("poll_interval", poll_interval, 0.2, 60)
    _require_range("timeout", timeout, 1, 3600)
    dest = _check_write_path(out_path, instance)
    body: dict[str, Any] = {}
    if parameters:
        body["parameters"] = _wrap_fields(parameters)
    data = await _client(instance).run_report(
        report_entity, body, poll_interval=poll_interval, timeout=timeout
    )
    dest.write_bytes(data)
    return {
        "report": report_entity,
        "bytes": len(data),
        "path": out_path,
        "parameters": parameters or {},
        "sandbox": _cfg().get(instance or _cfg().default).fs_sandbox("write"),
    }


@mcp.tool()
async def set_note(
    entity: str, record_id: str, note: str, instance: str | None = None
) -> Any:
    """Set (or clear) the Note text on a record.

    The contract API exposes a record's note as the `note` field. Pass note="" to
    clear it. record_id identifies the target record (key/GUID).

    Requires the instance's "allow_write": true.
    """
    _require_write(instance)
    client = _client(instance)
    rec = await client.get_entity(entity, record_id)
    body: dict[str, Any] = {"note": _wrap(note)}
    # carry the record's id so the PUT targets the same record (id is top-level,
    # NOT wrapped in a {"value": ...} envelope)
    if isinstance(rec, dict) and rec.get("id"):
        body["id"] = rec["id"]
    return await client.put_entity(entity, body)


@mcp.tool()
async def list_published(instance: str | None = None) -> Any:
    """List customization projects currently published on the instance (read-only)."""
    async with _customization(instance) as c:
        return await c.get_published()


@mcp.tool()
async def export_customization(
    project_name: str,
    out_path: str,
    instance: str | None = None,
) -> Any:
    """Export a customization project to a .zip on disk (Customization getProject).

    Pulls the project content via API and writes it to out_path. This closes the
    headless edit loop for endpoints: export_customization -> edit project.xml ->
    import_customization -> publish_customization (no browser export needed).
    out_path must be within the instance's write_roots (if configured).
    """
    import base64

    dest = _check_write_path(out_path, instance)
    async with _customization(instance) as c:
        res = await c.get_project(project_name)
    content = res.get("projectContentBase64") if isinstance(res, dict) else None
    if not content:
        return {"error": "no projectContentBase64 in response", "project": project_name,
                "raw": res}
    data = base64.b64decode(content)
    dest.write_bytes(data)
    return {"project": project_name, "path": out_path, "bytes": len(data)}


@mcp.tool()
async def import_customization(
    project_name: str,
    zip_path: str,
    is_replace_if_exists: bool = True,
    project_level: int | None = None,
    project_description: str | None = None,
    instance: str | None = None,
) -> Any:
    """Import a customization package (.zip on disk) into the instance.

    Creates/replaces the project; does NOT publish it. Requires the instance's
    profile to have "allow_publish": true.
    """
    _require_publish(instance)
    _check_read_path(zip_path, instance)  # sandbox + size cap
    content = encode_zip(zip_path)
    async with _customization(instance) as c:
        return await c.import_project(
            project_name,
            content_base64=content,
            is_replace_if_exists=is_replace_if_exists,
            project_level=project_level,
            project_description=project_description,
        )


@mcp.tool()
async def publish_customization(
    project_names: list[str],
    tenant_mode: str = "Current",
    tenant_login_names: list[str] | None = None,
    options: dict | None = None,
    wait_seconds: float = 40.0,
    instance: str | None = None,
) -> Any:
    """Publish customization project(s) — NON-BLOCKING (won't hang on the recompile).

    A site recompile takes 1-3 min, longer than the MCP request timeout, so this
    used to return a spurious timeout error even though the publish completed. Now
    it runs the publish in a BACKGROUND task (which owns the login session and
    polls to completion) and returns after up to `wait_seconds`:
      • status "completed" — finished within wait_seconds (incl. fast validation
        FAILURES, which surface here with the error log in `result`);
      • status "in_progress" — still working server-side (phase "begin" = the
        publishBegin call itself, which can exceed 60s on a cold site; phase
        "publishing" = recompiling); it WILL finish on its own. Poll
        `publish_status(job)` until status != in_progress. Do NOT re-publish
        (that would start a second recompile). Begin/auth failures surface via
        publish_status as status "error".

    WARNING: website-level — recompiles the site and affects ALL tenants. tenant_mode:
    Current | All | List (with tenant_login_names). `options` passes extra publishBegin
    flags. wait_seconds is clamped to [0, 120]. Requires "allow_publish": true.
    """
    _require_publish(instance)
    _require_range("wait_seconds", wait_seconds, 0, 120)
    inst = _cfg().get(instance or _cfg().default)
    client = CustomizationClient(inst)
    # The job is registered BEFORE publishBegin and begin runs INSIDE the background
    # task: on a cold IIS site publishBegin alone can exceed the MCP request timeout,
    # which used to kill the call with NO job recorded (publish_status said "none"
    # and you couldn't tell whether the publish had started). Begin/auth errors now
    # surface via publish_status as status "error" with phase "begin".
    job = "+".join(project_names)
    state: dict[str, Any] = {"job": job, "project_names": project_names, "phase": "begin",
                             "completed": False, "failed": None, "result": None, "error": None}
    _publish_jobs[job] = state

    async def _drive() -> None:
        waited = 0.0
        last: Any = None
        try:
            await client.publish_begin(project_names, tenant_mode, tenant_login_names, options)
            state["phase"] = "publishing"
            while waited < 1800:
                last = await client.publish_end()
                if isinstance(last, dict) and last.get("isCompleted"):
                    state.update(completed=True, failed=bool(last.get("isFailed")), result=last)
                    return
                await asyncio.sleep(3.0)
                waited += 3.0
            state.update(result=last, error="publish poll exceeded 1800s")
        except Exception as e:  # noqa: BLE001 — record, don't crash the loop
            state.update(error=str(e)[:400])
        finally:
            state.pop("_task", None)
            await client.aclose()

    state["_task"] = asyncio.create_task(_drive())
    # let it settle: fast projects + fast-fail validation finish inside wait_seconds
    waited = 0.0
    while waited < wait_seconds and not state["completed"] and state["error"] is None:
        await asyncio.sleep(2.0)
        waited += 2.0
    return _publish_job_view(state)


@mcp.tool()
async def publish_status(job: str | None = None) -> Any:
    """Check a background publish started by publish_customization (in-memory read,
    no API call — instant).

    job: the `job` id publish_customization returned; omit for the most recent.
    Returns the same shape (status completed | in_progress | error). A site recompile
    finishes on its own, so just poll this until status != "in_progress" — never
    re-run publish_customization to "retry" one that's still in_progress.
    """
    if not _publish_jobs:
        return {"status": "none",
                "note": "No publish started in this server session (state is in-memory; "
                        "a server restart clears it — verify via list_published instead)."}
    if job is None:
        job = next(reversed(_publish_jobs))
    state = _publish_jobs.get(job)
    if state is None:
        return {"status": "unknown", "job": job, "known_jobs": list(_publish_jobs)}
    return _publish_job_view(state)


@mcp.tool()
async def unpublish_customization(
    tenant_mode: str = "Current",
    tenant_login_names: list[str] | None = None,
    instance: str | None = None,
) -> Any:
    """Unpublish ALL customization projects (rollback). Website-level recompile.

    tenant_mode: Current | All | List. Requires "allow_publish": true.
    """
    _require_publish(instance)
    async with _customization(instance) as c:
        return await c.unpublish_all(tenant_mode, tenant_login_names)


def main() -> None:
    import atexit

    atexit.register(_shutdown_clients)  # free API license seats on exit
    try:
        mcp.run()
    finally:
        _shutdown_clients()


if __name__ == "__main__":
    main()
