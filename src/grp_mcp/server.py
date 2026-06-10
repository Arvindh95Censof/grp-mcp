"""grp-mcp MCP server.

Exposes Acumatica's contract-based REST API as MCP tools. All tools accept an
optional `instance` argument selecting a configured connection; when omitted the
default instance is used.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .acumatica import AcumaticaClient
from .config import Config, load_config
from .customization import CustomizationClient, encode_zip

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
    """Convert plain values into Acumatica's {"value": ...} envelope.

    Dicts are assumed already shaped (linked entity / wrapped field) and pass
    through; lists are treated as detail rows and wrapped element-wise.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return [{k: _wrap(v) for k, v in row.items()} if isinstance(row, dict) else row
                for row in value]
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
async def get_entity(
    entity: str,
    record_id: str | None = None,
    filter: str | None = None,
    select: str | None = None,
    expand: str | None = None,
    top: int | None = None,
    instance: str | None = None,
) -> Any:
    """Retrieve one or many records of a top-level entity.

    entity: endpoint entity name, e.g. "Customer", "SalesOrder", "Bill".
    record_id: fetch a single record by its key/id; omit to list.
    filter/select/expand/top: OData-style query options (contract API $filter etc).
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

    client = _client(instance)
    result = await client.get_entity(entity, record_id, params)

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
async def delete_entity(entity: str, record_id: str, instance: str | None = None) -> Any:
    """Delete a record by its id (the record's key GUID or keys path)."""
    return await _client(instance).delete_entity(entity, record_id)


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
async def list_published(instance: str | None = None) -> Any:
    """List customization projects currently published on the instance (read-only)."""
    async with _customization(instance) as c:
        return await c.get_published()


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
