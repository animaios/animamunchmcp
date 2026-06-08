"""Content-addressed parse cache for shared-host deployments.

When N home directories on one machine index overlapping repos, each runs an
independent tree-sitter parse of identical files. This cache stores parse output
keyed by (index_version, content_hash, language, filename) in a **shared**
SQLite store, so an identical file parses once regardless of which indexer asked
— turning Nx parsing into 1x + (N-1) lookups.

Opt-in: disabled unless ``JCODEMUNCH_PARSE_CACHE`` points at a shared directory
(all seats on the box set it to the same path). When unset, ``cached_parse_file``
is a thin pass-through to ``parse_file`` — zero behavior change.

Correctness:
* The key includes ``content_hash`` + ``language`` + ``filename`` (symbol ids
  embed the path), so a hit is byte-identical to a fresh parse.
* The key includes ``INDEX_VERSION``, so a parser/schema bump invalidates the
  cache (the index-version cache-key rule).
* Any read/deserialize failure falls back to a live parse — the cache can never
  produce wrong symbols, only a slower miss.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from .symbols import Symbol

logger = logging.getLogger(__name__)


def cache_dir() -> Optional[str]:
    """Shared parse-cache directory, or None when the cache is disabled."""
    d = (os.environ.get("JCODEMUNCH_PARSE_CACHE") or "").strip()
    return d or None


def _index_version() -> int:
    # Lazy import: parser must not depend on storage at import time.
    try:
        from ..storage.index_store import INDEX_VERSION
        return int(INDEX_VERSION)
    except Exception:
        return 0


def _connect(d: str) -> sqlite3.Connection:
    p = Path(d) / "parse_cache.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS parse_cache "
        "(key TEXT PRIMARY KEY, symbols TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def _key(content: str, filename: str, language: str) -> str:
    h = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
    return f"v{_index_version()}:{language}:{h}:{filename}"


def cached_parse_file(
    content: str,
    filename: str,
    language: str,
    source_bytes: Optional[bytes] = None,
    repo: Optional[str] = None,
) -> list[Symbol]:
    """Drop-in for ``parse_file`` that consults the shared parse cache when
    enabled. Transparent pass-through when ``JCODEMUNCH_PARSE_CACHE`` is unset."""
    from .extractor import parse_file  # lazy: avoid import cycle

    d = cache_dir()
    if not d:
        return parse_file(content, filename, language, source_bytes=source_bytes, repo=repo)

    key = _key(content, filename, language)

    # Read
    try:
        conn = _connect(d)
        try:
            row = conn.execute("SELECT symbols FROM parse_cache WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        if row:
            try:
                return [Symbol(**sd) for sd in json.loads(row[0])]
            except (ValueError, TypeError) as exc:
                logger.debug("parse cache deserialize failed for %s (%s); reparsing", filename, exc)
    except sqlite3.Error as exc:
        logger.debug("parse cache read failed (%s); reparsing", exc)
        return parse_file(content, filename, language, source_bytes=source_bytes, repo=repo)

    # Miss → parse, then store (best-effort)
    symbols = parse_file(content, filename, language, source_bytes=source_bytes, repo=repo)
    try:
        payload = json.dumps([dataclasses.asdict(s) for s in symbols], separators=(",", ":"))
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = _connect(d)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO parse_cache (key, symbols, created_at) VALUES (?, ?, ?)",
                (key, payload, now),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.debug("parse cache write failed for %s (%s)", filename, exc)
    return symbols
