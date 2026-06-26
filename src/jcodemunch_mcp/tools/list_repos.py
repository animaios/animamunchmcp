"""List indexed repositories."""

import time
from typing import Optional

from ..storage import IndexStore


def list_repos(storage_path: Optional[str] = None) -> dict:
    """List all indexed repositories.

    Returns:
        Dict with count, list of repos, and _meta envelope.
    """
    start = time.perf_counter()
    store = IndexStore(base_path=storage_path)
    repos = store.list_repos()
    elapsed = (time.perf_counter() - start) * 1000

    return {
        "count": len(repos),
        "repos": repos,
        "_meta": {
            "timing_ms": round(elapsed, 1),
        },
    }


def repos_report(storage_path: Optional[str] = None) -> list[dict]:
    """Cockpit view of indexed repos: per-repo counts + freshness.

    Returns per-repo metadata (counts, languages, indexed_at) from the index store.
    """
    store = IndexStore(base_path=storage_path)
    repos = store.list_repos()

    report: list[dict] = []
    for r in repos:
        report.append(
            {
                "repo_id": r.get("repo", ""),
                "display_name": r.get("display_name") or r.get("repo", ""),
                "source_root": r.get("source_root", ""),
                "file_count": r.get("file_count", 0),
                "symbol_count": r.get("symbol_count", 0),
                "languages": r.get("languages", {}) or {},
                "indexed_at": r.get("indexed_at", ""),
                "freshness": "fresh",
            }
        )
    return report
