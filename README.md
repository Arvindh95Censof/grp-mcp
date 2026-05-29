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
| `list_published` | List published customization projects (read-only). |
| `import_customization` | Import a customization `.zip` (does not publish). |
| `publish_customization` | Publish projects (async begin + poll). |
| `unpublish_customization` | Unpublish all customization projects (rollback). |

Every tool takes an optional `instance` arg to pick a connection; defaults to the
configured default instance.

The data tools use the **contract REST API** over OAuth2. The customization tools
use the **Customization Web API** over a cookie session (it rejects OAuth bearer);
both reuse the same credentials from your config.

### Publishing customization projects

Publishing is **website-level — it recompiles the site and affects ALL tenants**
on the instance, not just one. As a safety gate, `publish_customization`,
`import_customization`, and `unpublish_customization` are refused unless the
instance profile sets `"allow_publish": true`. Keep it `false` on prod profiles.

`publish_customization` runs the async flow automatically (`publishBegin` → poll
`publishEnd` until `isCompleted`). `tenant_mode` is `Current` (default), `All`, or
`List` (with `tenant_login_names`).

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

### 3. Configure (pick one)

Credentials are read **once at server startup**, in this priority order:

1. `GRP_MCP_CONNECTIONS` env var → path to a `connections.json`
2. `connections.json` in the current working directory
3. `connections.json` in the repo root
4. `.env` file in the current working directory

**Option A — `.env` (simplest, single instance):** copy `.env.example` to `.env`
and fill it in. Only loaded if the server's launch directory is the repo, so it
works best with a launcher that sets `cwd` (see below).

**Option B — `connections.json` (robust, multi-instance):** copy
`connections.example.json` to `connections.json`, add one or more named profiles.
Recommended for distribution because you can point at it with an absolute path
that does not depend on the launch directory.

Both `.env` and `connections.json` are gitignored — never commit real credentials.

### 4. Register with Claude

**Claude Code (CLI)** — user scope, available in all projects. Point at an
absolute `connections.json` so launch directory does not matter:

```bash
claude mcp add grp-mcp -s user \
  -e GRP_MCP_CONNECTIONS=/abs/path/to/connections.json \
  -- /abs/path/to/.venv/Scripts/grp-mcp.exe        # use grp-mcp on macOS/Linux
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "grp-mcp": {
      "command": "grp-mcp",
      "cwd": "C:\\path\\to\\grp-mcp",
      "env": { "GRP_MCP_CONNECTIONS": "C:\\path\\to\\grp-mcp\\connections.json" }
    }
  }
}
```

(`cwd` lets `.env` load; the `env` line makes `connections.json` work regardless.
Use one config method — you don't need both files.)

Restart the client after adding — tools load at startup.

## Notes

- Auth: OAuth2 resource-owner-password grant. Tokens auto-refresh.
- `endpoint_version` defaults to `24.200.001`; set it to match your instance's
  Default endpoint version (System → Web Service Endpoints).
- Generic Inquiries are read via OData and need the `tenant` (company login) set.
- Actions may return `202` + a `Location` header for long-running work; polling
  that location is not yet automated.

## Status

v0.1 — early. Roadmap: action polling, attachments.

## AFS Financial Report entities (instance-specific)

These custom entities are exposed on the **`Default2025` / `25.200.001`** endpoint
of the AFS Financial Report customization (`AFSCPFinancialReportv213032026`). They
are not part of grp-mcp itself — they were added in SM207060 and are reachable
through the generic tools. Documented here as a usage reference.

| Entity | Key field | Detail collection (use as `expand`) |
|--------|-----------|-------------------------------------|
| `FLRTReportDefinition` | `DefinitionCode` | `LineItems` → report line items |
| `FLRTGIDataSource` | `DataSourceCode` | `GIDataSource` → GI column defs |
| `FLRTFinancialReport` | `ReportCD` | `DefinitionLinks` → linked definitions |
| `FLRTPresentationGeneration` | `PresentationCD` | `PresentationDataSourceLink` → linked data sources |
| `FLRTTenantCredentials` | `CompanyNumber` | — |

Notes:
- Detail/expand names are **as configured in the endpoint**, which differ from the
  DAC view names (e.g. `GIDataSource` and `PresentationDataSourceLink` were renamed
  from `Columns` / `DataSourceLinks`). Pull the live names from
  `GET {entity_base}/swagger.json` if unsure.
- Endpoint field names are display-based (`DefinitionCode` not `DefinitionCD`,
  `VisibleinReport` not `IsVisible`). String-list fields take the **label**
  (`ReportType` = `"Balance Sheet"`, `"Custom"`).
- Example: `get_entity("FLRTReportDefinition", filter="DefinitionCode eq 'BALANCE_SHEET'", expand="LineItems")`.
- Process actions exist on some entities (e.g. `GenerateReport`, `DetectColumns`)
  — call via `invoke_action`.
- Security: `FLRTTenantCredentials` may expose secret fields (`ClientSecret`,
  `Password`) over REST — drop those from the endpoint if not needed.
- Web Service Endpoints can only be created/edited in the SM207060 UI; there is no
  REST API to build them.
