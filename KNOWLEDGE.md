# grp-mcp — Operational Knowledge Base

Hard-won, **generic** lessons for driving Acumatica ERP through grp-mcp. This is a distilled,
sanitized reference (no instance/tenant/client specifics) — the *how Acumatica actually behaves*
knowledge that turns "the screen won't write" into "here's the exact command shape that works."

Most of this is also enforced live inside the server (the `guide` tool + tool docstrings). This
file is the human-readable companion.

---

## 1. The five client planes — and which one to reach for

grp-mcp talks to Acumatica through five independent planes (four for driving, one diagnostic-only).
Picking the wrong one is the single biggest time-sink. When unsure, call **`guide`** or
**`screen_capabilities(screen_id)`** first.

| Plane | Tool surface | Best for | Blind spots |
|-------|--------------|----------|-------------|
| **Contract REST** | `create_or_update_entity`, `load_from_excel`, `get_entity`, `invoke_action` | Standalone entities exposed on the web-service endpoint; bulk CRUD | Context/master-detail screens the endpoint can't model; many import paths crash |
| **DAC OData** | `run_dac_odata`, `get_dac_metadata`, `count_entity` | Reading any table/DAC (incl. config singletons) + mandatory-field metadata | Read-only; needs the OData v4 role or returns 403 |
| **Classic screen SOAP** | `screen_submit`, `screen_get`, `screen_record`, `screen_insert_rows`, recipes | Context/wizard/master-detail screens; "as-a-user" command replay | No random-access to a non-first grid row; some action tags are silently no-ops |
| **Modern UI-screen** | `ui_screen_action`, `ui_set_field`, `ui_read_grid`, `ui_update_grid_row`, `ui_get_structure` | Actions the classic plane can't reach; grid-row identity; enum allowed-values; structured errors | Some codebehind-only toolbar actions (Copy/Paste, Insert-From) are shimmed to no-ops |
| **Classic ASPX callbacks** *(diagnostic-only)* | `diagnose_save_error` | Recovering the REAL validation message behind a failed grid save — both API planes above truncate it to "record raised at least one error" | Screens with no classic `.aspx` page; it replays a real Save (§11) |

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
(`kb-mcp-dual`: `search_kb` → `read_kb_file`) for that screen **and** the specific action. Read its
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
  empty `messages[]` on success; *record-level* validation errors come back in `messages[]`.
- **`graphIsDirty:true` after a field-set is normal** (cleared by Save/Cancel). A **`409` here is a
  business-rule error** (read `messages[]`), *not* a concurrency lock — sessions are isolated by
  their own cookie/graph.

### 4a. THERE ARE NO FIELD-LEVEL ERRORS — a bad value is discarded and WIPES the field

The single most dangerous property of this plane. A field-set response carries **only**
`{isNewEntry, graphIsDirty, actionStates, actionNamesPerView}` — **no `fieldStates`, no
`messages`, no error key anywhere**. Do not go looking for a red-field-outline payload; it does
not exist. An unparseable value is accepted with a **clean 200** and then **thrown away**, taking
the field's existing value with it:

| field | before | after setting `"NOT-A-DATE"` | what a Save then does |
|---|---|---|---|
| required date (e.g. `GL301000.DateEntered`) | a date | **null** | fails loudly — "cannot be empty" |
| **optional** date (e.g. `AP301000.DueDate`) holding a real value | a date | **null** | **persists the null over your data** |

Required fields are rescued by the required-check. **Optional fields have no protection: this is
silent data loss.** And `graphIsDirty` is **`true`** throughout — the value genuinely changed, it
changed to *nothing* — so dirtiness proves only that something moved, never that your value landed.

**The reliable detector is a READ-BACK.** After setting, ask the graph what it now holds
(`{"data":[], "viewsParams":{<view>:{}}}` returns `fieldStates` with current values; batch every
view into one POST). **Sent non-blank → field now blank = the value was discarded.** grp-mcp does
this automatically in `ui_screen_action` (`rejected_fields` on the result).

**Judge blankness ONLY — never value equality.** The plane reformats what it stores: send
`"01/01/2027"`, read back `"2027-01-01T00:00:00.0000000"`. An equality check fires on every date,
enum and selector. Blank-after-non-blank is unambiguous; anything cleverer cries wolf.

