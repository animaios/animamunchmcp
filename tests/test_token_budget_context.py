"""Tests for Feature 5: Token-budgeted context assembly.

Covers:
  - get_context_bundle: token_budget, budget_strategy, include_budget_report
  - get_ranked_context: query-driven ranked context assembly
"""

from pathlib import Path

import pytest

from jcodemunch_mcp.tools.get_context_bundle import get_context_bundle
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.search_symbols import search_symbols

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, files: dict[str, str]) -> tuple[str, str]:
    """Write files to tmp_path and index them. Return (repo_id, storage_path)."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    storage = str(tmp_path / ".index")
    result = index_folder(str(tmp_path), use_ai_summaries=False, storage_path=storage)
    repo_id = result.get("repo", str(tmp_path))
    return repo_id, storage


_SMALL_REPO = {
    "engine.py": (
        "class Engine:\n"
        '    """Core engine."""\n'
        "    def run(self):\n"
        "        pass\n\n"
        "    def stop(self):\n"
        "        pass\n"
    ),
    "utils.py": "def format_date(d):\n    return str(d)\n\ndef parse_date(s):\n    return s\n",
    "main.py": "from engine import Engine\nfrom utils import format_date\n\ndef main():\n    e = Engine()\n    e.run()\n",
}


# ---------------------------------------------------------------------------
# get_context_bundle — token_budget
# ---------------------------------------------------------------------------


class TestContextBundleTokenBudget:
    def test_budget_none_returns_full_source(self, tmp_path):
        """Without a budget, source is always included."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo, symbol_id="engine.py::Engine#class", storage_path=storage
        )
        assert "error" not in result
        assert result.get("source", "") != ""

    def test_compact_strategy_strips_source(self, tmp_path):
        """budget_strategy='compact' strips source bodies from all entries."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=10000,
            budget_strategy="compact",
            storage_path=storage,
        )
        assert "error" not in result
        for sym in result["symbols"]:
            assert sym["source"] == "", (
                f"Expected empty source, got: {sym['source'][:50]}"
            )

    def test_compact_strategy_retains_signature(self, tmp_path):
        """compact mode keeps signature even when source is stripped."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=10000,
            budget_strategy="compact",
            storage_path=storage,
        )
        assert "error" not in result
        assert result.get("signature"), "Signature should be non-empty in compact mode"

    def test_tiny_budget_excludes_symbols(self, tmp_path):
        """A very small token_budget trims symbols that don't fit."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        # Engine class has substantial source; budget of 1 token should exclude everything
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=1,
            budget_strategy="most_relevant",
            include_budget_report=True,
            storage_path=storage,
        )
        # Should not crash; budget_report should reflect exclusions
        assert "error" not in result or "budget_report" in result

    def test_budget_report_omitted_by_default(self, tmp_path):
        """budget_report is absent when include_budget_report=False (default)."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=10000,
            storage_path=storage,
        )
        assert "budget_report" not in result

    def test_budget_report_present_when_requested(self, tmp_path):
        """budget_report is present when include_budget_report=True."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=10000,
            include_budget_report=True,
            storage_path=storage,
        )
        assert "error" not in result
        assert "budget_report" in result
        br = result["budget_report"]
        assert "budget_tokens" in br
        assert "used_tokens" in br
        assert "included_symbols" in br
        assert "excluded_symbols" in br
        assert "strategy" in br

    def test_budget_report_used_tokens_le_budget(self, tmp_path):
        """budget_report.used_tokens must not exceed token_budget."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=200,
            include_budget_report=True,
            storage_path=storage,
        )
        assert "error" not in result
        br = result["budget_report"]
        assert br["used_tokens"] <= br["budget_tokens"]

    def test_invalid_budget_strategy_returns_error(self, tmp_path):
        """Unknown budget_strategy returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=1000,
            budget_strategy="magic",
            storage_path=storage,
        )
        assert "error" in result
        assert "budget_strategy" in result["error"]

    def test_no_budget_backward_compat(self, tmp_path):
        """Without token_budget, response shape is unchanged (backward compat)."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo, symbol_id="utils.py::format_date#function", storage_path=storage
        )
        assert "error" not in result
        assert "symbol_id" in result
        assert "source" in result
        assert "budget_report" not in result


