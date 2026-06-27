"""get_doc_health — single-shot doc-set health diagnostics (v2.0.0).

Aggregates signals already computed by other tools so an operator can see
the whole index at a glance:

  - section_count, doc_count
  - role_distribution (counts per metadata.role)
  - freshness: fresh / edited_uncommitted / stale_index counts
  - broken_link_count (delegates to get_broken_links)
  - stale_page_count (delegates to get_stale_pages when sources_dir resolves)
  - drift: max/mean cosine drift from check_embedding_drift (when canary
    has been captured)
  - bm25_stats: N, avgdl, df_top_k_size — quick sanity check that the
    corpus stats persisted at index time
  - has_embeddings, embedding_count

Single round-trip; cheap to run. Useful as a CI check or periodic ping.
"""

from __future__ import annotations

import re
import time

# Structural-integrity signals (#54). A column-0 H2+ line inside a stored
# fenced body means a fence accident swallowed real headings as code; a
# heading-level jump (e.g. H2 -> H4) signals a malformed outline. Markdown-
# variant fences are exempt (legitimate markdown examples).
_SWALLOWED_HEADING_RE = re.compile(r"(?m)^#{2,6}\s")
_MD_FENCE_LANGS = {"md", "markdown", "mdx"}


def _structural_signals(sections: list) -> dict:
    """Compute structural-integrity warnings from already-persisted data (#54):
    headings swallowed into stored code blocks, and heading-level skips. No
    parser change and no reindex needed beyond what populated code_blocks."""
    swallowed = 0
    level_skips = 0
    prev_level: dict[str, int] = {}
    for sec in sections:
        for blk in (sec.get("code_blocks") or []):
            if (blk.get("lang") or "").lower() in _MD_FENCE_LANGS:
                continue
            if _SWALLOWED_HEADING_RE.search(blk.get("content") or ""):
                swallowed += 1
        lvl = int(sec.get("level") or 0)
        if lvl > 0:
            doc = sec.get("doc_path", "")
            prev = prev_level.get(doc)
            if prev is not None and lvl - prev > 1:
                level_skips += 1
            prev_level[doc] = lvl
    return {
        "swallowed_heading_blocks": swallowed,
        "level_skips": level_skips,
        "structural_warning_count": swallowed + level_skips,
    }
from typing import Optional

from ..retrieval.freshness import FreshnessProbe
from ..storage import DocStore


def get_doc_health(
    repo: str,
    storage_path: Optional[str] = None,
) -> dict:
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    # Role distribution.
    roles: dict[str, int] = {}
    for sec in index.sections:
        role = ((sec.get("metadata") or {}).get("role")) or "unknown"
        roles[role] = roles.get(role, 0) + 1

    # Freshness — sample across all sections.
    probe = FreshnessProbe(store, owner, name, index)
    fresh_counts = {"fresh": 0, "edited_uncommitted": 0, "stale_index": 0}
    for sec in index.sections:
        bucket = probe.annotate(dict(sec))
        if bucket in fresh_counts:
            fresh_counts[bucket] += 1

    # Broken links — best-effort delegate.
    broken_link_count = -1
    try:
        from .get_broken_links import get_broken_links

        bl = get_broken_links(repo=f"{owner}/{name}", storage_path=storage_path)
        if isinstance(bl, dict) and "result" in bl:
            broken_link_count = int(bl["result"].get("broken_link_count", 0))
    except Exception:
        pass

    # v1.40.0: orphan sections — best-effort delegate to v1.39 tool.
    orphan_count = -1
    try:
        from .get_orphan_sections import get_orphan_sections

        orph = get_orphan_sections(repo=f"{owner}/{name}", storage_path=storage_path)
        if isinstance(orph, dict) and "result" in orph:
            orphan_count = int(orph["result"].get("orphan_count", 0))
    except Exception:
        pass

    # Embedding-drift canary.
    drift_alarm = None
    drift_max = None
    try:
        from ..embeddings.embed_drift import check_drift

        d = check_drift(base_path=storage_path)
        if d.get("has_canary"):
            drift_alarm = bool(d.get("alarm"))
            drift_max = d.get("max_drift")
    except Exception:
        pass

    # BM25 corpus sanity.
    bm25 = index.bm25_stats or {}
    bm25_summary = {
        "N": bm25.get("N", 0),
        "avgdl_title": (bm25.get("avgdl") or {}).get("title"),
        "avgdl_summary": (bm25.get("avgdl") or {}).get("summary"),
        "avgdl_content": (bm25.get("avgdl") or {}).get("content"),
        "df_size": len(bm25.get("df") or {}),
    }

    has_emb = index._has_embeddings()
    embedding_count = sum(1 for s in index.sections if s.get("embedding"))

    structural = _structural_signals(index.sections)

    payload = {
        "repo": f"{owner}/{name}",
        "section_count": len(index.sections),
        "doc_count": len(index.doc_paths),
        "role_distribution": roles,
        "freshness": fresh_counts,
        "broken_link_count": broken_link_count,
        "orphan_section_count": orphan_count,
        "structural": structural,
        "drift": {"has_canary": drift_alarm is not None, "alarm": drift_alarm, "max_drift": drift_max},
        "bm25": bm25_summary,
        "embeddings": {"present": has_emb, "covered_sections": embedding_count},
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "indexed_at": index.indexed_at,
        },
    }
    if index.head_sha:
        payload["head_sha"] = index.head_sha
    payload["source_dirty"] = bool(index.source_dirty)
    payload["sha_certified"] = bool(index.sha_certified)
    if index.repo_at_sha:
        payload["repo_at_sha"] = index.repo_at_sha
    return payload
