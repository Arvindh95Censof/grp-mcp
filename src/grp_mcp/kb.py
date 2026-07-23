"""Trusted knowledge-base adapter — server-fetched, unfakeable KB evidence.

The KB-first policy (server instructions) tells an agent to consult the
Acumatica knowledge base before a write. But grp-mcp cannot PROVE an agent
called a separate kb-mcp server — a caller can fabricate "I read document X".
Caller-supplied references are therefore an *attestation*, never proof.

This module makes the evidence real: the server reads the KB **it ships**
(the bundled ``KNOWLEDGE.md`` — grp-mcp's own distilled Acumatica-driving
lessons) and stamps a cryptographic digest of the exact section text into the
preflight result. The evidence is produced server-side from a source the
server controls, so it cannot be forged by the caller.

Scope note: this trusts KNOWLEDGE.md (packaged, offline, always present), not
the full external Obsidian vault. Narrower, but real and dependency-free — the
deliberate trade chosen for enforceable evidence (see KNOWLEDGE.md §20).

Pure/offline: no network, no API call. Safe to import from anywhere (imports
nothing from ``server``), so the enforcement/preflight path can use it without
a circular import.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
from pathlib import Path


def knowledge_text() -> str | None:
    """Read the bundled KNOWLEDGE.md. Works from an installed wheel
    (grp_mcp/KNOWLEDGE.md via force-include) and from an editable/src checkout
    (repo-root KNOWLEDGE.md, two dirs up from this file)."""
    try:
        p = importlib.resources.files("grp_mcp").joinpath("KNOWLEDGE.md")
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1], here.parent):
        cand = base / "KNOWLEDGE.md"
        if cand.is_file():
            return cand.read_text(encoding="utf-8")
    return None


def split_sections(text: str) -> list[dict]:
    """Split KNOWLEDGE.md into its top-level ``## N. Title`` sections (pure,
    unit-testable). Returns [{num, title, heading, body}] in document order;
    content before the first numbered heading is dropped (it's the intro)."""
    out: list[dict] = []
    cur: dict | None = None
    lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(\d+)\.\s+(.*)$", line)
        if m:
            if cur is not None:
                cur["body"] = "\n".join(lines).strip()
                out.append(cur)
            cur = {"num": m.group(1), "title": m.group(2).strip(),
                   "heading": f"{m.group(1)}. {m.group(2).strip()}"}
            lines = [line]
        elif cur is not None:
            lines.append(line)
    if cur is not None:
        cur["body"] = "\n".join(lines).strip()
        out.append(cur)
    return out


def _digest(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def doc_version() -> str | None:
    """A stable version stamp for the whole KB = digest of the full document.
    Changes iff KNOWLEDGE.md changes, so an enforcement layer can key on it."""
    text = knowledge_text()
    return _digest(text) if text is not None else None


# Terms that shouldn't count as a "topic" match on their own (too common).
_STOPWORDS = frozenset({
    "the", "a", "an", "to", "for", "of", "and", "or", "on", "in", "is", "it",
    "screen", "report", "value", "field", "record", "row", "data", "set",
})


def _tokens(query: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9]+", query.lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


def gather_evidence(query: str, *, max_sections: int = 3) -> dict:
    """Server-fetched KB evidence for a preflight query (a screen id, entity,
    or free text). Returns the matching KNOWLEDGE.md section(s) with a digest
    of the exact text the server read — verifiable, not caller-supplied.

    Shape:
      {
        "source": "KNOWLEDGE.md",
        "doc_version": "sha256:...",         # whole-doc stamp
        "matched": [
          {"section": "18. ...", "digest": "sha256:...", "excerpt": "...", "score": N},
          ...
        ],
        "match_count": N,
      }
    match_count == 0 means the KB has nothing specific on this query — a
    truthful signal (no fabricated coverage), not an error.
    """
    text = knowledge_text()
    if text is None:
        return {"source": "KNOWLEDGE.md", "error": "KNOWLEDGE.md not found in package or repo",
                "doc_version": None, "matched": [], "match_count": 0}
    secs = split_sections(text)
    toks = _tokens(query)
    scored: list[tuple[int, dict]] = []
    for s in secs:
        hay = (s["title"] + "\n" + s["body"]).lower()
        # title matches weigh more than body matches
        title_l = s["title"].lower()
        score = 0
        for t in toks:
            if t in title_l:
                score += 5
            score += hay.count(t)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    matched = []
    for score, s in scored[:max_sections]:
        body = s["body"]
        excerpt = body[:400] + ("…" if len(body) > 400 else "")
        matched.append({
            "section": s["heading"],
            "digest": _digest(body),
            "excerpt": excerpt,
            "score": score,
        })
    return {
        "source": "KNOWLEDGE.md",
        "doc_version": doc_version(),
        "matched": matched,
        "match_count": len(matched),
    }
