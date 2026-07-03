# grp-mcp

MCP server that exposes **Acumatica ERP** as tools for AI agents. Multi-instance,
OAuth2 — point it at any Acumatica site with a base URL + credentials.

It reaches Acumatica through **four client planes**, so an agent can read and
write almost anything:

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
- **Modern UI-screen plane** — the JSON protocol the real browser UI uses
  (`/t/<Tenant>/ui/screen/<ScreenID>`), reusing the SAME login session as the
  classic plane (no extra auth, no browser). `ui_get_structure` reads a screen's
  full descriptor — views, fields, **enum allowed-values**, action inventory,
  grid keys — and `ui_screen_action` sets fields + fires actions (incl.
  dialog-confirm). This reaches what classic SOAP can't: actions whose classic
  tag is a silent no-op (e.g. GL201000 Generate Calendar), enum value discovery,
  live workflow-aware field/action state, and **full grid CRUD** — read, insert,
  update, and delete an *existing* grid row (the classic engine can only append
  rows; editing one in place silently corrupts data there — see below), on both
  top-level grids and **master-detail** child grids under a header record. Works
  on any Modern-UI screen.

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
| `get_setup_guidance` | Baked-in **foundation setup map** — per-screen prerequisites, required fields, validation gotchas, correct order, which plane to drive it, and how to verify (System→GL→Common→CA→AP/AR→Tax→Currency). `screen_id=<ID>` for one screen, `area=<CA\|GL\|…>` for an area in order, or empty for the overview + cross-cutting rules. Read-only; the documented layer — recompute required-fields live per-instance. |

**Write**

