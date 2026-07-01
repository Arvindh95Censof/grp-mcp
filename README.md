# grp-mcp

MCP server that exposes **Acumatica ERP** as tools for AI agents. Multi-instance,
OAuth2 — point it at any Acumatica site with a base URL + credentials.

It reaches Acumatica through **three client planes** (plus a narrow fourth for
one confirmed platform gap), so an agent can read and write almost anything:

- **Contract-based REST** — CRUD entities, bulk-load from Excel/CSV, invoke
  actions, run reports, attach files, manage customization projects.
- **DAC-based OData** — read tables/DACs that aren't on the endpoint, plus
  mandatory-field metadata (`run_dac_odata`, `get_dac_metadata`).
- **Screen-based SOAP engine** — *drive screens the REST API can't*: context /
  master-detail / wizard screens (segment values, Enable Features, the financial
  calendar…). Discover (`list_screens`, `screen_get_schema`), read (`screen_get`),
  and write (`screen_submit`, with dry-run + per-field errors + dialog
  auto-answer) any screen by replaying its UI commands — no browser, no zeep.
  Higher-level writers (`screen_insert_rows` for master-detail/bulk grids,
  `screen_record` for idempotent create-or-edit) and ready-made setup recipes
  (`create_financial_calendar`, `create_ledger`, `chart_of_accounts`,
  `enable_features`, `manage_financial_periods`) sit on top.
- **Modern UI-screen plane** (internal, not a public tool) — for the rare action
  whose classic-SOAP schema tag is a confirmed no-op (currently just GL201000
  "Generate Calendar"), `ScreenClient.ui_command`/`ui_set_field` drive the same
  JSON protocol the real browser UI uses (`/t/<Tenant>/ui/screen/<ScreenID>`),
  reusing the SAME login session as the classic plane — no extra auth, no
  browser. `generate_master_calendar` is the one recipe built on it so far.

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
| `setup_readiness` | Report instance setup state: enabled features + **activation/install status** (Validated vs staged), per-module config gaps (ledger, CoA, customer/vendor class, …), financial calendar, **GL Preferences system accounts**, and **open periods** — the GL-phase gates — vs the implementation checklist. |

**Write**

| Tool | What it does |
|------|--------------|
| `create_or_update_entity` | Create/update a record (PUT, upsert by key). |
| `load_from_excel` | Bulk upsert an entity from `.xlsx`/`.csv` with column mapping + dry-run. |
| `setup_data_provider` | Create + fully configure a Data Provider (SM206015) from a data file (schema written directly from its header; optional file upload). |
| `attach_file` | Upload a file and attach it to a record (`files:put`). |
| `attach_file_to_provider` | Attach a source file to a Data Provider by id — GET-free (works around the `DataProvider` read-back 500). |
| `screen_get_schema` | Discover a screen's command schema via the screen-based SOAP API (containers → fields). |
| `screen_get` | Read current values from a screen via the SOAP Export op (the read counterpart to screen_submit; reaches config singletons/context grids with no DAC route). |
| `screen_submit` | Drive a screen via the screen-based SOAP API — writes screens the contract REST API can't (context/master-detail). Ergonomic set/key/action/new_row specs resolved against the schema (replays the LinkedCommand navigation chains). `dry_run` preview + `auto_answer` to clear confirmation dialogs. Surfaces per-field errors. |
| `screen_insert_rows` | Insert many grid/detail rows into one container in a single Save (master-detail / bulk-grid writer over the SOAP engine) — e.g. Chart of Accounts rows, subaccount segments. |
| `screen_record` | Create (`insert=True`) or edit one record on a master screen by key — idempotent setup helper over the SOAP engine. |
| `screen_preflight` | Check intended fields against a DAC's mandatory fields (OData CSDL), system columns filtered out — catch missing required fields before a Save fault. |
| `release_sessions` | Log out cached API sessions to free Web Service API license seats (trial = 2). |
| `list_screens` | Find a screen's ID by title (searches the site map) — feeds screen_get_schema/get/submit. |
| `whoami` | Active connection identity (user/tenant/endpoint), reachability, and cached sessions holding seats. |
| `enable_features` | Set feature flags on Enable/Disable Features (CS100000) + Save (stages them). Pass `activate=True` to also install them. |
| `activate_features` | Activate/install the staged feature set (CS100000 RequestValidation = the "Enable" button) — recompiles the site, then polls ActivationStatus until "Validated" (returns activated=true/false). |
| `create_financial_calendar` | Create the financial calendar (GL101000): first year → AutoFill → optional start date (`starts_on`, M/D/YYYY — set after AutoFill, dialog auto-answered) → Save. Fully SOAP, no UI. |
| `generate_master_calendar` | Generate financial periods (GL201000 "Generate Calendar") for a year range. Classic SOAP exposes this action's tag but it's a no-op there; this recipe drives the modern UI-screen JSON protocol instead (same login session, no browser). |
| `create_ledger` | Create a GL ledger (GL201500): LedgerID/Description/Type/Currency → Save. |
| `set_gl_preferences` | Set GL Preferences (GL102000): Retained Earnings + YTD Net Income system accounts (Liability) + posting flags → Save. The GL-phase keystone for posting. |
| `chart_of_accounts` | Create Chart of Accounts rows (GL202500) in one transaction (recipe over screen_insert_rows; dialog auto-answered). |
| `create_segmented_key` | Create a segmented key + its segments on Segment Keys (CS202000) — the prerequisite for `set_segment_value`. |
| `set_segment_value` | Add a value to a segment on Segment Values (CS203000) — navigates the header with a descriptor `set` so the value lands in the right segment. Requires the key to exist on CS202000 first. |
| `delete_segmented_key` | Tear down a segmented key in the correct children-first order (values → segment → master); recovers orphaned keys by recreating the master. Single-segment keys only (multi-segment reported for UI). |
| `manage_financial_periods` | Bulk period action on Manage Financial Periods (GL503000) — Open/Close/Lock/Unlock/Reopen/Deactivate a year range in one Process All. Periods must already be generated (`generate_master_calendar`). |
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
are transient and a PUT does nothing. Reading a contract works fine via
`get_endpoint_definition`. Two working ways to actually add entities/fields/actions:

