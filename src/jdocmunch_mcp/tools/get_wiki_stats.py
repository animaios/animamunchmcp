"""Wiki health dashboard: orphans, most-linked, tag distribution, section counts."""

import posixpath
import time
from typing import Optional

from ..storage.doc_store import DocStore


def _is_external(href: str) -> bool:
    return href.startswith(("http://", "https://", "ftp://", "mailto:", "tel:"))


def _resolve_file_path(source_doc: str, target_file: str) -> str:
    if target_file.startswith("/"):
        return target_file.lstrip("/")
    source_dir = posixpath.dirname(source_doc)
    return posixpath.normpath(posixpath.join(source_dir, target_file))


def get_wiki_stats(
    repo: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Aggregate wiki health metrics.

    Returns:
      - page_count: total indexed documents
      - section_count: total sections across all docs
      - orphan_pages: docs with zero inbound internal links
      - most_linked: top 10 most-linked-to documents
      - tag_distribution: {tag: count} across all sections
      - sections_per_doc: min/max/avg section counts
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    doc_path_set = set(index.doc_paths)
    sections = index.sections

    # Build inbound link counts
    inbound: dict = {dp: 0 for dp in doc_path_set}
    total_internal_links = 0

    for sec in sections:
        source_doc = sec.get("doc_path", "")
        refs = sec.get("references", [])

        for href in refs:
            if _is_external(href):
                continue
            file_part = href.split("#")[0]
            if not file_part:
                continue
            if ":" in file_part and not file_part.startswith("."):
                continue

            resolved = _resolve_file_path(source_doc, file_part)
            resolved_norm = posixpath.normpath(resolved)

            if resolved_norm in inbound:
                inbound[resolved_norm] += 1
                total_internal_links += 1

    # Orphans: pages with zero inbound links
    orphan_pages = sorted([dp for dp, count in inbound.items() if count == 0])

    # Most linked: top 10 by inbound count
    most_linked = sorted(
        [{"doc_path": dp, "inbound_links": count} for dp, count in inbound.items() if count > 0],
        key=lambda x: x["inbound_links"],
        reverse=True,
    )[:10]

    # Tag distribution
    tag_counts: dict = {}
    for sec in sections:
        for tag in sec.get("tags", []):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Sort tags by count descending
    tag_distribution = dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True))

    # Sections per doc
    doc_section_counts: dict = {}
    for sec in sections:
        dp = sec.get("doc_path", "")
        doc_section_counts[dp] = doc_section_counts.get(dp, 0) + 1

    counts = list(doc_section_counts.values()) if doc_section_counts else [0]

    # v1.34.0: surface persisted near-duplicate clusters when present.
    duplicate_clusters: list = []
    duplicate_section_count = 0
    try:
        from ..retrieval.dedup import load as _load_dupes
        duplicate_clusters = _load_dupes(storage_path, owner, name)
        # Each cluster of N members has N-1 "redundant" sections.
        duplicate_section_count = sum(
            max(0, len(c.get("member_ids") or []) - 1) for c in duplicate_clusters
        )
    except Exception:
        duplicate_clusters = []

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "page_count": len(doc_path_set),
            "section_count": len(sections),
            "total_internal_links": total_internal_links,
            "orphan_page_count": len(orphan_pages),
            "orphan_pages": orphan_pages,
            "most_linked": most_linked,
            "tag_count": len(tag_counts),
            "tag_distribution": tag_distribution,
            "sections_per_doc": {
                "min": min(counts),
                "max": max(counts),
                "avg": round(sum(counts) / len(counts), 1),
            },
            "duplicate_cluster_count": len(duplicate_clusters),
            "duplicate_section_count": duplicate_section_count,
            "duplicate_clusters": duplicate_clusters,
        },
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
