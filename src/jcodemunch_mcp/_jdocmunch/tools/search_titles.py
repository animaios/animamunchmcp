"""Title-only navigation search (v1.57.0).

`search_sections` does full hybrid retrieval (BM25 + semantic + content
scoring + many filters). That's the right tool when the agent needs an
*answer*. But when the agent just needs a *navigation hit* — "find the
section whose heading matches `Authentication`" — full retrieval is
overkill.

This tool restricts scoring to the `title` field. Token-overlap match
on lowercased titles, with phrase-presence and prefix bonuses. No
content reads, no embeddings, no posting-list traversal. Fast enough
to call on every keystroke if needed.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from ..storage import DocStore


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _score_title(title_lc: str, title_tokens: list[str],
                 query_lc: str, query_tokens: set[str]) -> float:
    if not title_lc or not query_tokens:
        return 0.0
    score = 0.0
    # Exact equality wins.
    if title_lc == query_lc:
        score += 100.0
    # Whole-phrase substring.
    elif query_lc and query_lc in title_lc:
        score += 30.0
    # Token overlap with prefix bonus.
    title_set = set(title_tokens)
    overlap = query_tokens & title_set
    score += 5.0 * len(overlap)
    # Title starts with query.
    if title_lc.startswith(query_lc):
        score += 10.0
    return score


def search_titles(
    repo: str,
    query: str,
    max_results: int = 10,
    storage_path: Optional[str] = None,
) -> dict:
    """Return sections whose titles best match the query.

    Scoring is title-only. Output is a list of handles
    ``{id, title, level, doc_path}`` plus a ``_score`` for ranking
    transparency. No content, no metadata, no embeddings.

    Use when the agent has a heading text from a URL fragment, a
    screenshot, or a previous search and just wants the section_id.
    """
    t0 = time.perf_counter()
    if not query or not query.strip():
        return {"error": "query must not be empty"}
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    query_lc = query.strip().lower()
    query_tokens = set(_tokenize(query_lc))

    scored: list[tuple[float, dict]] = []
    for sec in index.sections:
        title = sec.get("title") or ""
        if not title:
            continue
        title_lc = title.lower()
        title_tokens = _tokenize(title_lc)
        s = _score_title(title_lc, title_tokens, query_lc, query_tokens)
        if s > 0:
            scored.append((s, sec))

    # Stable ranking: score desc, then section_id asc (deterministic
    # cross-platform — same as v1.36.1 fix).
    scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))

    results = []
    for score, sec in scored[:max_results]:
        results.append({
            "id": sec.get("id"),
            "title": sec.get("title"),
            "level": sec.get("level"),
            "doc_path": sec.get("doc_path"),
            "_score": float(score),
        })

    return {
        "repo": f"{owner}/{name}",
        "query": query,
        "results": results,
        "result_count": len(results),
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "max_results": max_results,
        },
    }
