"""grp-mcp MCP server.

Exposes Acumatica's contract-based REST API as MCP tools. All tools accept an
optional `instance` argument selecting a configured connection; when omitted the
default instance is used.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .acumatica import AcumaticaClient
from .config import Config, load_config
from .customization import CustomizationClient, encode_zip

mcp = FastMCP("grp-mcp")

_config: Config | None = None
_clients: dict[str, AcumaticaClient] = {}
_cust_clients: dict[str, CustomizationClient] = {}


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


def _cust_client(instance: str | None) -> CustomizationClient:
    cfg = _cfg()
    name = instance or cfg.default
    if name not in _cust_clients:
        _cust_clients[name] = CustomizationClient(cfg.get(name))
    return _cust_clients[name]


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
    return await _client(instance).get_entity(entity, record_id, params)


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
    return await _cust_client(instance).get_published()


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
    return await _cust_client(instance).import_project(
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
    return await _cust_client(instance).publish(
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
    return await _cust_client(instance).unpublish_all(tenant_mode, tenant_login_names)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
