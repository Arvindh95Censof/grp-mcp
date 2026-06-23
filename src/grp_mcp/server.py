"""grp-mcp MCP server.

Exposes Acumatica's contract-based REST API as MCP tools. All tools accept an
optional `instance` argument selecting a configured connection; when omitted the
default instance is used.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .acumatica import AcumaticaClient, AcumaticaError
from .config import Config, Instance, load_config, save_config
from .customization import CustomizationClient, encode_zip
from .loaders import map_row, read_rows

mcp = FastMCP("grp-mcp")

_config: Config | None = None
_clients: dict[str, AcumaticaClient] = {}


def _cfg() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _client(instance: str | None) -> AcumaticaClient:
    cfg = _cfg()
    name = instance or cfg.default
    if name not in _clients:
        _clients[name] = AcumaticaClient(cfg.get(name))
    return _clients[name]


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


def _require_delete(instance: str | None) -> None:
    """Block record deletes unless the instance opted in (allow_delete)."""
    cfg = _cfg()
    name = instance or cfg.default
    if not cfg.get(name).allow_delete:
        raise PermissionError(
            f"Deletes are disabled for instance '{name}'. Set \"allow_delete\": true "
            f"in its connections.json profile to permit delete_entity."
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
        # `id` UNWRAPPED (it's the row/record identifier, not a {"value": ...} field;
        # wrapping it makes Acumatica reject the body / fail to match the row to update)
        return {k: (v if k == "id" else _wrap(v)) for k, v in value.items()}
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
    return {
        "active": cfg.default,
        "source_path": cfg.source_path,
        "instances": [
            {
                "name": n,
                "base_url": i.base_url,
                "endpoint": f"{i.endpoint_name}/{i.endpoint_version}",
                "tenant": i.tenant,
                "active": n == cfg.default,
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
    (the file is gitignored). Returns the updated profile list (no secrets).

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
    existed = name in cfg.instances
    cfg.instances[name] = inst
    _clients.pop(name, None)  # drop any stale cached client for this name
    if set_active or len(cfg.instances) == 1:
        cfg.default = name
    saved = save_config(cfg) if persist else None
    return {
        "added": name,
        "replaced": existed,
        "active": cfg.default,
        "persisted_to": saved,
        "instances": list_instances()["instances"],
    }


@mcp.tool()
def set_active_instance(name: str, persist: bool = False) -> dict:
    """Select which profile tools use by default (when called without `instance`).

    persist=true also writes the choice as "default" in connections.json so it
    survives a restart; otherwise it's a session-only switch.
    """
    cfg = _cfg()
    if name not in cfg.instances:
        raise KeyError(f"Unknown profile '{name}'. Configured: {', '.join(cfg.instances)}")
    cfg.default = name
    saved = save_config(cfg) if persist else None
    return {"active": name, "persisted": bool(saved), "persisted_to": saved}


@mcp.tool()
def remove_instance(name: str, persist: bool = True) -> dict:
    """Remove a connection profile (and drop its cached session).

    If it was the active profile, the active switches to another remaining profile.
    persist=true updates connections.json. Refuses to remove the last profile.
    """
    cfg = _cfg()
    if name not in cfg.instances:
        raise KeyError(f"Unknown profile '{name}'. Configured: {', '.join(cfg.instances)}")
    if len(cfg.instances) == 1:
        raise ValueError("refusing to remove the only configured profile.")
    del cfg.instances[name]
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
    """
    global _config
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
    _config = load_config()
    return list_instances()


@mcp.tool()
async def list_endpoints(instance: str | None = None) -> Any:
    """List all web service endpoints published on the instance.

    Returns each endpoint's name, version, and href (e.g. Default, GRPSetup,
    MANUFACTURING). Independent of the instance's configured endpoint.
    """
    return await _client(instance).list_endpoints()


@mcp.tool()
async def list_entities(refresh: bool = False, instance: str | None = None) -> Any:
    """List the top-level entities exposed by the instance's configured endpoint.

    Uses endpoint_name/endpoint_version from connections.json. Source: the
    endpoint's swagger.json (the metadata-root GET is often proxy-gated 401).
    Set refresh=true to bypass the per-session cache.
    """
    return await _client(instance).list_entities(refresh=refresh)


