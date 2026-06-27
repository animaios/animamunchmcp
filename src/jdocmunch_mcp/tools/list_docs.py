"""Doc-level inventory of an indexed repo (v1.55.0).

`list_repos` enumerates indexed repos. `get_toc_tree` returns section
trees per doc. Nothing returned a flat doc-level inventory: which
documents exist in this repo, how many sections each has, what format,
how many bytes on disk.

This tool fills that gap. Pure additive aggregation, handle-only —
nothing returned here requires a content read.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..storage import DocStore


def list_docs(
    repo: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return a flat list of indexed documents with per-doc stats.

    For each doc:
      - ``doc_path`` (POSIX-normalized relative path)
      - ``section_count`` (sections whose doc_path == this)
      - ``format`` (file extension, lowercase)
      - ``byte_size`` (current on-disk size of the cached source file,
        or 0 when the cache is missing — signal of a stale index)

    Sorted by `doc_path` ascending for stable output.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    counts: dict[str, int] = {}
    for sec in index.sections:
        dp = sec.get("doc_path") or ""
        if dp:
            counts[dp] = counts.get(dp, 0) + 1

    docs: list = []
    total_bytes = 0
    content_dir = store._content_dir(owner, name)
    for dp, n in counts.items():
        ext = os.path.splitext(dp)[1].lower()
        size = 0
        try:
            cached = store._safe_content_path(content_dir, dp)
            if cached and cached.exists():
                size = cached.stat().st_size
        except OSError:
            size = 0
        total_bytes += size
        docs.append({
            "doc_path": dp,
            "section_count": n,
            "format": ext or None,
            "byte_size": size,
        })

    docs.sort(key=lambda d: d["doc_path"])

    return {
        "repo": f"{owner}/{name}",
        "docs": docs,
        "doc_count": len(docs),
        "total_section_count": len(index.sections),
        "total_byte_size": total_bytes,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "indexed_at": index.indexed_at,
        },
    }
