"""Per-section freshness probe (v1.16.0).

Three buckets per section:

- ``fresh`` — section's stored ``content_hash`` matches the current
  byte-range read of its source file (and the file's stored ``file_hashes``
  entry matches the current full-file hash).
- ``edited_uncommitted`` — section's content_hash matches the index but
  the source file's full-file hash on disk differs from what was
  recorded in ``DocIndex.file_hashes`` (the file changed elsewhere even
  if this particular range is unaffected).
- ``stale_index`` — section's content_hash does NOT match the current
  byte-range read. The index is reading text the file no longer has.

Probe caches per-file lookups within a single search call to avoid
re-hashing for every result. The probe is constructed once per
``DocIndex.search`` and discarded afterward.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


class FreshnessProbe:
    """Per-section freshness check, scoped to one search call."""

    __slots__ = (
        "_store", "_owner", "_name", "_index", "_file_state", "_source_root",
        "_live_bytes",
    )

    def __init__(
        self, store, owner: str, name: str, index, source_root: Optional[str] = None
    ) -> None:
        self._store = store
        self._owner = owner
        self._name = name
        self._index = index
        # jdoc#71: when set, the probe reads the LIVE workspace files under
        # source_root instead of the cached raw-content mirror under the doc
        # index. Default (None) preserves the historical cached-mirror behavior
        # that every other consumer (get_doc_health, search freshness) relies on.
        self._source_root = source_root
        # Per-file cache of (full_file_hash, exists). Built lazily.
        self._file_state: dict[str, tuple[Optional[str], bool]] = {}
        # jdoc#74: live-source mode caches the per-file *preprocessed* bytes
        # (indexed representation domain) so every section in a file reuses one
        # read + one preprocess_content call. None = file missing/unreadable.
        self._live_bytes: dict[str, Optional[bytes]] = {}

    def _resolve_path(self, doc_path: str):
        """Resolve doc_path to the file to read: live source root or cached mirror."""
        if self._source_root:
            # Live-source mode: read the workspace file under source_root,
            # guarding against a doc_path that would escape the root.
            try:
                base = Path(self._source_root).resolve()
                candidate = (base / doc_path).resolve()
                if candidate != base and base not in candidate.parents:
                    return None
                return candidate
            except Exception:
                return None
        try:
            content_dir = self._store._content_dir(self._owner, self._name)
            return self._store._safe_content_path(content_dir, doc_path)
        except Exception:
            return None

    def _preprocessed_bytes(self, doc_path: str) -> Optional[bytes]:
        """Live-source mode: live file read + preprocess_content, in the indexed domain.

        jdoc#74: the index stores hashes and byte offsets over *preprocessed*
        content (``.json`` / ``.jsonc`` / ``.svg`` / ``.xml`` / ``.html`` /
        ``.mdx`` / ``.ipynb`` / ``.tscn`` / ``.tres`` are converted by
        ``preprocess_content`` before storage). Comparing those against raw
        workspace bytes false-flags transformed files as ``stale_index``. So in
        live mode we reproduce exactly what ``index_local`` fed to storage: read
        with ``encoding="utf-8", errors="replace", newline=""`` then
        ``preprocess_content``, and hash/slice the result encoded as UTF-8.
        """
        if doc_path in self._live_bytes:
            return self._live_bytes[doc_path]
        data: Optional[bytes] = None
        file_path = self._resolve_path(doc_path)
        if file_path and file_path.exists():
            try:
                with open(file_path, encoding="utf-8", errors="replace", newline="") as fh:
                    raw_text = fh.read()
                from ..parser import preprocess_content
                data = preprocess_content(raw_text, doc_path).encode("utf-8")
            except OSError:
                data = None
        self._live_bytes[doc_path] = data
        return data

    def _file_hash(self, doc_path: str) -> tuple[Optional[str], bool]:
        cached = self._file_state.get(doc_path)
        if cached is not None:
            return cached
        if self._source_root:
            data = self._preprocessed_bytes(doc_path)
            if data is None:
                self._file_state[doc_path] = (None, False)
            else:
                self._file_state[doc_path] = (hashlib.sha256(data).hexdigest(), True)
            return self._file_state[doc_path]
        file_path = self._resolve_path(doc_path)
        if not file_path or not file_path.exists():
            self._file_state[doc_path] = (None, False)
            return self._file_state[doc_path]
        try:
            data = file_path.read_bytes()
            full_hash = hashlib.sha256(data).hexdigest()
        except OSError:
            full_hash = None
        self._file_state[doc_path] = (full_hash, True)
        return self._file_state[doc_path]

    def annotate(self, sec: dict) -> str:
        """Compute the freshness bucket for one section dict.

        Mutates ``sec`` in place by setting ``sec["_freshness"] = bucket``,
        and returns the bucket string. Idempotent — calling twice is fine.
        """
        bucket = self._classify(sec)
        sec["_freshness"] = bucket
        return bucket

    def _classify(self, sec: dict) -> str:
        doc_path = sec.get("doc_path", "") or ""
        if not doc_path:
            return "fresh"

        full_hash, exists = self._file_hash(doc_path)
        if not exists:
            # File missing entirely — treat as stale_index.
            return "stale_index"

        stored_full_hash = (self._index.file_hashes or {}).get(doc_path)
        # When the file's full-file hash diverges from what was recorded at
        # index time, something changed even if this section's byte range
        # didn't. That's edited_uncommitted unless the section's own range
        # also doesn't hash.
        section_hash = sec.get("content_hash") or ""
        byte_start = int(sec.get("byte_start", 0) or 0)
        byte_end = int(sec.get("byte_end", 0) or 0)
        current_section_hash = self._byte_range_hash(doc_path, byte_start, byte_end)

        if section_hash and current_section_hash and section_hash != current_section_hash:
            return "stale_index"

        if stored_full_hash and full_hash and stored_full_hash != full_hash:
            return "edited_uncommitted"

        return "fresh"

    def _byte_range_hash(self, doc_path: str, byte_start: int, byte_end: int) -> str:
        if byte_end <= byte_start:
            return ""
        if self._source_root:
            # jdoc#74: slice the preprocessed bytes (indexed domain), since the
            # stored byte offsets were computed over preprocessed content.
            data = self._preprocessed_bytes(doc_path)
            if data is None:
                return ""
            return hashlib.sha256(data[byte_start:byte_end]).hexdigest()
        file_path = self._resolve_path(doc_path)
        if not file_path or not file_path.exists():
            return ""
        try:
            with open(file_path, "rb") as fh:
                fh.seek(byte_start)
                buf = fh.read(byte_end - byte_start)
        except OSError:
            return ""
        return hashlib.sha256(buf).hexdigest()

    def summary(self, sections: list) -> dict:
        """Aggregate counts across a result list. Side-effect-free."""
        counts = {"fresh": 0, "edited_uncommitted": 0, "stale_index": 0}
        for sec in sections:
            bucket = sec.get("_freshness")
            if bucket in counts:
                counts[bucket] += 1
        return counts
