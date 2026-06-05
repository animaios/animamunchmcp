"""Tests for the signal fusion pipeline (Weighted Reciprocal Rank)."""

import pytest

from jcodemunch_mcp.retrieval.signal_fusion import (
    ChannelResult,
    FusedResult,
    fuse,
    build_lexical_channel,
    build_identity_channel,
    build_structural_channel,
    build_similarity_channel,
    _bm25_score_no_identity,
    load_fusion_weights,
    DEFAULT_WEIGHTS,
    DEFAULT_SMOOTHING,
)


# ---------------------------------------------------------------------------
# Core WRR math
# ---------------------------------------------------------------------------

class TestFuse:
    """Test the core fuse() function."""

    def test_single_channel(self):
        ch = ChannelResult(name="lexical", ranked_ids=["a", "b", "c"])
        results = fuse([ch], smoothing=60)
        assert len(results) == 3
        # First result should have highest score
        assert results[0].symbol_id == "a"
        assert results[1].symbol_id == "b"
        assert results[2].symbol_id == "c"
        # Scores should be decreasing
        assert results[0].score > results[1].score > results[2].score

    def test_two_channels_agreement(self):
        """When both channels agree on ranking, the fusion should amplify."""
        ch1 = ChannelResult(name="lexical", ranked_ids=["a", "b", "c"])
        ch2 = ChannelResult(name="identity", ranked_ids=["a", "b", "c"])
        results = fuse([ch1, ch2], smoothing=60)
        assert results[0].symbol_id == "a"
        # "a" appears in both channels at rank 1
        assert "lexical" in results[0].channel_contributions
        assert "identity" in results[0].channel_contributions

    def test_two_channels_disagreement(self):
        """When channels disagree, fusion merges both perspectives."""
        ch1 = ChannelResult(name="lexical", ranked_ids=["a", "b"])
        ch2 = ChannelResult(name="identity", ranked_ids=["c", "a"])
        results = fuse([ch1, ch2], smoothing=60)
        # "a" appears in both → should rank highest
        ids = [r.symbol_id for r in results]
        assert ids[0] == "a"
        # "b" and "c" should both appear
        assert "b" in ids
        assert "c" in ids

    def test_empty_channels(self):
        results = fuse([], smoothing=60)
        assert results == []

    def test_empty_ranked_ids(self):
        ch = ChannelResult(name="lexical", ranked_ids=[])
        results = fuse([ch], smoothing=60)
        assert results == []

    def test_custom_weights(self):
        ch1 = ChannelResult(name="lexical", ranked_ids=["a"])
        ch2 = ChannelResult(name="identity", ranked_ids=["b"])
        # With lexical weight 10 and identity weight 1, "a" should win
        results = fuse([ch1, ch2], smoothing=60, weights={"lexical": 10.0, "identity": 1.0})
        assert results[0].symbol_id == "a"
        # Flip weights
        results2 = fuse([ch1, ch2], smoothing=60, weights={"lexical": 1.0, "identity": 10.0})
        assert results2[0].symbol_id == "b"

    def test_channel_weight_override(self):
        """Channel's own weight field takes precedence when non-default."""
        ch = ChannelResult(name="lexical", ranked_ids=["a"], weight=5.0)
        results = fuse([ch], smoothing=60)
        expected_score = 5.0 / (60 + 1)
        assert abs(results[0].score - expected_score) < 1e-9

    def test_smoothing_effect(self):
        ch = ChannelResult(name="lexical", ranked_ids=["a", "b"])
        # Lower smoothing → more top-heavy
        results_low = fuse([ch], smoothing=1)
        results_high = fuse([ch], smoothing=100)
        ratio_low = results_low[0].score / results_low[1].score
        ratio_high = results_high[0].score / results_high[1].score
        assert ratio_low > ratio_high  # Lower smoothing amplifies rank 1

    def test_channel_ranks_recorded(self):
        ch = ChannelResult(name="lexical", ranked_ids=["x", "y", "z"])
        results = fuse([ch], smoothing=60)
        assert results[0].channel_ranks["lexical"] == 1
        assert results[1].channel_ranks["lexical"] == 2
        assert results[2].channel_ranks["lexical"] == 3

    def test_wrr_formula_exact(self):
        """Verify the exact WRR formula: score = weight / (k + rank)."""
        ch = ChannelResult(name="lexical", ranked_ids=["a"])
        w = DEFAULT_WEIGHTS["lexical"]
        k = 60
        results = fuse([ch], smoothing=k)
        expected = w / (k + 1)
        assert abs(results[0].score - expected) < 1e-9

    def test_default_weight_channels_are_not_inert(self):
        """Regression (#324): channels built with the default weight sentinel
        must draw their weight from the weights dict, not collapse to 0.0.

        Before the fix, the fuse() sentinel (``!= 1.0``) disagreed with the
        builders' default (``0.0``), so every channel resolved to weight 0.0,
        every fused score was 0.0, and ordering silently fell back to a stable
        sort over insertion order.
        """
        # Exactly what the live callers emit: channels with no explicit weight.
        chans = [
            ChannelResult(name="lexical", ranked_ids=["a", "b", "c"]),
            ChannelResult(name="identity", ranked_ids=["c", "a"]),
        ]
        results = fuse(chans, weights=dict(DEFAULT_WEIGHTS))

        # Every contribution is non-zero (the inert-weight bug zeroed them all).
        for r in results:
            assert r.score > 0.0
            for contrib in r.channel_contributions.values():
                assert contrib > 0.0

        # The identity channel carries weight 2.0, so the identity top hit "c"
        # must outrank the lexical-only ordering. Under the bug, "a" came back
        # first purely from stable-sort insertion order.
        assert results[0].symbol_id == "c"

    def test_default_weight_sentinel_uses_channel_default(self):
        """A channel with weight=None resolves to DEFAULT_WEIGHTS by name."""
        ch = ChannelResult(name="identity", ranked_ids=["a"])  # weight defaults to None
        assert ch.weight is None
        results = fuse([ch], smoothing=60)
        expected = DEFAULT_WEIGHTS["identity"] / (60 + 1)
        assert abs(results[0].score - expected) < 1e-9

    def test_explicit_zero_weight_is_respected(self):
        """An explicit weight of 0.0 is a real override, not the 'use default' sentinel."""
        ch = ChannelResult(name="identity", ranked_ids=["a"], weight=0.0)
        results = fuse([ch], smoothing=60)
        assert results[0].score == 0.0


