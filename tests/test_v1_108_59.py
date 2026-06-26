"""Tests for v1.108.59: decision-context surfacing in impact analysis (F-15 #2).

`tools/decision_context.resolve_decision_context` mines decision-bearing commits
(revert/perf/refactor/rename/bugfix) from the git record and `get_blast_radius` /
`get_call_hierarchy` (include_impact=True) attach it behind an opt-in `include_decisions` flag.
Read-only; nothing is persisted.
"""

import subprocess
import sys

import pytest

from jcodemunch_mcp.tools.decision_context import resolve_decision_context
from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius
from jcodemunch_mcp.tools.get_call_hierarchy import get_call_hierarchy
from jcodemunch_mcp.tools.index_folder import index_folder


def _git(args, cwd):
    env_args = [
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
    ] + args
    subprocess.run(env_args, cwd=cwd, check=True, capture_output=True, text=True)


def _has_git():
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def _commit_all(src, message):
    _git(["add", "-A"], src)
    _git(["commit", "-m", message], src)


def _decision_repo(tmp_path):
    """A git repo whose focal file has a spread of decision-bearing commits."""
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()

    core = src / "core.py"
    caller = src / "caller.py"
    caller.write_text("from core import target\n\ndef run():\n    return target()\n")

    _git(["init"], src)

    core.write_text("def target():\n    return 1\n")
    _commit_all(src, "Add target and caller")

    core.write_text("def target():\n    return 2  # corrected\n")
    _commit_all(src, "fix: correct return value in target")

    core.write_text("def target():\n    return 2  # cached\n")
    _commit_all(src, "perf: cache target result")

    core.write_text("def target():\n    return 2\n")
    _commit_all(src, "refactor: simplify target body")

    core.write_text("def target():\n    return 2  # final\n")
    _commit_all(src, 'Revert "perf: cache target result"')

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store), str(src)


# ---------------------------------------------------------------------------
# resolve_decision_context — unit
# ---------------------------------------------------------------------------


def test_resolver_surfaces_decision_categories(tmp_path):
    _, _, src = _decision_repo(tmp_path)
    out = resolve_decision_context(src, ["core.py"])
    assert out["available"] is True
    assert out["surfaced_from"] == "git_history"
    cats = out["by_category"]
    assert cats.get("revert") == 1
    assert "perf" in cats and "refactor" in cats and "bugfix" in cats
    # The non-decision "Add target and caller" (feature) is excluded.
    assert "feature" not in cats
    assert out["volatility"] in ("moderate", "high")
    assert out["decision_count"] == 4
    assert "Surfaced read-only" in out["note"]
    # Each surfaced decision carries explainable evidence.
    rev = next(d for d in out["decisions"] if d["category"] == "revert")
    assert rev["sha"] and rev["date"] and rev["subject"]


def test_resolver_no_local_git_honest_hint():
    out = resolve_decision_context(None, ["x.py"])
    assert out["available"] is False
    assert out["reason"] == "no_local_git"
    assert "hint" in out


def test_resolver_not_a_git_repo_honest_hint(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = resolve_decision_context(str(plain), ["x.py"])
    assert out["available"] is False
    assert out["reason"] == "not_a_git_repo"


# ---------------------------------------------------------------------------
# get_blast_radius integration
# ---------------------------------------------------------------------------


def test_blast_radius_attaches_decisions_on_request(tmp_path):
    repo, store, _ = _decision_repo(tmp_path)
    res = get_blast_radius(repo, "target", storage_path=store, include_decisions=True)
    assert "decisions" in res
    d = res["decisions"]
    assert d["available"] is True
    assert d["by_category"].get("revert") == 1
    assert d["summary"]


def test_blast_radius_byte_identical_without_flag(tmp_path):
    repo, store, _ = _decision_repo(tmp_path)
    res = get_blast_radius(repo, "target", storage_path=store)
    assert "decisions" not in res


def test_blast_radius_decisions_cache_keyed(tmp_path):
    # The flag is part of the cache key: an off call after an on call must not
    # serve the decisions-bearing cached result.
    repo, store, _ = _decision_repo(tmp_path)
    on = get_blast_radius(repo, "target", storage_path=store, include_decisions=True)
    off = get_blast_radius(repo, "target", storage_path=store, include_decisions=False)
    assert "decisions" in on
    assert "decisions" not in off


# ---------------------------------------------------------------------------
# get_call_hierarchy (include_impact) integration
# ---------------------------------------------------------------------------


def test_impact_preview_attaches_decisions_on_request(tmp_path):
    repo, store, _ = _decision_repo(tmp_path)
    res = get_call_hierarchy(
        repo,
        "target",
        storage_path=store,
        include_impact=True,
        include_decisions=True,
    )
    assert "impact" in res
    assert "decisions" in res["impact"]
    assert res["impact"]["decisions"]["available"] is True
    assert res["impact"]["decisions"]["by_category"].get("revert") == 1


def test_impact_preview_byte_identical_without_flag(tmp_path):
    repo, store, _ = _decision_repo(tmp_path)
    res = get_call_hierarchy(
        repo,
        "target",
        storage_path=store,
        include_impact=True,
    )
    assert "impact" in res
    assert "decisions" not in res["impact"]