- **Customization project (headless, no browser).** An endpoint can live in a
  customization project as an `<EntityEndpoint>` block in `project.xml`; grp-mcp
  deploys it end-to-end with `export_customization` → edit `project.xml` →
  `import_customization` → `publish_customization` (needs `"allow_publish": true`).
  This is the version-controlled path and the way to clone an endpoint to other
  instances. Verified round-trip + the exact `project.xml` shape (each screen API =
  one `<TopLevelEntity>` with `<Fields>` + `<Mappings>`) are documented in
  [`playwright/EXTENDING_ENDPOINTS.md`](playwright/EXTENDING_ENDPOINTS.md).
- **SM207060 UI** — first-time bootstrap when no project exists yet; drive it with the
  Playwright scripts in `playwright/` (classic `.aspx` and modern `.html` both covered).

### Security model

This server holds ERP credentials and runs with the host user's privileges, so the
tools are sandboxed:

- **Token never leaves the instance.** Every authenticated request is checked
  against the configured origin (`scheme://host`); a `poll_action`/download URL on
  any other host is refused (prevents OAuth-token exfiltration / SSRF).
- **Writes are opt-in.** Record mutations (`create_or_update_entity`,
  `load_from_excel`, `invoke_action`, `run_import_scenario`, `set_note`,
  `attach_file`, `attach_file_to_provider`, `screen_submit`) require
  `"allow_write": true`; `delete_entity` requires the stricter
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

### KB-first CRUD policy

Before **any** create/update/delete (on a screen or an entity), consult the Acumatica
knowledge base (the **kb-mcp** server: `search_kb`, then `read_kb_file`) for that
screen/entity and the specific action — read its prerequisites, dependent screens,
required fields, validation rules, and ordering constraints, then verify each
prerequisite exists in the instance before writing. Pure reads are exempt.

This is stated in the server's MCP `instructions` (so any client is told to do it) and
echoed as a `PRECONDITION` on the write tools. It exists because Acumatica screens have
hard dependencies the screen won't surface until a write fails with a generic, misleading
error — e.g. *Segment Values* (CS203000) requires the key to exist on *Segment Keys*
(CS202000) with a `Validate=ON` segment, and a segmented key must be torn down
children-first (values → segments last-first → master) or it orphans. Driving a screen
cold wastes effort and produces false "this screen is broken" conclusions.

### Writing screens the REST API can't (screen-based SOAP)

The contract REST API addresses records by key and can't write **context screens**
— popup / master-detail / wizard screens whose insert or edit action only enables
once a parent record is loaded. `screen_get_schema` + `screen_submit` drive
Acumatica's screen-based SOAP API (`<base>/Soap/<ScreenID>.asmx`), replaying a UI
command sequence *as a user* so the screen has its context. Pure async httpx — no
zeep (its WSDL dependency is a dead end here: some screens' `?wsdl` 500s while the
SOAP operations themselves work).

- `screen_get_schema(screen_id)` returns the screen's containers → fields (the
  friendly names you reference in commands).