Screens are **not** consistent here: a few fields have a validating setter that refuses a bad value
outright, leaving the field **unchanged** and `graphIsDirty` **false** (`GL101000.BegFinYear` does
this). That case is invisible to a read-back and visible to a clean→clean dirty check — which is
why grp-mcp runs both guards. Both are partial. **A read-back of the persisted record
(`run_dac_odata` / `screen_get`) after a Save remains the only proof a write actually landed.**

Metadata guards (`ui_coerce_validate`) catch read-only fields and invalid enums from `/structure`,
but **cannot** catch an unparseable date — right field, right type name, not read-only. The two
mechanisms are complementary and neither is sufficient alone.

**`/structure` exposes only ONE container per view name — fields on a "duplicate" tab are
invisible to it, permanently.** A screen whose classic SOAP schema disambiguates several
containers bound to the SAME view as `"ViewName"`, `"ViewName: 1"`, `"ViewName: 2"` (several
tabs reading the same DAC) has those numbered duplicates' fields completely absent from
`/structure` — proven live on PY309000: `PayMode` lives on `"Employments: 2"` per classic's
schema, but modern's raw `/structure` JSON only ever has a plain `"Employments"` key, unaffected
by `ui_bootstrap` or record navigation (there is nothing more to fetch — this is what Acumatica's
endpoint actually returns). `ui_screen_action`'s unknown-field check used to be unconditional
(not even `skip_validation` bypassed it); fixed in v0.62.0 — `skip_validation=true` now lets such
a field through, reported in `unverifiable_fields`. That field is **not** verifiable via this
plane's own read-back either (`verify_sets`/`read_field_values` share the exact same blind spot),
so cross-check with `screen_get` (classic) or `run_dac_odata`/`get_entity` after saving.

### 4b. Warning/info toasts, and reading `messages[]` correctly

