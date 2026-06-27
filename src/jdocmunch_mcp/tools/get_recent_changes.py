"""Surface sections drifting from index state (v1.47.0).

The v1.16 FreshnessProbe classifies every section into ``fresh``,
``edited_uncommitted``, or ``stale_index`` buckets. `get_doc_health`
exposes the *counts*. `get_stale_pages` lists pages whose source has
diverged. Neither returns the actual section list of recently-edited
sections without re-running search_sections.

This tool walks every section through FreshnessProbe and returns the
ones in the non-fresh buckets — a pre-flight check before deciding
whether to re-index.

**Which layer this checks (jdoc#71).** By default the probe compares the
index against the cached raw-content *mirror* stored under the doc index,
NOT the live workspace files. So an empty result means "the stored mirror
and the index agree", which is not the same as "unrefreshed workspace files
match the index". After editing live docs, run ``index_local`` /
``index-file`` first. Pass ``live_source=True`` to instead read the live
workspace files under the index's ``source_root`` (when one is recorded);
``_meta.drift_layer`` reports which layer actually ran. This is also distinct
from Git-head certification: ``head_sha`` / ``source_dirty`` /
``sha_certified`` on search responses describe whether the index is pinned to
a committed SHA, and can lag content freshness (e.g. after a refresh-then-
commit a no-op post-commit refresh is needed to re-certify).
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..retrieval.freshness import FreshnessProbe
from ..storage import DocStore


def get_recent_changes(
    repo: str,
    include_stale: bool = True,
    include_edited: bool = True,
    live_source: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Return sections that have drifted from index state.

    By default this compares the index against the cached raw-content mirror,
    not the live workspace (see module docstring / ``live_source``).

    Args:
        repo: Repository identifier.
        include_stale: Surface sections in ``stale_index`` bucket
            (this section's byte range no longer hashes the same).
            Default True.
        include_edited: Surface sections in ``edited_uncommitted``
            bucket (file's full-file hash diverged but this section's
            range still matches). Default True.
        live_source: jdoc#71 — when True, read the LIVE workspace files
            under the index's ``source_root`` instead of the cached mirror.
            Falls back to the cached mirror (with ``_meta.drift_layer ==
            "cached_mirror"`` and ``live_source_available == False``) when the
            index has no usable ``source_root``. Default False (cached mirror).
        storage_path: Custom storage path.

    Returns:
        ``{repo, changes: [{id, title, doc_path, level, freshness},
        ...], change_count, by_bucket: {edited_uncommitted: N,
        stale_index: M}, total_sections, _meta}``. Sorted by
        ``(doc_path, byte_start)`` for stable output. ``_meta.drift_layer``
        is ``"live_source"`` or ``"cached_mirror"`` depending on which layer
        was actually read.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    # jdoc#71: opt into live-workspace drift only when a usable source_root is
    # recorded on the index; otherwise fall back to the cached-mirror probe and
    # report that the live layer was unavailable.
    source_root = getattr(index, "source_root", "") or ""
    live_source_available = bool(source_root) and os.path.isdir(source_root)
    use_live = bool(live_source) and live_source_available
    drift_layer = "live_source" if use_live else "cached_mirror"

    probe = FreshnessProbe(
        store, owner, name, index,
        source_root=source_root if use_live else None,
    )
    by_bucket = {"edited_uncommitted": 0, "stale_index": 0}
    changes: list = []
    for sec in index.sections:
        bucket = probe.annotate(dict(sec))  # don't mutate the source dict
        if bucket == "fresh":
            continue
        if bucket == "stale_index" and not include_stale:
            continue
        if bucket == "edited_uncommitted" and not include_edited:
            continue
        if bucket in by_bucket:
            by_bucket[bucket] += 1
        # Skip synthetic level-0 doc-roots (parser artifact).
        if sec.get("level") == 0:
            continue
        changes.append({
            "id": sec.get("id"),
            "title": sec.get("title"),
            "doc_path": sec.get("doc_path"),
            "level": sec.get("level"),
            "freshness": bucket,
        })

    changes.sort(key=lambda r: (r.get("doc_path", ""), r.get("id", "")))

    return {
        "repo": f"{owner}/{name}",
        "changes": changes,
        "change_count": len(changes),
        "by_bucket": by_bucket,
        "total_sections": len(index.sections),
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "include_stale": include_stale,
            "include_edited": include_edited,
            "indexed_at": index.indexed_at,
            # jdoc#71: name the layer this drift check actually read, so an
            # empty result isn't mistaken for "live workspace files are current".
            "drift_layer": drift_layer,
            "live_source_requested": bool(live_source),
            "live_source_available": live_source_available,
            "source_root": source_root,
        },
    }
