# grp-mcp — Operational Knowledge Base

Hard-won, **generic** lessons for driving Acumatica ERP through grp-mcp. This is a distilled,
sanitized reference (no instance/tenant/client specifics) — the *how Acumatica actually behaves*
knowledge that turns "the screen won't write" into "here's the exact command shape that works."

Most of this is also enforced live inside the server (the `guide` tool + tool docstrings). This
file is the human-readable companion.

---

## 1. The four client planes — and which one to reach for

grp-mcp talks to Acumatica through four independent planes. Picking the wrong one is the single
biggest time-sink. When unsure, call **`guide`** or **`screen_capabilities(screen_id)`** first.

| Plane | Tool surface | Best for | Blind spots |
|-------|--------------|----------|-------------|
| **Contract REST** | `create_or_update_entity`, `load_from_excel`, `get_entity`, `invoke_action` | Standalone entities exposed on the web-service endpoint; bulk CRUD | Context/master-detail screens the endpoint can't model; many import paths crash |
| **DAC OData** | `run_dac_odata`, `get_dac_metadata`, `count_entity` | Reading any table/DAC (incl. config singletons) + mandatory-field metadata | Read-only; needs the OData v4 role or returns 403 |
| **Classic screen SOAP** | `screen_submit`, `screen_get`, `screen_record`, `screen_insert_rows`, recipes | Context/wizard/master-detail screens; "as-a-user" command replay | No random-access to a non-first grid row; some action tags are silently no-ops |
| **Modern UI-screen** | `ui_screen_action`, `ui_set_field`, `ui_read_grid`, `ui_update_grid_row`, `ui_get_structure` | Actions the classic plane can't reach; grid-row identity; enum allowed-values; structured errors | Some codebehind-only toolbar actions (Copy/Paste, Insert-From) are shimmed to no-ops |

**Rule of thumb:** REST for plain entities → classic SOAP for context screens → modern UI plane for
what classic can't do. The modern plane is materially *more* capable than classic SOAP (schema
discovery via `/structure`, per-row GUIDs, enum options, `messages[]` errors, dialog confirmation),
but neither plane can drive *every* action — verify writes.

**Do not mix planes in one session.** Classic (`Export`/`Submit`) and modern (`ui_set_field`) keep
separate graph state and collide (spurious 409). One plane per session; cross-plane **verification**
in a *different* session is fine and encouraged.

---

## 2. KB-first policy (mandatory before any write)

Before ANY create/update/delete on a screen or entity, consult the Acumatica knowledge base
(`kb-mcp`: `search_kb` → `read_kb_file`) for that screen **and** the specific action. Read its
prerequisites, dependent screens, required fields, validation rules, and ordering constraints;
verify each prerequisite exists (`run_dac_odata` / `screen_get` / `setup_readiness`) and set up any
missing one first, recursively.

**Why it's non-negotiable:** Acumatica screens have hard dependencies the screen won't surface until
a write fails with a *generic/misleading* error. Driving a screen cold produces false
"this screen is broken / write-resistant" conclusions. This policy is embedded in the server's own
instructions; honor it. Pure reads are exempt.

---

## 3. Classic screen-SOAP engine — the command mechanics

- **Endpoint:** `{base}/Soap/{SCREENID}.asmx`. Login name must be **`user@Tenant`** on multi-tenant
  sites. `GetSchema` returns each field's descriptor carrying its `ObjectName`+`FieldName` (the
  internal view+field) **and its `LinkedCommand` navigation chain**.
- **The navigation chain is everything.** A field's descriptor carries the chain that *loads/selects*
  the record before the field binds. Hand-rolled bare `Key`/`Value` commands omit it and **silently
  no-op** (Submit returns 200/`ok`, nothing persists). The engine's ergonomic specs bake it in:
  - `{"set":"<Friendly>","to":v}` — descriptor Value (navigates if it's a key). **Use this to
    navigate a header**, not a flat `{"key"}`.
  - `{"key":"<Friendly>","to":v}` — bare flat Key (unreliable for navigation on many screens).
  - `{"action":"<Friendly>"}` — e.g. `Save`; `{"new_row":"<Container>"}`, `{"delete_row":…}`,
    `{"answer":"<Container>","to":"Yes"}`.
- **The "no-bind" signal.** A **persisted** Submit echoes a small body (~335 bytes). A multi-KB
  full-screen echo = *nothing bound*. `screen_submit` flags `nobind_suspected` when `ok` but the
  body is large. **`ok` ≠ persisted — always read back** (via a different plane/session).
- **`auto_answer="Yes"`** clears "Are you sure?" dialogs — but it can also MASK a no-bind failure as
  a fake `ok`. Trust the read-back, not the answer.
- **Field-level errors arrive inside HTTP 200** as `<Message>`/`<IsError>` (surfaced as
  `field_errors`); real faults are 500 with `<faultstring>` carrying the inner PX exception.
- **`PXSetupNotEnteredException` on GetSchema** means the *module isn't configured* (its
  Preferences/Setup form is blank) — not a broken screen. The engine reframes it as
  "PREREQUISITE NOT MET"; configure that setup form first.
- **⚠️ Never hand-roll a flat field command against a summary view.** A flat `Value` command with no
  bound record becomes a **mass update of every record** in that view. The shipped descriptor-based
  specs disallow flat commands for exactly this reason.

### Hard limits (platform, not tooling)

- **No random-access to a non-first grid row via classic SOAP.** `{"row":N}` / `{"key":field}` all
  leave the cursor on row 1 on config master-detail grids. The **modern plane** has per-row GUID
  identity and is the way to edit an arbitrary existing row.
- **Some action tags are exposed in `GetSchema` but no-op server-side** (the real implementation
  moved to the modern UI plane). "Clean success, zero effect, confirmed empty across multiple read
  channels" after exhausting client-side hypotheses → capture the real browser's Network tab; if it
  hits `/ui/screen/<ID>` instead of `/Soap/<ID>.asmx`, drive it on the modern plane.

---

## 4. Modern UI-screen plane — the JSON protocol

- Rides the **same login cookie** as classic SOAP (same ASP.NET app; no separate auth).
- **`GET /t/<Tenant>/ui/screen/<ScreenID>/structure`** is the modern schema endpoint: views, fields
  (type, required, readOnly, enabled, value, `commitChanges`), action states, and — crucially —
  **enum allowed-values** (`options:[{value,text}]` for PXStringList combos). Selectors expose their
  column schema; resolve their rows via `run_dac_odata` on the target DAC.
- **Write protocol:** bootstrap once → set a field via
  `{"data":[{"viewName":V,"fieldName":F,"value":val,"rowId":"","changeType":5}]}` → fire
  `{"command":[{"name":cmd}]}` → a `302 openDialog`/`openMessageBox` means confirm with
  `dialogCallback:{dialogResult:<WebDialogResult>,viewName:V}` (`OK=1, Cancel=2, Yes=6, No=7`).
- **`commitChanges` matters:** `commitChanges:true` fields POST per change; `commitChanges:false`
  fields buffer client-side and must ride in the committing request's `data[]`. `Save` returns 200 +
  empty `messages[]` on success; validation errors come back in `messages[]`.
- **`graphIsDirty:true` after a field-set is normal** (cleared by Save/Cancel). A **`409` here is a
  business-rule error** (read `messages[]`), *not* a concurrency lock — sessions are isolated by
  their own cookie/graph.
- **Grid rows** are addressed by their GUID `id` (from a loaded grid), via
  `activeRowContexts:[{dataView, dataKey:{…}}]` + `rowId`.
- **Non-200s on `/structure` are informative boundaries:** `409 SetupNotEntered` (module not
  configured), `403` (license/feature off), `404` (bad screen ID).
- **Codebehind-only toolbar actions can be shimmed to no-ops** on this plane too. Proven dead over
  the API: SM206025 **Insert-From** (sets dirty, copies no rows), **Copy/Paste document** (pastes
  empty). When a "clone the whole record" action is needed and both planes no-op, reproduce the
  *data* another way rather than chasing the action.

---

## 5. Data migration — Data Provider → Import Scenario → Import by Scenario

The three-screen pipeline (`SM206015` → `SM206025` → `SM206036`), proven committing end-to-end on
invoice (master-detail w/ computed field), journal batch (balanced debit/credit), and customer
(multi-view master) screens. **`import_excel` wraps the whole run with the traps below guarded.**

### The recipe

