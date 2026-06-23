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
from .config import Config, load_config
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
        # a nested / linked object -> recurse into its fields
        return {k: _wrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap(row) if isinstance(row, dict) else row for row in value]
    return {"value": value}


def _wrap_fields(fields: dict) -> dict:
    return {k: _wrap(v) for k, v in fields.items()}


@mcp.tool()
def list_instances() -> list[dict]:
    """List configured Acumatica instances (names + base URLs, no secrets)."""
    cfg = _cfg()
    return [
        {"name": n, "base_url": i.base_url, "default": n == cfg.default}
        for n, i in cfg.instances.items()
    ]


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
        return {
            "_warning": (
                f"'{entity}' has BQL-delegate views that break optimized list "
                f"queries; retried after dropping {dropped}. Those options were "
                f"ignored. Fetch a single record by key to use them. "
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
    """
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
    """
    import mimetypes
    from pathlib import Path

    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {file_path}")
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
    headers, rows = read_rows(path, sheet)
    if limit:
        rows = rows[:limit]
    mapped = [m for m in (map_row(r, column_map) for r in rows) if m]

    client = _client(instance)

    if dry_run:
        unknown: list[str] = []
        try:
            sch = await client.get_entity_schema(entity)
            valid = set(sch["scalar_fields"]) | set(sch["detail_fields"])
            used = {k for m in mapped for k in m}
            unknown = sorted(used - valid)
        except Exception:
            pass
        return {
            "dry_run": True,
            "entity": entity,
            "file_headers": headers,
            "row_count": len(mapped),
            "unknown_fields": unknown,
            "sample": mapped[:5],
            "note": ("No data written. Resolve unknown_fields (fix column_map), "
                     "then re-run with dry_run=false."),
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
    """Delete a record by its id (the record's key GUID or keys path)."""
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
    """
    params: dict[str, Any] = {}
    if filter:
        params["$filter"] = filter
    if expand:
        params["$expand"] = expand
    cfg = _cfg()
    name = instance or cfg.default
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
    """
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
    """
    import asyncio

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
    """
    from pathlib import Path

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
    Path(out_path).write_bytes(data)
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
    """
    from pathlib import Path

    body: dict[str, Any] = {}
    if parameters:
        body["parameters"] = _wrap_fields(parameters)
    data = await _client(instance).run_report(
        report_entity, body, poll_interval=poll_interval, timeout=timeout
    )
    Path(out_path).write_bytes(data)
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
    """
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
    """
    import base64
    from pathlib import Path

    async with _customization(instance) as c:
        res = await c.get_project(project_name)
    content = res.get("projectContentBase64") if isinstance(res, dict) else None
    if not content:
        return {"error": "no projectContentBase64 in response", "project": project_name,
                "raw": res}
    data = base64.b64decode(content)
    Path(out_path).write_bytes(data)
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
    mcp.run()


if __name__ == "__main__":
    main()
