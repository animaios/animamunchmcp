"""Shared jdocmunch <-> jcodemunch bridge helpers (jdoc#68).

Centralizes the best-effort jcodemunch import and the cross-suite ``code_repo``
resolution probe used by ``link_code_to_symbols``, ``get_undocumented_symbols``,
and ``resolve_related_code_repos``.

The two suites keep independent repo-identity models on purpose (jCodeMunch
identifies source-code repos, jdocmunch identifies documentation corpora), so a
syntactically valid jdocmunch docs handle (e.g. ``local/foo-docs``) is not a
resolvable jCodeMunch code handle. These helpers make that boundary explicit
rather than letting a wrong handle collapse into an empty result set.
"""

from __future__ import annotations

from typing import Optional


def import_search_symbols():
    """Return jcodemunch's ``search_symbols`` if importable, else ``None``."""
    try:
        from jcodemunch_mcp.tools.search_symbols import search_symbols  # type: ignore
        return search_symbols
    except Exception:
        return None


def import_code_index_store():
    """Return jcodemunch's ``IndexStore`` class if importable, else ``None``."""
    try:
        from jcodemunch_mcp.storage import IndexStore  # type: ignore
        return IndexStore
    except Exception:
        return None


def probe_code_repo(search_symbols, code_repo: str) -> Optional[str]:
    """Check that ``code_repo`` resolves to a loadable jCodeMunch index.

    jCodeMunch's ``search_symbols`` resolves the repo handle (and loads the
    index) before it runs the query, returning an ``{"error": ...}`` envelope
    for an unknown bare name, an ambiguous name, or a handle that resolves to
    ``owner/name`` with no loadable index (the docs-handle-as-code_repo case).

    Returns ``None`` when ``code_repo`` resolves (so an empty-but-valid result
    still means "no matches"), otherwise the jCodeMunch error string — which
    lets a caller distinguish a wrong handle type from a genuine zero-match.
    """
    try:
        out = search_symbols(repo=code_repo, query="*", max_results=1)
    except Exception as e:  # pragma: no cover - defensive
        return f"search_symbols raised: {e}"
    if isinstance(out, dict) and out.get("error"):
        return str(out["error"])
    return None


def code_repo_not_found_result(
    repo: str,
    code_repo: str,
    latency_ms: int,
    code_repo_error: Optional[str],
) -> dict:
    """Build the explicit invalid-code-handle diagnostic (jdoc#68).

    Reserved for the case where ``code_repo`` does not resolve to a jCodeMunch
    index — never for a resolved handle that simply produced no links.
    """
    return {
        "repo": repo,
        "code_repo": code_repo,
        "error": "code_repo_not_found",
        "_meta": {
            "latency_ms": latency_ms,
            "bridge_available": True,
            "code_repo_resolved": False,
            "code_repo_error": code_repo_error,
            "hint": (
                "code_repo is not a resolvable jCodeMunch repo handle. Use a "
                "handle from jcodemunch list_repos/resolve_repo, or call "
                "resolve_related_code_repos(repo=<docs repo>) to find candidates."
            ),
        },
    }