The top-right toast **is** `messages[]`, typed by `messageType` (`error`/`warning`/`info`). Only
`error` should raise. **Warnings and info are not failures but they are not noise either** — they
are how a screen says "I accepted your write and ignored it" ("the period is closed", "already
generated"). An `ok:true` **with** notices still warrants a read-back. grp-mcp surfaces them as
`notices` / `@grp.notices` rather than dropping them.

### 4c. `/structure` is the only discovery endpoint — and it caches well

- No slimming exists: `?fields=`, `?parts=`, `?$select=` are **ignored**; `/schema`, `/metadata`,
  `/fields`, `/views` are `404`; the bare screen path is `405`. You get the whole descriptor.
- It is **fat** — a document-entry screen runs 250–270 KB (a setup screen, ~15 KB) — and it is a
  **stateless** GET describing metadata, not record state. So it caches safely.
- It ships an **`ETag`, so revalidate instead of re-downloading**: a conditional GET is
  ~100 ms / 0 bytes versus ~280 ms / 270 KB.
- **TRAP:** that ETag is an **environment stamp, IDENTICAL for every screen on a tenant**
  (`<build>$<n>$<user>$<tenant>$<locale>$<userid>$$<metadata-version>`), *not* a per-screen content
  hash. Replaying one screen's ETag at another screen's URL returns **304**. The server will not
  catch a cache-key mix-up for you — key on screen **and** session identity (the user and locale
  ride in the stamp), and only ever send an entry's own ETag back to its own URL. The
  metadata-version segment changes on a customization publish, which invalidates every screen at
  once — so publishing must drop the whole cache.

### 4d. Graph state is sticky across sessions

`ui_bootstrap` deliberately does **not** send `clearSession` (that would reset company/branch and
selected-record context, breaking process actions). Because the forms-auth cookie is shared and
cached, a *later* client can therefore inherit a *previous* one's uncommitted graph state. When you
need a genuinely clean graph — reproducing a bug, a controlled test — send `clearSession:true`
explicitly. This trips up A/B comparisons: the "before" of your second case may be the "after" of
your first.
- **Grid rows** are addressed by their GUID `id` (from a loaded grid), via
  `activeRowContexts:[{dataView, dataKey:{…}}]` + `rowId`.
- **Non-200s on `/structure` are informative boundaries:** `409 SetupNotEntered` (module not
  configured), `403` (license/feature off), `404` (bad screen ID).
- **Codebehind-only toolbar actions can be shimmed to no-ops** on this plane too. Proven dead over
  the API: SM206025 **Insert-From** (sets dirty, copies no rows), **Copy/Paste document** (pastes
  empty). When a "clone the whole record" action is needed and both planes no-op, reproduce the
  *data* another way rather than chasing the action.
- **A grid write's own internal read used to re-clear the session** (fixed v0.63.0):
  `ui_insert_grid_row`/`ui_update_grid_row`/`ui_update_grid_rows`/`ui_delete_grid_row` all call
  `ui_grid_read` first, purely to fetch the current row list/columns for the Save payload — but
  that call forced `clearSession`, wiping any `ui_set_field` edits staged earlier in the same
  session (header fields set before inserting a detail row). Proven on PY309000: staging all
  header/employment fields then calling `ui_insert_grid_row` normally still failed with a
  required-header-field error, because the internal read wiped them first. `ui_grid_read` now
  takes `preserve_session=True` for exactly this internal use; the standalone `ui_read_grid` tool
  keeps the default fresh-reload behavior.
- **A grid Save's error response only ever echoed `grid_view` + its `parent` view** (fixed
  v0.63.0), so a validator error rooted in a THIRD, sibling view came back as a bare, undetailed
  "record raised at least one error" — proven on PY309000: inserting an `EmployeeBankDetails` row
  failed on `Employments.Step`/`Employments.Level` being required, but `Employments` was in
  neither `grid_view` nor `parent`, so that detail was invisible unless you manually forced the
  view into `viewsParams`. `_grid_save` now re-lists every view the session has bootstrapped
  (`ScreenClient._bootstrapped_views`, populated by `ui_bootstrap`/`ui_navigate_record`) and
  surfaces any of their per-field errors in the raised exception. This has a real floor, though: a
  selector/lookup failure on a grid CELL doesn't attach to any view's `fieldStates` at all (proven
  on the same investigation — PY309000's "Employee Bank" selector rejected both a real record's
  raw ID and its own code with an identical, generic "cannot be found in the system" fault on
  BOTH planes) — there the message stays generic because there genuinely is nothing more to
  surface that way.

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
2. **Provider** — a Data Provider (`SM206015`) that points at the file. A provider left at
   `<EmptyFileName>` reads **0 rows, silently** — the FileName parameter must point at the upload.
   **`setup_data_provider` creates one via the `DataProvider` CONTRACT entity — which 404s on an
   instance whose endpoint doesn't expose that entity (e.g. DBKK).** The provider mechanics do NOT
   require the contract entity, though — two endpoint-free paths:
   - **Reuse an existing provider.** Migration-configured instances ship providers already; DBKK has
     `ACU Import Fixed Assets`, `Import Fixed Assets`, `Import Fixed Asset - Parent / Child ID`,
     `Import Fixed Asset Classes`, `Asset Type`, `Import Asset Floor`, `Import Asset Room`, … — point
     your scenario's `ProviderID` at one and just re-point its FileName parameter to your upload.
   - **Drive `SM206015` on the screen plane.** Create the provider header + upload + point the FileName
     param entirely via `ui_screen_action` / `ui_update_grid_row` (classic/modern) — no endpoint entity.
     (Activate the schema Object + fill Fields headless per the provider-schema-gotchas below.)
   `import_excel` already re-points the FileName param via `ui_update_grid_row` (screen plane), so the
   RUN path needs no contract entity — only `setup_data_provider`'s create path does.
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
- **`=` formula sources: the classic writer mangles them, the modern plane persists them.** The
  classic SOAP writer silently corrupts `=` values (`='H'`→phantom literal `"H"`, `=[X]`→null).
  As of v0.53 **`build_import_scenario` auto-repairs** every `source.startswith("=")` row after the
  classic write by rewriting it through the modern grid plane (`ui_update_grid_row` on `FieldMappings`)
  and reports `formula_rows_fixed`. So `='const'`, `=[Self.Key]` key-restrictions, and
  `=LEFT(Concat(...),256)` computed values all survive now — you no longer have to flatten to plain
  columns. (If you hand-write the mapping via classic SOAP alone, they still mangle — use the tool.)
