"""Find all pages that link TO a given document (inverse reference graph)."""

import time
from typing import Optional

from ..storage.doc_store import DocStore


def _is_external(href: str) -> bool:
    return href.startswith(("http://", "https://", "ftp://", "mailto:", "tel:"))


def _resolve_file_path(source_doc: str, target_file: str) -> str:
    """Resolve a relative link target against the source document's directory."""
    if target_file.startswith("/"):
        return target_file.lstrip("/")
    import posixpath
    source_dir = posixpath.dirname(source_doc)
    resolved = posixpath.normpath(posixpath.join(source_dir, target_file))
    return resolved


def get_backlinks(
    repo: str,
    doc_path: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all sections that contain a link pointing to doc_path.

    Builds an inverse reference graph: given a target page, returns every
    section in the wiki that links to it. Useful for the LLM Wiki pattern —
    when a source changes, find which wiki pages reference it.

    Returns: list of {source_file, source_section, source_section_id, link}
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    # Normalize target: strip leading slash, normalize path
    import posixpath
    target_norm = posixpath.normpath(doc_path.lstrip("/"))

    backlinks: list = []

    for sec in index.sections:
        source_doc = sec.get("doc_path", "")
        refs = sec.get("references", [])

        for href in refs:
            if _is_external(href):
                continue

            # Strip anchor
            file_part = href.split("#")[0]
            if not file_part:
                continue

            resolved = _resolve_file_path(source_doc, file_part)
            resolved_norm = posixpath.normpath(resolved)

            if resolved_norm == target_norm:
                backlinks.append({
                    "source_file": source_doc,
                    "source_section": sec.get("title", ""),
                    "source_section_id": sec.get("id", ""),
                    "link": href,
                })

    # Deduplicate by (source_section_id, link)
    seen = set()
    unique = []
    for bl in backlinks:
        key = (bl["source_section_id"], bl["link"])
        if key not in seen:
            seen.add(key)
            unique.append(bl)

    # Group by source file for readability
    by_file: dict = {}
    for bl in unique:
        by_file.setdefault(bl["source_file"], []).append(bl)

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "target": doc_path,
            "backlink_count": len(unique),
            "source_file_count": len(by_file),
            "backlinks": unique,
        },
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
