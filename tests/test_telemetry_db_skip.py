"""Regression test for issue #276.

When `JCODEMUNCH_PERF_TELEMETRY=1` is set, `~/.code-index/telemetry.db` lives
alongside per-repo index files. Before the fix, `list_repos` would:

1. Match `telemetry.db` via the `*.db` glob.
2. Call `_connect()` on it, which auto-initialised the code-index schema
   (corrupting telemetry.db).
3. Read an empty `meta` table and return an entry with `repo=""`.
4. The empty `repo` propagated to `_get_bare_name_map`, where
   ``_, repo_name = "".split("/", 1)`` raised `ValueError`, aborting the
   cache build mid-way and breaking bare-name resolution for the rest of
   the session.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jcodemunch_mcp.storage import IndexStore
from jcodemunch_mcp.tools._utils import resolve_repo as resolve_bare_name
from jcodemunch_mcp.tools.index_folder import index_folder


def _make_telemetry_db(path: Path) -> None:
    """Create a telemetry.db with the schema token_tracker writes."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_calls (
                ts REAL, tool TEXT, dur_ms REAL, ok INTEGER, repo TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ranking_events (
                ts REAL, tool TEXT, repo TEXT, query_hash TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO tool_calls (ts, tool, dur_ms, ok, repo) VALUES (?, ?, ?, ?, ?)",
            (1.0, "search_symbols", 5.0, 1, "local/foo-deadbeef"),
        )
        conn.commit()
    finally:
        conn.close()


def test_list_repos_skips_telemetry_db(tmp_path):
    """telemetry.db must not appear as a phantom repo in list_repos."""
    store_path = tmp_path / "store"
    store_path.mkdir()
    _make_telemetry_db(store_path / "telemetry.db")

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
    index_folder(str(project), use_ai_summaries=False, storage_path=str(store_path))

    store = IndexStore(base_path=str(store_path))
    repos = store.list_repos()

    assert all(r["repo"] for r in repos), f"phantom empty repo entries: {repos}"
    assert not any("telemetry" in r["repo"] for r in repos)
    assert any(r.get("display_name") == "myproj" for r in repos)


def test_telemetry_db_schema_not_clobbered(tmp_path):
    """list_repos must NOT auto-initialise the code-index schema on telemetry.db."""
    store_path = tmp_path / "store"
    store_path.mkdir()
    telemetry = store_path / "telemetry.db"
    _make_telemetry_db(telemetry)

    store = IndexStore(base_path=str(store_path))
    store.list_repos()

    conn = sqlite3.connect(str(telemetry))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert "tool_calls" in tables
    assert "meta" not in tables, (
        "telemetry.db was vandalised — code-index schema bled in via _connect()"
    )
    assert "symbols" not in tables
    assert "files" not in tables


def test_bare_name_resolution_survives_telemetry_db(tmp_path):
    """The original symptom: bare-name `repo` lookups must not crash with
    'not enough values to unpack' when telemetry.db is present."""
    store_path = tmp_path / "store"
    store_path.mkdir()
    _make_telemetry_db(store_path / "telemetry.db")

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
    index_folder(str(project), use_ai_summaries=False, storage_path=str(store_path))

    owner, name = resolve_bare_name("myproj", storage_path=str(store_path))
    assert owner == "local"
    assert name.startswith("myproj-")


def test_list_repos_skips_org_savings_db(tmp_path):
    """The team-SKU org-rollup store (org_savings.db) must not surface as a
    phantom `local/org_savings` repo — it isn't a code index and could never be
    deleted, so it showed an un-removable sym-0 card in the console cockpit."""
    store_path = tmp_path / "store"
    store_path.mkdir()
    # A minimal org_savings.db (any schema; list_repos must skip it by name).
    conn = sqlite3.connect(str(store_path / "org_savings.db"))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS org_savings (org_id TEXT, seat_id TEXT, day TEXT)")
        conn.commit()
    finally:
        conn.close()

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "main.py").write_text("def hello(): return 1\n", encoding="utf-8")
    index_folder(str(project), use_ai_summaries=False, storage_path=str(store_path))

    repos = IndexStore(base_path=str(store_path)).list_repos()
    assert not any("org_savings" in (r.get("repo") or "") for r in repos), (
        f"org_savings.db leaked into list_repos: {repos}"
    )
    assert any(r.get("display_name") == "myproj" for r in repos)
