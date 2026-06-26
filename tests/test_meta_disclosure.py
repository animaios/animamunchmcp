"""Tests for T15: _meta.methodology + _meta.confidence_level on all 6 analytical tools.

All tests use the small_index / medium_index / hierarchy_index fixtures from conftest.py.
Tools that require git or a local repo (get_churn_rate, get_hotspots) are tested for
_meta field presence; git-specific results are not asserted.
"""

import pytest

from jcodemunch_mcp.tools.get_call_hierarchy import get_call_hierarchy
from jcodemunch_mcp.tools.get_repo_health import get_repo_health
from jcodemunch_mcp.tools.get_symbol_complexity import get_symbol_complexity
from jcodemunch_mcp.tools.search_symbols import search_symbols


def _first_function_id(repo, store):
    """Return the symbol ID of the first function in the index."""
    r = search_symbols(
        repo=repo,
        query="add",
        max_results=1,
        detail_level="compact",
        storage_path=store,
    )
    if r.get("results"):
        return r["results"][0]["id"]
    return None


_VALID_CONFIDENCE = {"low", "medium", "high"}


class TestGetCallHierarchyMeta:
    def test_methodology_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_call_hierarchy(repo=repo, symbol_id=sid, storage_path=store)
        assert "_meta" in r
        assert "methodology" in r["_meta"]
        # small_index has no function calls, so falls back to text_heuristic
        assert r["_meta"]["methodology"] == "text_heuristic"

    def test_confidence_level_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_call_hierarchy(repo=repo, symbol_id=sid, storage_path=store)
        assert "confidence_level" in r["_meta"]
        assert r["_meta"]["confidence_level"] in _VALID_CONFIDENCE

    def test_confidence_level_is_low(self, small_index):
        """Call hierarchy with no call data falls back to text heuristic — low confidence."""
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_call_hierarchy(repo=repo, symbol_id=sid, storage_path=store)
        assert r["_meta"]["confidence_level"] == "low"


class TestGetImpactPreviewMeta:
    def test_methodology_present(self, medium_index):
        repo, store = medium_index["repo"], medium_index["store"]
        r = search_symbols(
            repo=repo,
            query="get_user",
            max_results=1,
            detail_level="compact",
            storage_path=store,
        )
        if not r.get("results"):
            pytest.skip("no function in index")
        sid = r["results"][0]["id"]
        result = get_call_hierarchy(
            repo=repo, symbol_id=sid, storage_path=store, include_impact=True
        )
        assert "_meta" in result["impact"]
        assert "methodology" in result["impact"]["_meta"]
        assert result["_meta"]["methodology"] == "ast_call_references"

    def test_confidence_level_present(self, medium_index):
        repo, store = medium_index["repo"], medium_index["store"]
        r = search_symbols(
            repo=repo,
            query="get_user",
            max_results=1,
            detail_level="compact",
            storage_path=store,
        )
        if not r.get("results"):
            pytest.skip("no function in index")
        sid = r["results"][0]["id"]
        result = get_call_hierarchy(
            repo=repo, symbol_id=sid, storage_path=store, include_impact=True
        )
        assert result["impact"]["_meta"]["confidence_level"] in _VALID_CONFIDENCE

    def test_confidence_level_is_medium(self, medium_index):
        """Impact preview uses AST call references — medium confidence."""
        repo, store = medium_index["repo"], medium_index["store"]
        r = search_symbols(
            repo=repo,
            query="get_user",
            max_results=1,
            detail_level="compact",
            storage_path=store,
        )
        if not r.get("results"):
            pytest.skip("no function in index")
        sid = r["results"][0]["id"]
        result = get_call_hierarchy(
            repo=repo, symbol_id=sid, storage_path=store, include_impact=True
        )
        assert result["impact"]["_meta"]["confidence_level"] == "medium"


class TestGetSymbolComplexityMeta:
    def test_methodology_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert "_meta" in r
        assert "methodology" in r["_meta"]
        assert r["_meta"]["methodology"] == "stored_metrics"

    def test_confidence_level_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert r["_meta"]["confidence_level"] in _VALID_CONFIDENCE

    def test_confidence_level_is_medium(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        sid = _first_function_id(repo, store)
        if sid is None:
            pytest.skip("no function in index")
        r = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert r["_meta"]["confidence_level"] == "medium"


class TestGetRepoHealthChurnMeta:
    def test_churn_details_methodology(self, small_index):
        """get_repo_health(detailed=True, file_path=...) with churn details includes methodology."""
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(
            repo=repo, detailed=True, file_path="utils.py", storage_path=store
        )
        # churn is only available when a git repo is present; skip if missing
        if "details" not in r or "churn" not in r.get("details", {}):
            pytest.skip("get_repo_health churn requires git; skipping")
        churn = r["details"]["churn"]
        if "error" in churn:
            pytest.skip(f"churn sub-tool error: {churn['error']}")
        assert "_meta" in churn
        assert churn["_meta"].get("methodology") == "git_log"
        assert churn["_meta"].get("confidence_level") == "high"


class TestGetRepoHealthHotspotsMeta:
    def test_methodology_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert "_meta" in r
        assert "methodology" in r["_meta"]
        assert r["_meta"]["methodology"] == "aggregate"

    def test_confidence_level_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert r["_meta"]["confidence_level"] in _VALID_CONFIDENCE

    def test_confidence_level_is_medium(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert r["_meta"]["confidence_level"] == "medium"


class TestGetRepoHealthMeta:
    def test_methodology_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert "_meta" in r
        assert "methodology" in r["_meta"]
        assert r["_meta"]["methodology"] == "aggregate"

    def test_confidence_level_present(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert r["_meta"]["confidence_level"] in _VALID_CONFIDENCE

    def test_confidence_level_is_medium(self, small_index):
        repo, store = small_index["repo"], small_index["store"]
        r = get_repo_health(repo=repo, storage_path=store)
        assert r["_meta"]["confidence_level"] == "medium"
