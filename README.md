# grp-mcp

MCP server that exposes **Acumatica ERP** (contract-based REST API) as tools for
AI agents. Multi-instance, OAuth2. Point it at any Acumatica site by giving it a
base URL + OAuth credentials.

## Tools

**Discovery / metadata**

| Tool | What it does |
|------|--------------|
| `list_instances` | List configured profiles + which is active (no secrets). |
| `add_instance` | Add/replace a connection profile and save it to connections.json. |
| `set_active_instance` | Choose the default profile (session, or persisted). |
| `remove_instance` | Remove a profile (and drop its cached session). |
| `test_connection` | Verify a profile's credentials (token + contract read). |
| `list_endpoints` | List all web service endpoints on the instance (name/version). |
| `list_entities` | List top-level entities of the configured endpoint (via swagger.json). |
| `get_entity_schema` | Fields of one entity, split into scalar vs detail (nested). `deep=true` returns the full tree with every detail tab expanded to its nested fields. |
| `list_actions` | Actions invokable on an entity (for `invoke_action`). |
| `list_generic_inquiries` | Generic Inquiries exposed via OData (name + url). |
| `list_dacs` | List every DAC exposed via the DAC-based OData v4 interface. |
| `get_dac_metadata` | Read a DAC's field definitions from the OData CSDL ($metadata) incl. mandatory flags (`Nullable=false`/key). Covers single-row config DACs `run_dac_odata` can't. |

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
| `setup_data_provider` | Create + fully configure a Data Provider (SM206015) from a data file (schema written directly from its header; optional file upload). |
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
| `extend_endpoint` | **Verified no-op over REST** — kept for reference; extend endpoints via the SM207060 UI / playwright or a customization project instead. |

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

### Managing profiles at runtime

Every tool takes an optional `instance` arg to pick a profile; without it, the
**active** profile is used. You can manage profiles without hand-editing the file:

- `list_instances` — see all profiles, their endpoint/tenant/gates, and which is active.
- `add_instance(name, base_url, client_id, client_secret, username, password, …)` —
  register a new profile (e.g. a second Acumatica site) and save it to
  connections.json. Gates default to read-only; pass `set_active=true` to switch to it.
- `set_active_instance(name)` — change the default profile for subsequent calls
  (`persist=true` also writes it as `default` in the file so it survives a restart).
- `remove_instance(name)` — drop a profile and its cached session.
- `test_connection(instance)` — confirm a profile's OAuth creds actually work.

Each profile needs its **own** Connected Application registered on **that** instance
(Integration → Connected Applications, Resource-Owner-Password flow). Because the
SSRF guard pins the OAuth token to each profile's own origin, you cannot reach a host
that isn't a configured profile — add it first. connections.json is gitignored, so
saved secrets never leave the machine.

### Config UI (localhost)

Prefer a page over JSON/tools? Run the bundled config UI:

```bash
grp-mcp-ui            # or: python -m grp_mcp.ui
# -> http://127.0.0.1:8765
```

A single-file, dependency-free (stdlib `http.server`) page to **list / add / edit /
set-active / remove / test** profiles, writing the same connections.json. **First run
needs no config file** — on a fresh machine the page opens with an empty list; add your
first profile in the browser and it creates connections.json for you (no JSON editing).
It binds to `127.0.0.1` only (it edits credentials) and **never sends secrets to the
browser** —
the profile list only reports whether a secret/password is set. Leave the secret and
password blank when editing to keep the existing values. Because the MCP server reads
config at startup. To apply add/active changes to the live connector **without a
restart**, run the `reload_config` tool in Claude (it re-reads connections.json and
frees old sessions). Restarting the MCP also works. (Test works immediately — it opens
its own session.)

The header shows a build marker (e.g. `build 2`); if you don't see it after editing,
you're on a cached page or an old server process. Responses send `Cache-Control:
no-store`, so a hard refresh (Ctrl+Shift+R) is enough. If the page is blank or the
port won't bind, a previous instance is still holding it — find and stop it:

```bash
# Windows:  netstat -ano | findstr :8765   then   taskkill /F /PID <pid>
# macOS/Linux:  lsof -ti:8765 | xargs kill
```

### Bulk loading from Excel/CSV

`load_from_excel` turns a master file (Chart of Accounts, sub-account values,
trial balance, …) into one call instead of hundreds of `create_or_update_entity`.
The first row is the header; `column_map` maps a header to an entity field name
(omit to use headers verbatim, or map to `""` to ignore a column). It defaults to
`dry_run=true` — it parses, maps, and validates field names against the schema and
returns a preview **without writing**; re-run with `dry_run=false` to load. Only
scalar fields are supported (no nested detail rows).

### Extending an endpoint contract

`extend_endpoint` is a **verified no-op over REST** and is kept only for reference.
`WebServiceEndpoints` (SM207060) is a stateful wizard form — its create/extend views
are transient and a PUT does nothing. To actually add entities/fields/actions to an
endpoint, use the **SM207060 UI** (drive it with the playwright scripts in
`playwright/`) or a **customization project** (`export_customization` → edit
`project.xml` → `import_customization` → `publish_customization`). Reading a contract
works fine via `get_endpoint_definition`.

### Security model

This server holds ERP credentials and runs with the host user's privileges, so the
tools are sandboxed:

- **Token never leaves the instance.** Every authenticated request is checked
  against the configured origin (`scheme://host`); a `poll_action`/download URL on
  any other host is refused (prevents OAuth-token exfiltration / SSRF).
- **Writes are opt-in.** Record mutations (`create_or_update_entity`,
  `load_from_excel`, `invoke_action`, `run_import_scenario`, `set_note`,
  `attach_file`) require `"allow_write": true`; `delete_entity` requires the stricter
  `"allow_delete": true`; customization publish/import/unpublish require
  `"allow_publish": true`. **Default is read-only.**
- **Filesystem is fenced.** Tools that read (`attach_file`, `import_customization`,
  `load_from_excel`) or write (`download_file`, `run_report`, `snapshot_entity`,
  `export_customization`) a local path enforce `read_roots` / `write_roots` (a path
  must sit inside an allowed dir if the list is set) and a `max_file_bytes` size cap
  on reads. Leave the root lists empty only on a trusted single-user host.
- **Bounded loops.** Pagination and polling arguments are range-checked, so a
  `page_size`/`poll_interval` of 0 can't spin forever.
- **Sessions released.** Token refreshes are serialized (one login, not N), and API
  sessions are logged out on shutdown to free license seats.

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

**Mandatory-field discovery — `get_dac_metadata`.** `run_dac_odata` only reads DACs
exposed as OData *collections*; single-row config DACs (e.g. `GLSetup` = GL
Preferences, `FinYearSetup` = Financial Year) serve no collection route and 404.
`get_dac_metadata` reads the DAC OData CSDL (`<dac base>/$metadata`) instead, which
describes **every** DAC's fields — name, type, key, and `Nullable` flag. A field with
`Nullable="false"` (or a key field) is **mandatory** at the DB level. Args: `dac`
(filter to one entity type, case-insensitive; omit for all), `mandatory_only` (return
only required fields), `raw` (return the CSDL XML verbatim). The parser matches CSDL
tags by local name, so it's namespace/OData-version-proof.

Two gotchas it works around: this platform's OData layer **500s on JSON metadata**
("only supported at platform implementing .NETStandard 2.0") and ignores `$format`, so
the tool requests `Accept: application/xml`. And `Nullable=false` is the **DB-enforced**
required set — graph-validated business-required fields (e.g. GL Preferences' Retained
Earnings / YTD Net Income accounts) are `Nullable=true` here and won't show; cross-check
the screen's KB form reference for those.

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

v0.2 — 38 tools (incl. runtime profile management). Covers the contract REST API (CRUD, actions, `$skip` paging,
attachments up/down, notes, reports), DAC + GI OData (incl. CSDL metadata / mandatory-field
discovery), import scenarios, and the
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
