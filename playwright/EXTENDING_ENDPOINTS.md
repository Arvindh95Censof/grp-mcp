# Extending an Acumatica Web Service Endpoint (headless Playwright)

How to add a top-level entity (a screen) to a contract-based endpoint on
**SM207060** without the UI, so it becomes usable through the grp-mcp REST API.

Verified on **csmdev / 2025R1Setup** (Acumatica build 25.101, contract v4),
endpoint `GRPSetup/24.200.001`, adding `AccountClass` → screen *Account Classes*.

## Why Playwright (and not the REST API)

The contract exposes a `WebServiceEndpoints` entity, so it's tempting to PUT to it.
**It does not work** — verified:

- A PUT with `CreateEntity` is a **no-op** (returns 200, adds nothing).
- `CreateEntity` / `EntityProperties` read back **empty** — they're transient form
  working-views, not persistable state.
- `EntityTree.Value` encodes **internal screen node IDs** (`...#E/10894`) the form
  generates when you pick a screen — you can't hand-supply them.
- The wizard ops (`Insert`, `ExtendEntity`, `PopulateFields`, `Save`) exist as
  actions but their params aren't in the contract and they need **live form state**
  that doesn't survive stateless REST calls.

So SM207060 is a **stateful wizard**. `get_endpoint_definition` (grp-mcp) can READ a
contract; writing requires driving the UI. This script does that.

> Endpoint edits **go live immediately on Save** — no customization publish/recompile
> needed (unlike a customization project, which is website-level).

## The click-path (what the script automates)

1. Login, open `Main?ScreenId=SM207060`.
2. The form renders in a nested iframe whose URL matches `SM207060.aspx` — target
   that frame (the top `Main?ScreenId=SM207060` URL also contains "SM207060", so
   match the **`.aspx`** specifically).
3. Load the endpoint via the key selectors `edInterfaceName` + `edGateVersion`
   (fill + Enter; each is a server round-trip).
4. Click **Insert** on the entity-tree toolbar: `div.toolsBtn[data-cmd="InsertNew"]`
   (dispatch mousedown/mouseup/click — Playwright `.click()` is blocked by overlay).
5. **Create Entity** dialog → fill `edObjectName`; pick the screen via its selector
   popup: click `.control-SelectorN` → type into the filter box
   `…edScreenID_pnl_tlb_fb_text` → **Enter commits the active (filtered) row**.
   - The screen selector matches by **screen TITLE** (e.g. `Account Classes`), not
     the `GLxxxxxx` id. Typing the id clears the field.
6. Click **OK** `#…pnlCreateEntity_btnOK` → entity node appears in the tree.
7. **Populate Fields**: select the new node, click `[data-cmd="PopulateFields"]` →
   in the dialog pick the **Object** (data view, e.g. `AccountClassRecords`) via its
   selector popup (same magnifier→filter→Enter) → `[data-cmd="SelectAll"]` →
   OK `#…pnlPopulateFields_PXButton5`.
8. **Save**: `[data-cmd="Save"]`. Live immediately.

### Key control IDs (stable on 2024R2/2025R1, contract v4)

| Purpose | Selector |
|---|---|
| Endpoint name / version | `#ctl00_phF_form_edInterfaceName_text` / `…_edGateVersion_text` |
| Insert entity | `div.toolsBtn[data-cmd="InsertNew"]` |
| Create-Entity: name | `#ctl00_phG_pnlCreateEntity_formCreateEntity_edObjectName` |
| Create-Entity: screen filter box | `…edScreenID_pnl_tlb_fb_text` |
| Create-Entity: OK | `#ctl00_phG_pnlCreateEntity_btnOK` |
| Populate: object view | `#ctl00_phG_pnlPopulateFields_formPopulateFields_PXTextEdit1` |
| Populate: object filter box | `…PXTextEdit1_pnl_tlb_fb_text` |
| Populate: Select All / OK | `[data-cmd="SelectAll"]` / `#ctl00_phG_pnlPopulateFields_PXButton5` |
| Save | `[data-cmd="Save"]` |

### Gotchas

- **networkidle never fires** (Acumatica long-polls) — use `domcontentloaded` + fixed
  waits.
- **Selector popups render in the same `.aspx` frame** but the grid uses an
  active-cell overlay → don't dblclick rows; **filter + Enter** commits the active row.
- Toolbar `div.toolsBtn` elements aren't Playwright-"actionable" → dispatch synthetic
  `MouseEvent`s via `frame.evaluate`.
- `PopulateFields` opens its own dialog needing an **Object** (the screen's data view);
  selecting it fills the field grid, then **Select All → OK**.

## Run

```powershell
$env:GRP_BASE="https://csmdev.censof.com/2025R1Setup"
$env:GRP_USER="<your-user>"; $env:GRP_PASS="********"
$env:NODE_PATH=(npm root -g)               # use the global Playwright install
npx playwright install chromium            # once
node add_endpoint_entity.js --endpoint GRPSetup --version 24.200.001 `
     --entity AccountClass --screen "Account Classes" --view AccountClassRecords
# add --debug to dump screenshots into ./shots
```

Credentials come from **env vars only** — never hard-code them (the repo is on GitHub).

## Verify (grp-mcp)

```
get_entity_schema("AccountClass", refresh=true)   # fields present?
get_entity("AccountClass", top=3)                 # returns live rows?
```

## Make it repeatable across instances

Once added in one instance, add the endpoint to a **customization project** in
SM204505, export the ZIP, and deploy elsewhere with grp-mcp:
`import_customization` → `publish_customization`. That's the version-controlled path;
this script is the bootstrap that creates it the first time.