- `screen_submit(screen_id, commands)` runs a sequence of ergonomic command specs:
  - `{"set": "<Field>", "to": <value>}` — set a field (navigates if it's a key)
  - `{"key": "<Field>", "to": <value>}` — select an existing record via a key
  - `{"action": "<Name>"}` — click a button, e.g. `{"action": "Save"}`
  - `{"new_row": "<Container>"}` / `{"delete_row": "<Container>"}` — detail rows
  - `{"answer": "<Container>", "to": "Yes"}` — answer a pop-up dialog

  Qualify a name as `Container.Field` when the same friendly name appears in more
  than one container (the tool errors and lists the options). Example — update a
  customer's name on **Customers** (AR303000):

  ```json
  [{"set": "CustomerSummary.CustomerID", "to": "ABARTENDE"},
   {"set": "CurrentCustomer.AccountName", "to": "USA Bartending School"},
   {"action": "Save"}]
  ```

  Add a detail row: select the parent key(s), `new_row` the detail container, set
  the row's fields, then `{"action": "Save"}`. Read a screen back with
  `screen_get(screen_id, fields, top)` (the SOAP Export op) — e.g. the financial
  calendar periods: `screen_get("GL101000", ["Periods.PeriodNbr","Periods.StartDate","Periods.Description"])`.
  `screen_submit` returns per-field errors in `field_errors` (the API reports
  field problems inside an HTTP 200, and on a fatal action it re-reads the field
  state to surface why); pass `dry_run=true` to preview (drops the Save/Delete so
  nothing persists). `screen_get` takes `filters` (e.g. `[{"field":"CustomerSummary.CustomerID","value":"ABARTENDE"}]`)
  to read one record. Find ScreenIDs with `list_screens("financial year")`.

  Higher-level writers sit on top of the engine so you don't hand-build command
  lists for the common shapes:

  - `screen_insert_rows(screen_id, container, rows, header?, auto_answer?)` — the
    master-detail / bulk-grid writer: one `NewRow` + field SETs per row, all under
    one Save (Chart of Accounts, subaccount segments, GL batch lines).
  - `screen_record(screen_id, key_field, key_value, fields, insert?)` — create
    (`insert=True`) or edit one record by key; an idempotent, re-runnable setup step.
  - `screen_submit(..., auto_answer="Yes")` — retry once with a confirmation dialog
    answered when a Save/Release raises an "Are you sure?" pop-up (only containers
    that actually expose a dialog get one).
  - **No-bind guard** — a persisted Submit returns a tiny (~335-byte) empty result;
    if it instead returns a multi-KB full-content echo (commands didn't bind — e.g.
    navigation didn't select the intended record), the result carries
    `nobind_suspected: true` + a `warning`. `ok` alone is not proof of a write —
    read the record back to confirm.
  - **Navigating to a record**: select it with a descriptor `{"set": "<key>", "to":
    v}` (replays the field's LinkedCommand chain, actually loading the record) — NOT
    a flat `{"key": ...}`, which often leaves the screen on its default record so
    writes land in the wrong place. `set_segment_value` (CS203000) is the worked
    example: `set SegmentedKeyID → NewRow → set Value → Save`.
  - `screen_preflight(dac, provided)` — the screen-based SOAP plane returns **no
    field-state** (no combo option-lists, no required flags — `Submit` echoes an
    empty result), so required-field checking comes from the OData CSDL instead:
    this reports which of a DAC's mandatory fields you haven't supplied (system
    columns filtered out). Treat `missing` as a strong hint, not a hard gate.

  Ready-made recipes over the engine for common setup steps:
  `create_financial_calendar` / `create_ledger` / `enable_features`, and
  `chart_of_accounts(accounts)` to populate GL202500 in one transaction.

How it works (the bit that matters): each command is built by **cloning the
field's descriptor from `GetSchema`**, which carries the `LinkedCommand` navigation
chain that actually loads/navigates the record, then overwriting its value. Bare
hand-built commands omit that chain and silently no-op (Submit returns ok but
nothing persists). Field-level errors come back in `messages` (the API reports them
inside an HTTP 200, not as a fault). `screen_submit` needs `"allow_write": true`;
it opens and closes its own SOAP session per call so it never holds an API license
seat at idle (a trial license allows only 2 — always log out).

### Attachments and reports

- `attach_file` uploads a file onto a record (`files:put`); `list_attachments`
  lists what's attached (name + href); `download_file` pulls an attachment to disk.
- `attach_file_to_provider` attaches a source file to a Data Provider **by id**,
  building the `files:put` URL from a template instead of reading the record first
  — a workaround for the `DataProvider` contract entity, which 500s on read-back
  (its `Link` field carries a BQL delegate). `setup_data_provider` uses the same
  GET-free upload.
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
pip install grp-mcp
```

To upgrade later:

```bash
pip install --upgrade grp-mcp
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
- Actions may return `202` + a `Location` for long-running work — check it with
  `poll_action` (204 = finished, 202 = still running).
- `snapshot_entity` writes to `<connections dir>/snapshots/` by default; that
  folder is gitignored (it can contain business data).

## Status

v0.18 — 60 tools across three client planes (plus a narrow internal fourth): contract
REST (CRUD, actions, `$skip` paging, attachments up/down, notes, reports), DAC + GI OData
(incl. CSDL metadata / mandatory-field discovery), and the **screen-based SOAP engine**
(drives context/master-detail/wizard screens the REST API can't). On top sit setup recipes —
`enable_features` + `activate_features` (install/recompile), `create_financial_calendar`
(incl. start date), `create_ledger`, `create_segmented_key` → `set_segment_value`,
`chart_of_accounts`, `generate_master_calendar` + `manage_financial_periods`
(open/close/lock/reopen/deactivate) — plus import scenarios, the Customization Web API,
and `setup_readiness` (reports feature activation, GL preferences, and open periods).
**The GL foundation chain is now grp-mcp-drivable fully end-to-end, no manual/UI steps
at all** — the one action classic SOAP couldn't reach (GL201000 "Generate Calendar") is
driven by a small internal fourth plane instead (see below).

### The modern UI-screen plane (internal — how `generate_master_calendar` works)

`GL201000`'s "Generate Calendar" exposes a matching action tag (`GenerateYears`) in the
classic typed SOAP schema, but invoking it there is a confirmed no-op: a clean, empty
~335-byte success every time, with zero effect, verified empty across three independent
read channels. Network capture during a manual UI click showed why — the browser calls a
*different* endpoint entirely, `/t/<Tenant>/ui/screen/<ScreenID>` (the modern UI's own
JSON protocol), not the classic `/Soap/<ScreenID>.asmx` this engine's `ScreenClient` uses.
The classic SOAP shim advertises the action; its handler for this one isn't wired up on
this platform build.

Reverse-engineered live and now built as `ScreenClient.ui_command`/`ui_set_field`:
- Reuses the **same login session** as the classic plane (same cookie — no separate auth).
- Set a field: `POST {"data":[{"viewName":V,"fieldName":F,"value":val,"rowId":"","changeType":5}],...}`
- Fire an action: `POST {"command":[{"name":cmd}],"data":[],...}` → `200` if it just runs,
  or `302 {"redirects":[{"settings":{"type":"openDialog","viewName":V}}]}` if it needs
  confirmation — answered with `{"command":[{"name":cmd}],"dialogCallback":
  {"dialogResult":1,"viewName":V},...}` (`dialogResult` follows the public
  `PX.Data.WebDialogResult` enum: OK=1, Yes=6, No=7, Cancel=2, ...).

Proven end-to-end (`FinPeriod` DAC read-back, matching timestamps): generated periods for
2027 and 2028 in one call. Real Acumatica errors (e.g. a business-rule rejection) surface
as clean exceptions through this path too — it isn't just a silent-success trap like the
classic-SOAP call was. Kept deliberately narrow (not a public tool) — reach for it only
when a specific action is confirmed classic-SOAP-dead, the same bar as everything in
Known limitations below.

### Known limitations (by design / platform)

- **Endpoint writes (SM207060)** are a stateful wizard — extend entities via the UI /
  customization project, not REST.
- **Multi-segment segmented-key deletion** is impossible via *any* API (and via the UI):
  Acumatica requires ≥1 segment and won't cascade-delete a key's segments, so the last
  segment always orphans. `delete_segmented_key` handles single-segment keys + safely
  stops on multi-segment (KB-confirmed; structural change = "contact support").
- **Tenant snapshot (SM203520)** is UI-only — the create dialog's action is server-gated
  behind a client-opened panel the typed SOAP API can't reach, and it requires maintenance
  mode (which locks the instance). Left as a deliberate manual step. (Unlike GL201000's
  gap, this one is genuinely client-gated even in the modern UI, not just missing from the
  classic shim — not yet attempted via the `/ui/screen/` plane.)
- **Combo/dropdown allowed-values** aren't exposed by the typed SOAP schema; use
  `screen_preflight` (CSDL mandatory fields) and known enums instead.
- A list GET (no `record_id`) can't return nested detail collections (Acumatica REST).

Roadmap: GL phase fully closed. Next:
numbering sequences (CS201010) and branches (CS102000) — same tier of foundation
prerequisite as GL. Also: nested detail rows in `load_from_excel`.

