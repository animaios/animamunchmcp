"""get_undocumented_symbols — companion to jcodemunch's get_untested_symbols (v1.22.0).

Given a doc index and a jcodemunch code repo, returns the symbols from
``code_repo`` that are *not* mentioned in the doc index (by name, qualified
name, or any common variant).

Implementation strategy mirrors v1.17 ``link_code_to_symbols``:

1. Best-effort import of jcodemunch's ``search_symbols``. Missing → return
   `_meta.bridge_available=false` + empty result so jdocmunch stays
   standalone-friendly.
2. Walk the code repo's symbols (paginate via search_symbols broad query
   ``*`` or empty — different jcodemunch versions; we try both).
3. Build a haystack = lowercased title + summary + content concatenation
   across all doc sections.
4. For each symbol: check name + qualified_name (when present); if neither
   appears in the haystack, mark undocumented.

Returns ``{undocumented:[{symbol_id, name, kind, qualified_name}], coverage:
{total_symbols, documented, undocumented_count, coverage_pct}, _meta}``.
"""

from __future__ import annotations

import time
from typing import Optional

from ..storage import DocStore
from ._bridge import (
    import_search_symbols,
    probe_code_repo,
    code_repo_not_found_result,
)


def _enumerate_symbols(search_symbols, code_repo: str, max_symbols: int) -> list[dict]:
    """Best-effort enumeration of symbols in a jcodemunch repo.

    jcodemunch versions differ in how they accept "list everything" — we
    try a couple of shapes. Stops at max_symbols.
    """
    out: list[dict] = []
    seen_ids: set[str] = set()

    for query in ("*", "."):
        try:
            res = search_symbols(repo=code_repo, query=query, max_results=max_symbols)
        except Exception:
            continue
        if not isinstance(res, dict):
            continue
        rows = res.get("results") or []
        for r in rows:
            sid = r.get("id") or r.get("symbol_id") or ""
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            out.append(r)
            if len(out) >= max_symbols:
                return out
        if out:
            break
    return out


def get_undocumented_symbols(
    repo: str,
    code_repo: str,
    max_symbols: int = 1000,
    storage_path: Optional[str] = None,
) -> dict:
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    search_symbols = import_search_symbols()
    bridge_available = search_symbols is not None

    if not bridge_available:
        return {
            "repo": f"{owner}/{name}",
            "code_repo": code_repo,
            "undocumented": [],
            "coverage": {
                "total_symbols": 0,
                "documented": 0,
                "undocumented_count": 0,
                "coverage_pct": None,
            },
            "_meta": {
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "bridge_available": False,
                "hint": "Install jcodemunch-mcp in this environment to enable inverse coverage.",
            },
        }

    # jdoc#68: validate code_repo before enumeration. A docs handle reused as
    # code_repo previously enumerated zero symbols and reported coverage_pct
    # null — indistinguishable from a documented-but-empty repo. Only fires on
    # an unresolvable handle.
    code_repo_error = probe_code_repo(search_symbols, code_repo)
    if code_repo_error is not None:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return code_repo_not_found_result(
            f"{owner}/{name}", code_repo, latency_ms, code_repo_error
        )

    symbols = _enumerate_symbols(search_symbols, code_repo, max_symbols)

    # Build a haystack from every doc section. Title + summary are always
    # present after load; content only when inline (v1.18 structured
    # OpenAPI sections). Tokenize the haystack with the BM25 tokenizer so
    # that "auth_helper" / "DocumentedClass" match the same way the index
    # uses them (snake_case + CamelCase split, lowercase, stop-word drop).
    from ..retrieval.tokenize import tokenize_unique

    haystack_tokens: set[str] = set()
    # Exact lowercased inline-code spans (#59): an exact name match here is an
    # authoritative "documented" signal, stronger than incidental token overlap.
    exact_spans: set[str] = set()
    for sec in index.sections:
        haystack_tokens |= tokenize_unique(sec.get("title") or "")
        haystack_tokens |= tokenize_unique(sec.get("summary") or "")
        if sec.get("content"):
            haystack_tokens |= tokenize_unique(sec["content"])
        # Inline backtick mentions are how prose names symbols (#59): feed them
        # into the haystack (recall) and keep their exact forms (precision).
        for span in sec.get("inline_code", []) or []:
            haystack_tokens |= tokenize_unique(span)
            exact_spans.add(span.lower())

    undocumented: list[dict] = []
    documented_count = 0
    for sym in symbols:
        name = sym.get("name") or ""
        qualified = sym.get("qualified_name") or sym.get("fqn") or ""
        sym_tokens = tokenize_unique(name) | tokenize_unique(qualified)
        sym_tokens = {t for t in sym_tokens if len(t) >= 3}
        # Documented when an exact inline-span names the symbol, or at least one
        # of its meaningful tokens appears in the doc index.
        exact_hit = name.lower() in exact_spans or (qualified and qualified.lower() in exact_spans)
        hit = exact_hit or (bool(sym_tokens) and bool(sym_tokens & haystack_tokens))
        if hit:
            documented_count += 1
        else:
            undocumented.append(
                {
                    "symbol_id": sym.get("id") or sym.get("symbol_id"),
                    "name": sym.get("name"),
                    "kind": sym.get("kind"),
                    "qualified_name": sym.get("qualified_name") or sym.get("fqn"),
                }
            )

    total = len(symbols)
    coverage_pct = round(100.0 * documented_count / total, 2) if total else None

    return {
        "repo": f"{owner}/{name}",
        "code_repo": code_repo,
        "undocumented": undocumented,
        "coverage": {
            "total_symbols": total,
            "documented": documented_count,
            "undocumented_count": len(undocumented),
            "coverage_pct": coverage_pct,
        },
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "bridge_available": True,
            "max_symbols": max_symbols,
        },
    }
