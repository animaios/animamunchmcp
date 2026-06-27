"""Find sections with zero inbound backlinks — documentation rot finder (v1.39.0).

Some doc sets accumulate sections that nobody links to. They're not
broken, not stale, not duplicated — just *orphaned*. Agents searching
for an answer can still find them via search_sections, but no curated
navigation reaches them. Maintainers want to know about them so they
can either link them in or delete them.

This tool inverts the link graph one time and reports every section
whose doc_path receives zero inbound references from any other section
(including from itself's own document).

Pairs with v1.18+ get_broken_links (links pointing nowhere) and v1.31+
get_stale_pages (sections drifting from source). The three together
form the doc-health triad.
"""

from __future__ import annotations

import posixpath
import time
from typing import Optional

from ..storage import DocStore


def _is_external(href: str) -> bool:
    return href.startswith(("http://", "https://", "ftp://", "mailto:", "tel:"))


def _resolve_file_path(source_doc: str, target_file: str) -> str:
    if target_file.startswith("/"):
        return target_file.lstrip("/")
    source_dir = posixpath.dirname(source_doc)
    return posixpath.normpath(posixpath.join(source_dir, target_file))


def get_orphan_sections(
    repo: str,
    include_same_doc: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Return sections whose doc_path receives zero inbound links.

    Args:
        repo: Repository identifier.
        include_same_doc: If True, count links from sections in the same
            doc_path as inbound (a TOC linking to its own headings would
            then keep all those sections from being flagged). Default
            False — only cross-document references count.
        storage_path: Custom storage path.

    Returns:
        ``{"result": {"orphan_sections": [...], "orphan_count": N,
        "total_sections": M, "_meta": {...}}}``. Each orphan entry is
        ``{id, title, doc_path, level, summary}`` — handle-only, no
        content reads.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    # Phase 1: build set of doc_paths that receive inbound links.
    referenced_docs: set = set()
    for sec in index.sections:
        source_doc = sec.get("doc_path", "")
        for href in sec.get("references", []) or []:
            if _is_external(href):
                continue
            file_part = href.split("#")[0]
            if not file_part:
                # anchor-only — internal jump within source doc.
                if include_same_doc:
                    referenced_docs.add(source_doc)
                continue
            resolved = posixpath.normpath(_resolve_file_path(source_doc, file_part))
            if not include_same_doc and resolved == source_doc:
                continue
            referenced_docs.add(resolved)

    # Phase 2: any section whose doc_path is NOT in referenced_docs is
    # an orphan. Skip the synthetic level-0 doc roots — they have title
    # equal to the doc filename and aren't authored navigation targets.
    orphans: list = []
    for sec in index.sections:
        dp = sec.get("doc_path", "")
        if not dp:
            continue
        if dp in referenced_docs:
            continue
        if sec.get("level") == 0:
            # Synthetic doc-root from the parser; not user-facing.
            continue
        orphans.append({
            "id": sec.get("id"),
            "title": sec.get("title"),
            "doc_path": dp,
            "level": sec.get("level"),
            "summary": sec.get("summary"),
        })

    # Stable order: by doc_path, then byte_start (preserved by indexing order).
    orphans.sort(key=lambda r: (r.get("doc_path", ""), r.get("id", "")))

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "orphan_count": len(orphans),
            "total_sections": len(index.sections),
            "orphan_sections": orphans,
        },
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "include_same_doc": include_same_doc,
        },
    }
