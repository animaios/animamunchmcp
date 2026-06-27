"""get_doc_coverage tool: Check which symbols have matching documentation sections."""

import re
import time
from typing import Optional

from ..storage import DocStore


def _symbol_name_from_id(symbol_id: str) -> str:
    """Extract the bare symbol name from a jcodemunch symbol ID.

    jcodemunch IDs follow: {repo}::{filepath}::{name}#{type}
    E.g. 'my-repo::src/server.py::handle_request#function' -> 'handle_request'
    Falls back to the full ID if the expected format is not matched.
    """
    # Strip the type suffix
    part = symbol_id.rsplit("#", 1)[0] if "#" in symbol_id else symbol_id
    # Take the last component after '::'
    if "::" in part:
        part = part.rsplit("::", 1)[-1]
    return part.strip()


def _matches_section(symbol_name: str, sections: list, doc_path: Optional[str] = None) -> list:
    """Return sections whose title mentions the symbol name (case-insensitive).

    Matches whole-word or camelCase boundary. Returns list of matching section summaries.
    """
    name_lower = symbol_name.lower()
    # Allow underscores and camelCase boundaries
    pattern = re.compile(
        r"(?<![a-z0-9_])" + re.escape(name_lower) + r"(?![a-z0-9_])",
        re.IGNORECASE,
    )
    matches = []
    for sec in sections:
        if doc_path and sec.get("doc_path") != doc_path:
            continue
        title = sec.get("title", "")
        if pattern.search(title.lower()):
            matches.append({
                "section_id": sec.get("id", ""),
                "section_title": title,
                "doc_path": sec.get("doc_path", ""),
            })
    return matches


def get_doc_coverage(
    repo: str,
    symbol_ids: list,
    storage_path: Optional[str] = None,
) -> dict:
    """Report which symbols have corresponding documentation sections.

    Given a list of jcodemunch symbol IDs, checks the jdocmunch index for
    sections whose title mentions the symbol name. Bridges jcodemunch <-> jdocmunch.

    Output: {documented: [...], undocumented: [...], coverage_pct}
    Each documented entry includes matching section IDs.
    symbol_ids capped at 200.
    """
    t0 = time.perf_counter()
    symbol_ids = symbol_ids[:200]

    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    sections = index.sections
    documented = []
    undocumented = []

    for sid in symbol_ids:
        sym_name = _symbol_name_from_id(sid)
        if not sym_name:
            undocumented.append({"symbol_id": sid, "symbol_name": sym_name})
            continue

        matches = _matches_section(sym_name, sections)
        if matches:
            documented.append({
                "symbol_id": sid,
                "symbol_name": sym_name,
                "matching_sections": matches,
            })
        else:
            undocumented.append({
                "symbol_id": sid,
                "symbol_name": sym_name,
            })

    total = len(symbol_ids)
    doc_count = len(documented)
    coverage_pct = round(doc_count / total * 100, 1) if total > 0 else 0.0

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "symbols_checked": total,
            "documented_count": doc_count,
            "undocumented_count": len(undocumented),
            "coverage_pct": coverage_pct,
            "documented": documented,
            "undocumented": undocumented,
        },
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