# ---------------------------------------------------------------------------
# Channel builders
# ---------------------------------------------------------------------------

def test_all_builders_default_weight_to_none_sentinel():
    """Regression (#324): the builders' default weight must be the same
    sentinel fuse() recognises (None), so the documented WRR weights apply
    on the live fusion=True paths instead of zeroing out."""
    builders = (
        build_lexical_channel,
        build_structural_channel,
        build_identity_channel,
        build_similarity_channel,
    )
    for b in builders:
        assert b.__kwdefaults__["weight"] is None, b.__name__


class TestBuildIdentityChannel:

    def _make_sym(self, name, sym_id=None):
        return {
            "id": sym_id or f"test.py::{name}",
            "name": name,
            "kind": "function",
            "file": "test.py",
            "line": 1,
            "signature": f"def {name}()",
        }

    def test_exact_match(self):
        syms = [self._make_sym("get_symbol_source"), self._make_sym("other")]
        ch = build_identity_channel(syms, "get_symbol_source")
        assert ch.ranked_ids[0] == "test.py::get_symbol_source"

    def test_prefix_match(self):
        syms = [self._make_sym("get_symbol_source"), self._make_sym("other")]
        ch = build_identity_channel(syms, "get_sym")
        assert "test.py::get_symbol_source" in ch.ranked_ids

    def test_no_match(self):
        syms = [self._make_sym("foo")]
        ch = build_identity_channel(syms, "zzz_nonexistent")
        assert ch.ranked_ids == []


class TestBuildStructuralChannel:

    def test_ranks_by_pagerank(self):
        syms = [
            {"id": "a", "file": "high.py"},
            {"id": "b", "file": "low.py"},
        ]
        pr = {"high.py": 0.9, "low.py": 0.1}
        ch = build_structural_channel(syms, pr)
        assert ch.ranked_ids == ["a", "b"]

    def test_candidate_filter(self):
        syms = [
            {"id": "a", "file": "f.py"},
            {"id": "b", "file": "f.py"},
        ]
        pr = {"f.py": 0.5}
        ch = build_structural_channel(syms, pr, candidate_ids={"a"})
        assert ch.ranked_ids == ["a"]


# ---------------------------------------------------------------------------
# BM25 without identity
# ---------------------------------------------------------------------------

class TestBm25NoIdentity:

    def test_no_identity_boost(self):
        """_bm25_score_no_identity should NOT include identity scoring."""
        sym = {
            "id": "test.py::foo",
            "name": "foo",
            "kind": "function",
            "file": "test.py",
            "line": 1,
            "signature": "def foo()",
            "keywords": [],
            "summary": "",
            "docstring": "",
        }
        # Query that exactly matches the name
        idf = {"foo": 1.0}
        score = _bm25_score_no_identity(sym, ["foo"], idf, 5.0)
        # Should be positive (BM25 term match) but NOT have the 50.0 identity boost
        assert 0 < score < 50.0


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

class TestLoadFusionWeights:

    def test_defaults(self):
        weights, smoothing = load_fusion_weights()
        assert weights == DEFAULT_WEIGHTS
        assert smoothing == DEFAULT_SMOOTHING
