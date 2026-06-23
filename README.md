# grp-mcp

MCP server that exposes **Acumatica ERP** (contract-based REST API) as tools for
AI agents. Multi-instance, OAuth2. Point it at any Acumatica site by giving it a
base URL + OAuth credentials.

## Tools

**Discovery / metadata**

| Tool | What it does |
|------|--------------|
| `list_instances` | List configured instances (no secrets). |
| `list_endpoints` | List all web service endpoints on the instance (name/version). |
| `list_entities` | List top-level entities of the configured endpoint (via swagger.json). |
| `get_entity_schema` | Fields of one entity, split into scalar vs detail (nested). |
| `list_actions` | Actions invokable on an entity (for `invoke_action`). |
| `list_generic_inquiries` | Generic Inquiries exposed via OData (name + url). |
| `list_dacs` | List every DAC exposed via the DAC-based OData v4 interface. |

**Read**

| Tool | What it does |
|------|--------------|
| `get_entity` | Get one record or a filtered list; supports `$filter/$select/$expand/$top/$skip/$custom`. |
| `fetch_all_entities` | Retrieve **all** records of an entity, auto-paging with `$top/$skip`. |
| `count_entity` | Count records (client-side, auto-paged; scope with `filter`). |
| `run_generic_inquiry` | Run a Generic Inquiry via OData. |
| `run_dac_odata` | Query a single DAC via OData v4 (reaches tables **not** on the endpoint). |
| `list_attachments` | List files attached to a record (name + download href). |
| `download_file` | Download a record's attached file to disk. |
| `get_endpoint_definition` | Read an endpoint's contract (entity tree/props) from SM207060. |

**Write**

| Tool | What it does |
|------|--------------|
| `create_or_update_entity` | Create/update a record (PUT, upsert by key). |
| `load_from_excel` | Bulk upsert an entity from `.xlsx`/`.csv` with column mapping + dry-run. |
| `attach_file` | Upload a file and attach it to a record (`files:put`). |
| `set_note` | Set/clear a record's Note text. |
| `delete_entity` | Delete a record by id. |
| `invoke_action` | Run a record action (Release, ConfirmShipment, …). |
| `run_import_scenario` | Drive Import-by-Scenario (SM206036): prepare (+ optional import). |
| `run_report` | Run a Report-type entity and save the rendered file (PDF) to disk. |
| `poll_action` | Check a long-running action's status by its `Location`. |

**Contract / config**

| Tool | What it does |
|------|--------------|
| `extend_endpoint` | Add entities/fields to a contract via the `WebServiceEndpoints` entity (gated). |

**Safety**

| Tool | What it does |
|------|--------------|
| `snapshot_entity` | Dump an entity to JSON before risky changes (rollback aid). |

**Customization Web API**

| Tool | What it does |
|------|--------------|
| `list_published` | List published customization projects (read-only). |
| `export_customization` | Export a project to a `.zip` on disk (headless edit loop). |
| `import_customization` | Import a customization `.zip` (does not publish). |
| `publish_customization` | Publish projects (async begin + poll). |
| `unpublish_customization` | Unpublish all customization projects (rollback). |

Every tool takes an optional `instance` arg to pick a connection; defaults to the
configured default instance.

The data tools use the **contract REST API** over OAuth2. The customization tools
use the **Customization Web API** over a cookie session (it rejects OAuth bearer);
both reuse the same credentials from your config.

### Bulk loading from Excel/CSV

`load_from_excel` turns a master file (Chart of Accounts, sub-account values,
trial balance, …) into one call instead of hundreds of `create_or_update_entity`.
The first row is the header; `column_map` maps a header to an entity field name
(omit to use headers verbatim, or map to `""` to ignore a column). It defaults to
`dry_run=true` — it parses, maps, and validates field names against the schema and
returns a preview **without writing**; re-run with `dry_run=false` to load. Only
scalar fields are supported (no nested detail rows).

### Extending an endpoint contract

`extend_endpoint` adds top-level entities/fields to an existing endpoint by writing
to the `WebServiceEndpoints` entity (the SM207060 form projected over REST). This
is a **website-level contract change** — gated behind `"allow_publish": true`. It
is experimental: the underlying form is wizard-driven, so not every screen maps
cleanly in a single PUT. Read first with `get_endpoint_definition`, snapshot, and
test on a throwaway endpoint — never the one the server is actively using.

### Paging large tables

The contract API caps a single list GET, so a plain `get_entity` (no `record_id`)
can silently return **only the first page** of a big table. Two fixes:

- `get_entity` accepts `$skip` (the `skip` arg) to grab the next page manually.
- `fetch_all_entities` loops `$top`/`$skip` until the last (short) page and returns
  `{count, records}` — use it whenever you need the **whole** table (full Chart of
  Accounts, all vendors, …). `page_size` sets rows per request; `max_records` caps
  early. `count_entity` and `snapshot_entity` auto-page too, so counts and snapshots
  cover the full table rather than page 1.

### DAC-based OData (data not on the endpoint)

The contract API only sees entities that were added to the endpoint in SM207060.
`list_dacs` + `run_dac_odata` reach data **directly from DACs** through the
DAC-based OData v4 interface (`<base>/t/<Tenant>/api/odata/dac/<DAC>`), bypassing
the endpoint entirely — handy for reading a screen/table you haven't exposed.
Read-only, and it needs the `tenant` (company login) set in config. `run_dac_odata`
supports `$filter/$select/$expand/$top/$skip`. Note `list_dacs` can return thousands
of DACs; it's best browsed with a known DAC name in hand.

### Attachments and reports

- `attach_file` uploads a file onto a record (`files:put`); `list_attachments`
  lists what's attached (name + href); `download_file` pulls an attachment to disk.
- `run_report` runs a **Report-type** endpoint entity: it PUTs the report with its
  parameters, polls the returned `Location` until the render completes, and writes
  the file (usually PDF) to disk. The report must first be added to the endpoint as
  a Report entity (see it in `list_entities`).

### Detail-field guard

A list GET (no `record_id`) cannot return detail/nested collections — Acumatica
silently omits them. `get_entity` detects when a list query asks for a detail field
via `expand`/`select` and returns a `_warning` explaining the field is absent and
how to fetch it (per record, by key). `get_entity_schema` labels which fields are
detail so you know up front.

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
git clone https://github.com/Arvindh95Censof/grp-mcp.git
cd grp-mcp
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e .
```

Dependencies install automatically (`mcp`, `httpx`, `pydantic`, `python-dotenv`,
`openpyxl`). Note: the `.venv` is **not relocatable** — if you move the repo,
recreate the venv and `pip install -e .` at the new path, then update the launcher.

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
- Actions may return `202` + a `Location` for long-running work — check it with
  `poll_action` (204 = finished, 202 = still running).
- `snapshot_entity` writes to `<connections dir>/snapshots/` by default; that
  folder is gitignored (it can contain business data).

## Status

v0.2 — 31 tools. Covers the contract REST API (CRUD, actions, `$skip` paging,
attachments up/down, notes, reports), DAC + GI OData, import scenarios, and the
Customization Web API. By-design gap: endpoint **writes** (SM207060) are a stateful
wizard — do those via the SM207060 UI / playwright or a customization project, not
REST. Roadmap: nested detail rows in `load_from_excel`.

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
- Web Service Endpoints are editable in the SM207060 UI and, on newer builds, via
  the `WebServiceEndpoints` entity (see `extend_endpoint` / `get_endpoint_definition`).
  The entity is a projection of the wizard-driven form, so complex contracts are
  still more reliably built in the UI.
