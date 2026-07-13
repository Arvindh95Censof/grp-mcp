# grp-mcp — Complete Installation & Setup Guide

**Version 0.52.4 · Acumatica ERP MCP Server**

This guide takes a **fresh Windows, macOS, or Linux PC** from nothing installed to a
working grp-mcp connection inside Claude. It covers every dependency, every credential,
the configuration file, how to register the server with Claude, and how to verify and
troubleshoot the result.

grp-mcp is published on PyPI as `grp-mcp`. There is **no git clone and no manual build**
for the normal install path — the recommended method downloads the finished package
automatically.

**Links:**
- PyPI (package): **https://pypi.org/project/grp-mcp/**
- Source (GitHub): **https://github.com/Arvindh95Censof/grp-mcp**
- Releases / changelog: **https://github.com/Arvindh95Censof/grp-mcp/releases**

---

## 1. What grp-mcp is

grp-mcp is a Model Context Protocol (MCP) server that exposes an **Acumatica ERP** instance
as tools an AI agent (Claude) can call. It connects to any Acumatica site given a base URL
plus credentials, and reaches Acumatica through **four client planes**:

- **Contract-based REST** — CRUD entities, bulk-load from Excel/CSV, invoke actions, run
  reports, attach files, manage customization projects.
- **DAC-based OData** — read tables/DACs not exposed on the endpoint, plus mandatory-field
  metadata.
- **Screen-based SOAP engine** — drive screens the REST API can't (context / master-detail /
  wizard screens) by replaying their UI commands. No browser required.
- **Modern UI-screen plane** — the JSON protocol the real browser UI uses, for actions the
  classic plane can't reach and full grid CRUD.

Across the four planes it exposes **~95 tools** (v0.52.1). Beyond entity CRUD and screen
driving these include: **setup discovery** on a blind instance (`screen_prereqs`,
`screen_discover_prereqs`, `module_setup_plan`, `screen_autofill` — infer prerequisites and
build order with no source in hand), **bulk execution** (`screen_bulk_load` — N master
records to any classic screen with no endpoint entity; `ensure_entity_on_endpoint` — make a
screen REST-drivable in one call), and a **guarded data-migration suite**
(`setup_data_provider` + `build_import_scenario` + `import_excel` + `stock_scenario_info` —
create and run SM206015/SM206025/SM206036 import scenarios with the known silent-failure
traps caught up front; see Section 13.4). When unsure which tool fits, ask Claude to call
**`guide`** first — it routes by task.

You do **not** need to understand the planes to install it — this guide is purely about
getting it running.

---

## 2. What you need before you start

| # | What | Who provides it |
|---|------|-----------------|
| 1 | A computer with internet access | — |
| 2 | Claude Code CLI **or** Claude Desktop installed | You (Section 5) |
| 3 | `uv` / `uvx` package runner | You (Section 4) |
| 4 | An Acumatica **Connected Application** (Client ID + Client Secret) | An Acumatica **admin** on the target instance |
| 5 | An Acumatica **username + password** grp-mcp will act as | Same admin, or an existing account you hold |
| 6 | The instance **base URL, tenant, endpoint name + version** | From the Acumatica instance |

Items 1–3 are software you install on the new PC. Items 4–6 are information/access that
someone with admin rights on the Acumatica side sets up **once per instance** — it can be a
different person, and it does **not** need redoing for each new machine (the same Connected
Application is reused).

---

## 3. System requirements & dependencies

### 3.1 Prerequisites at a glance

| Prerequisite | Requirement | How to get it |
|--------------|-------------|---------------|
| Operating system | Windows 10/11, macOS, or Linux | Developed and run on Windows 11 |
| **Python** | **3.10 or newer — but you usually do NOT install it yourself** | `uv` provisions a suitable Python automatically. Only install Python manually if you plan to develop grp-mcp from source (Section 13.2) |
| **`uv` / `uvx`** | latest | Section 4 — one-time install; this is the only mandatory manual prerequisite besides the Claude host |
| Claude Code CLI or Claude Desktop | latest | Section 5 — the MCP host that loads the server |
| Internet access | — | Needed once to download the package + dependencies (then cached) |

> **Do I need Python?** No, not by hand for the normal install. `uvx` downloads the package
> *and* a matching Python runtime if none is found. A manual Python 3.10+ install is only
> required for the **from-source developer path** (Section 13.2), where `pip install -e` is used.

### 3.2 Python packages (installed automatically)

You do **not** install these by hand — `uvx`/`pip` pulls them in. Listed for reference/audit:

| Package | Minimum | Purpose |
|---------|---------|---------|
| `mcp` | 1.2.0 | MCP server framework |
| `httpx` | 0.27 | Async HTTP client (all Acumatica calls) |
| `pydantic` | 2.6 | Config/model validation |
| `python-dotenv` | 1.0 | `.env` loading |
| `openpyxl` | 3.1 | Excel/CSV bulk loaders |

Development-only (not needed to run): `pytest` ≥ 8.

### 3.3 Console entry points

The package installs two commands:

- `grp-mcp` — the MCP server itself (what Claude launches).
- `grp-mcp-ui` — a small local web page for creating/editing the connections file.

---

## 4. Install `uv` (the package runner)

grp-mcp uses [`uv`](https://docs.astral.sh/uv/getting-started/installation/) to run directly
from PyPI with no separate install/upgrade step. Install it once.

**Windows (PowerShell):**

```powershell
winget install --id=astral-sh.uv -e
```

or, if you already have any Python installed:

```powershell
pip install uv
```

**macOS / Linux:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Verify:**

```bash
uv --version
uvx --version
```

You do **not** need to separately install Python — `uv` provisions one if none is found.

---

## 5. Install the Claude host

Install **Claude Code CLI** (or **Claude Desktop**) using Anthropic's official instructions
at [docs.claude.com](https://docs.claude.com), then confirm:

```bash
claude --version
```

If you use Claude Desktop instead of the CLI, the only difference is how you register the
server (Section 9) — everything else is identical.

---

## 6. Acumatica-side prerequisites (one-time, per instance)

Whoever has admin rights on the Acumatica instance does this **once**. The output is the six
values you'll enter in Section 7.

1. Log into Acumatica as an administrator.
2. Go to **Integration → Connected Applications** (screen `SM303010`).
3. Create a new Connected Application with the **Resource Owner Password Credentials** (OAuth 2.0
   password grant) flow enabled.
4. Record the **Client ID** (looks like `a1b2c3d4-...@CompanyLogin`) and the **Client Secret**.
5. Confirm a **Web Service Endpoint** is published — **System → Integration → Web Service
   Endpoints** (`SM207060`). Note the endpoint **name** (usually `Default`) and **version**
   (e.g. `24.200.001`).
6. **Enable OData access for the account (required for probing).** The grp-mcp user must have
   the **OData v4 role** assigned (Acumatica screen **Users**, `SM201010` → **User Roles**;
   role management is `SM201005`). Without OData access, the DAC-based OData v4 interface returns
   **HTTP 403** and every *probing* tool fails — including `run_dac_odata`, `get_dac_metadata`,
   `tree_triage`, `stock_scenario_info`, and any screen inspection that reads DAC metadata.
   Read/write over the contract REST endpoint still works without it, but discovery/probing does
   not, so enable this.
7. Record the instance **base URL** (e.g. `https://yourcompany.acumatica.com` or
   `http://localhost/2025R2`) and the **tenant / company login name** (the "Company" you sign
   into).
8. Decide the **username + password** grp-mcp will act as — a dedicated service account or an
   existing user.

> **Web Services API seats:** Acumatica licenses limit concurrent API sessions. A **trial
> license typically allows only 2**. grp-mcp shares and releases sessions to stay within this,
> but if you see `API Login Limit`, see Section 11.

### The six values to hand off

| Value | Example |
|-------|---------|
| Base URL | `http://localhost/2025R2` |
| Client ID | `a1b2c3d4-...@CompanyLogin` |
| Client Secret | `s3cr3t...` |
| Username | `admin` |
| Password | `••••••••` |
| Tenant (company login) | `CompanyLogin` |
| Endpoint name / version | `Default` / `24.200.001` |

---

## 7. Create the connections file

grp-mcp reads its instance credentials from a **`connections.json`** file (recommended, supports
multiple instances) **or** from environment variables (single instance). Pick one.

### 7.1 Option A — the config UI (recommended)

grp-mcp ships a small local config page — no template to copy, no JSON to hand-write:

```bash
uvx --from grp-mcp grp-mcp-ui
```

This opens **http://127.0.0.1:8765** in your browser (open it manually if it doesn't launch).
On a fresh machine the page starts empty:

1. Add a new profile.
2. Fill in the six values from Section 6.
3. Save.

It writes a `connections.json` file. **Note the full path it saved to** — you need it in
Section 9. The page never returns your secret/password to the browser once saved (only whether
one is set). Stop the command with `Ctrl+C` when done.

### 7.2 Option B — write `connections.json` by hand

Create a file anywhere (e.g. `C:\grp-mcp\connections.json`):

```json
{
  "default": "myinstance",
  "instances": {
    "myinstance": {
      "base_url": "http://localhost/2025R2",
      "client_id": "CLIENT_ID@CompanyLogin",
      "client_secret": "secret",
      "username": "admin",
      "password": "password",
      "endpoint_name": "Default",
      "endpoint_version": "24.200.001",
      "tenant": "CompanyLogin",
      "branch": "",
      "allow_write": false,
      "allow_delete": false,
      "allow_publish": false,
      "read_roots": ["C:\\path\\to\\data"],
      "write_roots": ["C:\\path\\to\\out"],
      "max_file_bytes": 50000000
    }
  }
}
```

### 7.3 Connection field reference

| Field | Required | Default | Meaning |
|-------|----------|---------|---------|
| `base_url` | yes | — | Root URL of the Acumatica site |
| `client_id` | yes | — | Connected Application Client ID |
| `client_secret` | yes | — | Connected Application secret |
| `username` | yes | — | Acumatica user grp-mcp acts as |
| `password` | yes | — | That user's password |
| `endpoint_name` | no | `Default` | Web service endpoint name |
| `endpoint_version` | no | `24.200.001` | Endpoint version |
| `tenant` | no | `""` | Company login name (needed for OData & the customization/cookie login) |
| `branch` | no | `""` | Optional login branch |
| `allow_write` | no | `false` | Gate for create/update, bulk load, actions, import scenarios, notes, attachments |
| `allow_delete` | no | `false` | Gate for record deletes (stricter than write) |
| `allow_publish` | no | `false` | Gate for Customization API writes (publish/import/unpublish) |
| `read_roots` | no | `[]` | Folders local **reads** are confined to. **Empty = unrestricted** |
| `write_roots` | no | `[]` | Folders local **writes** are confined to. **Empty = unrestricted** |
| `max_file_bytes` | no | `50000000` | Max read/download size (bytes) |

### 7.4 Option C — environment variables (single instance)

Instead of a file, set these (or put them in a `.env` file):

```
GRP_MCP_BASE_URL=https://your-instance.acumatica.com
GRP_MCP_CLIENT_ID=YOUR_CLIENT_ID@CompanyLogin
GRP_MCP_CLIENT_SECRET=your_client_secret
GRP_MCP_USERNAME=admin
GRP_MCP_PASSWORD=your_password
GRP_MCP_ENDPOINT_NAME=Default
GRP_MCP_ENDPOINT_VERSION=24.200.001
GRP_MCP_TENANT=CompanyLogin
GRP_MCP_BRANCH=
GRP_MCP_ALLOW_WRITE=false
GRP_MCP_ALLOW_DELETE=false
GRP_MCP_ALLOW_PUBLISH=false
```

`GRP_MCP_CONNECTIONS` points at a `connections.json` and takes priority over the single-instance
vars when set.

---

## 8. Security & write gates (read this before enabling writes)

grp-mcp is **read-only by default**. Each instance opts into writes explicitly:

- **`allow_write`** — create/update records, bulk load, run actions/import scenarios, set notes,
  attach files, drive screen writes.
- **`allow_delete`** — delete records (a stricter, separate gate).
- **`allow_publish`** — Customization API writes. **Publishing is website-level and affects ALL
  tenants on the instance** — enable only when you mean it.

**Filesystem sandbox:** `read_roots` / `write_roots` confine local file tools. **An empty list
means UNRESTRICTED** — any path the OS user can reach. Set roots to enforce a sandbox. Every
file-touching tool result echoes a `sandbox` field so the active mode is never a silent
assumption.

**Admin persistence gate:** changing the on-disk `connections.json` from within Claude (persisting
a new profile) requires the environment variable **`GRP_MCP_ALLOW_ADMIN=1`**. Without it, added
profiles live only for the session. Session-only profiles are never written to disk.

**Credential handling:** secrets are never returned by list/inspect tools, never echoed into error
messages, and the OAuth/login response bodies adjacent to credential requests are never surfaced.

---

## 9. Register grp-mcp with Claude

### 9.1 Claude Code (CLI)

Run as **one single line** (in PowerShell do **not** split with `\` — that's bash syntax and
breaks the command):

```powershell
claude mcp add grp-mcp -s user -e GRP_MCP_CONNECTIONS="C:\full\path\to\connections.json" -- uvx grp-mcp
```

Replace the path with wherever Section 7 saved the file. `-s user` makes it available in every
project on this machine.

To also enable writes for that server, add more `-e` flags, e.g.
`-e GRP_MCP_ALLOW_WRITE=true` (or set the gate per-instance in `connections.json`).

### 9.2 Claude Desktop

Add to `claude_desktop_config.json` (`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "grp-mcp": {
      "command": "uvx",
      "args": ["grp-mcp"],
      "env": {
        "GRP_MCP_CONNECTIONS": "C:\\full\\path\\to\\connections.json"
      }
    }
  }
}
```

Use double backslashes (`\\`) in JSON paths on Windows.

---

## 10. Restart and verify

1. **Restart Claude Code / Claude Desktop completely.** MCP servers load only at startup — a
   running session won't pick up a new registration.
2. Start a new conversation and ask Claude to run the **`whoami`** tool (or **`test_connection`**).
   A working setup returns your Acumatica username, tenant, and `"reachable": true`.

If it fails, the error says whether it's a **credentials** problem (recheck Section 6/7) or a
**connectivity** problem (recheck the base URL / network / that the site is running).

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| First `uvx grp-mcp` is slow (~5–10 s) | Downloading dependencies | Normal; every later run is ~1 s (cached) |
| `API Login Limit` | All Web Services API seats in use (trial = 2) | Ask Claude to run **`release_sessions`**; grp-mcp also self-heals with one retry |
| `whoami` returns `reachable: false` | Wrong base URL, site down, or network | Verify the base URL in a browser; check the site/app pool is running |
| OAuth / token error | Wrong Client ID/Secret or username/password | Recheck the Connected Application values (Section 6) |
| `HTTP 403` on `run_dac_odata` / `get_dac_metadata` / `tree_triage` / `stock_scenario_info` / screen probes | Account lacks OData access | Assign the **OData v4 role** to the user (Section 6, step 6). Contract REST still works without it; probing does not |
| Tool says it needs `allow_write` | Instance is read-only | Set `allow_write: true` on the profile (or `-e GRP_MCP_ALLOW_WRITE=true`) |
| PowerShell command breaks on `\` | `\` is bash line-continuation | Keep the command on one line, or use backtick `` ` `` in PowerShell |
| Persisted profile "vanished" after restart | It was added session-only | Re-add with persistence and `GRP_MCP_ALLOW_ADMIN=1`, or edit `connections.json` directly |
| Tool not found after upgrade | Host cached the old server | Fully restart Claude |
| Import scenario **Prepare stages 0 rows** (no error) | One of three silent causes: the .xlsx was authored by **openpyxl** (Acumatica's Excel provider can't read it — author with real Excel); the provider's **FileName parameter** still says `<EmptyFileName>` / an old file; the provider **object name doesn't match the worksheet name** | Use **`import_excel`** — it guards all three up front and fails loud with a checklist. Providers built by `setup_data_provider` are pointed automatically |
| Import finishes but **0 rows Processed** and no error | The mapping stages fields but never commits — missing the trailing **`<Save>`** action row | `build_import_scenario` appends `<Save>` automatically; the honest signal is `IsProcessed`, not the "finished" status |
| Import fails **`'BaseQty' cannot be empty`** (or similar computed field) | A numeric field (Qty/Amount) was mapped to a **bare literal** (read as a phantom source column → empty), or a **priming field was omitted** (e.g. AR line needs `InventoryID` mapped before `Qty`) | Map numeric fields to a **real column**, and mirror the vendor recipe's field order — call **`stock_scenario_info(screen_id)`**; `build_import_scenario`'s preflight warns on both |
| GL import fails **`'CreditAmt' cannot be empty`** | Debit/credit columns alternate blanks; a blank cell imports as EMPTY | Put an explicit **`0`** in the empty side, and re-import with a **fresh file** (a same-name re-upload can read a stale cached copy) |
| `run_import_scenario` crashes `Sequence contains no matching element` | The contract-REST import path breaks on many target screens | Use **`import_excel`** (classic-plane runner) instead |
| Mapping/detail rows saved "ok" but read back wrong | Batching many `new_row` in one submit corrupts silently on state-dependent grids | Write one row per call (`build_import_scenario` / `screen_bulk_load` do this); always read back |

---

## 12. Upgrading

With the `uvx` install path there is **nothing to do** — `uvx` resolves the latest published
version of `grp-mcp` from PyPI on each run. To force a specific version, pin it in the launch
command (e.g. `uvx grp-mcp==0.52.4`). After any upgrade, **fully restart Claude** so the host
reloads the server. See the release history at
**https://github.com/Arvindh95Censof/grp-mcp/releases** (and the package page,
**https://pypi.org/project/grp-mcp/**).

---

## 13. Optional extras

### 13.1 Multiple people / multiple instances

Each person or Acumatica instance needs its **own** Connected Application (Section 6). Add each as
a separate **named profile** in `connections.json` (via the config UI or by hand) rather than
overwriting the first. Tools take an optional `instance` argument; without it, calls route to the
`default` profile.

### 13.2 Install from source (developers only)

If you're modifying grp-mcp rather than just running it, a **manual Python 3.10+ install is
required** for this path:

```bash
git clone https://github.com/Arvindh95Censof/grp-mcp.git
cd grp-mcp
pip install -e ".[dev]"        # editable install with pytest
python -m pytest tests/ -q     # run the test suite
```

Register the local checkout with Claude by pointing the launch command at the editable install's
`grp-mcp` entry point. Editing source requires **restarting the MCP server** to take effect.

### 13.3 Knowledge base (kb-mcp)

grp-mcp follows a **KB-first policy** for writes: before mutating a screen it expects an Acumatica
knowledge base (served by a separate `kb-mcp` server) to be available for prerequisite lookups.
This is optional for read-only use and for basic connectivity, but recommended before driving
setup screens. It is configured and installed separately.

### 13.4 Data migration (Data Provider → Import Scenario → Import by Scenario)

Bulk data migration in Acumatica runs through a **three-screen pipeline**, and grp-mcp drives all
three end-to-end (proven committing on AR301000 invoices, GL301000 journal batches, and AR303000
customers):

1. **Data Provider** (`SM206015`) — points at the source file and defines its columns.
   → `setup_data_provider(name, file_path, object_name)` creates it, uploads the file, **and
   points the FileName parameter** at it (the step whose absence silently reads 0 rows).
2. **Import Scenario** (`SM206025`) — maps source columns → target screen fields.
   → `build_import_scenario(name, screen_id, provider, provider_object, mapping)` writes the
   mapping (one row per submit, auto-appends the `<Save>` action, reads it back to verify) and
   runs a **preflight** that warns about the common silent-failure traps.
3. **Import by Scenario** (`SM206036`) — Prepare (stage) → Import (commit).
   → `import_excel(scenario_name, file_path, do_import=true)` runs the whole thing with polling
   and an honest `IsProcessed` verdict.

**Don't hand-craft the mapping — clone the vendor recipe.** Acumatica ships inactive
`ACU Import …` scenarios for the migration screens. Call **`stock_scenario_info(screen_id)`** to
read the authoritative field order, the exact source-column names, and priming fields; build your
file with those column headers and mirror the mapping.

**Rules that make it work reliably (all guarded/warned by the tools):**

- **Real Excel only.** An `.xlsx` written by openpyxl reads as empty; author with real Excel (COM).
- **Fresh filename per re-import.** A same-name re-upload can read a stale cached copy.
- **Plain column references** in the mapping (the classic writer silently drops `=` formulas).
- **Numeric fields → a real column**, never a bare literal like `"1"` (a bare value binds as a
  phantom source column and imports empty).
- **Priming field before the computed field** — e.g. map `InventoryID` before `Qty` so the line's
  computed `BaseQty` defaults; put an explicit **`0`** in alternating-blank debit/credit columns.
- **End with `<Save>`**, and trust **`IsProcessed`** (not "finished") as the success signal.

**Prerequisites still apply.** An import only commits if the target screen's **master data already
exists** — you can't import an AP bill without a vendor, or a cash sale without a cash account. A
real migration is therefore: set up master data first (companies, accounts, classes, customers,
vendors, items…), **then** run the transactional imports in dependency order.

---

## 14. Quick-start cheat sheet

```text
# 1. Install the runner (Windows)
winget install --id=astral-sh.uv -e

# 2. Create the credentials file
uvx --from grp-mcp grp-mcp-ui        # fill in values at http://127.0.0.1:8765, Save, note the path

# 3. Register with Claude Code (one line)
claude mcp add grp-mcp -s user -e GRP_MCP_CONNECTIONS="C:\path\to\connections.json" -- uvx grp-mcp

# 4. Fully restart Claude, then in a new chat:
#    "run the whoami tool"  ->  expect reachable: true
```

---

*grp-mcp v0.52.4 · PyPI: https://pypi.org/project/grp-mcp/ · Source: https://github.com/Arvindh95Censof/grp-mcp*
