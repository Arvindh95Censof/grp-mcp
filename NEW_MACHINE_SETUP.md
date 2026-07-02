# grp-mcp — New Machine Setup

A step-by-step guide for setting up **grp-mcp** (the Acumatica ERP MCP server) on a
fresh computer, from nothing installed to a working connection in Claude.

grp-mcp is published on PyPI as [`grp-mcp`](https://pypi.org/project/grp-mcp/). No
git clone, no manual build — everything below downloads the finished package.

---

## What you need before starting

| # | What | Who provides it |
|---|------|------------------|
| 1 | A computer with internet access | — |
| 2 | Claude Code CLI or Claude Desktop installed | You |
| 3 | An Acumatica **Connected Application** (Client ID + Client Secret) | An Acumatica **admin** on the target instance |
| 4 | An Acumatica username/password grp-mcp will act as | Same admin, or you if you already have one |

Steps 1-2 are software you install. Steps 3-4 are information/access someone with
admin rights on the Acumatica side must set up **once** — it can be a different
person than whoever is doing the laptop setup, and does not need to be redone for
each new machine (the same Connected Application can be reused).

---

## Part A — Get the Acumatica credentials (one-time, per Acumatica instance)

Whoever has admin rights on the Acumatica instance:

1. Log into Acumatica.
2. Go to **Integration → Connected Applications**.
3. Create a new Connected Application with the **Resource Owner Password
   Credentials** flow enabled.
4. Note down:
   - **Client ID** (looks like `a1b2c3d4-...@CompanyLogin`)
   - **Client Secret**
5. Also note down:
   - The instance **base URL** (e.g. `https://yourcompany.acumatica.com`)
   - The **tenant / company login name** (the "Company" you log into)
   - The **endpoint name and version** — check *System → Web Service Endpoints*
     for the endpoint marked "Default" and its version (e.g. `24.200.001`)
   - A **username + password** for the Acumatica user account grp-mcp will act as
     (this can be a dedicated service account or an existing user)

Keep these six values handy — you'll enter them in Part D.

---

## Part B — Install `uv` (the package runner)

grp-mcp uses [`uv`](https://docs.astral.sh/uv/getting-started/installation/) to run
directly from PyPI with no separate install/upgrade step. Install it once:

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

Verify it worked:
```bash
uv --version
uvx --version
```

You do **not** need to separately install Python — `uv` can provision one itself
if none is found.

---

## Part C — Install Claude Code CLI (if not already installed)

Follow Anthropic's official Claude Code installation instructions
([docs.claude.com](https://docs.claude.com)) for your OS, then confirm it works:
```bash
claude --version
```

(If you're using Claude Desktop instead of the CLI, skip to Part E for the config
file shape — the difference is only in how you register the server.)

---

## Part D — Create `connections.json` (the credentials file)

grp-mcp ships a small local config page for this — no template file to copy, no
JSON to hand-write:

```bash
uvx --from grp-mcp grp-mcp-ui
```

This opens **http://127.0.0.1:8765** in your browser (open it manually if it
doesn't launch automatically). On a fresh machine the page starts empty:

1. Click to add a new profile.
2. Fill in the six values from Part A (base URL, Client ID, Client Secret,
   username, password, tenant, endpoint name/version).
3. Save.

This writes a `connections.json` file. **Note the full path it was saved to** —
you'll need it in the next step. (The page never sends your secret/password back
to the browser once saved — only whether one is set.)

Close the page / stop the command (Ctrl+C) once you're done — you don't need it
running afterward.

---

## Part E — Register grp-mcp with Claude

### Claude Code (CLI)

Run this as **one single line** (do not split it across lines with `\` in
PowerShell — that's bash syntax and will break the command):

```powershell
claude mcp add grp-mcp -s user -e GRP_MCP_CONNECTIONS="C:\full\path\to\connections.json" -- uvx grp-mcp
```

Replace the path with wherever Part D actually saved the file. `-s user` makes it
available in every project on this machine, not just one folder.

### Claude Desktop

Add this to `claude_desktop_config.json` (found at
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "grp-mcp": {
      "command": "uvx",
      "args": ["grp-mcp"],
      "env": { "GRP_MCP_CONNECTIONS": "C:\\full\\path\\to\\connections.json" }
    }
  }
}
```

---

## Part F — Restart and verify

1. **Restart Claude Code / Claude Desktop completely.** MCP servers are only
   loaded at startup — a running session won't pick up a new registration.
2. Start a new conversation and ask Claude to run the `whoami` tool (or
   `test_connection`). A working setup returns your Acumatica username, tenant,
   and `"reachable": true`.

If it fails, the error message will say whether it's a credentials problem
(check Part A's values) or a connectivity problem (check the base URL / network).

---

## Notes / gotchas

- **First `uvx grp-mcp` run takes ~5-10 seconds** (downloading dependencies);
  every run after that is ~1 second (cached).
- **PowerShell line continuation is `` ` `` (backtick), not `\`.** If a command
  needs to span multiple lines, use a backtick at the end of each line, or
  just keep it on one line as shown above — safest either way.
- **A trial Acumatica license typically allows only 2 concurrent API sessions.**
  If you see "API Login Limit" errors, ask Claude to run the `release_sessions`
  tool.
- **Multiple people / multiple Acumatica instances:** each person or instance
  needs its **own** Connected Application (Part A) — repeat that part, then add
  another named profile via the config UI (Part D) rather than overwriting the
  first one.
- To **upgrade** grp-mcp later: nothing to do — `uvx` always resolves the latest
  published version automatically on each run.
