"""Role discovery tool — companion to v1.19's role classifier (v1.50.0).

`search_sections(role=...)` filters by role; `search_sections(profile=...)`
boosts a role bundle. Both require knowing which roles exist in a repo.
v1.40's `get_doc_health` exposes a `role_distribution` field as part of
the unified rollup. This tool surfaces it as a standalone, deeper view —
mirrors v1.46's `get_all_tags` for the role axis.

Returns the per-role section count plus a small sample of section ids
per role so agents can probe a class without re-running search.
"""

from __future__ import annotations

import time
from typing import Optional

from ..storage import DocStore


def get_all_roles(
    repo: str,
    sample_size: int = 3,
    storage_path: Optional[str] = None,
) -> dict:
    """Return every distinct role with section counts and id samples.

    Args:
        repo: Repository identifier.
        sample_size: How many section_ids to surface per role. Default 3.
            Pass 0 to omit samples entirely.
        storage_path: Custom storage path.

    Returns:
        ``{repo, roles: [{role, section_count, samples:[id,...]}, ...]
        sorted by count desc then role asc, total_unique,
        total_sections_classified, total_sections, _meta}``.
        Sections without `metadata.role` are bucketed under "unknown".
    """
    t0 = time.perf_counter()
    if sample_size < 0:
        return {"error": "sample_size must be non-negative"}
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    counts: dict[str, int] = {}
    samples: dict[str, list] = {}
    classified = 0
    for sec in index.sections:
        meta = sec.get("metadata") or {}
        role = (meta.get("role") or "").strip().lower() or "unknown"
        if role != "unknown":
            classified += 1
        counts[role] = counts.get(role, 0) + 1
        if sample_size > 0:
            bucket = samples.setdefault(role, [])
            if len(bucket) < sample_size:
                sid = sec.get("id")
                if sid:
                    bucket.append(sid)

    rows = []
    for role, cnt in counts.items():
        entry = {"role": role, "section_count": cnt}
        if sample_size > 0:
            entry["samples"] = samples.get(role, [])
        rows.append(entry)
    rows.sort(key=lambda r: (-r["section_count"], r["role"]))

    return {
        "repo": f"{owner}/{name}",
        "roles": rows,
        "total_unique": len(rows),
        "total_sections_classified": classified,
        "total_sections": len(index.sections),
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "sample_size": sample_size,
        },
    }