| Tool | What it does |
|------|--------------|
| `create_or_update_entity` | Create/update a record (PUT, upsert by key). Auto-corrects a real Acumatica quirk: a successful write can echo a nested detail collection you just wrote as `[]`; when detected, this re-fetches the record with that field expanded and patches in the real values (a failed re-fetch surfaces as `_unverified_details` instead of a silent wrong `[]`). |
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
| `ui_get_structure` | Discover a screen via the **modern UI-screen API** (`/ui/screen/<ID>/structure`): views → fields (type, required, readonly, enabled, **enum allowed-values**), the live action inventory (enabled/visible/confirmation), and grid key fields. Richer than screen_get_schema; a live workflow-aware preflight. Read-only, any Modern-UI screen. |
| `screen_capabilities` | Recommend WHICH plane/tool to drive a screen with — the router for "use JSON or SOAP when needed". Probes `/structure` and returns, per operation shape (edit master, select-row-then-act, dialog action, selector field, on-contract entity), the tool to use and why. Encodes the plane-by-shape rule so you don't find the right plane by trial-and-error. Read-only. |
| `ui_screen_action` | Drive a screen via the **modern UI-screen API** — set fields, then fire an action (auto-answers confirmation dialogs). Reaches dialog actions classic SOAP can't (e.g. GL201000 Generate Calendar) and plain record edits (action="Save"). `tree_select` selects a TREE node first (trees aren't grids); `record_key` scopes a keyed primary view to one record. Preflights action + field names against `/structure`; surfaces the screen's own `messages[]` errors. Requires allow_write (a destructive action also requires allow_delete). FORM-view fields only — for a GRID cell, use the grid tools below. |
| `ui_resolve_selector` | Resolve a lookup/selector FORM field (per `ui_get_structure`'s `selector` marker) to its `{id, text}` value — the modern-plane equivalent of clicking the magnifier, searching, and picking a row. `pick` disambiguates duplicate titles (common across modules). Generalizes to any selector on any screen from its own `/structure` metadata. Read-only, no gate. |
| `ui_tree_dialog_insert` | Add a child under a TREE node via its INSERT DIALOG — the full "select node → Insert → fill popup → OK → Save" flow (the capability behind adding an entity to a web-service endpoint, SM207060). Runs the real UI's 5-phase sequence in one call. Resolve selector fields with `ui_resolve_selector` first. `record_key` scopes a keyed primary view. Requires allow_write. |
| `ui_populate_endpoint_entity_fields` | Fill an endpoint entity's scalar FIELDS from one of its screen data views (SM207060 "select entity → Populate → pick Object → Select All → OK → Save"). `ui_tree_dialog_insert` adds only the entity shell; this exposes its fields on the contract (entity-scoped data-view lookup resolved automatically; `data_view_pick` disambiguates). Requires allow_write. |
| `ui_read_grid` | Read GRID rows via the **modern UI-screen plane** — the read peer of the grid CRUD below. Returns each row flattened to `{field: value}` + its `_rowId`, live per-cell state (enum options, readonly), and the grid's key fields. `parent` reads a CHILD grid under a header (master-detail). Read-only, no gate. |
| `ui_insert_grid_row` | Append a NEW row to a GRID on the modern UI-screen plane (`changes.inserted`). `parent` targets a CHILD grid under a header — the parent-linkage id is auto-filled server-side, so `values` needs only the child's own fields. Requires allow_write. |
| `ui_update_grid_row` | Edit ONE **existing** GRID row in place on the modern UI-screen plane (`changes.modified`) — the capability classic screen SOAP lacks (its positional row selector is inert, see Known limitations). Matched by key; `parent` targets a CHILD grid under a header. Requires allow_write. |
| `ui_delete_grid_row` | Delete an existing GRID row (matched by key) on the modern UI-screen plane (`changes.deleted`). `parent` targets a CHILD grid under a header. **Requires allow_delete.** |
| `ui_grid_row_action` | Select an EXISTING grid row by key, then fire a screen-level ACTION on it (the "click a row → hit a toolbar button" flow) — closes the one thing classic SOAP structurally can't do (it can't address an existing grid row by key; proven: SM203520 Restore Snapshot faults "A snapshot is not selected" via SOAP). Auto-answers the confirmation dialog; `confirm=False` arms without committing; `parent` scopes a tenant/master. Returns an honest `status` (committed / dialog_open / redirected). Requires allow_write. |
| `release_sessions` | Log out cached API sessions to free Web Service API license seats (trial = 2). |
| `list_screens` | Find a screen's ID by title (searches the site map) — feeds screen_get_schema/get/submit. |
| `whoami` | Active connection identity (user/tenant/endpoint), reachability, and cached sessions holding seats. |
| `enable_features` | Set feature flags on Enable/Disable Features (CS100000) + Save (stages them). Pass `activate=True` to also install them. |
| `activate_features` | Activate/install the staged feature set (CS100000 "Enable" button) — fires it via the **modern UI plane** (`requestValidation`; the classic SOAP action NREs on a large feature set), recompiles the site, then **non-blocking**: watches ActivationStatus for up to `wait_seconds` (default 40) and returns status `completed` \| `in_progress`. A long recompile finishes on its own — poll `activate_features_status`. |
| `activate_features_status` | Quick single read of CS100000 ActivationStatus (no recompile) — poll after `activate_features` returns `in_progress` until activated=true. |
| `create_financial_calendar` | Create the financial calendar (GL101000): first year → AutoFill → optional start date (`starts_on`, M/D/YYYY — set after AutoFill, dialog auto-answered) → Save. Fully SOAP, no UI. |
| `generate_master_calendar` | Generate financial periods (GL201000 "Generate Calendar") for a year range. Classic SOAP exposes this action's tag but it's a no-op there; this recipe drives the modern UI-screen JSON protocol instead (same login session, no browser). |
| `create_ledger` | Create a GL ledger (GL201500): LedgerID/Description/Type/Currency → Save. |
| `set_gl_preferences` | Set GL Preferences (GL102000): Retained Earnings + YTD Net Income system accounts (Liability) + posting flags → Save. The GL-phase keystone for posting. |
| `chart_of_accounts` | Create Chart of Accounts rows (GL202500) in one transaction (recipe over screen_insert_rows; dialog auto-answered). |
| `create_numbering_sequence` | Create a numbering sequence (CS201010): header + one subsequence (start/end/warning/step) → Save. Foundation prerequisite — auto-generates document IDs (GL batches, invoices, bills, …) and auto-numbered key segments. |
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
| `publish_customization` | Publish projects — **non-blocking**: runs the site recompile (1-3 min, longer than the MCP request timeout) in a background task and returns after `wait_seconds` (default 40) with status `completed` \| `in_progress` \| `error`. A fast-fail validation error still surfaces here; a long recompile returns `in_progress` and finishes on its own. |
| `publish_status` | Check a background publish started by `publish_customization` (instant in-memory read; no API call). Poll until status ≠ `in_progress`; never re-publish an in-progress one. |
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
grp-mcp-ui                        # venv install
uvx --from grp-mcp grp-mcp-ui     # uvx (note --from: this script's name differs
                                   # from the package name, so the bare `uvx
                                   # grp-mcp-ui` form resolves it as its own
                                   # (nonexistent) package and fails)