- **End with a `<Save>` action row** or the import stages every field and commits **nothing**
  (0 rows Processed, no error).
- **Batching many `new_row` in one submit corrupts state-dependent grids** — one row per call.
- **`run_import_scenario` (contract path) crashes** `Sequence contains no matching element` on many
  target screens — use `import_excel` (classic-plane runner).
- **Grouping:** blank document/batch number + identical header groups source rows into ONE document
  (invoice/batch). For distinct documents, supply a unique reference or force `<NEW>`.
- **The prepared/staged rows behind a run (`SYData`) and a provider's own field schema
  (`SYProviderField`, `SYMappingField`, `SYHistory`) have NO DAC-OData collection — not a permission
  gate, a genuine absence of a route.** They ARE listed as `EntityType`s in the `$metadata` CSDL (so
  a naive "is this DAC exposed" check says yes), but the platform never registers an `EntitySet` for
  them, and Acumatica's own service document confirms it (they're absent from both). Verified this
  is a hard dead end, not a wrong URL: a flat collection `GET`, `?$expand=`, an undeclared navigation
  segment off a parent that DOES have an EntitySet (`SYMapping`, `SYProviderObject`), and even a
  **direct fetch by the entity's own composite key** all `404`. Same class as the `DataProvider`
  contract-REST 404 above, one plane over — Acumatica only gives a standalone OData route to entities
  meant to be queried independently; pure detail/staging records reachable only through a parent
  screen don't get one, by design. **Read them via the UI-screen grid instead**
  (`ui_read_grid`/`screen.ui_grid_read`, `parent={"view": <header view>, "key": {"Name": <scenario>}}`)
  — that's the only route that exists, works at any scale (proven live: 6,977 prepared rows × 41
  columns in one call), and every field is right there including `ErrorMessage`/`IsProcessed`/
  `IsActive` for auditing a run's failures.

### Prerequisites still apply

An import only commits if the target screen's **master data already exists** — no AP bill without a
vendor, no cash sale without a cash account, no fixed asset without an asset class. A real migration
is: **set up master data first** (companies, accounts, classes, customers, vendors, items…), **then**
run the transactional imports in dependency order.

### Provider-schema gotchas (all now headless — v0.54)

The `SM206015` provider isn't usable until its schema Object + Fields are populated and active.
Doing this headless has three traps that each surface as a misleading downstream error:

- **The Objects grid `ProviderID` ≠ the id `setup_data_provider` returns.** The tool returns the
  entity/NoteID (e.g. `390a05d0-…`); the value the grid rows key on is a *different* GUID
  (e.g. `84ad0300-…`). Selecting a grid row / filtering `PX_Api_SYProviderField` with the wrong one
  fails silently ("schema object is not selected", empty reads). **Read the real grid ProviderID via
  `ui_read_grid` (or `run_dac_odata('PX_Api_SYProviderObject')`) and use that.**
- **The schema Object ships `IsActive:false`.** An inactive object → "Provider Object … cannot be
  found" at scenario build. Flip it: `ui_update_grid_row("Objects", {LineNbr:1}, {IsActive:true})`.
- **"Fill Schema Fields" IS headless-reachable (v0.54).** It's a codebehind action on a *selected
  detail-grid row*, so it was unreachable until `ui_screen_action` gained `grid_select`. Call it with
  `grid_select={"view":"Objects","key":{...}}` + `save_after=true` and the Fields schema populates
  without a manual click. (Verify the written fields via `run_dac_odata('PX_Api_SYProviderField',
  filter="ProviderID eq <gridProviderID>")` — `screen_get` reads the provider schema as empty.)

### Verbatim vendor-scenario clone still isn't needed

`insertFrom` copies no rows and Copy/Paste pastes empty, so a row-for-row clone of a vendor scenario
still isn't worth chasing — but you no longer lose its `=IsNull(...)` guards by rebuilding, because
`build_import_scenario` now persists `=` formulas (see the formula trap above). Plain-column mapping
plus a well-formed file, or formulas where you need them, reproduces the recipe's intent.

**Proven end-to-end fully headless (2026-07-14, DBKK `FA303000`):** both providers built (header +
file pointed + object activated + 35 fields filled via `grid_select`), both scenarios built with
`formula_rows_fixed`, Parent Prepared **6978 rows clean, zero manual clicks**.

