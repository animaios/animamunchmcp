"""Glossary tools: lookup_term, list_terms (v1.19.0)."""

from __future__ import annotations

from typing import Optional

from ..retrieval.glossary import load_terms, lookup
from ..storage import DocStore


def lookup_term(
    repo: str,
    term: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return every glossary entry whose term equals ``term``
    (case-insensitive, exact)."""
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    matches = lookup(storage_path, owner, name, term)
    return {
        "repo": f"{owner}/{name}",
        "term": term,
        "matches": matches,
        "_meta": {"match_count": len(matches)},
    }


def list_terms(
    repo: str,
    prefix: Optional[str] = None,
    max_results: int = 100,
    storage_path: Optional[str] = None,
) -> dict:
    """List glossary terms, optionally filtered by prefix.

    Output is sorted alphabetically (case-insensitive) and capped at
    ``max_results``.
    """
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    entries = load_terms(storage_path, owner, name) or []
    pre = (prefix or "").strip().lower()
    if pre:
        entries = [e for e in entries
                   if isinstance(e, dict) and (e.get("term") or "").lower().startswith(pre)]
    entries = sorted(entries, key=lambda e: (e.get("term") or "").lower())
    return {
        "repo": f"{owner}/{name}",
        "prefix": prefix,
        "terms": entries[:max_results],
        "_meta": {
            "match_count": len(entries),
            "returned": min(len(entries), max_results),
        },
    }
