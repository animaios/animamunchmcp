"""Detect wiki pages whose source documents have changed since last index."""

import os
import re
import time
from typing import Optional

import yaml

from ..storage.doc_store import DocStore

_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n?", re.DOTALL)


def _extract_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content. Returns empty dict if none."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _file_hash(path: str) -> Optional[str]:
    """SHA-256 of a file's content, or None if unreadable."""
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, IOError):
        return None


def get_stale_pages(
    repo: str,
    sources_dir: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Find wiki pages whose declared sources have been modified.

    Convention: wiki pages include a YAML frontmatter block with a `sources`
    key listing relative paths to raw source files:

        ---
        sources:
          - raw/article-one.md
          - raw/paper.pdf
        ---

    This tool reads each indexed page's raw content from the store, parses
    frontmatter, and checks whether source files have changed on disk.
    A page is stale if any declared source:
      - has a different hash than when the page was last indexed
      - no longer exists on disk (flagged as missing)

    Args:
        repo: Repository identifier
        sources_dir: Base directory to resolve relative source paths.
                     If omitted, uses the index's source_root.
        storage_path: Override for DOC_INDEX_PATH
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    # Determine base dir for resolving source paths
    base_dir = sources_dir
    if not base_dir:
        # Try to get source root from the index metadata
        raw = store._index_path(owner, name)
        if raw.exists():
            import json
            with open(raw, "r", encoding="utf-8") as f:
                idx_data = json.load(f)
            base_dir = idx_data.get("source_root", "")
    if not base_dir:
        return {"error": "Cannot determine sources_dir. Pass it explicitly or ensure index has source_root."}

    # Collect frontmatter sources from each doc
    stale_pages: list = []
    pages_with_sources = 0
    total_sources_checked = 0

    seen_docs = set()
    for sec in index.sections:
        doc_path = sec.get("doc_path", "")
        if doc_path in seen_docs:
            continue
        seen_docs.add(doc_path)

        # Read raw content from store
        content = store.get_section_content(owner, name, sec["id"], _index=index)
        if not content:
            # Try reading the full file from content cache
            content_dir = store._content_dir(owner, name)
            safe_path = store._safe_content_path(content_dir, doc_path)
            if safe_path and safe_path.exists():
                content = safe_path.read_text(encoding="utf-8", errors="replace")
            else:
                continue

        # For root section (level 0), content has the full file start
        # But we need the raw file to get frontmatter
        # Read from the content cache directly
        content_dir = store._content_dir(owner, name)
        safe_path = store._safe_content_path(content_dir, doc_path)
        if not safe_path or not safe_path.exists():
            continue

        raw_content = safe_path.read_text(encoding="utf-8", errors="replace")
        fm = _extract_frontmatter(raw_content)
        sources = fm.get("sources", [])

        if not sources or not isinstance(sources, list):
            continue

        pages_with_sources += 1
        stale_sources: list = []

        for src_path in sources:
            if not isinstance(src_path, str):
                continue
            total_sources_checked += 1
            abs_path = os.path.normpath(os.path.join(base_dir, src_path))

            if not os.path.exists(abs_path):
                stale_sources.append({
                    "source": src_path,
                    "reason": "missing",
                })
                continue

            current_hash = _file_hash(abs_path)
            # Check against stored file hash if available
            stored_hash = index.file_hashes.get(src_path)
            if stored_hash and current_hash and current_hash != stored_hash:
                stale_sources.append({
                    "source": src_path,
                    "reason": "modified",
                })
            elif not stored_hash:
                # Source isn't tracked in the index — can't determine staleness
                # but we note it for awareness
                stale_sources.append({
                    "source": src_path,
                    "reason": "untracked",
                })

        if stale_sources:
            stale_pages.append({
                "doc_path": doc_path,
                "title": fm.get("title", doc_path),
                "stale_sources": stale_sources,
            })

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "pages_scanned": len(seen_docs),
            "pages_with_sources": pages_with_sources,
            "sources_checked": total_sources_checked,
            "stale_page_count": len(stale_pages),
            "stale_pages": stale_pages,
        },
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }
