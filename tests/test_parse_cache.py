"""Tests for the content-addressed parse cache (shared-host indexing)."""

from __future__ import annotations

import dataclasses

import pytest

from jcodemunch_mcp.parser import parse_file
from jcodemunch_mcp.parser.parse_cache import (
    cached_parse_file,
    cache_dir,
    _connect,
    _evict_oldest,
    _key,
    _max_rows,
    DEFAULT_MAX_ROWS,
)

PY = "def alpha(x):\n    return x + 1\n\nclass Beta:\n    def gamma(self):\n        return 2\n"


def test_disabled_is_passthrough(monkeypatch):
    monkeypatch.delenv("JCODEMUNCH_PARSE_CACHE", raising=False)
    assert cache_dir() is None
    direct = parse_file(PY, "m.py", "python")
    via = cached_parse_file(PY, "m.py", "python")
    assert [dataclasses.asdict(s) for s in via] == [dataclasses.asdict(s) for s in direct]


def test_hit_is_identical_to_fresh_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE", str(tmp_path))
    fresh = [dataclasses.asdict(s) for s in parse_file(PY, "m.py", "python")]
    miss = [dataclasses.asdict(s) for s in cached_parse_file(PY, "m.py", "python")]  # populates
    hit = [dataclasses.asdict(s) for s in cached_parse_file(PY, "m.py", "python")]   # from cache
    assert miss == fresh
    assert hit == fresh


def test_second_call_reads_cache_not_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE", str(tmp_path))
    cached_parse_file(PY, "m.py", "python")  # populate
    import jcodemunch_mcp.parser.extractor as extractor
    calls = {"n": 0}
    real = extractor.parse_file

    def _counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(extractor, "parse_file", _counting)
    out = cached_parse_file(PY, "m.py", "python")
    assert calls["n"] == 0  # served from cache, parser not invoked
    assert out and out[0].name == "alpha"


def test_key_varies_by_content_path_language_version(tmp_path, monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE", str(tmp_path))
    k1 = _key(PY, "m.py", "python")
    assert k1 != _key(PY + "\n", "m.py", "python")   # content
    assert k1 != _key(PY, "other.py", "python")       # path (symbol ids embed path)
    assert k1.startswith("v")                          # index-version namespaced


def test_corrupt_row_falls_back_to_parse(tmp_path, monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE", str(tmp_path))
    conn = _connect(str(tmp_path))
    conn.execute(
        "INSERT OR REPLACE INTO parse_cache (key, symbols, created_at) VALUES (?, ?, ?)",
        (_key(PY, "m.py", "python"), "{not valid json", "now"),
    )
    conn.commit()
    conn.close()
    # Must not raise — falls back to a live parse.
    out = cached_parse_file(PY, "m.py", "python")
    assert any(s.name == "alpha" for s in out)


def _rowcount(d: str) -> int:
    conn = _connect(d)
    try:
        return conn.execute("SELECT COUNT(*) FROM parse_cache").fetchone()[0]
    finally:
        conn.close()


def _seed(d: str, n: int) -> None:
    conn = _connect(d)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO parse_cache (key, symbols, created_at) VALUES (?, '[]', 'now')",
            [(f"k{i}",) for i in range(n)],
        )
        conn.commit()
    finally:
        conn.close()


def test_max_rows_default_and_override(monkeypatch):
    monkeypatch.delenv("JCODEMUNCH_PARSE_CACHE_MAX_ROWS", raising=False)
    assert _max_rows() == DEFAULT_MAX_ROWS
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE_MAX_ROWS", "10")
    assert _max_rows() == 10
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE_MAX_ROWS", "not-a-number")
    assert _max_rows() == DEFAULT_MAX_ROWS  # bad value → default, never crashes


def test_evict_trims_to_cap_oldest_first(tmp_path):
    d = str(tmp_path)
    _seed(d, 10)  # k0..k9, rowid ascending
    conn = _connect(d)
    try:
        deleted = _evict_oldest(conn, 4)
        conn.commit()
        remaining = {r[0] for r in conn.execute("SELECT key FROM parse_cache").fetchall()}
    finally:
        conn.close()
    assert deleted == 6
    assert remaining == {"k6", "k7", "k8", "k9"}  # oldest six gone, newest four kept


def test_evict_noop_under_cap_and_when_disabled(tmp_path):
    d = str(tmp_path)
    _seed(d, 3)
    conn = _connect(d)
    try:
        assert _evict_oldest(conn, 5) == 0   # under cap
        assert _evict_oldest(conn, 0) == 0   # 0 disables the cap
        assert _evict_oldest(conn, -1) == 0  # negative disables the cap
    finally:
        conn.close()
    assert _rowcount(d) == 3


def test_write_path_enforces_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE", str(tmp_path))
    monkeypatch.setenv("JCODEMUNCH_PARSE_CACHE_MAX_ROWS", "5")
    # Each distinct file content is a fresh key; writing >5 must self-trim.
    for i in range(12):
        cached_parse_file(PY + f"\n# {i}\n", f"m{i}.py", "python")
    assert _rowcount(str(tmp_path)) <= 5
