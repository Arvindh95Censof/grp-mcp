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
| **Classic screen SOAP** | `screen_submit`, `screen_get`, `screen_record`, `screen_insert_rows`, recipes | Context/wizard/master-detail screens; "as-a-user" command replay | Non-first grid row reachable ONLY by setting its exposed KEY field first; a non-key `set` edits the current row (§3); some action tags are silently no-ops |
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
- **Friendly name ≠ DAC field name — a `set` using the DAC name SILENTLY no-ops (validated
  2026-07-20).** The specs take the CONTAINER's FRIENDLY name (from `screen_get_schema`), which maps
  to an underlying DAC field; sending the DAC field name is an unrecognized friendly command → it
  no-ops and the field keeps its default. Worked example — ledger type on GL201500 is friendly
  `Type` → DAC `BalanceType` (`screen_get_schema('GL201500')`: `LedgerSummary.Type →
  LedgerRecords.BalanceType`; `get_dac_metadata('Ledger')` has `BalanceType` and NO `Type`). A
  `set BalanceType` no-ops → `Type` defaults to **Actual**: the first ACTUAL ledger looks fine by
  luck, the second STATISTICAL one fails *"actual ledger already associated."* `create_ledger`
  already uses the friendly `Type`, so this bites only when hand-driving the screen — it is NOT a
  tool defect. **Field names are PLANE-SPECIFIC: screen/friendly → `Type`; DAC/OData/contract
  (`run_dac_odata`, `create_or_update_entity`) → `BalanceType`** (and the value there is a code like
  `A`/`S`, not `"Actual"`). Corollary: a validation REJECTION proves the write path works and the
  VALUE is wrong — not that the operation is impossible.
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

- **Row targeting via classic SOAP: possible ONLY by setting the grid's KEY field first
  (corrected 2026-07-20 — the earlier blanket "no random access" claim was WRONG).** Setting a
  **key** field DOES select that row: on CS205000 `AttributeDetails`, `set ValueID='BBB'` +
  `delete_row` deleted **BBB** specifically, twice over (then `CCC`), leaving the other rows —
  including row 0 — completely untouched (original ValueID *and* description intact, so nothing
  was edited-in-place). Two hard conditions:
  1. the key field must be **exposed as a settable field in that container's schema** — where it
     isn't (`EmployeeBankDetailID` is absent from PY309000's `BankDetails`) there is nothing to
     select with, and you really are stuck on row 0;
  2. it must be the **key** — setting a NON-key field never navigates, it edits the current row
     (see the grid-write semantics below).
  The **modern plane** (per-row GUID identity) remains the fallback when the key isn't exposed —
  but it needs `/structure` column metadata, which some grids omit entirely (§11).
- **Classic grid-write semantics (2026-07-20).** Three rules — note the differing evidence
  strength, rule 1 is far better established than 2–3:
  1. **`set` on a NON-KEY grid field MODIFIES THE CURRENT ROW — it never navigates/locates.**
     (A **key** field is the exception and DOES select the row — see the row-targeting bullet
     above; every test below used non-key fields.)
     **Directly observed, cross-module: PY309000 `EmployeeBankDetails` (custom Payroll) AND
     GL301000 `GLTranModuleBatNbr` (stock GL).** On GL301000 the sharp version of the test was
     run: setting `TransactionDescription` to `"Credit line"` — a value lines 2 and 4 *already
     had* — still overwrote **row 0** and left those lines untouched. So `set` does not locate a
     matching row even when an exact match exists; a preceding `set` never selects a row
     (despite reading like navigation), it edits the current one.
  2. **`delete_row` deletes ROW 0**, whatever you set before it — so a `set`+`delete_row` pair
     meant to "select then delete" instead *edits row 0, then deletes row 0*. **DIRECTLY
     OBSERVED** on CS205000 `AttributeDetails` (a throwaway 3-row attribute: `delete_row` with no
     preceding set removed `AAA`, leaving `BBB`+`CCC`).
  3. ~~Sets issued after a `delete_row` start a NEW row.~~ **REFUTED — they EDIT THE SURVIVING
     ROW.** Directly observed on CS205000: with rows `BBB`,`CCC`, a `delete_row` + `set
     Description` left exactly ONE row — `CCC`, carrying the new description. No new row was
     created. An earlier version of this section asserted the opposite, inferred from a PY309000
     save rejection; that inference was **wrong**.

  **CRITICAL: rules 2–3 are PER-GRID and do NOT generalize.** They were re-tested back on
  PY309000's `EmployeeBankDetails` by rebuilding a 50/50 two-row setup and replaying the exact
  sequence CS205000 semantics say must COMMIT (delete row 0 → sets edit the survivor → one row at
  100%). It was **reproducibly REJECTED** on the sum rule. So `EmployeeBankDetails` demonstrably
  does NOT behave like `AttributeDetails`, and CS205000's result cannot be treated as the
  platform-wide truth.

  **The PY309000 mechanism is UNDETERMINED — two candidates fit equally and cannot be separated
  on that grid:** (a) `delete_row` silently no-ops (the known per-grid quirk `_verify_deletes`
  exists for — classic `DeleteRow` can return ok while the row survives; reproduced GL202500,
  fine CA203000), so the sets edit CIMB → 100+50 = 150 → reject; or (b) the delete fires but
  sets-after-delete start a NEW row here (the old rule 3), giving survivor 50 + new 100 = 150 →
  reject. Both yield exactly 150. An isolated delete test can't separate them either, because
  deleting row 0 of a 50/50 pair leaves 50 — which the invariant rejects regardless. **Only a
  grid with no cross-row invariant can be characterised at all.**

  **Practical rule: do not assume grid-write semantics transfer between grids.** Rule 1 (a
  non-key `set` edits the current row) held on every grid tested (PY309000, GL301000, CS205000).
  Rules 2–3 hold on CS205000 and provably not on PY309000's bank grid. Characterise the specific
  grid you're writing to, and **always read back** — `ok:true` proves nothing. Meta-lesson from
  this thread: each rule survived until it was tried on a *different* screen, and four separate
  claims were overturned that way (the selector hint's cause, rule 3, rule 3's generality, and
  finally the blanket "no row targeting" limit).
- **`_verify_deletes` false-positived on every successful targeted delete (found + FIXED +
  LIVE-VERIFIED 2026-07-20, v0.64.11).** The read-back guard flagged a silent no-op whenever the verification
  Export returned ANY rows — never checking that a returned row actually *carried* the searched
  value. On CS205000 the classic Export's filter does not discriminate at all: filtering
  `AttributeDetails.ValueID` for an **existing** value and for a **deleted** one both return the
  same header-level row with a **blank** detail column. So a delete that genuinely worked came
  back `ok:false` + "STILL EXISTS". The guard now (a) flags a real no-op only when a returned row
  carries the value, (b) reports `delete_verified: "unverified"` + a note when rows come back that
  do NOT carry it (the filter isn't discriminating, so absence proves nothing either) instead of
  failing the call, and (c) passes only on a genuinely empty read-back. Verified live after the
  fix shipped: the identical command that returned `ok:false` + "STILL EXISTS" now returns
  `ok:true` + `delete_verified:"unverified"` + the note, with `run_dac_odata` confirming the
  targeted row really was gone and row 0 untouched. **Lesson: a verification step is itself code
  that needs verifying — this one had been silently mis-reporting since 2026-07-15**, and it only
  surfaced because a delete that was *known* to have worked was reported as failed. The design
  point is the **third state**: pass/fail alone forced a screen where verification is *impossible*
  to be reported as failure; "I can't tell, here's why, confirm with X" is a legitimate verifier
  outcome.

  **DOUBT RAISED AND CLOSED — the silent-no-op phenomenon is REAL; only the guard's
  implementation was wrong.** Because the guard was written 2026-07-15 on the strength of a
  "silent no-op reproduced on GL202500", and that observation used the same non-empty-rows logic
  now proven to false-positive, it was fair to suspect the original finding was itself a false
  positive. **Re-tested safely** (throwaway account `Z99999`, Type E, zero transactions, created
  and deleted on GL202500 — never touching a referenced account): `set Account` + `delete_row` +
  Save **deleted it cleanly**, `delete_verified: true`, absence confirmed by `run_dac_odata`.
  The decisive detail: `delete_verified: true` is only reachable when the verification Export
  returns **nothing**, so **GL202500's Export filter DOES discriminate** (unlike CS205000's). The
  old buggy logic would therefore have taken the same `else` branch and ALSO passed this delete —
  **so the bug cannot explain the 2026-07-15 report.** That report stands: the row genuinely
  survived. Most plausible cause is a legitimate refusal reported without an error — per the KB,
  *"an account that has transactions posted cannot be deleted"* (deactivate instead), so a
  DeleteRow on a referenced account is refused silently rather than faulting. Net: the guard's
  PURPOSE is vindicated, its IMPLEMENTATION was broken, and the breakage only ever bit screens
  whose Export doesn't discriminate. Candidate (a) in the PY309000 analysis above stands
  unweakened.

  **Reliable patterns:** INSERT = `new_row` + sets. TARGETED UPDATE/DELETE = `set <KEY field>` to
  select the row, then edit or `delete_row` (see the row-targeting bullet above — this works).
  **The classic-SOAP plane is stuck on row 0 when the key is NOT a settable field in that
  container** (`EmployeeBankDetailID` is absent from `BankDetails`). Escalation order for that
  case: (1) modern `ui_delete_grid_row` — needs `/structure` column metadata, which some grids
  omit (§11); (2) **`aspx_delete_grid_row` / `diagnose_save_error(operation="delete")` — the
  classic ASPX grid often exposes the key even when BOTH of the above can't see it** (proven: the
  ASPX plane addresses PY309000's bank rows by `EmployeeBankDetailID`, which is invisible to SOAP
  and `/structure`); (3) browser UI only if even the ASPX grid lacks the key. See §11 for the
  ASPX-delete mechanics + the FULL-KEY requirement. The old delete-every-row-and-reinsert
  workaround is now a last resort — it **burns identity values every time** (EMP001's bank row
  went 14542 → 14547 → 14550 → 14552 across restores).
