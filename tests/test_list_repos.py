"""Tests for repos_report (the cockpit view powering `list-repos --json`)."""

from __future__ import annotations

from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.list_repos import repos_report


def test_repos_report_shape(tmp_path):
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    r = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert r["success"] is True

    report = repos_report(storage_path=str(store))
    assert isinstance(report, list) and report
    entry = report[0]
    assert {
        "repo_id",
        "display_name",
        "source_root",
        "file_count",
        "symbol_count",
        "languages",
        "indexed_at",
        "freshness",
    } <= entry.keys()
    assert entry["source_root"]
    assert entry["symbol_count"] >= 1
    assert entry["file_count"] >= 1
    assert isinstance(entry["languages"], dict)
    assert entry["freshness"] in ("fresh", "edited_uncommitted", "stale_index")