1. **Clone the vendor scenario, don't guess.** Acumatica ships inactive **`ACU Import …`** scenarios
   for the migration screens (`PX_Api_SYMapping` where `CreatedByScreenID='SM209900'`). Call
   **`stock_scenario_info(screen_id)`** to read the authoritative field order, the exact
   source-column names, and priming fields; build your file with those headers.
2. **Provider** (`setup_data_provider`) — it uploads the file **and points the FileName parameter**
   at it. A provider left at `<EmptyFileName>` reads **0 rows, silently**.
3. **Scenario** (`build_import_scenario`) — writes the mapping one row per submit, auto-appends the
   `<Save>` action, reads it back, and runs a **preflight** that warns on the traps.
4. **Run** (`import_excel` / `SM206036` prepareImport) — Prepare stages, Import commits. Poll; trust
   **`IsProcessed`**, not the "finished" status.

### The silent-failure traps (all guarded/warned by the tools)

- **openpyxl `.xlsx` reads as EMPTY.** Acumatica's Excel provider can't read inline-strings files —
  author with **real Excel (COM)**.
- **Same-filename re-upload can read a STALE cached copy.** Use a fresh filename when re-importing.
- **The worksheet name must match the provider object name** (default `Template`).
- **Numeric field → a real COLUMN, never a bare literal.** A bare `Value="1"` binds as a *phantom
  source column* named "1" → the field imports **empty** (this is the classic `'BaseQty' cannot be
  empty`: Qty mapped to "1" → empty Qty → empty BaseQty).
- **Map the PRIMING field before the computed one.** e.g. map `InventoryID` before `Qty` so the
  line's computed `BaseQty` defaults.
- **Alternating-blank columns need an explicit 0.** For debit/credit pairs (a GL line is debit XOR
  credit), put `0` in the empty side — a truly blank cell imports as EMPTY (`'CreditAmt' cannot be
  empty`).
- **Plain column refs only.** The classic writer silently **drops `=` formula values** to null
  (the vendor's `=IsNull(...)` guards can't be reproduced field-by-field). Supply real values instead
  of relying on blank-cell fallback.
- **End with a `<Save>` action row** or the import stages every field and commits **nothing**
  (0 rows Processed, no error).
- **Batching many `new_row` in one submit corrupts state-dependent grids** — one row per call.
- **`run_import_scenario` (contract path) crashes** `Sequence contains no matching element` on many
  target screens — use `import_excel` (classic-plane runner).
- **Grouping:** blank document/batch number + identical header groups source rows into ONE document
  (invoice/batch). For distinct documents, supply a unique reference or force `<NEW>`.

### Prerequisites still apply

An import only commits if the target screen's **master data already exists** — no AP bill without a
vendor, no cash sale without a cash account, no fixed asset without an asset class. A real migration
is: **set up master data first** (companies, accounts, classes, customers, vendors, items…), **then**
run the transactional imports in dependency order.

### Verbatim scenario clone is NOT API-drivable (and isn't needed)

Reproducing a vendor scenario row-for-row (to keep its `IsNull` guards) fails on every plane:
`insertFrom` copies no rows, Copy/Paste pastes empty, no writer persists a formula. You don't need
it — plain-column `build_import_scenario` + a well-formed file reproduces the recipe's intent.

---

## 6. Foundation / GL setup — order and gotchas

Empirically-confirmed build order (each step gates the next):

1. **Features** (`CS100000`) — set the feature flag, then **`activate_features`** — the apply is the
   `RequestValidation` action (flips `Pending → Validated`). It **recompiles the site (~1–3 min)**;
   the in-flight call often 500s as the app pool restarts, so it's **fire-and-verify**
   (`activate_features` polls `ActivationStatus`).
2. **Financial calendar** (`GL101000`) — `create_financial_calendar(first_year, starts_on=…)`:
   `FirstFinancialYear → AutoFill (Create Periods) → set start AFTER AutoFill → Save`, with
   `auto_answer="Yes"`. Start-date is fully SOAP-settable (old "picker-only" verdict was wrong).
3. **Ledger** (`GL201500`) — `create_ledger`. **This alone does NOT make it the org's Actual
   Ledger.** You must separately **link ledger → org** on **`CS101500`** (Companies → Ledgers tab).
   GL screens behave as if no ledger exists until this link is made.
4. **GL preferences** (`GL102000`) — `set_gl_preferences(retained_earnings, ytd_net_income, …)`.
   Both accounts must be **type Liability** and must already exist in the CoA.
