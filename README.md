# grp-mcp

MCP server that exposes **Acumatica ERP** (contract-based REST API) as tools for
AI agents. Multi-instance, OAuth2. Point it at any Acumatica site by giving it a
base URL + OAuth credentials.

## Tools

| Tool | What it does |
|------|--------------|
| `list_instances` | List configured instances (no secrets). |
| `get_entity` | Get one record or a filtered list of an entity. |
| `create_or_update_entity` | Create/update a record (PUT, upsert by key). |
| `delete_entity` | Delete a record by id. |
| `invoke_action` | Run a record action (Release, ConfirmShipment, …). |
| `run_generic_inquiry` | Run a Generic Inquiry via OData. |

Every tool takes an optional `instance` arg to pick a connection; defaults to the
configured default instance.

## Setup

### 1. Acumatica: register a Connected Application (OAuth2)

In Acumatica: **Integration → Connected Applications**. Create one with the
**Resource Owner Password Credentials** flow enabled. Note the **Client ID**
(looks like `GUID@CompanyLogin`) and **Client Secret**.

### 2. Install

```bash
git clone https://github.com/Arvindh95/grp-mcp.git
cd grp-mcp
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e .
```

### 3. Configure

Single instance — copy `.env.example` to `.env` and fill it in.

Multiple instances — copy `connections.example.json` to `connections.json` and
add named profiles. `connections.json` and `.env` are gitignored.

### 4. Register with Claude

Add to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "grp-mcp": {
      "command": "grp-mcp",
      "cwd": "C:\\Temp\\grp-mcp"
    }
  }
}
```

(Or `"command": "python", "args": ["-m", "grp_mcp.server"]`.)

## Notes

- Auth: OAuth2 resource-owner-password grant. Tokens auto-refresh.
- `endpoint_version` defaults to `24.200.001`; set it to match your instance's
  Default endpoint version (System → Web Service Endpoints).
- Generic Inquiries are read via OData and need the `tenant` (company login) set.
- Actions may return `202` + a `Location` header for long-running work; polling
  that location is not yet automated.

## Status

v0.1 — early. Roadmap: action polling, AFS project setup tools, attachments.