- **A delete of a REFERENCED row is refused SILENTLY — `ok:true`, no error, row survives.** This
  is the real "silent no-op delete" (re-confirmed 2026-07-20, see the `_verify_deletes` entry
  above): Acumatica blocks the delete because something else points at the row, but the classic
  Submit reports success rather than faulting. Per the KB, *"an account that has transactions
  posted cannot be deleted"* — deactivate instead (clear **Active**). Expect the same shape
  wherever a row is referenced (a posted GL account, a bank row used by payroll, …). A fresh,
  unreferenced row on the SAME grid deletes cleanly — proven by creating and deleting a
  zero-transaction account on GL202500 — so a failed delete says the row is referenced, NOT that
  the grid can't be deleted from. **Always read back with `run_dac_odata`; `ok:true` proves
  nothing here.**
- **Order destructive multi-step submits so a mis-fire VIOLATES a business rule.** The three failed
  revert attempts above each tripped `ValidateBankPercentSum` ("Percent should be 100 for sum of
  all banks") and were rejected atomically with the data verified untouched between tries. That was
  by design: the commands were sequenced so any wrong row-targeting would leave the sum ≠ 100 and
  fail, rather than silently persisting corrupted data. On a grid with a cross-row invariant, let
  that invariant be your safety net — and always read back, since `ok:true` alone proves nothing.
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

**The clean→clean dirty check FALSE-POSITIVES on a no-op re-set — now guarded by a read-back
(v0.64.18, live-confirmed).** The graphIsDirty net flags any set that leaves the graph clean→clean
as "silently refused". But clean→clean is a refusal ONLY if the field does not already hold the
value: re-setting a key (or any field) to its CURRENT value on an existing record is a no-op, not a
refusal. The net's earlier "no false positives" was only ever verified on GL101000/AP101000 with
NON-key fields — a key field on an existing record is exactly the untested case, and it fails.
`ui_screen_action` now runs `reconcile_rejected_sets`: one read-back that splits genuine refusals
(field holds something else → stay in `rejected_fields`, `ok:false`) from no-ops (field already
holds the sent value → moved to informational `noop_fields`, `ok` untouched). The equality compare
lives ONLY here and is safe precisely because this path sees only clean→clean fields, which store
verbatim (keys/CDs), never the reformatted types (dates/selectors) that go dirty and never reach it
— which is why the read-back guard above still forbids value-equality but this may use it. A failed
read-back keeps everything as genuine (never drop a real refusal).

**LIVE-CONFIRMED before/after on CS102000 `BAccount.AcctCD` (branch MAIN, 2026-07-20):** navigate
to the branch, re-set `AcctCD="MAIN"` (its own value) → `graphIsDirty` stayed false. OLD code:
`rejected_fields:[AcctCD]`, `ok:false`, "SILENTLY REFUSED … Fix and re-run" — on a branch that was
completely intact. NEW code (both a direct call and via the MCP): `noop_fields:[AcctCD]`, `ok:true`,
no warning; branch unchanged (`run_dac_odata`). The live value arrived as a **space-padded selector
dict** `{id:"MAIN      "}` and the normalize (strip + selector-id extraction) matched it against the
sent `"MAIN"` — a shape the unit tests hadn't exercised that literally. (CS101500, named in the same
report, could NOT be tested on csmdev: its `/structure` hits the duplicate-key server bug — §1/§the
grid-500 note — so `ui_screen_action` can't run there at all; the original CS101500 observation was
on an instance where `/structure` works.)

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
- **`/structure` can itself return a bare HTTP 500 from a genuine Acumatica server bug, not a
  caller/grp-mcp issue** — proven live on EP203000 (Employees): the endpoint's own metadata-builder
  throws an unhandled .NET Dictionary duplicate-key exception (`"An item with the same key has
  already been added."`, likely two fields/views colliding under an internal key) and returns
  `{"title": "...", "status": 500}` with no further detail. `ui_get_structure`/`ui_screen_action`/
  `screen_capabilities` cannot work on such a screen — there is nothing to retry or fix client-side.
  `_ui_error` detects this specific message and labels it a SERVER-SIDE bug rather
  than leaking a bare ".NET exception text" as if the caller did something wrong; `screen_get_schema`
  (classic SOAP, a different metadata source, unaffected by this bug) is the proven-working
  fallback — verified live to return EP203000's full schema. `screen_capabilities` degrades
  gracefully on this specific error (returns SOAP-only recommendations + `modern_plane_unavailable`)
  instead of propagating the exception, since its whole job is answering "which plane do I use" —
  crashing there is exactly backwards. `diagnose_save_error` is unaffected either way (it never
  calls `/structure`, discovering everything from the classic page's own HTML).
  Confirmed server-side 4 independent ways (grp-mcp's own client, raw httpx bypassing grp-mcp
  entirely, and — twice — the real user's own authenticated browser session), each with a fresh
  `traceId` (proves live reproduction, not a cached replay). A full sweep of csmdev's entire
  SiteMap (2921 distinct ScreenIDs, 8-way concurrency, one shared login, ~34min) found **12
  screens affected total, not just EP203000**: `AP201000` (Vendor Classes), `AP303000` (Vendors),
  `AP305000` (Batch Payments), `AR201000` (Customer Classes), `CS101500` (Companies), `DS1C3000`
  (Manage Signature), `EP203000` (Employees), `EP301020` (Expense Receipt), `IN202000`
  (Non-Stock Items), `IN202500` (Stock Items), `PM301000` (Projects), `SM204570` (Source Code).
  The modern DATA protocol (POST `/ui/screen/<ID>` bootstrap) fails with the IDENTICAL error on
  these screens — the crash lives in a shared screen-descriptor builder, so the whole modern
  plane is dead there, not just `/structure`.
  **Root cause — verified vs. open (2026-07-19):** the bug does NOT reproduce on stock
  instances — local 2025R2 SalesDemo and 2026R1 return clean `/structure` for every affected
  screen — so it is INSTANCE-SPECIFIC, triggered by csmdev's published customization stack, not
  a stock Acumatica flaw. An earlier hypothesis (cross-container friendly-field-name collision in
  the serializer) was REFUTED by controlled comparison: stock AP303000 carries 93 such label
  collisions and works fine — label collisions are ubiquitous on large screens and harmless. The
  affected list matches GRP customization footprints (the `GRP.EInvoicing` DLL extends exactly
  these master/document screens; `DS1C3000` is a DigiSign custom screen whose same-CP siblings
  `DS1C1000`/`DS1C2000` do NOT crash). The exact colliding element inside the customization
  layer was not isolated — no server stack trace is externally reachable (Request Profiler off,
  Error.aspx and every Accept-header variant return the same bare RFC7807 envelope). None of
  this changes the fix: `_ui_error` matches on error TEXT, not a screen_id list, so any screen
  that hits this — whatever the trigger — is covered.

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

Empirically-confirmed build order (each step gates the next; re-proven live on a **blank**
2026R1 tenant 2026-07-20 — the order below corrects an earlier version of this list AND the
old `canonical_order` in setup_map.json, both of which put GL prefs before the CoA and the
calendar generation before GL prefs, and both of which fail on a blank tenant):

1. **Features** (`CS100000`) — set the feature flag, then **`activate_features`** — the apply is the
   `RequestValidation` action (flips `Pending → Validated`). It **recompiles the site (~1–3 min)**;
   the in-flight call often 500s as the app pool restarts, so it's **fire-and-verify**
   (`activate_features` polls `ActivationStatus`). **v0.66.0:** a server-side rejection of the
   Enable command (e.g. an NRE naming a feature field) now returns `status: "failed"` with the
   FULL error — it used to be swallowed into `"in_progress"`, sending you into an endless poll
   on an activation that never started. Only a dropped connection still means "keep polling".
2. **Financial calendar** (`GL101000`) — `create_financial_calendar(first_year, starts_on=…)`.
   **v0.66.0:** the tool now also sends **`FirstPeriodStartDate`** (DAC `PeriodsStartDate`,
   defaulting to the year start; override with `periods_start_date=`) — it is REQUIRED and
   `AutoFill` does **not** derive it: without it the Save fails on a blank tenant with
   *"Please configure all the Financial Periods for the Year"*. Note the plane-specific name:
   the classic plane wants the friendly `FirstPeriodStartDate`, not the DAC name.
3. **Ledger** (`GL201500`) — `create_ledger`. **This alone does NOT make it the org's Actual
   Ledger.** You must separately **link ledger → org** on **`CS101500`** (Companies → Ledgers tab).
   GL screens behave as if no ledger exists until this link is made. The Ledger entity's type
   field is **`Type`** (`"Actual"`/`"Statistical"`/…), NOT `BalanceType` — and as of v0.66.0
   `create_or_update_entity` REJECTS unknown field names instead of letting Acumatica silently
   drop them (the old behavior defaulted `Type` to Actual and only failed two records later).
4. **Account classes** (`GL202000`) — optional, but do it before the CoA if custom classes are
   wanted. On a fresh instance the read 500s with `PXSetupNotEnteredException[Branch]` until a
   company + branch RECORD exist (CS101500/CS102000).
5. **Chart of accounts** (`GL202500`) — `chart_of_accounts(accounts)` (grid writer). Must include
   Retained Earnings + YTD Net Income, both **Liability**.
6. **GL preferences** (`GL102000`) — `set_gl_preferences(retained_earnings, ytd_net_income, …)`.
   HARD GATE, must come AFTER the CoA: a bare Save fails with *"'YTD Net Income Account' cannot
   be empty. 'Retained Earnings Account' cannot be empty."* (the "a bare Save materializes the
   setup row" rule does NOT hold for GL102000). Both accounts must be **type Liability**.
7. **Generate periods** (`GL201000` "Generate Calendar") — REQUIRES GLSetup (step 6): fails with
   `SetupNotEntered` until GL preferences exist. Classic SOAP silently no-ops the action;
   `generate_master_calendar` drives it on the **modern plane** (proven).
8. **Open periods** (`GL503000`) — `manage_financial_periods` (`Action=Open`, `ProcessAll`) —
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
- **Column names are the CLASSIC grid's dataFields, not the modern plane's field names** (proven
  live, GL301000: modern `CreditAmt` vs classic column `CuryCreditAmt`). A RowChanges `Key` the
  grid doesn't know either CRASHES the callback (raw `e`-prefixed exception text instead of the
  `0|` envelope — the ICallbackEventHandler error channel) or silently no-ops with a clean full
  response. Harvest the authoritative list from a targeted grid `Refresh` first (its response's
  `"dataField"` entries; present even at zero rows) and validate keys against it. The Refresh
  also PRIMES the server-side graph — a Save without one can silently no-op even with correct keys.
- A screen-specific caveat class exists: on a screen whose validation is deferred (e.g. GL301000's
  batch balance only checks at Release, and an On-Hold batch saves drafts freely), the "failing"
  change you replay may actually be VALID — it will PERSIST (Acumatica auto-clears the opposite
  amount column rather than erroring). `possibly_saved: true` is the tool's honest signal; verify
  via OData and revert (ui_update_grid_row restored it cleanly).
- **`possibly_saved: true` (no alert, no grid errors, graph not marked dirty) is UNCONFIRMED, not
  a guarantee — CORRECTED (2026-07-19) after this exact shape stopped reproducing.** The original
  GL301000 persist above was real and OData-verified at the time, but the IDENTICAL request shape
  failed to persist on repeat in a fresh session — 5/5 attempts, every target field echoed
  `ReadOnly="False"` (so a locked cell is *a* confirmed cause — proven separately on AP301000,
  where an existing line's `AccountID` was `ReadOnly="True"` and the edit silently dropped — but
  not the *only* one; the full mechanism behind the flip is still not understood). The tool now
  checks each target field's `ReadOnly` on the row's own Save-response echo (`_row0_readonly_fields`,
  aligned from the END of the cell list — a fixed leading file/note-icon cell offsets the position)
  and names it when found; otherwise it says plainly that no explanation was found. Either way the
  result carries a `note` telling the caller to verify via `run_dac_odata` before trusting
  `possibly_saved` in **either** direction — do not read a missing note as "definitely saved" or a
  present one as "definitely not."
- **Headerless LIST screens (the grid IS the primary view, e.g. GL202500)**: pass `record_key={}`
  — navigation is skipped (there is no header record to load; forcing it fails "record did not
  load"). LIMITATION (proven live, insert AND update): RowChanges against the PRIMARY grid never
  bind — the Save answers clean with `isDirty:1` and zero error text. ROOT CAUSE (captured live
  2026-07-20, superseding the earlier "per-cell commit flow" hypothesis): the browser addresses
  the RowChanges callback to **the grid control** (`__CALLBACKID=ctl00$phL$grid`, one `Save|` +
  RowChanges per ROW commit, answered with `<UpdateResult Status Affected>`), and only a separate
  ds-addressed `Save|` (`ctl00$phDS$ds`, Ctrl+S) persists to the DB. This client always posts
  RowChanges to the ds — the right addressee for child grids under a header, the wrong one for a
  headerless primary grid. Same envelope grammar, same cell format; emulable if ever needed, but
  headerless screens are normally written via contract REST / the modern UI, so the tool keeps
  the explicit `note` saying validation never fired (an empty error list must not read as "no
  problem found"). Detail/child grids under a loaded header remain the fully supported shape.
- **A replayed change that is actually VALID persists** — the tool requires `allow_write` and
  flags `possibly_saved: true`; only replay changes that already failed.
- One-shot scripts against this plane must `await logout_session_cache()` on exit or each process
  orphans a "Max Web Services API Users" seat until idle-timeout.

**Cross-module validation matrix** (each screen is a different module/codebehind, run against a
real record, DB state confirmed unchanged after every case):

| Screen | Module | Shape | What it confirmed / broke |
|---|---|---|---|
| PY309000 | Payroll (custom) | detail grid under header | baseline protocol; real cross-row error recovered |
| GL301000 | General Ledger | detail grid under header | column-name mismatch (`CreditAmt`→`CuryCreditAmt`); server crash channel |
| GL202500 | General Ledger | headerless list (grid = primary view) | `record_key={}`; RowChanges can't bind to a primary grid |
| AP301000 | Accounts Payable | detail grid under header | read-only-cell no-op (`AccountID` locked on an existing line) |
| AR301000 | Accounts Receivable | detail grid under header | clean pass — column guard + real range-validation error, no new fix needed |
| CA202000 | Cash Management | master-detail SETUP screen (not a transaction doc) | a grid can have NO classic equivalent at all; escalate to modern-plane writes (below) |

AR301000 is the first of five where nothing broke: the general mechanisms (column guard, error
extraction, honest-uncertainty labeling) held with zero code changes, recovering a genuine
field-range error (*"The value must be less than or equal to 100"* on `DiscPct`). None of the
fixes above are screen-specific code (`if screen_id == ...` doesn't exist anywhere in `aspx.py`) —
each screen exposed a different GENERAL failure class, fixed once at the mechanism level, not
patched per screen.

**When `diagnose_save_error` can't reach the grid at all — escalate to the modern plane directly.**
Proven live on CA202000: the `ETDetails` grid (Entry Types tab) has **no classic ASPX markup** —
it's a newer grid the classic page was never given, so `diagnose_save_error` correctly refuses
with `"no control bound to view 'ETDetails'"` rather than lying about it. That refusal is the
END of what the classic-only tool can do, but NOT the end of what's diagnosable — the modern
plane's own write tools should be tried directly as the next step, and they can succeed even when
`diagnose_save_error` can't:
- `ui_read_grid` / `ui_update_grid_row` hit the SAME wall for this specific grid (0 rows, "no row
  matches key") — a plain read/update genuinely can't see this grid's data through either plane
  without some activation step neither's simple call path triggers.
- `ui_insert_grid_row`, however, **worked** and returned a real, specific, useful error:
  `"Another process has added the 'CashAccountETDetail' record. Your changes will be lost."` —
  Acumatica correctly caught the attempted duplicate-key insert. Insert doesn't need to FIND an
  existing row first (update/read do), so it can reach real server-side validation that update
  and read cannot on a grid whose rows aren't otherwise visible.

**Escalation order for a failed grid save with only a generic error**: (1) `diagnose_save_error`
first — usually the richest per-field/per-row detail when the classic plane can reach the grid;
(2) if it refuses because the grid has no classic dataMember, try `ui_insert_grid_row` /
`ui_update_grid_row` directly on the modern plane — don't stop at a `ui_read_grid` 0-row result,
since insert in particular can still reach validation a read/update can't. Verify with
`run_dac_odata` either way; revert any accidental real persist immediately (this was tested live,
reverted, and re-verified — see the ASPX protocol memory for the exact repro/revert).

**Escalation order to DELETE a specific grid row**: (1) modern `ui_delete_grid_row` (key-addressed,
needs `/structure` column metadata); (2) classic `screen_submit` `delete_row` — only reaches a
non-row-0 row if the key is a settable field in the container; (3) `aspx_delete_grid_row` — the
ASPX grid usually exposes the key even when the other two can't (see the ASPX-delete paragraph
below for the full-key requirement); (4) browser UI. Read back after any of them.

**ASPX targeted DELETE — the plane can address a row that SOAP and `/structure` both can't
(built + proven live 2026-07-20, v0.64.12).** The classic ASPX grid exposes its key as a real
dataField even where the SOAP container schema AND the modern `/structure` omit it — so
`aspx_delete_grid_row` (or `diagnose_save_error(operation="delete")`) can remove a specific row by
key on those grids. Two facts proven live: (a) `row_key` genuinely BINDS on this plane — an update
keyed to PY309000's 2nd bank row changed only that row, row 0 untouched; (b) a keyed `<Deleted>`
RowChanges section PERSISTS and hits the right row — on CS205000 `AttributeDetails`, deleting the
middle value by key left the other two. **HARD REQUIREMENT: `row_key` must be the row's FULL key
— every key cell.** A single-column identity key needs one cell (`EmployeeBankDetailID`); a
COMPOSITE key needs all parts — CS205000's `ValueID` alone SILENTLY no-op'd (`possibly_saved:true`,
nothing deleted), `AttributeID`+`ValueID` worked. A `delete` with no `row_key` is refused up front
(it would fall back to row 0). Caveat: on a grid with a cross-row invariant a standalone delete may
still be rejected — PY309000's `EmployeeBankDetails` delete ENGAGES (sum drops, a row leaves the
working set) but the 100%-sum rule rejects the Save; that's the same domain constraint a human
hits, not a tool limit (rebalance in the same operation). **Two live over-claims this build, both
caught by reading the DB back**: a partial-key delete that reported `possibly_saved:true` had
changed nothing, and "engages" on PY309000 is not "persists". `possibly_saved`/`ok:true` prove
nothing — always `run_dac_odata`.

**v0.64.13 — the plane can now READ ITS OWN ROWS BACK, which turns two of the above from
"documented footguns" into enforced behaviour.** The grid Refresh callback that
`replay_grid_save` already ran to harvest column names *also* carries every row; it was being
thrown away. `_grid_rows` now parses it. Two payoffs:
- **Keys that match zero or many rows are REFUSED before the write.** The `row_key` is matched
  against the grid's real rows: zero matches → `refused` + `grid_rows`; more than one match →
  `refused` as a partial key, because which row the server picks is not a thing to discover by
  deleting one. **KNOWN GAP, measured live 2026-07-20 — this does NOT catch a partial key that is
  unique within the grid.** `{"ValueID": "BBB"}` on CS205000 matches exactly ONE grid row, so it
  passes the pre-flight, yet the server still requires the FULL key and silently no-ops (verified:
  `possibly_saved:true`, rows 3→3, nothing deleted). The grid payload carries no "is key" flag, so
  the tool cannot tell which columns form the key. **The post-Save read-back below is what catches
  this case** — the two checks are layered on purpose, and the pre-flight alone is not sufficient.
- **`possibly_saved`'s ambiguity is resolved by a post-Save re-read.** `save_verified` (plus
  `delete_verified` for deletes) is `true` / `false` / `"unverified"` with a reason: delete checks
  the key is gone, insert checks the row count grew, update checks the keyed row now carries the
  values sent. **Scope, stated honestly: this reads the SCREEN's rows, so it proves the grid
  changed, not that the transaction committed** — it rules out the silent no-op this plane is
  known for, and nothing more. `run_dac_odata` is still the authority on database state.

Two parsing details that are load-bearing (both from a verbatim live capture, kept as a test
fixture): the Props JSON is entity-escaped (`&quot;`) while the `<Rows>` XML beside it is literal,
**in the same payload** — so values are unescaped individually, never wholesale; and the columns
array contains leading bare `{}` entries (the file/note indicator cells) plus `"visible":0`
columns that are still real `<Cell>` positions, so cell→field alignment is positional over ALL
slots rather than an end-offset guess.

**A docstring shipped in v0.64.12 promised `deleted_verified` "from a real `run_dac_odata`
read-back" — no such read-back existed.** It was an overclaim written into the docs of the very
tool whose purpose is to stop the caller trusting unverified success. v0.64.13 makes the claim
true (via the grid re-read, and the docstring now states that narrower scope precisely).

**REFUTED (2026-07-20): the row index is NOT a row locator, and `Row i="0"` does NOT collide with
an existing row.** An external bug report blamed `replay_grid_save`'s hardcoded `i="0"` for
PY309000's erratic inserts, and this file asserted that as root-caused. Direct testing killed it.
Throwaway grid `ZZIDX` on csmdev CS205000 `AttributeDetails`: inserting at `i="0"` into a ONE-row
grid **appended** (existing row intact), and inserting at `i="99"` into a TWO-row grid **appended a
third row cleanly** — an index that addressed anything real could not behave that way. `i` is the
row's ordinal *within the RowChanges batch*; the server assigns the new row's position itself.
This is coherent with the rest of the plane: `delete` and `update` target rows through the
`row_key` CELLS, never through `i`, so **nothing here uses the index to address a row**.

What remains TRUE: PY309000/`EmployeeBankDetails` returned DIFFERENT error text across IDENTICAL
repeat inserts — `"'Employee Bank' cannot be found in the system"` once, `"'Employee Bank' cannot
be empty"` the next, on the same valid input. Real validation of unchanged data does not do that,
so the symptom is real; only the *explanation* (row-index collision) was wrong.

**Leading candidate now (2026-07-20 reframe — NOT proven): stale/sticky graph state.** Acumatica's
graph is sticky across sessions (§4d), and this session produced DIRECT evidence of stale artifacts
on this exact grid: a `screen_submit` split of EMP001's bank rows spawned a PHANTOM row carrying an
account number **never sent** — leftover uncommitted state flushed into the Save (and the grid was
left at 200%, with the percent-sum invariant NOT firing on the SOAP plane; see §11a finding 1). If
leftover rows from earlier attempts contaminate what the validator sees, the input is identical but
the graph is not — which fits "different error each time" far better than anything about the request.
**Honest caveat: this evidence is CROSS-PLANE** (the phantom row was on the SOAP `screen_submit`
path; #4's symptom was ASPX-plane inserts), so it is a strong candidate, not a proven cause. Direct
reproduction is impractical and low-value: PY309000's read-back is inert (columns, no rows — §11a
finding 2), so grid state can't be observed between attempts without repeated real-employee
mutations, and the tool already warns about the symptom. **Investigation parked here.**
`replay_grid_save` attaches a `note` to any insert that returns error text — ruling out the index
mechanism and naming the stale-graph candidate without asserting it. **Lesson: "root-caused to the
exact line" was an inference from a plausible-looking code smell, never a test — and even the
replacement is labelled a candidate, not a conclusion.** The setup for the *index* test was itself
wrong twice before it was right — the first throwaway attribute was ControlType=Text (which
legitimately has NO value list, so every insert was correctly dropped, on BOTH planes, silently); a
`screen_submit` "ok:true" for those rows meant nothing.

**CLOSED (2026-07-20) — headerless list-screen grid binding (GL202500), root cause CAPTURED.** The
long-standing "RowChanges never bind on a headerless primary grid" limit was resolved by capturing
the browser's actual save-callback traffic on GL202500 (a live Active-checkbox toggle, discarded via
Cancel; DB untouched). Verdict: the earlier "per-cell commit flow" hypothesis was directionally
right but wrong in detail — the browser fires a per-ROW commit callback addressed to **the grid
control** (`__CALLBACKID=ctl00$phL$grid`, `Save|` + `<RowChanges>` with `Commit="1"`; response
`<UpdateResult Status="1" Affected="1">` echoing the row with per-cell ReadOnly flags; ds flips
`isDirty:1`), then persists with a separate ds-addressed `Save|`. Our replay's ds-addressed
RowChanges is simply the wrong ADDRESSEE for that one grid shape. The envelope grammar is identical,
so grid-addressed emulation is feasible — still not built (headerless screens are written via
contract REST / modern UI), but the question is now answered by capture, not inference.

**The "cannot be found in the system" selector error is a SubstituteKey — send the display
name, NOT the code/id (root-caused from source + a live persisted write; CORRECTS an earlier
wrong conclusion).** PY309000's `EmployeeBankID` and `TptEmployeeBankID` reject a confirmed-valid
value with *"'Employee Bank' cannot be found in the system"* — the code `MBB` AND the numeric id
`1148` both fail, even though the bank exists (`run_dac_odata` on `CSPYEmployeeBank`). An earlier
version of this doc called that an "unresolvable Acumatica-side wiring gap grp-mcp can't work
around." **That was wrong.** Reading the customization source (Payroll.dll's DAC, from the vendor
source zip) showed the cause: `[PXSelector(... , SubstituteKey = typeof(CSPYEmployeeBank.name))]`.
A `SubstituteKey` selector accepts the target record's DISPLAY value — here the bank's **name**
(`"Malayan Banking Berhad (Maybank)"`), not its code or id. Sending the name **resolves**, proven
by a real committed insert via `screen_submit` (a new `CSPYEmployeeBankDetail` row persisted, bank
id 1148, verified in the DB). So these fields ARE writable through grp-mcp today — the whole
failure was a value-form mismatch. General rule: for a selector, send the value of the column
named by `ui_get_structure`'s `lookup.value_field`; when that's absent (see #2 below) or a
known-good value gets "cannot be found," it's a SubstituteKey — query the target table and send
its **name/description**. (Third field, `EmployeeInstitution.SchemeCD`, is a different shape — an
aggregated `Search4<…GroupBy>` selector, no SubstituteKey — not retested with this understanding.)

**Selector-hint annotation (shipped):** because the SubstituteKey can't be read from any runtime
API (it lives only in the compiled DAC), grp-mcp can't auto-translate the value for a
metadata-blind grid — but it now DETECTS the "cannot be found in the system" error and attaches an
actionable `hint`/`selector_hint` to the result of `screen_submit` (per-field-error `hint`) and
`diagnose_save_error` (`selector_hint`). Pure helper `_selector_value_hint` in `screen.py`.
**The message names BOTH causes of that error, commonest first** — (1) the value genuinely does
not exist in the target table, (2) it exists but was sent in the wrong FORM (SubstituteKey). The
tool cannot distinguish them from the error text alone, so it must not assert either. v0.64.9
led with (2) alone and thereby mis-diagnosed (1); **cross-screen testing on GL301000 caught it**
(a genuinely nonexistent account `ZZZ999` → `"'Account' cannot be found in the system"` got a
confident SubstituteKey explanation that was simply wrong). Fixed in v0.64.10 — a reminder that
validating a heuristic on the ONE screen that inspired it will confirm the happy path and miss
the mis-fire. Cross-screen validation also proved the hint reaches errors via all three channels:
on PY309000 it arrived in `alert`, on GL301000 only in `cell_errors`/`rows_error_text` (the alert
was the generic "raised at least one error") — which is why the check scans all of them. (An auto-translate for *exposed* selectors was
considered and deferred: it only helps selectors whose `/structure` metadata is present — which
mostly already work via `value_field` — and it's gated on verifying whether `value_field` reports
the substitute key vs. the display code when they differ, which wasn't confirmable on an exposed
field of that shape.)

**Also (not a grp-mcp bug):** `ui_get_structure`'s `grids` section returns ONLY the key field for
every grid on PY309000 (`EmployeeBankDetails`, `EmploymentHistories`, `EmpPayTransactions`,
`EmployeeProjects`, `EmployeeInstitutions`, `EmployeeCashAward` — all six show
`columns: [<key field only>]`) — grp-mcp faithfully parses whatever Acumatica's `/structure`
returns (`cd.get("columns")`, no filtering), so the gap is server-side. This is also WHY the
SubstituteKey can't be auto-resolved for this grid: with no column metadata there's no
`lookup.value_field` and no target-DAC to translate against — hence the hint (above) rather than
an auto-fix. Whether the metadata gap is PY309000-specific or broader is unconfirmed — not swept.

### 11a. Multi-section batch — `aspx_grid_batch` (v0.64.15): change an INVARIANT-guarded grid

`aspx_delete_grid_row` and `diagnose_save_error` each send ONE RowChanges section. A grid with a
**cross-row invariant** (PY309000 `EmployeeBankDetails`: percent must sum to 100) rejects a
standalone delete — the survivors no longer sum to 100. A human deletes AND rebalances in one Save.
`aspx_grid_batch(screen_id, record_key, grid_view, operations)` does the same: several ops
(`{operation, cells, row_key}`) become sibling sections (`<Deleted>` + `<Modified>` + …) in ONE
envelope, one atomic Save. Every op is pre-flighted against ONE grid snapshot (unknown-column /
no-match / partial-key) and the WHOLE batch is refused (`refused_ops`, nothing sent) if any op
fails — a half-applied atomic Save is worse than none. After a clean Save the grid is re-read once
and each op gets its own verdict in `verifications` (`save_verified` true|false|"unverified");
`all_verified` is their AND. The single-op path (`replay_grid_save`) and the batch path now share
one set of helpers (`_preflight_op`, `_cells_xml`, `_read_save_response`, `_verify_one_op`) so they
validate and verify identically — the refactor is behaviour-preserving (all prior tests still pass).

**Evidence — all tested LIVE (csmdev, 2026-07-20), delete+rebalance PASS now proven END-TO-END:**
- **Test A (mechanism):** throwaway CS205000 `ZZBATCH`, 3 rows. One batch = `<Deleted>`(BBB) +
  `<Modified>`(CCC.Description). `all_verified:true`, rows 3→2; **`run_dac_odata` confirmed BBB gone
  AND CCC changed** — both sections committed atomically from one Save. The one real unknown
  ("untested whether the server accepts a multi-section envelope") is closed.
- **Test B1 (invariant validates NET state):** real PY309000 bank grid, batch updating EMP001's only
  row 100→50 → `alert:"Percent should be 100 for sum of all banks"`, `possibly_saved:false`;
  `run_dac_odata` confirmed the row **unchanged**. Decisive: the server reported the sum as **50**,
  i.e. it validated the MODIFIED value — the invariant is checked against the **net post-batch
  state**, and the batch path surfaced the real rule rather than a silent no-op.
- **Test B2 (delete+rebalance PASS, END-TO-END):** on the real PY309000 bank grid, a batch of TWO
  `<Deleted>` + one `<Modified>` (delete two rows, set the survivor to 100) netting to 100 committed
  atomically; **`run_dac_odata` confirmed** EMP001 left with exactly one row at 100. So a
  delete-and-rebalance that a standalone delete cannot do (invariant would reject) PASSES as one
  batch — the whole reason this tool exists, now directly observed, not just composed.

**Three things that setting B2 up TAUGHT (all live, all now documented):**
1. **`screen_submit` corrupted the grid during setup — do NOT use the classic SOAP plane to build a
   multi-row split.** One `new_row` plus an edit-existing-row in a single `screen_submit` produced
   a PHANTOM third row (bank data I never sent) and left the grid summing **200%**, all under
   `ok:true`. This is the documented "phantom artifact rows / values cross" hazard (§3) AND a new
   finding: **the percent-sum invariant did NOT fire on the SOAP plane** (200% persisted) even
   though it fires on the ASPX plane (B1). Different planes, different validation paths — the SOAP
   plane is NOT a safe way to write this grid. The cleanup itself (delete both extras + rebalance
   in one `aspx_grid_batch`) is what became Test B2.
2. **`aspx_grid_batch`/`aspx_delete_grid_row` read-back is INERT on PY309000 child grids.** The
   grid Refresh returns the COLUMNS but **no `<Row>` elements at all** (captured: an 866-byte body,
   `EmployeeBankDetails` columns present, zero rows — the child grid's data never materializes in
   the Refresh, consistent with `/structure` also omitting its columns). So `rows_before`/`after`
   are empty → the row_key pre-flight is SKIPPED and every post-Save verdict is `"unverified"`.
   The WRITE still works (RowChanges match by key cells server-side, DB-confirmed), but on the exact
   grid this plane exists for, **`run_dac_odata` is the ONLY check**. v0.64.16 surfaces this loudly:
   when a keyed op runs on a columns-but-no-rows grid the result carries `grid_rows_readable:false`
   + a `guard_note` saying both guards were inert — so silence never reads as "checked and fine".
3. **The ASPX navigate can fail transiently** ("record did not load — no header dataKey"); the
   identical call succeeded on retry. Worth one retry before concluding a key is wrong.

Caveat carried in the tool: verifying an INSERT inside a batch that also deletes is unreliable
(insert is checked by row-count growth, which the concurrent delete masks) → that op returns
`save_verified:"unverified"`. As always this proves the GRID changed, not that the txn committed —
`run_dac_odata` remains the authority.

### 11c. Classic TREE nodes ARE addressable — "browser-click only" REFUTED (v0.65.0)

`build_company_tree`'s docstring said EP204061 "can't be driven by the API — its parent link is set
by clicking a tree node, and no field/path/command reproduces that (exhaustively proven)." That was
true of the SOAP and modern planes. **It is false for the ASPX plane.** Reverse-engineered + proven
live 2026-07-20:

1. **Selection lives in the tree control's hidden `_state`** — not in any command:
   `<PXTreeView SelectedNodeID="<domId>" SelectedValue="<key>" ParentValue="<parentKey>"/>`
2. **Fire the datasource reload** and the detail form/grids re-bind to that node:
   `__CALLBACKID=ctl00$phDS$ds`, `__CALLBACKPARAM=ReloadPage|<ctl00_phDS_ds LoadedLevel="-1"><![CDATA[]]></ctl00_phDS_ds>`
3. A **node-scoped action** (Up/Down/AddWorkGroup/…) is then just another ds command + Save.

**MEASURED addressing rules** (all combinations tested): `SelectedNodeID`+`SelectedValue` WORKS;
`SelectedValue` alone or a WRONG `SelectedNodeID` FAILS silently. So **the dom id is load-bearing
and must be exact**; `ParentValue` is optional. A **collapsed (lazy) child selects fine** — no
expansion needed — but it has NO markup in the page, so its dom id can never be scraped.

**The dom id must therefore be DERIVED, not scraped.** It encodes the node's sibling-index path
(`_node_0_1_0` = "root's 2nd child's 1st child"), with siblings ordered by **`SortOrder`** — so it
comes from the tree's own DAC (`EPCompanyTree`: WorkGroupID/ParentWGID/SortOrder), which is also the
only complete view of the tree. `_tree_node_dom_id` does this; `aspx_tree_node_action` is the tool
(select-only is safe and needs no gate; an action needs allow_write, allow_delete if it deletes).

**Proof:** selecting two nodes loaded two different records (`selected_name` echo), and firing `Up`
committed a real SortOrder swap to the DB. **Known limit:** `DeleteWorkGroup` fires but stages
NOTHING (`staged:false`) — a silent no-op, almost certainly an unanswered confirmation dialog; the
tool reports that honestly rather than claiming success, and Up/Down do stage. Deleting workgroups
still needs the browser UI (and there, the toolbar Save must actually be clicked — a staged delete
that is never saved looks done in the UI while the DB is untouched; Ctrl+S is more reliable than
the icon).

### 11d. `build_company_tree` mis-nested every tree deeper than one level — FIXED (v0.65.0)

The EP204060 indent was fired as `Right` × **absolute depth** on the row just inserted. Both halves
are wrong, and the real semantics are not guessable — measured with a 4-node probe
(0/1/0/1 presses → ROOT / ROOT / child-of-#2 / ROOT):

- **OFF BY ONE:** the presses issued after inserting node N take effect on node **N+1**, never on N.
- **ABSOLUTE + RESETTING:** n presses set that next node's level to **n**; the level resets every
  step (it does not accumulate, and it is not a delta). `Left` appeared to be ignored.

So the count to issue in step N is simply **the next node's depth**. Before the fix a 3-level tree
came back `verified:false` with children flattened to the wrong parents; after it, the same
structure builds `verified:true` (3 levels + an outdent, parents confirmed against EPCompanyTree).
The tool's own parent read-back is what caught this — a builder that verifies is worth more than one
that assumes.

### 11e. Modern-plane `tree_select` wrote to the WRONG NODE, silently — FIXED (v0.66.6)

`ui_select_tree_node`'s `select_command` defaults to `"EnablePopulate"`, the selection-changed
handler captured from **SM207060**. It is that graph's own action, not a framework primitive — so
on any other tree screen the server simply **ignores** the command: HTTP 200, no message,
`activeRowContexts` echoed back. The selection never takes, and every subsequent `set_field` lands
on whatever node is **current**.

Measured live on **EP205015** (Approval Maps, 2026-07-21): selecting rule `fb88…` and setting
`CurrentNode.Name` renamed the **step** `fa88…` instead — and it **committed**, under `ok:true`
with no notice. Nothing short of reading the DB back would have caught it, which is precisely the
check nobody runs after a clean success. The docstring already warned the command "likely differs
on OTHER tree screens"; the code never checked.

Two fixes:

- **Refuse up front.** `ui_select_tree_node` now validates `select_command` against the screen's own
  `/structure` actions and raises before posting, listing the screen's real actions and pointing at
  `aspx_tree_node_action` (§11c) when a tree has no modern-plane selection handler at all.
- **Let callers override it.** `ui_screen_action` never forwarded `select_command` (or
  `ancestor_keys`), so there was no way to pass a screen's own handler even if you knew it.
  `tree_select` now accepts `{"view", "key", "parent_key"?, "ancestor_keys"?, "select_command"?}`,
  and an unknown tree view is rejected like an unknown grid.

The general lesson is the one §13 states from the other direction: **a validation error proves the
field is live; silence does not prove it is dead** — and here, silence did not prove the write went
where it was aimed. A protocol that ignores unknown commands cannot be driven by defaults borrowed
from one captured screen.

### 11f. Tree columns came from `grids` only → endpoint building was dead — FIXED (v0.66.7)

A tree control is listed under `grids` on some screens and under **`views`** on others.
`ui_select_tree_node` looked only in `grids`, so on SM207060 (whose `EntityTree` is in `views`)
`columns` came back `None` and the control block went out with `columns: []`. **The server answers
an empty column list with no data at all** — the response carries no `controlsData` key, so
`_tree_row_by_title` found nothing and every caller reported *"entity `<X>` not found"*.

That reads as a bad argument, which is why it hid: the real failure was a broken node lookup, and
it took down the whole endpoint-building chain — `ui_tree_dialog_insert`,
`ui_populate_entity_fields`, `generate_endpoint_entity`.

Falling back to `views` for the column list fixes it: the SM207060 root select goes from **0 rows to
591**, and `ui_populate_entity_fields` runs through node lookup, dialog open and fill to a genuine
business refusal (*"The Populate button is disabled"* — correct for an INHERITED `↓` entity, which
must be extended before its fields can be populated).

Two diagnostic notes worth keeping:

- A **bad root key hard-errors** (`"ROOT"` alone → *"Index was outside the bounds of the array"*)
  while the correct one returns 200 with nothing. Same asymmetry §13 states: an error proves the
  input reached live code; silence proves nothing. The silent case was the broken one.
- A follow-up call carrying the same `controlsParams` returned the rows even while the select
  response didn't — that was the symptom that located the empty-columns cause. It was briefly
  implemented as a second round trip and then **removed**: once the columns are right the select
  carries the data itself, and shipping a fallback whose necessity can't be demonstrated is how a
  codebase accumulates cargo.

### 11b. No classic grid at all → routed to the modern plane (v0.64.15)

Some grids render ONLY on the modern plane and emit no classic control config (observed: CA202000
`ETDetails`). The ASPX plane genuinely cannot address those — a real, permanent limit, not a bug.
Rather than surface `find_grid_control`'s bare "no control bound to view" error, the three ASPX
tools (`diagnose_save_error`, `aspx_delete_grid_row`, `aspx_grid_batch`) now catch that specific
case (`_classic_grid_missing`) and return `{no_classic_grid: true, recommend: …}` pointing at the
modern-plane grid tools (`ui_read_grid` → `ui_delete_grid_row`/`ui_insert_grid_row`/
`ui_update_grid_row`), which key rows via `/structure` and need no classic markup. The match is on
find_grid_control's own messages, so an ordinary business/validation error is NOT swallowed as this
case (unit-tested both ways).

### 11g. `navigate()` can land on the WRONG record and say nothing — GUARDED (v0.68.2)

On some screens the ASPX plane **cannot navigate at all**, and before v0.68.2 `navigate()` reported
success anyway. Measured live on EP205015 (Approval Maps, csmdev 2025R1, 2026-07-21):

- The datasource's `commandStates` list has **no navigation command** — no `First`/`Next`/`Prev`/
  `Last`/`Refresh`; only `Cancel`/`Save`/`Insert`/`Delete` and the screen's tree actions. Read the
  list from any callback response before assuming a command exists: an unknown command returns a
  clean 2,300-byte no-op, not an error.
- Every key-commit shape is IGNORED: the header selector's edit params (raw, `$text`, both),
  `__EVENTTARGET`, the key in the URL query string, a key inside the envelope addressed to the
  form control, and a **synthetic ObjectStateFormatter DataKey injected into all 97 `_state`
  fields** (encoder byte-exact against the screen's own key — still ignored). The server rebuilds
  the graph per callback and defaults to the FIRST record.
- Root cause: the page is a 2025R1 **hybrid** — the Map selector renders as `qp-editor-wrapper`, a
  modern web component. Its commit travels the modern JSON protocol, never the WebForms fields this
  plane speaks. Expect the same on any header whose editor renders as `qp-*`.

So `Cancel` answered with map 1 — carrying a perfectly well-formed dataKey — and `navigate()`
returned it as success. Any subsequent grid write would have hit the wrong map: the same
silent-wrong-target family as 11e and the modern session cache. Now `navigate()` **decodes the
returned dataKey** (ASP.NET ObjectStateFormatter pair-list; string + Int32 tokens) and raises
`"the key commit was IGNORED"` when the loaded key differs from the requested one. Key types the
decoder doesn't cover (e.g. GUIDs) mean *cannot verify*, not *verified* — no exception is raised on
those, so the read-back remains the authority. Bonus fix in the same release: the header form block
is found by `phF` prefix scan, not the hardcoded `ctl00_phF_form` (EP205015 names it `mapForm`).

Practical rule: a screen whose header selector is a `qp-*` component navigates only on the modern
plane. For EP205015 that means `record_key` on the modern tools, or the §14 XML round-trip for
whole-record work; the classic tree stays unreachable because you can never steer the classic graph
off record 1.

---

## 12. Failure routing — errors that route you to the working plane (v0.66.0)

A live 2026R1 foundation build produced a defect register whose common shape was: **a failure path
raising a bare message when the codebase already held the knowledge needed to route the caller to
the plane that works.** v0.66.0 closes that class at five choke points:

- **`create_or_update_entity` validates field names before the PUT.** The contract layer silently
  DISCARDS unknown properties — no error, field left at its default (proven: Ledger `BalanceType`
  dropped, `Type` defaulted to Actual, surfaced two records later as a misleading "actual ledger
  already associated" error). Unknown names now raise pre-PUT with difflib close-matches. Costs
  nothing after the first call (swagger.json is cached per client); fails open if the schema
  itself can't be fetched.
- **`run_dac_odata` failures carry a `HINT`** distinguishing three measured shapes the raw
  404/400 explains none of: (a) name not exposed → close matches from the service document;
  (b) DAC exists in `$metadata` as an EntityType but serves **no EntitySet** (detail/staging DACs,
  single-row config DACs) → every collection read 404s regardless of query shape; read fields via
  `get_dac_metadata`, rows via the owning screen; (c) the name resolved server-side to the WRONG
  DAC — `'NumberingSequence'` binds to the Numbering HEADER, so `StartNbr` errors "Could not find
  a property … on type 'PX.Objects.CS.Numbering'"; the sequence detail is only reachable via
  `ui_read_grid('CS201010','Sequence')`. Diagnosis runs on the failure path only and never masks
  the original error.
- **A screen with NO classic ASPX page routes instead of raising.** All four ASPX tools
  (`diagnose_save_error`, `aspx_delete_grid_row`, `aspx_grid_batch`, `aspx_tree_node_action`)
  previously died at `open()` with "no __RequestVerificationToken …" on modern-only screens
  (observed: CS201010) — leaving a generic "raised at least one error" with no recovery path. They
  now return `{no_classic_page: true, recommend: …}` pointing at the modern-plane tools, and note
  that diagnosing a failing Save there means re-running via `ui_screen_action` (whose per-field
  guards name the refused value) and bisecting. Distinct from 11b: that's "page exists, one grid
  unbound"; this is "no classic page at all".
- **Classic-plane `_find_field` misses name the schema tool.** "field 'X' not found" now lists the
  available containers (or the container's fields if only the field half is wrong) and points at
  `screen_get_schema` — because the usual cause is not a typo but the §3 plane-naming trap:
  modern view names ≠ classic container names (measured: `ui_get_structure('SM203520')` exposes
  `Companies`; the classic schema wants `CompanySummary`).
- **`ui_screen_action`'s dirty-after-Save warning is now an honest AMBIGUOUS verdict.** Measured
  both ways: on some screens `graphIsDirty:true` after Save = genuinely unsaved; on others the
  value persisted anyway (CS202000 LookupMode). An in-session read-back CANNOT disambiguate — the
  graph holds the staged values either way, and reloading it to check would discard them if they
  truly hadn't saved. The warning now says exactly that and directs to an out-of-band read
  (`run_dac_odata` / `get_entity` / fresh `screen_get`) BEFORE any re-run of the Save.

Related fix, same register: `activate_features` no longer swallows a server-side rejection of the
Enable command as `"in_progress"` (see §6 step 1), and its error text is no longer truncated at
160 chars — the old cut landed mid-sentence at "…instance of ", discarding exactly the object name
that identifies the null.

## 13. ARM row sets (CS206010) — writing rows AND formulas headlessly

Analytical Report Manager row sets are drivable end-to-end, but ONLY as a **two-step** write. Proven
live on a 69-row Penyata Kedudukan Kewangan copy (2026-07-21, csmdev/AI MPM), every claim below
DB-verified via `run_dac_odata('RMRow')`.

### The recipe

```
1. screen_submit    insert the row: Rows.Code, Rows.Description, Rows.Type   -- NO formula
2. aspx_grid_batch  update Formula, row_key = FULL key {RowSetCode, RowNbr}
3. run_dac_odata    verify (ok:true on this screen proves nothing — see below)
```

Step 2 is the load-bearing one: `aspx_grid_batch` wrote `=sum('0030','0041')` onto a Total row and
the DB confirmed it. Deletes work the same way (`operation:"delete"`, full key), also verified.

### Why the formula CANNOT go in the insert (measured, not inferred)

Sending `Rows.Value` in the same Submit as the row fails **two different ways**, and both are
misleading:
- A formula containing a **function call** hard-errors: *"The expression contains a call of an
  undefined function sum()."* — and identically for `Sum()`, so **it is NOT a casing problem.** The
  SOAP import processor validates with a GENERIC expression parser that has no ARM function library
  loaded; the ASPX plane runs inside the real ARM graph, which is why the same string succeeds there.
- A formula with only row references (`=@0130+@0232`) raises **nothing at all** and is **silently
  dropped** — no error, no value, `ok:true`.

Isolation test that settles it: one insert carrying `Description`, `Type`, `Height`, `Indent` and
`Value` persisted the first FOUR and dropped only `Value`. So it is not a key-fields-only limit and
not the wrong row type — `Formula` specifically is unwritable on the classic import path.

### `Rows.Type` — the full enum, and why a wrong label is dangerous

Valid labels (read off the live dropdown): **`GL`, `Caption`, `Line`, `Total`, `Header`, `Sort`**.
Observed `RowType` ints: `0`=GL, `1`=Caption, `2`=Line, `3`=Total.

An INVALID label is **silently coerced to `GL`(0)** under `ok:true` — `Calculation` and `Formula`
both did this. That matters twice over: the row is quietly the wrong type, AND `Formula` is only
applicable to `Total` rows, so a later formula write onto a mis-typed row **no-ops silently**. (My
first ASPX formula update "failed" purely because it targeted a `Caption` row — the plane was fine.)

### Formulas: what you can actually write

Formulas are Excel-like, entered as plain text in the **Value** column (the magnifier opens the
documented *Formula Editor* dialog, with a **Validate** button — but typing the text directly is
equally valid). Reference: KB `Formula_Functions__00a69dbb`, `Formula_Operators__cb4ef681`,
`Formula_Parameters__f3d27b0f`.

- **Row/cell references** — `@0130` (a row code), `A11`/`B12` (column-letter + row cells).
- **Interval functions — the ones that matter for financial statements:**
  - `Sum(from, to)` — sums a **RANGE**, not a list: `=Sum('0030','0041')` covers every row code in
    between. **This is the single most important gotcha**: a new row inserted inside an existing
    range joins that subtotal silently, and a row added just outside it is silently excluded.
  - `Sort(from, to, column)` / `SortD(...)` — ascending / descending over a range.
- **Math**: `Abs`, `Floor`, `Ceiling`, `Round(x, decimals)`, `Min`, `Max`, `Pow`.
- **Conditional / null**: `IIf(expr, truePart, falsePart)`, `IsNull(value, nullValue)`,
  `NullIf(v1, v2)`, `Switch(expr1, val1, expr2, val2, …)`.
- **Conversion**: `CBool CDate CStr CDbl CSng CDec CInt CShort CLong`.
- **Text**: `LTrim RTrim Trim Format UCase LCase InStr InStrRev Len Left Right Replace PadLeft
  PadRight`.
- **Date/time**: `Today() Now() TodayUTC() NowUTC() DateAdd DateDiff Day Month Year DayOfWeek
  DayOfYear DayOrdinal Hour Minute Second MonthName`.
- **Acumatica-specific** (`Report.` prefix): `ExtToInt ExtToUI IntToExt IntToUI UIToExt UIToInt
  GetDescription GetDefExt/Int/UI GetDisplayName GetFormat GetMask GetBranchText FormatPeriod
  FormatYear`.
- Comments inside a formula: `/* commented line */`.

Note the live data uses **lowercase** `sum('0030','0041')` and works — the ARM runtime accepts it;
only the SOAP validator is strict. Match the existing rows' style rather than "fixing" the case.

### Data Source (the GL account behind a `GL` row) lives in a SEPARATE DAC

The grid's `DataSourceID` is an FK auto-created per row. The account/mask is on **`RMDataSource`**:
`StartAccount` + `EndAccount` (set BOTH to the same value for one account, or to the same wildcard
mask — live examples: `A11201`, `A14???`, `A63???`), plus `Expand`, `AmountType`, `AccountClassID`,
`LedgerID`, branch/period/project range fields. The modern plane cannot see the Data Source column
at all, so read it via `run_dac_odata('RMDataSource')` keyed on the row's `DataSourceID`.

### Two traps that cost real time here

- **`screen_submit` CANNOT target an existing detail row — by design.** Setting a detail container's
  key field does not select a row; the import processor treats it as a NEW row and errors "A record
  with the same value of the Code field already exists". Positional `{"row": N}` is refused outright
  (see §3) because it silently writes to row 1. To edit an existing grid row use the ASPX plane
  (full key) or the modern grid tools — never a key-set on the classic plane.
- **NEVER interleave API writes with an open browser page on the same record.** Acumatica records
  carry a `tstamp`; API writes bump it, and the stale page then fails its Save with *"Another
  process has updated the 'RMRowSet' record. Your changes will be lost."* That guard is protective
  (it blocks a stale overwrite, nothing is lost) but it will silently waste a long manual edit.
  Pick ONE plane per session; if you must switch, reload the page first.

### Meta-lesson (cost three false negatives in one session)

**A validation ERROR is proof the field is live; silence is not proof it is dead.** "undefined
function sum()" meant the server had read the formula and understood it well enough to reject it —
that was the moment to conclude "wrong context", not "unwritable". Every wrong verdict here came
from an invalid fixture: a `Caption` row for the update test, a same-batch type change for the
insert test, and an invented enum label. Same failure mode §11 already warns about; check the
fixture is valid before believing a null result.

### Selector columns are a PAIR — and the display twin is not writable (v0.66.3)

A classic grid's selector cell (`"dataType":9`) is two things: the stored FK in `dataField`, and a
display twin named by `textFieldColumn`/`textField`. Live CS206010 Data Source column:

```json
{"textFieldColumn":"DataSourceIDText","dataType":9,"dataField":"DataSourceID",
 "textField":"DataSourceIDText","formEditorID":"edDataSource"}
```

`_grid_column_slots` reads only `dataField` (it must — the twin is NOT its own `<Cell>` position,
and adding it would misalign every row parse), so the twin used to be reported as "not a column of
this grid", sending callers hunting for a typo that didn't exist. `_grid_selector_text_fields` now
harvests the pairs separately and the pre-flight names the real constraint instead.

**What the wire actually carries** (captured live, browser DOM + XHR hook): committing the Data
Source cell sends `<Cell Value="31555" Key="DataSourceID"/>` — the RESOLVED id — and **no text cell
at all**. So the twin is not merely unlisted, it is never sent; a selector needs the id. Some
selectors resolve only through their own editor dialog (CS206010's Data Source Editor is the
`ctl00_phG_grid_lv0_edDataSource_pnlARmDataSet` PXSmartPanel), and for those this plane cannot set
the value at all until that panel's protocol is driven the way §11c drives the classic tree.

### Failed writes STAGE PHANTOM ROWS that outlive the session (issue #4 — reproduced on demand)

The long-parked PY309000 mystery — *identical input, different error text on each retry* — is a
**sticky graph**, and CS206010 reproduces it deterministically:

1. `ui_insert_grid_row` fails validation → the row is **left staged in the graph**.
2. A retry then fails with *"A record with the same value of the Code field already exists"* — while
   `run_dac_odata` shows **no such row in the DB**.
3. The phantom is visible to *other planes*: the ASPX `diagnose_save_error` reported the same
   duplicate error for a row that exists only in the graph.
4. Each further failed attempt stacks another phantom, so error messages accumulate and contradict
   each other (`['Code already exists', "'Type' cannot be empty"]` from one insert).

**`release_sessions` does NOT clear it** (it drops logins; the graph is server-side and sticky per
§4d). **`ui_screen_action(screen_id, action="Cancel")` DOES** — it returns `graphIsDirty:false` and
the next attempt starts clean.

Practical rule: **after any failed grid write, Cancel before retrying.** Otherwise the second error
is describing a graph you did not build, and "the same call returned a different error" is the
symptom you will chase. Always confirm what actually persisted with `run_dac_odata`, never with the
error text.

### Data Source (dialog-only cells) — protocol MAPPED, write NOT achieved (2026-07-21)

The `Data Source` column is the one part of a row set this plane still cannot write. Recorded here
because the next attempt should start from these facts rather than repeat the search.

**Why no cell-level write can work.** The cell renders `class="editor ReadOnly GridEditorText"` —
it is *read-only text plus a magnifier*. Measured, all four planes:
`screen_submit` (`Rows.DataSource` and `RowsDataSourceID.DataSourceIDText`) drops it silently;
`ui_insert_grid_row` cannot even insert on this grid (`'Type' cannot be empty` — the modern plane
fails to bind `RowType`); `aspx_grid_batch` with `DataSourceID` is refused **and still committed a
side effect** (the row's FK churned 31551→31553, orphaning the old record — proof that
`save_verified:false` means *your field didn't take*, NOT *nothing happened*); the contract
endpoint exposes only 2 fields for that container (`DataSourceID`, `DataSourceIDText`), so
`generate_endpoint_entity` cannot reach `StartAccount` either.

**The dialog protocol, captured live and confirmed working up to the Save:**
```
editor    ctl00_phG_grid_lv0_edDataSource         (from the column's formEditorID)
panel     <editor>_pnlARmDataSet                   (a PXSmartPanel, EMPTY until loaded)
form      <panel>_frmDataSet                       (materialises only in the Load response)
open      __CALLBACKID=<panel as $-name>  param: Load|<panel LoadedLevel="-1"><![CDATA[]]></panel>
save      __CALLBACKID=<form  as $-name>  param: Save|<form  LoadedLevel="-1"><![CDATA[]]></form>
          + discrete params  <form$-name>$<Field>$text = value,  <form$-name>$edID = <fk>
binding   the editor's hidden _state carries the record: <PXText Value="31556"/>
```
`formEditorID` is in the PAGE's `var _<grid> = {...}` config — a grid **Refresh response does NOT
carry it** (that cost a live call returning "no dialog columns").

**Five hypotheses tested, all rejected** — the Save returns clean and `RMDataSource.StartAccount`
stays null: (1) bind→Load; (2) a following ds-level Save; (3) harvesting the Load HTML's hidden
`_state` inputs (the Load body is RAW HTML, not Props blocks, so `_parse_control_blocks` finds
nothing in it); (4) Load→bind; (5) selecting the row by constructing the grid `_state`
`DataValues="<Row i=N>…"` from the GL202500 capture — that one broke the Load outright.

**Traps found while probing, worth not rediscovering:**
- A loose `<panel>_frm[A-Za-z0-9]*` match returns a **CSS class name**: the Load body opens with a
  `<style>` block containing the same string. Anchor on `name="…_state"`.
- Writing the editor `_state` BEFORE the Load makes the panel answer with a stunted body (3410
  bytes, no form) instead of the full render (83026). Order is load-bearing, though neither order
  persists.
- Setting the field via JS in a browser does NOT work either — the framework serialises from its own
  control state, so only `edID` reaches the wire. Do not use a browser DOM write to "confirm" a
  field is settable.

**Status: Data Source remains a manual browser step.** The tool built for this
(`aspx_cell_dialog_set`) was **deliberately not shipped** — it returned `ok:true` while persisting
nothing, which is the exact failure class §12 exists to remove. Next attempt should capture the
browser's COMPLETE dialog exchange (every field, in order, with each `_state` at each step) rather
than reconstructing from sampled pieces.

### Known gap in our own guards

`classify_writable` sources field metadata from the modern `/structure`, but classic-only fields
(here `Formula`, `DataSourceID`) are **absent from the modern grid entirely** — unknown fields are
treated as writable so nothing is silently dropped, which means the one field that genuinely cannot
be written is precisely the one we cannot warn about. `screen_submit` reported `ok:true` with empty
`field_errors` for every drop above. Until that is closed, **read back after every classic-plane
write to this screen.**

## 14. XML round-trip — clone a WHOLE record graph (`export_screen_xml` / `import_screen_xml`, v0.67.0)

**Reach for this when a record's real content lives in structures no plane can address.** The proven
case is **EP205015 (Approval Maps)**: its steps, rules, conditions and approvers hang off a
PXTreeView with no modern-plane selection handler, so node-scoped edits are impossible (§11e) and the
UI itself cannot copy a map. Acumatica's own **Clipboard > Export/Import as XML** carries the entire
graph, so the round trip is the only full-fidelity path — and it makes such screens API-drivable.

Availability is declared server-side by `App_Data/XmlExportDefinitions/<SCREENID>.xml`. That file is
also the fastest way to see WHAT a screen's XML will carry, without exporting anything:

```xml
<relations format-version="4" main-table="EPAssignmentMap" file-name="(Name)">
  <link from="EPRule (AssignmentMapID)"         to="EPAssignmentMap (AssignmentMapID)"/>
  <link from="EPRuleCondition (RuleID)"         to="EPRule (RuleID)"/>
  <link from="EPRuleEmployeeCondition (RuleID)" to="EPRule (RuleID)"/>
  <link from="EPRuleApprover (RuleID)"          to="EPRule (RuleID)"/>
</relations>
```

### Export is MODERN-plane only

Two traps, both of which read as "not supported" if you stop early:

- The command is the menu **SUB-command `CopyPaste@ExportXml`**. Plain `ExportXml` is silently inert
  (200, command states echoed, nothing else), and `/structure` lists only the top-level `CopyPaste`
  — so `ui_screen_action` REFUSES it and this must go through `ui_command`.
- The **classic ASPX plane cannot do it at all**: the same command only repaints, and three postback
  shapes returned no `Content-Disposition`. The modern plane instead answers with a `redirects`
  block carrying the real URL — the whole trick, and invisible from classic:
  `{"redirects":[{"url":"/Frames/GetFile.ashx?fileID=<guid>&..."}]}`.

`Frames/GetFile.ashx` is ALSO the attachments handler (`filesMenuUrls.filesDisplayUrl` in the page
config), so finding it there proves nothing about export — the `fileID` is what matters.

### Import needs BOTH planes

1. **Classic** multipart POSTBACK uploads the file to the page-level dialog. Control names are
   template-level, so they are the same on every screen:
   `ctl00$usrCaption$dlgUploadXml$upl$upl` + `__EVENTTARGET=ctl00$usrCaption$dlgUploadXml$btnUpload`.
2. **Modern** `CopyPaste@ImportXml`, then `Save`.

Skip step 1 and the import reports *"The file is not found, or you don't have enough rights to see
the file"* — it reads a file the session must already hold.

### The identity rule — one of its failure modes is SILENT

Import always **INSERTS**; it never fills the current record (proven: `Insert` first gives
`isNewEntry:true` and the same error). `xml_as_new_record()` encodes the shape:

| payload | result |
|---|---|
| identity attribute REMOVED | *"Cannot insert explicit value for identity column … IDENTITY_INSERT is OFF"* — loud, harmless |
| identity **`"0"`** | correct — server assigns the next id and children follow |
| a NONZERO unused id (999999) | header imports, **every child row is SILENTLY DROPPED** |

The last row is why the helper exists rather than leaving callers to hand-edit: children keep their
uplink to the id you invented, and a header-only record looks perfectly healthy in the UI. **Always
read the detail table back with `run_dac_odata`** — `import_screen_xml` returns a `verify` field
saying exactly that, because the tool cannot detect this for you.

Two more rules the helper applies: GUIDs are remapped per **distinct value** (so a child's parent
pointer — `EPRule.StepID` → its step's `RuleID` — still resolves; remapping each occurrence
independently orphans the tree), and only the **FIRST** `Name=` is the record name — on EP205015 the
later ones are step/rule names, and renaming those rewrites the workflow's contents.

Verified live end to end: map 15 cloned with step + 2 rules (parenting, sequences, ApproveType W/A,
workgroups) and all 5 conditions incl. brackets/operators.

### 14a. STRING-keyed screens clone by RENAMING CODES — and ARM (CS206000) is the big win

`as_new_record` is built for an INTEGER identity (`id_field` → `"0"`). A screen whose key is a
string CODE has no identity column, so that helper does not apply — you clone by rewriting the codes
and regenerating the record UID. **CS206000 (Report Definitions)** is the case that matters, because
its export carries the ENTIRE Analytical Report Manager graph in one document:

```
RMReport ─ RMDataSource, RMStyle
RMColumnSet ─ RMColumn ─ RMColumnHeader (formulas + styles)
RMRowSet ─ RMRow (RowCode, RowType, Formula, LineStyle, Indent) ─ RMDataSource
RMUnitSet ─ RMUnit           + Ledger (reference, do-not-change)
```

That **supersedes the two-step ARM recipe** in §13 for whole-set work: row `Formula` no longer needs
a second `aspx_grid_batch` pass, and the per-row `RMDataSource` (StartAccount/EndAccount/
AccountClassID/StartSub/EndSub) rides along in the same file.

Only THREE rows need editing — child rows carry no parent code at all, they uplink positionally by
XML nesting:

| element | edit |
|---|---|
| `RMReport` row | `ReportCode`, `ReportUID` (fresh GUID), `Description`, `RowSetCode`, `ColumnSetCode` |
| `RMColumnSet` row | `ColumnSetCode`, `Description` |
| `RMRowSet` row | `RowSetCode`, `Description` |
| `RMRow` / `RMColumn` / `RMColumnHeader` | **nothing** — nested uplink |

**The trap: row sets and column sets are SHARED system-wide** (KB: the code must be unique across
the whole system, and one set is reused by many reports). Import a file that keeps `RowSetCode="PKK"`
and you do not create anything — you **UPDATE the live shared set**, silently changing every other
report that uses it. Rename all three codes, or know exactly which set you are rewriting. Codes are
max 10 alphanumeric.

Verified live end to end on csmdev (2026-07-21): `PKK` → `AITESTRPT` imported, then RE-EXPORTED and
diffed against the source. After normalising only the intended renames the two files are **identical,
453/453 lines** — 69 rows, 4 columns, 24 column headers, 27 account ranges, 17 row formulas, 6 header
formulas, all styles. Source report untouched (same `ReportUID`).

One gotcha worth naming: `export_screen_xml` without `record_key` failed here with *"The Export as
XML button is disabled"*. That is "no record is loaded", NOT "this screen has no XML definition" —
pass `record_key={"view":"Report","key":{"ReportCode":…}}`. Unlike EP205015, CS206000 exposes real
navigation commands (First/Previous/Next/Last), so its record targeting is unproblematic.

## 15. Modern-plane writes must NAME their record target (BREAKING, v0.68.0)

**"No record specified" never meant "no record loaded".** The modern session is cached ACROSS CALLS
(keyed `base_url|user|tenant`, and it survives across PROCESSES), so a write with no target acts on
whatever the PREVIOUS operation left current. That is not theoretical: it renamed a live approval map
(EP205015 map 15) when an `Insert` silently didn't apply and the following field-set + Save landed on
the inherited record. Same shape as §11e — something quietly doesn't apply, then a write lands
somewhere else.

```
record_key=… / parent=…   act on THIS record            (preferred)
target="current"          act on whatever is loaded     (explicit opt-in)
```

Enforced on `ui_screen_action`, `ui_update_grid_row(s)`, `ui_insert_grid_row`, `ui_delete_grid_row`,
`ui_grid_row_action`. The refusal names the parameter THAT tool takes (`parent` for grid tools,
`record_key` for `ui_screen_action`), and fires before login — a mis-targeted call cannot reach the
network.

**Exempt, each for a reason:** `Insert` CREATES the record so it cannot name one; `Cancel`/`Repaint`
discard or redraw rather than write; `ui_run_process` has no record parameter at all (it drives a
filter) — no ceremony was added where there is no target.

### Why this is caller-facing instead of a heuristic

Inferring which screens are keyed was tried and **measured wrong**:

| screen | actually keyed? | `urlFieldNames` |
|---|---|---|
| EP205015 approval maps | **yes** | `None` |
| SM207060 endpoint | yes | `['InterfaceName','GateVersion']` |
| CS100000 Features | **no** (singleton) | `['Status']` |

A guard built on that signal would have REFUSED the screen that caused the damage and WAVED THROUGH
the singleton. The plane cannot tell, so the caller must say. Singleton setup screens (GL102000,
CS100000) genuinely have no key and pass `target="current"` — accurate, not a workaround.

**The escape hatch is real, and that is the risk.** `target="current"` restores exactly the behaviour
that caused the incident. It is correct for keyless singletons; anywhere else it deserves a moment's
thought rather than a reflex when a refusal is inconvenient.

---

*This file is generic operational knowledge. Instance-specific state (credentials, tenant names,
per-client configuration) is intentionally excluded and should never be committed to a public repo.*
