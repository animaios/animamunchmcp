"""Single-doc detail view — pairs with v1.55 list_docs (v1.58.0).

`list_docs(repo)` enumerates documents with brief stats. When an agent
zooms into one doc, it then needs role/tag distribution, the section
list (handles), and per-doc byte info. Before v1.58 that meant calling
`list_docs` (whole repo) + `get_toc(path_glob=...)` + `get_all_tags`/
`get_all_roles` and filtering each. Now one call.

Output is a doc-scoped slice of get_index_overview (v1.56) plus the
flat section list — handle-only, no content reads.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..storage import DocStore


def get_doc(
    repo: str,
    doc_path: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return a per-doc detail view: sections + role/tag dists + size.

    Sections are emitted as ``{id, title, level, byte_start, byte_end}``
    handles ordered by `byte_start`. `role_distribution` and
    `tag_distribution` map name → section_count, sorted count desc then
    name asc.
    """
    t0 = time.perf_counter()
    if not doc_path:
        return {"error": "doc_path is required"}
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    # Filter to this doc.
    doc_secs = [s for s in index.sections if s.get("doc_path") == doc_path]
    if not doc_secs:
        return {"error": f"Document not found in index: {doc_path}"}

    doc_secs.sort(key=lambda s: s.get("byte_start", 0) or 0)

    # Section list (handles).
    sections = []
    for s in doc_secs:
        sections.append({
            "id": s.get("id"),
            "title": s.get("title"),
            "level": s.get("level"),
            "byte_start": int(s.get("byte_start", 0) or 0),
            "byte_end": int(s.get("byte_end", 0) or 0),
        })

    # Distributions.
    role_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for s in doc_secs:
        r = ((s.get("metadata") or {}).get("role") or "").strip().lower()
        if r:
            role_counts[r] = role_counts.get(r, 0) + 1
        for t in (s.get("tags") or []):
            if isinstance(t, str) and t.strip():
                k = t.strip().lower()
                tag_counts[k] = tag_counts.get(k, 0) + 1

    def _topdist(counts: dict[str, int], key: str) -> list:
        rows = [{key: k, "section_count": v} for k, v in counts.items()]
        rows.sort(key=lambda r: (-r["section_count"], r[key]))
        return rows

    # On-disk size of the cached source file.
    byte_size = 0
    try:
        cached = store._safe_content_path(store._content_dir(owner, name), doc_path)
        if cached and cached.exists():
            byte_size = cached.stat().st_size
    except OSError:
        pass

    fmt = (os.path.splitext(doc_path)[1] or "").lower() or None

    return {
        "repo": f"{owner}/{name}",
        "doc_path": doc_path,
        "format": fmt,
        "byte_size": byte_size,
        "section_count": len(sections),
        "sections": sections,
        "role_distribution": _topdist(role_counts, "role"),
        "tag_distribution": _topdist(tag_counts, "tag"),
        "indexed_at": index.indexed_at,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        },
    }