@mcp.tool()
async def get_entity_schema(
    entity: str, refresh: bool = False, instance: str | None = None
) -> Any:
    """List the fields of one entity in the configured endpoint contract.

    entity: e.g. "Customer", "Project", "SalesOrder". Returns field names +
    count (from swagger.json). Use before create_or_update_entity to know which
    fields exist on the screen.
    """
    return await _client(instance).get_entity_schema(entity, refresh=refresh)


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


@mcp.tool()
async def extend_endpoint(
    endpoint_name: str,
    endpoint_version: str,
    create_entities: list[dict] | None = None,
    fields: list[dict] | None = None,
    entity_properties: list[dict] | None = None,
    extend_current_endpoint: list[dict] | None = None,
    instance: str | None = None,
) -> Any:
    """[DOES NOT WORK over REST — kept for reference] Attempt to add entities to a
    contract via the WebServiceEndpoints entity.

    Verified on csmdev 2025R1: a PUT here is a NO-OP. WebServiceEndpoints (SM207060)
    is a STATEFUL WIZARD form, not a CRUD entity -- CreateEntity/EntityProperties are
    transient working-views (empty on read), the EntityTree encodes internal screen
    node IDs the form generates, and the create/extend ops (Insert, ExtendEntity,
    PopulateFields, Save) are container actions whose parameters aren't in the
    contract and which need live form state that doesn't survive stateless calls.

    To actually extend an endpoint, use one of:
      - Playwright on the SM207060 UI (drives the real wizard), or
      - a customization project: import_customization + publish_customization.

    Reading a contract works fine: use get_endpoint_definition.
    (allow_publish gate retained in case a future build makes this writable.)
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
    instance: str | None = None,
) -> Any:
    """Retrieve one or many records of a top-level entity.

    entity: endpoint entity name, e.g. "Customer", "SalesOrder", "Bill".
    record_id: fetch a single record by its key/id; omit to list.
    filter/select/expand/top/skip: OData-style query options (contract API $filter,
            $skip for paging the next page of a large list, etc).
    custom: $custom param to pull fields NOT in the contract (unexposed elements /
            user-defined fields), format "<View>.<Field>" comma-separated.

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

    client = _client(instance)
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
    instance: str | None = None,
) -> Any:
    """Retrieve ALL records of an entity, auto-paging with $top/$skip.

    The contract API caps a single list GET, so a plain get_entity can silently
    return only the first page of a big table. This loops $skip until the last
    (short) page, concatenating results.

    page_size: rows per request ($top). max_records: hard cap to stop early
    (None = no cap). Use filter/select to scope/shrink. Returns {count, records}.
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
    rows = await _client(instance).get_all(
        entity, params, page_size=page_size, max_records=max_records
    )
    return {"entity": entity, "count": len(rows), "records": rows}


@mcp.tool()
async def create_or_update_entity(
    entity: str,
    fields: dict,
    instance: str | None = None,
) -> Any:
    """Create or update a record (PUT). Acumatica upserts by key fields.

    entity: e.g. "Customer", "SalesOrder".
    fields: plain field->value map; scalars are auto-wrapped. Detail lines go in
            a list, e.g. {"OrderType": "SO", "CustomerID": "ABC",
                          "Details": [{"InventoryID": "ITEM1", "OrderQty": 2}]}.

    Requires the instance's "allow_write": true (default is read-only).
    """
    _require_write(instance)
    return await _client(instance).put_entity(entity, _wrap_fields(fields))


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
async def load_from_excel(
    entity: str,
    path: str,
    column_map: dict | None = None,
    sheet: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    stop_on_error: bool = False,
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

    limit caps rows processed; stop_on_error aborts on the first failed row.
    Tip: run get_entity_schema(entity) first to get exact field names.
    """
    _require_range("limit", limit, 1, 1000000)
    _check_read_path(path, instance)  # sandbox + size cap (read-side guard)
    if not dry_run:
        _require_write(instance)
    headers, rows = read_rows(path, sheet)
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
            "note": (
                "Schema validation FAILED — could not confirm field names; fix the "
                "error and re-run before loading. No data written."
                if schema_error
                else "No data written. Resolve unknown_fields (fix column_map), "
                "then re-run with dry_run=false."
            ),
        }

    created, errors = 0, []
    for i, fields in enumerate(mapped, start=2):  # row 2 = first data row
        try:
            await client.put_entity(entity, _wrap_fields(fields))
            created += 1
        except Exception as e:
            errors.append({"row": i, "error": str(e)[:300], "fields": fields})
            if stop_on_error:
                break
    return {
        "dry_run": False,
        "entity": entity,
        "processed": created + len(errors),
        "succeeded": created,
        "failed": len(errors),
        "errors": errors[:50],
    }