python -m grp_mcp.ui              # either install method
# -> http://127.0.0.1:8765
```

A single-file, dependency-free (stdlib `http.server`) page to **list / add / edit /
set-active / remove / test** profiles, writing the same connections.json. **First run
needs no config file** — on a fresh machine the page opens with an empty list; add your
first profile in the browser and it creates connections.json for you (no JSON editing).
It binds to `127.0.0.1` only (it edits credentials) and **never sends secrets to the
browser** — the profile list only reports whether a secret/password is set. Leave the secret and
password blank when editing to keep the existing values. The MCP server reads config
only at startup, so to apply add/active changes to the live connector **without a
restart**, run the `reload_config` tool in Claude (it re-reads connections.json and
frees old sessions) — or just restart the MCP. (Test works immediately — it opens its
own session.)

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

A PUT to `WebServiceEndpoints` (SM207060) is a **verified no-op** — it's a stateful
wizard form whose create/extend views are transient — so the old `extend_endpoint`
helper is **no longer registered as an MCP tool** (it was a trap: a clean success that
changed nothing; the function survives only as importable reference). Reading a
contract works fine via `get_endpoint_definition`. Three working ways to actually add
entities/fields/actions:

- **Modern UI-screen plane (no browser).** `ui_tree_dialog_insert` adds an entity or
  detail collection by driving the real SM207060 wizard, and
  `ui_populate_endpoint_entity_fields` exposes its scalar fields — end to end (see the
  modern-plane tools above).

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
  against the configured origin (`scheme://host`) **and the base-URL path prefix**
  (e.g. `/2026R1`); a `poll_action`/download URL on another host — *or a same-host
  URL pointing at a different app path* — is refused (prevents OAuth-token
  exfiltration / SSRF, incl. sibling apps on the same server).
- **Writes are opt-in.** Record mutations (`create_or_update_entity`,
  `load_from_excel`, `invoke_action`, `run_import_scenario`, `set_note`,
  `attach_file`, `attach_file_to_provider`, `screen_submit`, `ui_screen_action`,
  `ui_insert_grid_row`, `ui_update_grid_row`) require `"allow_write": true`.
  **Deletes require the stricter `"allow_delete": true` across ALL planes** —
  not just `delete_entity`, but also a `screen_submit` `delete_row` **or
  record-level `Delete` action**, `delete_segmented_key`, a destructive
  `ui_screen_action` (e.g. `action="Delete"`), and `ui_delete_grid_row`, so the
  screen/UI planes can't sidestep the delete gate. Customization
  publish/import/unpublish require `"allow_publish": true`. **Default is
  read-only.**
