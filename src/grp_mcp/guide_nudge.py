"""Tool-selection nudge — a soft, warn-only reminder to consult the discovery
tools (guide / screen_capabilities / get_setup_guidance) before an
ambiguous-plane ERP mutation.

This is deliberately NOT the write-preflight (preflight.py): it never blocks
anything, at any enforcement level. Today, "call guide first" lives only as
prose in the server's MCP instructions blob — nothing in the runtime checks or
even hints that it happened. Getting the WRITE plane wrong is the case that
actually costs something (a real mutation attempt against the wrong tool), so
this nudges once per session on the first ERP-mutation call, and never again
once a discovery tool has been consulted. Reads are never nudged — guessing
wrong on a read just costs a retry.

Process-lifetime state (module global), same pattern as kb_client._CACHE: one
grp-mcp server process is a reasonable proxy for "one session".
"""

from __future__ import annotations

from . import enforcement

_consulted = False

HINT = (
    "grp-mcp has ~106 tools across five Acumatica planes — guessing the wrong "
    "one for this screen/entity is a common mistake. Call guide() or "
    "screen_capabilities(screen_id) first to confirm the plane/tool before "
    "further writes this session. (This hint fires once per session.)"
)


def mark_consulted() -> None:
    """Record that a discovery tool (guide / screen_capabilities /
    get_setup_guidance) has been called this session."""
    global _consulted
    _consulted = True


def reset() -> None:
    """Test-only: clear the process-lifetime consulted flag."""
    global _consulted
    _consulted = False


def maybe_hint(tool_name: str) -> str | None:
    """Return the nudge text if `tool_name` is an ERP-mutation tool and no
    discovery tool has been consulted yet this session; else None."""
    if _consulted:
        return None
    if not enforcement.is_erp_mutation(tool_name):
        return None
    return HINT


def stamp_hint(result: object, tool_name: str) -> object:
    """Attach a one-shot `tool_selection_hint` to a mutation tool's result, if
    applicable. No-op if the result isn't a dict, already carries the key, or
    the hint doesn't apply. Returns the same object for inline use."""
    if isinstance(result, dict) and "tool_selection_hint" not in result:
        hint = maybe_hint(tool_name)
        if hint is not None:
            result["tool_selection_hint"] = hint
    return result