@mcp.tool()
async def delete_entity(entity: str, record_id: str, instance: str | None = None) -> Any:
    """Delete a record by its id (the record's key GUID or keys path).

    Requires the instance's "allow_delete": true (default off, stricter than write).
    """
    _require_delete(instance)
    return await _client(instance).delete_entity(entity, record_id)


@mcp.tool()
async def count_entity(
    entity: str,
    filter: str | None = None,
    select: str | None = None,
    instance: str | None = None,
) -> Any:
    """Count records of an entity (optionally scoped by filter).

    NOTE: the contract API has no server-side $count, so this fetches matching
    rows (auto-paging with $skip so big tables aren't under-counted) and counts
    them. Pass select=<a key field> to shrink the payload, and use filter to scope.
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    rows = await _client(instance).get_all(entity, params)
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
    A caller-supplied `path` must be inside the instance's write_roots (if configured).
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if expand:
        params["$expand"] = expand
    cfg = _cfg()
    name = instance or cfg.default
    if path:
        _check_write_path(path, instance)
    data = await _client(instance).get_all(entity, params)

    if not path:
        base = os.path.dirname(os.environ.get("GRP_MCP_CONNECTIONS", "")) or os.getcwd()
        out_dir = os.path.join(base, "snapshots")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{entity}_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    n = len(data) if isinstance(data, list) else (0 if data is None else 1)
    return {"entity": entity, "instance": name, "count": n, "path": path}


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
    instance: str | None = None,
) -> Any:
    """Invoke an action on a record (e.g. Release, ConfirmShipment).

    entity: entity the action belongs to, e.g. "SalesOrder".
    action: action name, e.g. "Release".
    entity_ref: identifies the target record, e.g. {"OrderType": "SO", "OrderNbr": "000123"}.
    parameters: optional action parameters.
    Returns 202 + a Location to poll for long-running actions.

    Requires the instance's "allow_write": true (actions mutate ERP state).
    """
    _require_write(instance)
    body = {"entity": _wrap_fields(entity_ref), "parameters": _wrap_fields(parameters or {})}
    return await _client(instance).invoke_action(entity, action, body)


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
    instance: str | None = None,
) -> Any:
    """Query a single DAC through the DAC-based OData v4 interface.

    dac: the DAC OData name from list_dacs (e.g. "PX_Objects_GL_GLTran", "Account").
    filter/select/expand/top/skip: OData v4 query options ($filter, $select, ...).
    Read-only. Use this to read tables/screens NOT exposed on the contract endpoint
    (the contract API only sees entities added to the endpoint). Requires `tenant`.
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
    return await _client(instance).run_dac(dac, params)


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
    instance: str | None = None,
) -> Any:
    """Publish one or more customization projects (async begin + poll until done).

    WARNING: website-level — recompiles the site and affects ALL tenants on the
    instance. tenant_mode: Current | All | List (with tenant_login_names).
    `options` passes extra publishBegin flags (e.g. merge/db-script options).
    Requires the instance's profile to have "allow_publish": true.
    """
    _require_publish(instance)
    async with _customization(instance) as c:
        return await c.publish(
            project_names,
            tenant_mode=tenant_mode,
            tenant_login_names=tenant_login_names,
            options=options,
        )


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
