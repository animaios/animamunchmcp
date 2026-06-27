"""Tag discovery tool — companion to v1.45.0's `tags` filter (v1.46.0).

`search_sections(tags=[...])` filters by tag, but agents had no way to
*discover* what tags exist in a repo without scanning all sections
themselves. This tool aggregates `Section.tags` across the index and
returns the unique tag set with per-tag section counts.

Lowercase + strip normalization (matches the v1.45 filter normalization
so the surfaced names are exactly what you'd pass back in).
"""

from __future__ import annotations

import time
from typing import Optional

from ..storage import DocStore


def get_all_tags(
    repo: str,
    min_section_count: int = 1,
    storage_path: Optional[str] = None,
) -> dict:
    """Return every unique tag across the repo with section counts.

    Args:
        repo: Repository identifier.
        min_section_count: Drop tags appearing in fewer than this many
            sections. Default 1 (return all). Use to filter out
            single-occurrence tags that are likely typos.
        storage_path: Custom storage path.

    Returns:
        ``{tags: [{tag, section_count}, ...] sorted by count desc then
        tag asc, total_unique, total_sections_tagged, repo, _meta}``.
    """
    t0 = time.perf_counter()
    if min_section_count < 1:
        return {"error": "min_section_count must be >= 1"}
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    counts: dict[str, int] = {}
    sections_with_any_tag = 0
    for sec in index.sections:
        sec_tags = sec.get("tags") or []
        # Normalize per-section: dedupe within a section so a section
        # with [#api, #api] only counts once.
        normalized: set = set()
        for t in sec_tags:
            if not isinstance(t, str):
                continue
            n = t.strip().lower()
            if n:
                normalized.add(n)
        if normalized:
            sections_with_any_tag += 1
        for n in normalized:
            counts[n] = counts.get(n, 0) + 1

    filtered = [
        {"tag": tag, "section_count": cnt}
        for tag, cnt in counts.items()
        if cnt >= min_section_count
    ]
    # Sort: count desc, then tag asc for determinism.
    filtered.sort(key=lambda r: (-r["section_count"], r["tag"]))

    return {
        "repo": f"{owner}/{name}",
        "tags": filtered,
        "total_unique": len(filtered),
        "total_sections_tagged": sections_with_any_tag,
        "total_sections": len(index.sections),
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "min_section_count": min_section_count,
        },
    }
