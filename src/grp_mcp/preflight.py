"""Write preflight — the enforceable "consult the KB before you write" step.

Order, per the user's design:
  1. consult **kb-mcp-dual** (its semantic search finds the relevant screen KB)
  2. then read **KNOWLEDGE.md** (grp-mcp's own hard-won experience notes)
  3. the live prerequisite check on the instance stays with the per-screen
     preflight tools (screen_preflight / setup_readiness / ui_preflight) — this
     engine gathers the KB evidence and applies the level decision.

Enforcement level (from Instance.effective_enforcement()):
  - "off"     -> skipped entirely (returns None); legacy behaviour.
  - "warn"    -> gather evidence, attach it to the result, always proceed.
  - "enforce" -> gather evidence; BLOCK the write if kb-mcp-dual could not be
                 consulted (available=False). Turning on enforce is opting into
                 the kb-mcp-dual dependency — that is the point of "consult
                 first". KNOWLEDGE.md is best-effort supporting evidence and
                 never blocks on its own.

Never raises: gathering evidence must not itself break a write. Failures are
folded into the decision (blocked only under enforce, with a clear reason).
"""

from __future__ import annotations

from . import kb, kb_client


async def gather(tool_name: str, *, level: str, screen_id: str | None = None,
                 query: str | None = None, kb_timeout: float = 120.0) -> dict | None:
    """Run the write preflight for `tool_name` at enforcement `level`.

    Returns None when level == "off". Otherwise a decision dict:
      {
        "level": "warn"|"enforce",
        "tool": tool_name,
        "query": "...",
        "consult_order": ["kb-mcp-dual", "KNOWLEDGE.md"],
        "kb": {...},          # kb-mcp-dual evidence (available True/False)
        "knowledge": {...},   # KNOWLEDGE.md evidence
        "blocked": bool,
        "block_reason": str | None,
      }
    """
    if level == "off":
        return None

    q = query or screen_id or tool_name

    # 1) consult kb-mcp-dual FIRST (the semantic search does the finding)
    kb_ev = await kb_client.consult(q, timeout=kb_timeout)
    # 2) then grp-mcp's own experience notes
    note_ev = kb.gather_evidence(q)

    blocked = False
    reason = None
    if level == "enforce" and not kb_ev.get("available"):
        blocked = True
        reason = (
            f"enforcement=enforce and kb-mcp-dual could not be consulted "
            f"({kb_ev.get('reason', 'unavailable')}). The KB must be consulted "
            f"before this write. Fix the KB server (kb_server.json / "
            f"GRP_MCP_KB_SERVER), lower enforcement to 'warn', or set it 'off'."
        )

    return {
        "level": level,
        "tool": tool_name,
        "query": q,
        "consult_order": ["kb-mcp-dual", "KNOWLEDGE.md"],
        "kb": kb_ev,
        "knowledge": note_ev,
        "blocked": blocked,
        "block_reason": reason,
    }