### Pre-import DATA validation — `validate_import_setup` (v0.55)

**Prepare only STAGES rows; foreign-key values aren't validated until COMMIT** — so a Prepare can
report "6978 rows clean" and the Import then fails row-by-row on missing masters. `validate_import_setup`
front-runs that, screen-agnostic and with ZERO curated FK map:
- reads the scenario mapping → the committed (target field ← source column) pairs;
- reads the file's DISTINCT value per source column (6978 rows collapse to ~117 class codes);
- reads the target screen's live modern `/structure` — **each PXSelector field self-describes its
  master in its `viewName`**: `_Cache#<OwnerDAC>_<Field>_<TargetDAC>+<key>_` (e.g. ClassID →
  `PX.Objects.FA.FAClass`, Department → `EPDepartment`, LocationID → `GL.Branch`), and `valueField`
  is the master's value column. So the tool BULK-queries each master DAC via OData and diffs locally —
  no per-value probing, no hard-coded field→DAC table. (`_lookup_meta` parses this; `get_ui_structure`
  now exposes it as each field's `lookup`.)
- classifies: enum options; lookup missing-in-master (BLOCKER); the record's OWN key (AssetCD) as a
  COLLISION check (present = duplicate import, not must-exist); a non-key self-reference (ParentAssetID)
  as a WARNING ("import the parent file first"); required-but-blank (BLOCKER).
- `import_excel(validate=True)` (default) runs it and attaches `validation` — a non-blocking auto-warn
  BEFORE Prepare/Import.

**Grid-column fields too.** Multi-row grid columns (e.g. `AssetBalance.DepreciationMethodID`)
aren't materialized in the modern `/structure`, so their master comes from a second source: the
**OData CSDL NavigationProperty** — `<NavigationProperty Type="…FADepreciationMethod"><Referential
Constraint Property="DepreciationMethodID" ReferencedProperty="MethodID"/></NavigationProperty>` on
the grid's DAC gives the target master for ANY field (schema-level, works for grids). `_csdl_fk_target`
parses it; the grid's owning DAC comes from `get_ui_structure` grids' `dac`. Since the FK references
the master's INTERNAL key (MethodID) but the file uses the human CODE (MethodCD), the code column is
**auto-detected by value coverage** (`_match_master_column`: query the master, pick the column whose
values best cover the file's — no per-DAC config). A resolved master whose column matches NONE of the
file's values → `warning` "value not found in master" with a sample of valid codes.

**What stays `unverified`:** non-FK data fields (dates, amounts, serial no. — nothing to check) and
masters not exposed as an OData collection (custom/segmented, e.g. the Building lookup). Never a false
"OK". Proven live on DBKK FA303000: caught 48 mandatory-blank ClassID rows, LocationID `MHQ` absent,
Department `2/5/308/500` absent, **19 asset IDs that already existed** (real duplicate-import collision
a prefix-only manual check missed), AND `DepreciationMethodID='S'` invalid (valid codes are `SL-…`) —
the last one via the grid/CSDL path.

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
  profiles are **session-only** — and, as of v0.61.0, a session-only add ALSO needs that same
  env var if it requests `allow_write`/`allow_delete`/`allow_publish` (a read-only session-only
  add stays ungated). This closes a local-file-exfiltration path: a session-only profile with
  write access pointed at an attacker-controlled `base_url` could otherwise read any file inside
  its (unrestricted-by-default) read sandbox and upload it there via `attach_file_to_provider`,
  without ever touching `connections.json` or needing the admin gate.
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
3. **AUDIT THE BUILT ARTIFACTS — not the working tree.** See below. PyPI is permanent: a version
   can never be reused or truly unpublished.
4. `twine upload dist/grp_mcp-<version>*` (version-specific glob, or clear `dist/` first).
5. Auth: API token (username `__token__`). Users upgrade with `pip install --upgrade grp-mcp` /
   `uvx` resolves latest automatically.
6. Verify what landed: compare the published `sha256`
   (`https://pypi.org/pypi/grp-mcp/<ver>/json` → `urls[].digests`) against the local file you
   audited. A digest match is conclusive — no need to re-download and re-scan.

**The audit (step 3) is not optional.** The sdist is public and the build back-end ships the
working tree by default, which is wider than what you think you wrote:

- Unpack **both** the wheel and the sdist and grep them for real credential values, internal
  hostnames, client/tenant names, and any local config file. `pyproject.toml` excludes the obvious
  offenders (agent scratch dirs, virtualenvs, the local connections file, binary docs) — verify,
  don't assume.
- **`.gitignore` protects the FILE, not values copied out of it.** A credential pasted into a source
  comment, a test fixture, or a docstring example is tracked, committed and shipped. Grep the
  artifacts for the *values*, not just for filenames. (Learned the hard way: a live ERP username
  reached a built wheel via a comment pasting a captured `ETag`, whose `$<user>$` segment was
  mistaken for a database name.)
- Stock Acumatica names (`Company`, `SalesDemo`) are product defaults and fine to ship. Your own
  tenant names are not.
- `twine`'s progress bar reports the **multipart body** size (file + README metadata, tens of kB
  larger), not the file size. That is not a mismatch — check the digest, not the bar.

After upload, PyPI's `info.version` ("latest") can lag the `releases` list by a minute — a version
is installable as soon as it appears in `releases`.

---

## 11. Classic ASPX diagnostic plane — recovering the REAL error behind a failed save

**The problem it solves (proven raw, 2026-07-17):** when a grid save fails validation, the classic
SOAP plane truncates the reason and the modern JSON plane returns only the generic
`"...record raised at least one error. Please review the errors."` — its `fieldStates` never
serializes a hidden tab's grid, so the concrete message (e.g. a cross-row rule like
*"Percent should be 100 for sum of all banks"*, or *"'Employee Bank' cannot be empty. Account No
is required"*) is **absent from the entire response**, not merely unparsed. The detail exists only
on the screen's legacy ASP.NET WebForms page (`/Pages/XX/.../SCREENID.aspx`), spoken through the
classic `ICallbackEventHandler` callback protocol.

**The tool:** `diagnose_save_error(screen_id, record_key, grid_view, values, row_key?, operation?)`
replays the failing change on that plane and returns `alert` (the headline message) plus any
per-row/per-cell error attributes. Page path auto-resolves from the SiteMap. Diagnostic-only by
design — the other planes remain the write path.

**Protocol facts (for maintainers extending it):**
- Shares the existing cookie login; no separate auth, and `__DataSourceSessionID` rides **empty**
  (no session bootstrap exists — graph context lives in the `ctl00_*_state` fields, which are
  plain single-URL-encoded XML, not opaque ViewState).
- The server is **stateless between callbacks**: fold each response block's `dataKey` back into
  its `_state` field (`<PXBoundPanel PageCount=".." PageIndex="0" DataKey="view,/wEW.."/>`) or
  the next call sees an empty graph.
- `__CALLBACKPARAM` is `Command|<envelope XML>` — the envelope is required (a bare command
  no-ops). Record load-by-key = `Cancel` + the key field's discrete edit param (same semantics
  as the classic SOAP plane). Saves always target the datasource (`__CALLBACKID=ctl00$phDS$ds`)
  with `<RowChanges><Modified|Inserted>` CDATA addressed at the grid control.
- Grid data never needs to load: the Save validates RowChanges against DB rows rebuilt from the
  header dataKey. Activate the grid's tab via its `_state` `SelectedIndex` or errors don't render.
- Control discovery: each control's config is emitted as `var _<control_id> = {json}` — the id is
  in the **var name with a leading underscore** (invisible to `\b`-anchored scans; matching the
  "nearest preceding id" picks the WRONG control and the Save no-ops with a clean ~54-char ack).
  Map the `"dataMember"` occurrence to its owning var declaration and prefer bodies with the
  grid-specific `"levels":` key.
- **A replayed change that is actually VALID persists** — the tool requires `allow_write` and
  flags `possibly_saved: true`; only replay changes that already failed.
- One-shot scripts against this plane must `await logout_session_cache()` on exit or each process
  orphans a "Max Web Services API Users" seat until idle-timeout.

---

*This file is generic operational knowledge. Instance-specific state (credentials, tenant names,
per-client configuration) is intentionally excluded and should never be committed to a public repo.*