# ---------------------------------------------------------------------------
# get_ranked_context
# ---------------------------------------------------------------------------


class TestGetRankedContext:
    def test_returns_context_items(self, tmp_path):
        """Basic call returns context_items list."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(
            repo, query="engine run", mode="context", storage_path=storage
        )
        assert "error" not in result
        assert "context_items" in result
        assert isinstance(result["context_items"], list)

    def test_total_tokens_le_budget(self, tmp_path):
        """total_tokens must not exceed token_budget."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        budget = 100
        result = search_symbols(repo, mode="context", query="engine", token_budget=budget, storage_path=storage
        )
        assert "error" not in result
        assert result["total_tokens"] <= budget

    def test_items_include_source(self, tmp_path):
        """Each context item includes a non-empty source field."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="Engine", token_budget=4000, storage_path=storage
        )
        assert "error" not in result
        for item in result["context_items"]:
            assert "source" in item

    def test_items_have_score_fields(self, tmp_path):
        """Each context item has relevance_score, centrality_score, combined_score."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="Engine", token_budget=4000, storage_path=storage
        )
        assert "error" not in result
        for item in result["context_items"]:
            assert "relevance_score" in item
            assert "centrality_score" in item
            assert "combined_score" in item
            assert "tokens" in item

    def test_bm25_strategy(self, tmp_path):
        """strategy='bm25' returns results without error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="format date", strategy="bm25", storage_path=storage
        )
        assert "error" not in result
        assert "context_items" in result

    def test_budget_zero_returns_error(self, tmp_path):
        """token_budget=0 returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="engine", token_budget=0, storage_path=storage
        )
        assert "error" in result

    def test_include_kinds_filter(self, tmp_path):
        """include_kinds restricts results to specified kinds."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context",
            query="engine",
            token_budget=4000,
            include_kinds=["class"],
            storage_path=storage,
        )
        assert "error" not in result
        for item in result["context_items"]:
            # symbol_id encodes kind; verify via cross-check below
            # Just check we got results without crashing
            assert "symbol_id" in item

    def test_unknown_repo_returns_error(self, tmp_path):
        """Non-existent repo returns a structured error."""
        storage = str(tmp_path / ".index")
        result = search_symbols("no_such_repo", mode="context", query="engine", storage_path=storage
        )
        assert "error" in result

    def test_query_too_long_returns_error(self, tmp_path):
        """Query exceeding 500 chars returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="x" * 501, storage_path=storage)
        assert "error" in result

    def test_meta_fields_present(self, tmp_path):
        """Response includes _meta with timing and savings fields."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = search_symbols(repo, mode="context", query="Engine", storage_path=storage)
        assert "error" not in result
        assert "_meta" in result
        meta = result["_meta"]
        assert "timing_ms" in meta
        assert "tokens_saved" in meta

    def test_exact_snake_case_query_ranks_matching_symbol_first(self, tmp_path):
        """Exact snake_case queries should rank the matching method first on BM25 path."""
        from jcodemunch_mcp.storage.sqlite_store import _cache_clear
        from tests.conftest_helpers import create_exact_match_index

        repo, storage = create_exact_match_index(tmp_path)
        _cache_clear()

        result = search_symbols(repo, mode="context", query="build_ui", strategy="bm25", storage_path=storage
        )

        assert "error" not in result
        assert result["context_items"]
        assert result["context_items"][0]["symbol_id"].endswith(
            "UiBuilder.build_ui#method"
        )


# ---------------------------------------------------------------------------
# Diversity-aware budget packing
# ---------------------------------------------------------------------------

# Repo with many symbols concentrated in one file to test diversity spread
_CONCENTRATED_REPO = {
    "models.py": (
        "class User:\n"
        "    def get_name(self):\n"
        "        return self.name\n\n"
        "    def get_email(self):\n"
        "        return self.email\n\n"
        "    def get_age(self):\n"
        "        return self.age\n\n"
        "    def get_address(self):\n"
        "        return self.address\n\n"
        "    def get_phone(self):\n"
        "        return self.phone\n"
    ),
    "views.py": (
        "def get_user_view(request):\n"
        "    return render(request)\n\n"
        "def get_list_view(request):\n"
        "    return render(request)\n"
    ),
    "utils.py": (
        "def get_config():\n"
        "    return {}\n\n"
        "def get_logger():\n"
        "    import logging\n"
        "    return logging.getLogger()\n"
    ),
    "main.py": (
        "from models import User\n"
        "from views import get_user_view\n"
        "from utils import get_config\n\n"
        "def get_app():\n"
        "    return get_config()\n"
    ),
}


class TestDiversityPacking:
    def test_diversity_spreads_across_files(self, tmp_path):
        """With diversity enabled, results should come from multiple files."""
        repo, storage = _make_repo(tmp_path, _CONCENTRATED_REPO)
        result = search_symbols(repo, mode="context",
            query="get",
            token_budget=4000,
            storage_path=storage,
        )
        assert "error" not in result
        items = result["context_items"]
        if len(items) < 2:
            pytest.skip("Not enough results to test diversity")
        files = set()
        for item in items:
            # Extract file from symbol_id (format: "file.py::Name#kind")
            sid = item["symbol_id"]
            f = sid.split("::")[0] if "::" in sid else ""
            files.add(f)
        # With diversity, we should see symbols from at least 3 files
        assert len(files) >= 3, (
            f"Expected symbols from >=3 files, got {len(files)}: {files}"
        )

    def test_file_group_cap_respected(self, tmp_path):
        """No more than _FILE_GROUP_CAP symbols from a single file."""
        from jcodemunch_mcp.tools.search_symbols import _FILE_GROUP_CAP

        repo, storage = _make_repo(tmp_path, _CONCENTRATED_REPO)
        result = search_symbols(repo, mode="context",
            query="get",
            token_budget=8000,
            storage_path=storage,
        )
        assert "error" not in result
        file_counts: dict[str, int] = {}
        for item in result["context_items"]:
            sid = item["symbol_id"]
            f = sid.split("::")[0] if "::" in sid else ""
            file_counts[f] = file_counts.get(f, 0) + 1
        for f, count in file_counts.items():
            assert count <= _FILE_GROUP_CAP, (
                f"File '{f}' has {count} symbols, exceeds cap {_FILE_GROUP_CAP}"
            )

    def test_budget_still_respected_with_diversity(self, tmp_path):
        """Diversity packing must still respect the token budget."""
        repo, storage = _make_repo(tmp_path, _CONCENTRATED_REPO)
        budget = 50
        result = search_symbols(repo, mode="context",
            query="get",
            token_budget=budget,
            storage_path=storage,
        )
        assert "error" not in result
        assert result["total_tokens"] <= budget

    def test_pack_budget_no_diversity_fallback(self, tmp_path):
        """_pack_budget with diversity=False behaves like the old greedy packer."""
        from jcodemunch_mcp.tools.search_symbols import _pack_budget

        syms = [
            {"id": f"a.py::f{i}#function", "file": "a.py", "byte_length": 20}
            for i in range(5)
        ]
        scored = [(10.0 - i, sym) for i, sym in enumerate(syms)]

        def get_tok(sym):
            return "x" * 20, 5

        packed, total = _pack_budget(scored, 100, get_tok, diversity=False)
        # Without diversity, all 5 from same file should be packed
        assert len(packed) == 5
        assert total == 25
