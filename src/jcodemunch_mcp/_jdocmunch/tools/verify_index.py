"""verify_index — byte-offset integrity check (v1.27.0).

Walks every section in an indexed repo, byte-range-reads its current
on-disk content, recomputes SHA-256, and compares to the stored
``content_hash``. Surfaces drift loud and early — the kind of silent
corruption that motivated B1 / B2 in the v1.10 audit.

Output:

    {
        repo, section_count,
        clean_count, drift_count, missing_count, error_count, skipped_count,
        drift_sections:[{section_id, doc_path, expected_hash, actual_hash}],
        missing_sections:[{section_id, doc_path, reason}],
        skipped_sections:[{section_id, doc_path, reason}],
        _meta: {latency_ms}
    }

Reasons for ``missing``:
  - "no_doc_path" (section persisted without a doc_path)
  - "file_missing" (cached raw file not on disk)

Reasons for ``skipped`` (jdoc#33 — unverifiable by design, distinct from
corruption; e.g. the structured OpenAPI parser persists every section with
``byte_start=0, byte_end=0``):
  - "empty_byte_range" (byte_end <= byte_start)

Invariant: clean_count + drift_count + missing_count + error_count +
skipped_count == section_count, so every section is accounted for.

Designed to be cheap enough for CI: O(N) where N is section count, with
one file read per distinct doc_path (cached within the call).
"""

from __future__ import annotations

import hashlib
import time
from typing import Optional

from ..storage import DocStore


def verify_index(
    repo: str,
    storage_path: Optional[str] = None,
    sample: Optional[int] = None,
) -> dict:
    """Verify every section's stored hash against its current byte range.

    Args:
        repo: jdocmunch repo identifier.
        storage_path: Override DOC_INDEX_PATH for tests.
        sample: When set, verify only the first N sections (cheap CI mode).
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    sections = index.sections
    if sample is not None and sample > 0:
        sections = sections[:sample]

    clean = 0
    drift: list[dict] = []
    missing: list[dict] = []
    skipped: list[dict] = []
    error_count = 0

    # Cache file bytes per doc_path so we hash each file at most once.
    file_cache: dict[str, Optional[bytes]] = {}
    content_dir = store._content_dir(owner, name)

    def _bytes_for(doc_path: str) -> Optional[bytes]:
        if doc_path in file_cache:
            return file_cache[doc_path]
        path = store._safe_content_path(content_dir, doc_path)
        if not path or not path.exists():
            file_cache[doc_path] = None
            return None
        try:
            file_cache[doc_path] = path.read_bytes()
        except OSError:
            file_cache[doc_path] = None
        return file_cache[doc_path]

    for sec in sections:
        sid = sec.get("id", "")
        doc_path = sec.get("doc_path", "")
        if not doc_path:
            missing.append({"section_id": sid, "doc_path": "", "reason": "no_doc_path"})
            continue

        byte_start = int(sec.get("byte_start", 0) or 0)
        byte_end = int(sec.get("byte_end", 0) or 0)
        expected_hash = sec.get("content_hash") or ""

        if byte_end <= byte_start:
            # Section persisted without a byte range — unverifiable by
            # design, not a drift. Tracked so the counters sum (jdoc#33).
            skipped.append(
                {"section_id": sid, "doc_path": doc_path, "reason": "empty_byte_range"}
            )
            continue

        data = _bytes_for(doc_path)
        if data is None:
            missing.append({"section_id": sid, "doc_path": doc_path, "reason": "file_missing"})
            continue

        try:
            chunk = data[byte_start:byte_end]
        except Exception:
            error_count += 1
            continue

        actual_hash = hashlib.sha256(chunk).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            drift.append(
                {
                    "section_id": sid,
                    "doc_path": doc_path,
                    "expected_hash": expected_hash,
                    "actual_hash": actual_hash,
                }
            )
        else:
            clean += 1

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "repo": f"{owner}/{name}",
        "section_count": len(sections),
        "clean_count": clean,
        "drift_count": len(drift),
        "missing_count": len(missing),
        "error_count": error_count,
        "skipped_count": len(skipped),
        "drift_sections": drift,
        "missing_sections": missing,
        "skipped_sections": skipped,
        "_meta": {
            "latency_ms": latency_ms,
            "files_read": sum(1 for v in file_cache.values() if v is not None),
            "sample": sample,
        },
    }