- **Filesystem sandbox is OPT-IN, not on by default.** Tools that read (`attach_file`,
  `import_customization`, `load_from_excel`) or write (`download_file`, `run_report`,
  `snapshot_entity`, `export_customization`) a local path enforce `read_roots` /
  `write_roots` **only if those lists are set** — an **empty list means UNRESTRICTED**
  (any path the OS user can reach), *not* sandboxed. To make this impossible to
  over-trust, each file-touching tool echoes a `sandbox` field in its result
  (`UNRESTRICTED — no write_roots set` vs `restricted to [...]`). A `max_file_bytes`
  cap applies to reads. Set the root lists on any multi-user or untrusted host.
- **Config mutations need an admin opt-in.** `add_instance` / `remove_instance` /
  `set_active_instance` **persisting** to `connections.json` (which stores
  credentials) require the `GRP_MCP_ALLOW_ADMIN=1` env var — a gate separate from the
  ERP write gates, so an agent can't silently rewrite your credential file.
  `persist=false` (session-only) needs no gate.
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

This is stated in the server's MCP `instructions` (so any client is told to do it, and
sees every write tool named explicitly) — the two trickiest to get wrong,
`screen_submit` and `ui_screen_action`, also carry an explicit `PRECONDITION` note in
their own docstring. It exists because Acumatica screens have
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

**Option A — `uvx` (recommended, no persistent install).** [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
runs a PyPI package straight from its cache, installing on first use and reusing
that cache after (no venv to manage, no separate upgrade step — it always
resolves the latest version unless you pin one):

```bash
uvx grp-mcp          # first run installs (~5-10s); every run after is ~1s
```

If `uv` isn't installed yet: `pip install uv`, or see the link above. This is
what the `claude mcp add` command below uses. (The bundled config UI's script
has a different name than the package — see [Config UI](#config-ui-localhost)
for its `uvx --from` form.)

**Option B — a dedicated venv (classic, most explicit):**

```bash
python -m venv .venv
.venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install grp-mcp
```

To upgrade later: `pip install --upgrade grp-mcp`.

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

**Installed from PyPI (`uvx`/`pip install`, no git clone)?** Neither example file
ships in the package — only the `grp_mcp` Python package itself does. Use the
bundled [Config UI](#config-ui-localhost) instead (`uvx --from grp-mcp grp-mcp-ui`
or `grp-mcp-ui` in a venv install): it creates `connections.json` for you from a
blank state, no template file or hand-written JSON needed.

Both `.env` and `connections.json` are gitignored — never commit real credentials.

### 4. Register with Claude

**Claude Code (CLI)** — user scope, available in all projects. Point at an
absolute `connections.json` so launch directory does not matter:

```bash
# Option A (uvx) — no prior install needed, uvx fetches it on first launch:
claude mcp add grp-mcp -s user \
  -e GRP_MCP_CONNECTIONS=/abs/path/to/connections.json \
  -- uvx grp-mcp

# Option B (venv) — point at the venv's installed script:
claude mcp add grp-mcp -s user \
  -e GRP_MCP_CONNECTIONS=/abs/path/to/connections.json \
  -- /abs/path/to/.venv/Scripts/grp-mcp.exe        # use grp-mcp on macOS/Linux
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "grp-mcp": {
      "command": "uvx",
      "args": ["grp-mcp"],
      "env": { "GRP_MCP_CONNECTIONS": "C:\\path\\to\\connections.json" }
    }
  }
}
```

(Using a venv install instead: set `"command"` to the venv's `grp-mcp`/`grp-mcp.exe`
path and drop `args`; `cwd` also lets a same-directory `.env` load.)

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

## Development

Smoke tests (pure logic — config/gating model, the write/delete/publish gates, the value
wrapper, the modern UI-screen error parser, and the grid CRUD payload shapes incl.
master-detail; no live instance needed):

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

## Status

v0.36 — 74 tools across four client planes (v0.36: hardened the classic-SOAP write
path against four silent-wrong-behavior bugs — `screen_submit` now pre-validates each
`set` against modern-plane metadata and **refuses** a read-only field or invalid-enum
write instead of returning `ok:true` and dropping it silently; `screen_get` filters
accept operator aliases via `op` (`>=`, `!=`, …) and **reject** an unknown key/condition
instead of silently defaulting to `Equals` and returning the wrong rows; insert/Save
faults keep their full message and attach `required_fields`/`fields_you_set` hints
instead of a truncated `"record raised"`. Platform limitations SOAP can't fix — grid
key-edit footgun, sparse schemas, no-SOAP-delete-on-pure-grids, REST-only
reports/attachments — are documented in `get_setup_guidance`. v0.35: `publishBegin` runs inside the
background publish job so a cold-site begin can't be lost to a client timeout —
`publish_status` now reports phase `begin`/`publishing`; entity tools
(`get_entity`/`create_or_update_entity`/`get_entity_schema`/`list_entities`/
`fetch_all_entities`/`count_entity`/`delete_entity`/`invoke_action`) accept
`endpoint="<Name>/<Version>"` to target a non-default endpoint without config
changes; HTTP read timeout raised to 120s with explicit timeout errors for cold
IIS / wide-row reads): contract REST (CRUD, actions, `$skip` paging,
attachments up/down, notes, reports — with an auto-fix for a detail-collection write-echo
quirk), DAC + GI OData (incl. CSDL metadata / mandatory-field discovery), the **screen-based
SOAP engine** (context/master-detail/wizard screens REST can't), and the **modern UI-screen
plane** (`ui_get_structure` + `ui_screen_action` for dialog actions classic SOAP can't reach,
enum-value discovery, and live workflow-aware state — plus full **grid CRUD**,
`ui_read_grid`/`ui_insert_grid_row`/`ui_update_grid_row`/`ui_delete_grid_row`, on top-level
and master-detail grids; `ui_grid_row_action` to select an existing row then fire a toolbar
action on it — the row-scoped action SOAP can't reach, e.g. SM203520 Restore Snapshot;
**tree + insert-dialog** screens via `ui_resolve_selector` +
`ui_tree_dialog_insert` + `ui_populate_endpoint_entity_fields`, e.g. creating a
web-service-endpoint entity AND exposing its fields on SM207060 — end to end, no browser;
and `screen_capabilities` to pick the right plane/tool per operation). On top sit setup recipes — `enable_features` + `activate_features`
(install/recompile), `create_financial_calendar` (incl. start date), `create_ledger`,
`create_numbering_sequence`, `create_segmented_key` → `set_segment_value`,
`chart_of_accounts`, `generate_master_calendar` + `manage_financial_periods`
(open/close/lock/reopen/deactivate) — plus import scenarios, the Customization Web API,
`setup_readiness`, and `get_setup_guidance` (a baked-in foundation setup map: prereqs,
required fields, gotchas, and plane-to-drive per screen — including the **blank-tenant
bootstrap recipe**: base currency is created inline by the company save, the Actual ledger
comes via the `Ledger` entity with a nested `Companies` link, feature activation must fire
the modern-plane `requestValidation`, and `AccountClass` 500s until a branch record exists;
plus the project.xml **endpoint-transplant method** for building comprehensive web-service
endpoints headlessly). **The financial foundation chain — System→GL→CA→AP→AR→Tax — is fully
grp-mcp-drivable end-to-end, no manual/UI steps**, proven live on a real instance, including
from a completely blank tenant.

### The modern UI-screen plane (`ui_get_structure` / `ui_screen_action`)

Some actions expose a tag in the classic typed SOAP schema but their handler isn't wired up
there — invoking it is a silent no-op (clean ~335-byte success, zero effect; GL201000
"Generate Calendar" is the found example). The real implementation lives behind the modern
UI's own JSON protocol at `/t/<Tenant>/ui/screen/<ScreenID>`, which the browser itself
calls. It **reuses the same login session** as the classic plane (same cookie — no separate
auth, no browser), and is a genuine superset: schema discovery, **enum allowed-values**, row
identity, structured errors, dialog confirmation, and persisted field-writes.

Protocol (reverse-engineered live, now `ScreenClient.get_ui_structure`/`ui_set_field`/`ui_command`):
- Discover: `GET .../structure` → views + fields (type/required/readonly/enabled/**options**),
  action inventory (enabled/visible/confirm), grid key fields.
- Set a field: `POST {"data":[{"viewName":V,"fieldName":F,"value":val,"rowId":"","changeType":5}],...}`
  (enums use the option `value`; booleans `"true"`/`"false"`).
- Fire an action: `POST {"command":[{"name":cmd}],"data":[],...}` → `200`, or
  `302 openDialog` → auto-confirmed with `dialogCallback:{dialogResult:1,viewName:V}`
  (`dialogResult` = public `PX.Data.WebDialogResult`: OK=1, Yes=6, No=7, Cancel=2, …).

Proven: field write persists (set→Save→cross-plane read-back), dialog action generates
periods, and the screen's own `messages[]` surface as clean errors (e.g. `"'Retained
Earnings Account' cannot be empty"`). **Two rules baked in:** (1) load the views you edit
(and the primary view) so a Save validates a full record and actions have company context;
(2) one plane per session — never interleave classic (Export/Submit) with modern ops on the
same session (separate graph state → 409). Works on any Modern-UI screen; an unconfigured
module returns a clear "PREREQUISITE NOT MET" and an unlicensed module is access-denied.

### Grid CRUD — editing an existing row (`ui_read_grid` / `ui_insert_grid_row` / `ui_update_grid_row` / `ui_delete_grid_row`)

The classic screen-SOAP engine can **append** a detail row (`new_row`) but cannot
**edit an existing one in place** — its positional row selector (`{"row": N}`,
the `RowNumber` service command) does not move the grid cursor at all; a `set`
after it silently lands on row 1. Proven live (GL202500 Chart of Accounts): a
`{"row": 8}` + field `set` returned a clean ~335-byte "success" while actually
overwriting row 1 — a silent **wrong-row write**, worse than a no-op. `{"row": ...}`
now raises `ScreenError` instead of risking that.

The fix is a genuine capability, not a workaround: the modern UI-screen plane
addresses grid rows individually, reverse-engineered from a live browser capture
of a cell-edit Save. One rule unifies all three write ops — **the row's key
field(s) must be inside its `values`**, or the server misinterprets the request:

| Op | `controlsParams.<grid>.changes` channel | Gotcha if the key is missing |
|----|------------------------------------------|-------------------------------|
| Update | `modified: [{id, index, values}]` | inserts a new blank row instead |
| Insert | `inserted: [{id: <generated>, index, values}]` | — (no key to omit; but omitting a *required* column still fails validation) |
| Delete | `deleted: [{id, index, values}]` | silently no-ops (clean 200, nothing removed) |

The `columns` array and pager fields must also be echoed back in the request, or
the Save returns a clean 200 that persists **nothing** — `ui_grid_read` (the
`ScreenClient` method backing all four tools) handles this for you.

**Master-detail.** A detail grid (e.g. CA202000's `ETDetails` entry types under a
selected cash account) only populates once its header is loaded. Pass
`parent={"view": "<PrimaryView>", "key": {keyField: value}}` and the tools
navigate the master (set its key, `changeType:5`) and co-request the child grid
in the *same* call, then keep the master loaded across the write. The
parent-linkage id is auto-filled server-side on insert — `values` needs only the
child's own fields. Proven end-to-end (insert → update → delete) on both a
top-level grid (GL202500) and a master-detail child grid (CA202000 `ETDetails`
under `CashAccount`).

Payload shapes are locked by regression tests in `tests/test_smoke.py`
(`test_insert_payload_has_key_columns_id_no_datakey`, `test_md_insert_navigates_master_and_scopes_viewsparams`,
and siblings) — a refactor that breaks the key-in-values rule or drops the
`columns` echo fails a test instead of shipping a silent no-op.

### Known limitations (by design / platform)

- **Endpoint entity adds (SM207060)** are fully drivable via the modern plane — a REST
  PUT to the WebServiceEndpoints entity is a no-op, but `ui_tree_dialog_insert` runs the
  real UI's 5-phase flow (select the endpoint's root tree node → open the Create-Entity
  dialog → fill it → commit the dialog → Save) in one call, and `ui_resolve_selector`
  turns a screen title into the `ScreenID` value it needs first. Proven + reproduced live
  (2026-07-02, multiple entities on a fresh endpoint, each verified against the contract's
  own `swagger.json`). `EntityType` (a required-looking dialog field) resolves server-side
  at commit time — omit it. `ui_populate_endpoint_entity_fields` then fills in the entity's
  scalar **fields** from a chosen screen data view (proven live: field_count 1 → 20), so a
  full "create entity + expose its fields" is now API-drivable end to end. Nested **detail
  collections** are also fully drivable by the same tools: `ui_tree_dialog_insert` targeting
  an entity node with `ObjectType="D"`/`"L"` creates the detail node, and
  `ui_populate_endpoint_entity_fields(..., detail_title=...)` populates its fields — a
  detail node selects via its FULL ancestor path (root→entity→detail), not just its
  immediate parent, or the select silently no-ops (`activeRowId` comes back `null`).
  Proven byte-identical against a live browser capture (2026-07-02): a detail populate that
  returns 0 fields is a genuine platform outcome, not a tool gap — either the chosen data
  view legitimately maps no scalar fields onto that object (matched the browser exactly,
  no error either side), or the view's fields collide with a name already staged elsewhere
  on that endpoint (e.g. `"An element with the DesignID name already exists"` — a
  `messageType:"error"` on the commit call, which the browser also hit and which
  `_ui_error` already surfaces as a raised `ScreenError` rather than a silent no-op).
  Pick a data view whose fields don't already exist on that node, or use a different
  (never-populated) detail node, to get a non-empty populate — proven live (2026-07-02,
  through the actual MCP tool): a brand-new detail node populated cleanly right after an
  already-poisoned sibling kept colliding on every view tried, so the collision sticks to
  the specific node once a field has landed there, not the entity/endpoint as a whole.
  **Endpoint ACTIONS** (wiring a button to an API
  action) still need the customization-XML route (SM204505 export/import) — too fragile to
  drive as a dialog; not yet started.
- **Multi-segment segmented-key deletion** is impossible via *any* API (and via the UI):
  Acumatica requires ≥1 segment and won't cascade-delete a key's segments, so the last
  segment always orphans. `delete_segmented_key` handles single-segment keys + safely
  stops on multi-segment (KB-confirmed; structural change = "contact support").
- **Tenant snapshot (SM203520)** requires **maintenance mode** (which locks the instance) —
  a deliberate maintenance-window operation, left manual for safety. (The create action
  itself is drivable via the modern `/ui/screen/` plane, like Generate Calendar, but is
  gated behind the instance-locking maintenance step by design.)
- **A classic-SOAP grid row can't be edited by position** — see **Grid CRUD** above.
  `screen_submit`'s `{"row": N}` selector doesn't move the grid cursor and now raises
  an error rather than risk a wrong-row write; use `ui_update_grid_row` instead.
- **A list GET can't return nested detail collections** — see **Detail-field guard**
  above. `create_or_update_entity` handles the parallel write-side case automatically
  (see its Write-table entry), which also documents two related gotchas on nested
  detail arrays: they always **append** rather than upsert-by-content, and a row's
  `id` is **not stable** across separate requests.

Roadmap: nested detail rows in `load_from_excel`; grid CRUD on line-number-keyed
transactional document grids (invoice/bill lines) is unproven beyond the master-detail
case already verified (CA202000 entry types).

