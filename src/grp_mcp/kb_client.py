"""Consult kb-mcp-dual — the semantic Acumatica KB — as an MCP client.

The trusted-evidence problem (see kb.py): grp-mcp cannot prove an *agent*
called kb-mcp-dual. The fix here is that **grp-mcp itself** calls it. kb-mcp-dual
is a stdio MCP server, so grp-mcp launches it as a subprocess, speaks MCP, runs
its ``search_kb`` semantic search, and digests what comes back. The evidence is
produced by grp-mcp from the real KB search — not caller-supplied, not fakeable.

Why call it instead of reading the vault files directly: kb-mcp-dual owns the
*finding* — a multilingual embedding index over ~85k chunks. Re-implementing
that with filename matching would be strictly worse. grp-mcp consults the KB;
kb-mcp-dual does the search.

Launch spec resolution (a JSON ``{command, args, env}``), first hit wins:
  1. path in ``GRP_MCP_KB_SERVER``
  2. ``kb_server.json`` in the process working directory
  3. ``kb_server.json`` at the grp-mcp repo root (two dirs up from this file)
No spec found -> the feature is OFF: consult() returns available=False and the
preflight degrades gracefully (KNOWLEDGE.md + live checks still run).

Cost note: this spawns kb-mcp-dual per call, which loads its embedding model
(seconds). Writes are not a hot path, so that is acceptable for now; a
persistent cached session is a future optimization.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_ENV_SPEC = "GRP_MCP_KB_SERVER"

# Process-lifetime result cache. Consulting kb-mcp-dual cold costs ~20s (it loads
# an embedding model + index per spawn); the KB is stable within a session, so a
# repeat query returns instantly. Keyed by (query, top_k, guide_filter).
_CACHE: dict[tuple, dict] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _digest(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def _read_spec(p: Path) -> dict | None:
    try:
        if p and p.is_file():
            spec = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(spec, dict) and spec.get("command"):
                return spec
    except Exception:  # noqa: BLE001 — a bad spec file must not crash a write
        return None
    return None


def load_spec(explicit: str | None = None) -> dict | None:
    """Resolve the kb-mcp-dual launch spec, or None if not configured.

    An explicit path (argument or GRP_MCP_KB_SERVER) is AUTHORITATIVE — if given
    but missing/invalid, returns None rather than silently using a different
    file. Only when no path is specified does it fall back to kb_server.json in
    the working directory, then at the grp-mcp repo root."""
    cand = explicit or os.environ.get(_ENV_SPEC)
    if cand:
        return _read_spec(Path(cand))
    here = Path(__file__).resolve()
    for p in (Path.cwd() / "kb_server.json", here.parents[2] / "kb_server.json"):
        spec = _read_spec(p)
        if spec:
            return spec
    return None


def _tool_text(result: object) -> str:
    """Pull the text payload out of an MCP CallToolResult."""
    content = getattr(result, "content", None)
    if not content:
        return ""
    parts = []
    for c in content:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


async def consult(query: str, *, top_k: int = 8, guide_filter: str = "",
                  spec: dict | None = None, timeout: float = 120.0,
                  use_cache: bool = True) -> dict:
    """Ask kb-mcp-dual to semantically search the KB for `query` and return
    verifiable evidence (digested results). Never raises — a KB that is
    unconfigured, unreachable, or slow degrades to available=False so a write
    preflight can decide what to do based on the enforcement level.

    Returns:
      {available: True, source: "kb-mcp-dual", query, match_count,
       matched: [{path, title, heading, score, digest, excerpt}]}
      or {available: False, source: "kb-mcp-dual", reason}
    """
    import asyncio

    cache_key = (query, top_k, guide_filter)
    if use_cache and cache_key in _CACHE:
        return {**_CACHE[cache_key], "cached": True}

    spec = spec or load_spec()
    if not spec:
        return {"available": False, "source": "kb-mcp-dual",
                "reason": ("no kb_server.json configured — set GRP_MCP_KB_SERVER "
                           "or create kb_server.json ({command, args, env})")}

    async def _run() -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=spec["command"],
            args=list(spec.get("args") or []),
            env={**os.environ, **(spec.get("env") or {})},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(
                    "search_kb",
                    {"query": query, "top_k": top_k, "guide_filter": guide_filter},
                )
                text = _tool_text(res)
                items = json.loads(text) if text.strip() else []
        matched = []
        for it in items[:top_k]:
            snip = it.get("snippet") or ""
            path = it.get("path") or ""
            matched.append({
                "path": path,
                "title": it.get("title"),
                "heading": it.get("heading"),
                "score": it.get("score"),
                "digest": _digest(path + "\n" + snip),
                "excerpt": snip[:400] + ("…" if len(snip) > 400 else ""),
            })
        return {"available": True, "source": "kb-mcp-dual", "query": query,
                "match_count": len(matched), "matched": matched}

    try:
        result = await asyncio.wait_for(_run(), timeout=timeout)
        if use_cache and result.get("available"):
            _CACHE[cache_key] = result
        return result
    except asyncio.TimeoutError:
        return {"available": False, "source": "kb-mcp-dual",
                "reason": f"kb-mcp-dual did not respond within {timeout:g}s"}
    except Exception as e:  # noqa: BLE001 — consulting the KB must never break a write
        return {"available": False, "source": "kb-mcp-dual",
                "reason": f"{type(e).__name__}: {e}"}