5. **Chart of accounts** (`GL202500`) — `chart_of_accounts(accounts)` (grid writer).
6. **Generate periods** (`GL201000` "Generate Calendar") — classic SOAP silently no-ops this;
   `generate_master_calendar` drives it on the **modern plane** (proven).
7. **Open periods** (`GL503000`) — `manage_financial_periods` (`Action=Open`, `ProcessAll`) —
   cleanly SOAP-drivable.

`setup_readiness` reports the gaps (feature activation, GL prefs, open periods, calendar) so you know
what's missing before driving a screen.

---

## 7. Segment values & segmented keys

- **Segment values (`CS203000`) ARE writable** via the screen SOAP engine — `set_segment_value(...)`.
  The long "write-resistant" verdict was a **navigation bug**: navigate the header with a descriptor
  `{"set":"…SegmentedKeyID","to":…}`, **not** a flat `{"key"}` (flat-key left the cursor on the
  default segment, so writes silently landed there). Value must fit the segment's Length/EditMask.
- **Contract-REST insert of segment values is platform-blocked** (the flat endpoint entity can't
  establish the segment's parent context) — use the SOAP recipe.
- **Segmented keys (`CS202000`)** — `create_segmented_key` (needs ≥1 segment). The master DAC is
  **`Dimension`** (verify create/delete there, not `Segment`/`SegmentValue`).
- **Multi-segment key teardown is impossible via any API — and even in the UI.** Segments delete
  last-first, but the final segment can't be deleted (a key must keep ≥1), and deleting the header
  orphans the last segment. `delete_segmented_key` handles single-segment fully and safely stops on
  multi-segment. This is an Acumatica limitation, not a tooling gap.

---

## 8. Other screen recipes & limits

- **Company tree** (workgroup hierarchy) — build it via **`EP204060` (Import Company Tree)**, a grid
  + indent screen, **not** `EP204061` (the tree-click screen, whose parent link is unreachable via
  API). `build_company_tree(structure)` flattens to pre-order DFS, inserts each node, fires `Right`
  (indent) × depth before Save, and verifies every parent.
- **Tenant snapshot** (`SM203520`) — modern-plane-drivable (`exportSnapshotCommand` → `openDialog`),
  but the real constraint is the **maintenance-mode business prerequisite** (`SM203510` locks the
  instance). It's a deliberate maintenance-window op, not a casual pre-build step.
- **"UI-only, no API path" verdicts deserve a modern-plane network-capture attempt** before being
  accepted — a classic-SOAP no-op is a *plane* limit, not necessarily an Acumatica limit. Genuinely
  client-gated actions (a server-disabled button) are a different, real class.

---

## 9. Connections, seats & routing gotchas

- **Web Services API seats are limited (trial = 2).** Every login consumes one. Leaked sessions →
  `API Login Limit`. **`release_sessions`** frees cached REST clients; the engine also self-heals
  with one retry. Always release in long/standalone runs.
- **Persisting a profile from within Claude needs `GRP_MCP_ALLOW_ADMIN=1`.** Without it, added
  profiles are **session-only**.
- **Session-only profiles don't route on disk-backed tools.** `run_dac_odata`, `count_entity`, and
  others **re-read `connections.json` each call** and silently fall back to the persisted active
  profile — dangerous when two profiles share a tenant name (wrong site, no error). For real work:
  persist to `connections.json` + `reload_config`, and **pass `instance="<name>"` explicitly**.
- **`DataProvider` contract entity can 500 on read-back** on some builds (a BQL-delegate field);
  the provider row still gets created — verify via the `SM206015` UI or the mechanism in
  `setup_data_provider`, not a GET-by-id.

---

## 10. Publishing grp-mcp (maintainers)

1. Bump `version` in `pyproject.toml` (PyPI rejects duplicate versions).
2. `python -m build`
3. `twine upload dist/grp_mcp-<version>*` (version-specific glob, or clear `dist/` first).
4. Auth: API token (username `__token__`). Users upgrade with `pip install --upgrade grp-mcp` /
   `uvx` resolves latest automatically.

After upload, PyPI's `info.version` ("latest") can lag the `releases` list by a minute — a version
is installable as soon as it appears in `releases`.

---

*This file is generic operational knowledge. Instance-specific state (credentials, tenant names,
per-client configuration) is intentionally excluded and should never be committed to a public repo.*
