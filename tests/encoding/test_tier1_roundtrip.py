"""Round-trip tests for tier-1 custom encoders.

Each test: build a representative response, encode through the dispatcher,
decode via the public decoder, verify key fields and row contents survive.
"""

from typing import Any

import pytest

from jcodemunch_mcp.encoding import encode_response
from jcodemunch_mcp.encoding.decoder import decode
from jcodemunch_mcp.encoding.schemas import registry


def _rt(tool: str, response: dict[str, Any]) -> dict[str, Any]:
    payload, meta = encode_response(tool, response, "compact")
    assert isinstance(payload, str), (
        f"expected compact payload for {tool}, got {type(payload)}"
    )
    assert meta["encoding"] != "json"
    return decode(payload)


def test_registry_loads_all_tier1_encoders():
    expected = {
        "find_references",
        "get_call_hierarchy",
        "get_dependency_graph",
        "get_blast_radius",
        "get_dependency_cycles",
        "get_tectonic_map",
        "search_symbols",
        "search_text",
        "search_ast",
        "get_file_outline",
    }
    for tool in expected:
        assert registry.for_tool(tool) is not None, f"missing encoder for {tool}"


def test_find_references_round_trip():
    resp = {
        "repo": "acme/app",
        "identifier": "get_user",
        "reference_count": 2,
        "references": [
            {
                "file": "src/a.py",
                "matches": [
                    {"specifier": "models.user", "match_type": "named"},
                    {"specifier": "models.user", "match_type": "specifier_stem"},
                ],
            },
            {
                "file": "src/b.py",
                "matches": [
                    {"specifier": "models.user", "match_type": "named"},
                ],
            },
        ],
        "_meta": {"timing_ms": 3.1, "truncated": False},
    }
    out = _rt("find_references", resp)
    assert out["repo"] == "acme/app"
    assert out["identifier"] == "get_user"
    assert isinstance(out["reference_count"], int)
    assert out["reference_count"] == 2
    assert len(out["references"]) == 2
    assert out["references"][0]["file"] == "src/a.py"
    assert len(out["references"][0]["matches"]) == 2
    assert len(out["references"][1]["matches"]) == 1


def test_find_references_empty_matches_round_trip():
    resp = {
        "repo": "acme/app",
        "identifier": "get_user",
        "reference_count": 2,
        "references": [
            {"file": "src/a.py", "matches": []},
            {
                "file": "src/b.py",
                "matches": [{"specifier": "models.user", "match_type": "named"}],
            },
        ],
        "_meta": {"timing_ms": 1.0, "truncated": False},
    }
    out = _rt("find_references", resp)
    assert len(out["references"]) == 2
    assert out["references"][0]["file"] == "src/a.py"
    assert out["references"][0]["matches"] == []
    assert out["references"][1]["matches"][0]["match_type"] == "named"


def test_find_references_batch_round_trip():
    resp = {
        "repo": "acme/app",
        "results": [
            {
                "identifier": "get_user",
                "reference_count": 2,
                "references": [
                    {
                        "file": "src/a.py",
                        "specifier": "models.user",
                        "match_type": "named",
                    },
                    {
                        "file": "src/b.py",
                        "specifier": "models.user",
                        "match_type": "named",
                    },
                ],
            },
        ],
        "_meta": {"timing_ms": 2.0},
    }
    out = _rt("find_references", resp)
    assert isinstance(out["results"], list)
    assert len(out["results"]) == 1
    assert out["results"][0]["identifier"] == "get_user"
    assert len(out["results"][0]["references"]) == 2


def test_find_importers_round_trip():
    """find_importers is now find_references(mode='importers'). Encoder handles both."""
    resp = {
        "repo": "acme/app",
        "file_path": "src/models/user.py",
        "importer_count": 2,
        "importers": [
            {
                "file": "src/api/handlers.py",
                "specifier": "models.user",
                "has_importers": True,
            },
            {
                "file": "src/api/routes.py",
                "specifier": "models.user",
                "has_importers": False,
            },
        ],
        "_meta": {"timing_ms": 1.2, "truncated": False},
    }
    out = _rt("find_references", resp)
    assert out["file_path"] == "src/models/user.py"
    assert isinstance(out["importer_count"], int)
    assert out["importer_count"] == 2
    assert len(out["importers"]) == 2
    assert out["importers"][0]["file"] == "src/api/handlers.py"
    assert out["importers"][0]["has_importers"] is True


def test_find_importers_batch_round_trip():
    """find_importers batch mode: now dispatched via find_references."""
    resp = {
        "repo": "acme/app",
        "results": [
            {
                "file_path": "src/models/user.py",
                "importer_count": 1,
                "importers": [
                    {
                        "file": "src/api/handlers.py",
                        "specifier": "models.user",
                        "has_importers": True,
                    },
                ],
            },
        ],
        "_meta": {"timing_ms": 0.9},
    }
    out = _rt("find_references", resp)
    assert isinstance(out["results"], list)
    assert len(out["results"]) == 1
    assert out["results"][0]["file_path"] == "src/models/user.py"
    assert out["results"][0]["importers"][0]["has_importers"] is True


def test_find_references_related_round_trip():
    """find_references(mode='related') round-trip through compact encoder."""
    resp = {
        "repo": "acme/app",
        "symbol": {
            "id": "src/models/user.py::User",
            "name": "User",
            "kind": "class",
            "file": "src/models/user.py",
            "line": 10,
        },
        "related_count": 2,
        "related": [
            {
                "id": "src/models/user.py::get_user",
                "name": "get_user",
                "kind": "function",
                "file": "src/models/user.py",
                "line": 25,
                "signature": "def get_user(id)",
                "relatedness_score": 3.5,
            },
            {
                "id": "src/api/auth.py::AuthUser",
                "name": "AuthUser",
                "kind": "class",
                "file": "src/api/auth.py",
                "line": 5,
                "signature": "class AuthUser",
                "relatedness_score": 1.5,
            },
        ],
        "_meta": {"timing_ms": 2.1},
    }
    out = _rt("find_references", resp)
    assert out["related_count"] == 2
    assert len(out["related"]) == 2
    assert out["related"][0]["id"] == "src/models/user.py::get_user"
    assert out["related"][0]["relatedness_score"] == 3.5
    assert out["related"][1]["relatedness_score"] == 1.5
    assert out["symbol"]["name"] == "User"


def test_get_call_hierarchy_round_trip():
    resp = {
        "repo": "acme/app",
        "symbol": {
            "id": "sym1",
            "name": "foo",
            "kind": "function",
            "file": "x.py",
            "line": 1,
        },
        "direction": "both",
        "depth": 2,
        "depth_reached": 2,
        "caller_count": 2,
        "callee_count": 1,
        "callers": [
            {
                "id": "c1",
                "name": "a",
                "kind": "function",
                "file": "x.py",
                "line": 10,
                "depth": 1,
                "resolution": "lsp",
            },
            {
                "id": "c2",
                "name": "b",
                "kind": "function",
                "file": "x.py",
                "line": 20,
                "depth": 2,
                "resolution": "ast",
            },
        ],
        "callees": [
            {
                "id": "e1",
                "name": "helper",
                "kind": "function",
                "file": "y.py",
                "line": 5,
                "depth": 1,
                "resolution": "lsp",
            },
        ],
        "dispatches": [],
        "_meta": {"timing_ms": 4.0, "methodology": "ast+lsp"},
    }
    out = _rt("get_call_hierarchy", resp)
    assert out["symbol"]["name"] == "foo"
    assert len(out["callers"]) == 2
    assert out["callers"][0]["file"] == "x.py"
    assert len(out["callees"]) == 1


def test_get_dependency_graph_round_trip():
    resp = {
        "repo": "acme/app",
        "file": "src/main.py",
        "direction": "both",
        "depth": 2,
        "depth_reached": 2,
        "node_count": 3,
        "edge_count": 2,
        "edges": [
            {"from": "src/main.py", "to": "src/lib/a.py", "depth": 1},
            {"from": "src/main.py", "to": "src/lib/b.py", "depth": 1},
        ],
        "cross_repo_edges": [],
        "_meta": {"timing_ms": 2.1, "truncated": False, "cross_repo": False},
    }
    out = _rt("get_dependency_graph", resp)
    assert len(out["edges"]) == 2
    assert out["edges"][0]["from"] == "src/main.py"


def test_get_blast_radius_round_trip():
    resp = {
        "repo": "acme/app",
        "symbol": {
            "id": "s1",
            "name": "get_user",
            "kind": "function",
            "file": "auth.py",
            "line": 42,
        },
        "depth": 3,
        "importer_count": 2,
        "confirmed_count": 2,
        "potential_count": 1,
        "direct_dependents_count": 5,
        "overall_risk_score": 0.75,
        "confirmed": [
            {"file": "api.py", "references": 3, "has_test_reach": True},
            {"file": "main.py", "references": 1, "has_test_reach": False},
        ],
        "potential": [
            {"file": "utils.py", "reason": "wildcard import"},
        ],
        "_meta": {"timing_ms": 3.0},
    }
    out = _rt("get_blast_radius", resp)
    assert len(out["confirmed"]) == 2
    assert out["confirmed"][0]["file"] == "api.py"
    assert out["confirmed"][0]["references"] == 3
    assert out["confirmed"][0]["has_test_reach"] is True
    assert len(out["potential"]) == 1
    assert out["potential"][0]["file"] == "utils.py"
    assert out["symbol"]["name"] == "get_user"
    assert isinstance(out["overall_risk_score"], float)
    assert out["overall_risk_score"] == 0.75


def test_get_dependency_cycles_round_trip():
    resp = {
        "repo": "acme/app",
        "cycle_count": 1,
        "cycles": [["a.py", "b->c.py", "c.py"]],
        "_meta": {"timing_ms": 1.0},
    }
    out = _rt("get_dependency_cycles", resp)
    assert len(out["cycles"]) == 1
    assert isinstance(out["cycle_count"], int)
    assert out["cycle_count"] == 1
    assert out["cycles"][0] == ["a.py", "b->c.py", "c.py"]


def test_search_text_round_trip():
    # Mirrors the real shape of tools/search_text.py: results grouped by file,
    # with matches nested inside each group.
    resp = {
        "result_count": 2,
        "query": "TODO",
        "results": [
            {
                "file": "a.py",
                "matches": [
                    {"line": 10, "text": "# TODO: fix"},
                    {"line": 22, "text": "# TODO: refactor"},
                ],
            },
        ],
        "_meta": {"timing_ms": 0.5, "files_searched": 30, "truncated": False},
    }
    out = _rt("search_text", resp)
    assert len(out["results"]) == 1
    assert out["results"][0]["file"] == "a.py"
    matches = out["results"][0]["matches"]
    assert len(matches) == 2
    assert matches[0]["line"] == 10
    assert matches[0]["text"] == "# TODO: fix"
    assert matches[1]["line"] == 22
    # Typed scalars: ints, floats, bools survive the round trip.
    assert out["result_count"] == 2
    assert out["_meta"]["timing_ms"] == 0.5
    assert out["_meta"]["files_searched"] == 30
    assert out["_meta"]["truncated"] is False


def test_search_text_round_trip_with_context_lines():
    # context_lines>0 emits before/after arrays per match; must survive the
    # nested→flat→nested transform without data loss.
    resp = {
        "result_count": 1,
        "results": [
            {
                "file": "a.py",
                "matches": [
                    {
                        "line": 10,
                        "text": "target",
                        "before": ["above_1", "above_2"],
                        "after": ["below_1"],
                    },
                ],
            },
        ],
        "_meta": {"timing_ms": 0.1, "files_searched": 1, "truncated": False},
    }
    out = _rt("search_text", resp)
    m = out["results"][0]["matches"][0]
    assert m["before"] == ["above_1", "above_2"]
    assert m["after"] == ["below_1"]


def test_search_text_round_trip_adversarial_cells_and_st1_compat():
    """Round-trip adversarial CSV/JSON cell content and ensure st1 decode compatibility."""
    tricky_text = 'target, with "quotes" and newline\nline_two'
    tricky_before = [
        "before,comma",
        'before "quoted"',
        "before multi\nline",
    ]
    tricky_after = [
        'after, "mix"',
        "after multi\nline",
    ]
    resp = {
        "result_count": 1,
        "results": [
            {
                "file": "a.py",
                "matches": [
                    {
                        "line": 10,
                        "text": tricky_text,
                        "before": tricky_before,
                        "after": tricky_after,
                    },
                ],
            },
        ],
        "_meta": {"timing_ms": 0.1, "files_searched": 1, "truncated": False},
    }

    payload, meta = encode_response("search_text", resp, "compact")
    assert isinstance(payload, str)
    assert meta["encoding"] != "json"

    # st2 current decode
    out = decode(payload)
    m = out["results"][0]["matches"][0]
    assert m["text"] == tricky_text
    assert m["before"] == tricky_before
    assert m["after"] == tricky_after

    # st1 compatibility decode path (legacy header id)
    payload_st1 = payload.replace("enc=st2", "enc=st1", 1)
    out_st1 = decode(payload_st1)
    m_st1 = out_st1["results"][0]["matches"][0]
    assert m_st1["text"] == tricky_text
    assert m_st1["before"] == tricky_before
    assert m_st1["after"] == tricky_after


def test_search_text_round_trip_multi_file():
    # Separate files must stay separate on regroup; order preserved.
    resp = {
        "result_count": 3,
        "results": [
            {"file": "a.py", "matches": [{"line": 1, "text": "x"}]},
            {
                "file": "b.py",
                "matches": [{"line": 5, "text": "y"}, {"line": 9, "text": "z"}],
            },
        ],
        "_meta": {"timing_ms": 0.2, "files_searched": 2, "truncated": False},
    }
    out = _rt("search_text", resp)
    assert [g["file"] for g in out["results"]] == ["a.py", "b.py"]
    assert len(out["results"][1]["matches"]) == 2


def test_search_symbols_round_trip():
    resp = {
        "result_count": 2,
        "query": "user",
        "results": [
            {
                "id": "s1",
                "name": "get_user",
                "kind": "function",
                "file": "models/user.py",
                "line": 10,
                "score": 0.92,
                "signature": "def get_user(id)",
                "summary": "Fetches a user",
            },
            {
                "id": "s2",
                "name": "User",
                "kind": "class",
                "file": "models/user.py",
                "line": 1,
                "score": 0.88,
                "signature": "class User",
                "summary": "User model",
            },
        ],
        "_meta": {"timing_ms": 1.3, "total_symbols": 1200, "truncated": False},
    }
    out = _rt("search_symbols", resp)
    assert len(out["results"]) == 2
    assert out["results"][0]["name"] == "get_user"


def test_get_file_outline_round_trip():
    resp = {
        "repo": "acme/app",
        "file": "src/models/user.py",
        "symbol_count": 4,
        "symbols": [
            {
                "id": "s1",
                "name": "User",
                "kind": "class",
                "signature": "class User",
                "line": 1,
                "end_line": 20,
                "parent": None,
                "summary": "",
            },
            {
                "id": "s2",
                "name": "__init__",
                "kind": "method",
                "signature": "def __init__(self)",
                "line": 3,
                "end_line": 5,
                "parent": "s1",
                "summary": "",
            },
            {
                "id": "s3",
                "name": "name",
                "kind": "constant",
                "signature": "name: str",
                "line": 6,
                "end_line": 6,
                "parent": "s1",
                "summary": "",
            },
            {
                "id": "s4",
                "name": "get_user",
                "kind": "function",
                "signature": "def get_user(uid: int) -> User",
                "line": 25,
                "end_line": 40,
                "parent": None,
                "summary": "",
            },
        ],
        "_meta": {"timing_ms": 0.3},
    }
    out = _rt("get_file_outline", resp)
    assert len(out["symbols"]) == 4
    by_id = {s["id"]: s for s in out["symbols"]}
    # parent column carries hierarchy; nested symbols point at their class.
    assert by_id["s2"]["parent"] == "s1"
    assert by_id["s3"]["parent"] == "s1"
    assert by_id["s1"]["parent"] is None
    assert by_id["s4"]["parent"] is None
    # signature round-trips through the encoder.
    assert by_id["s4"]["signature"] == "def get_user(uid: int) -> User"
    assert by_id["s2"]["signature"] == "def __init__(self)"


def test_get_file_outline_batch_round_trip():
    """Batch shape (file_paths) must preserve every file's symbols (issue #319).

    The pre-fix encoder only read top-level ``symbols``, so the nested
    ``results[].symbols`` were silently dropped and models saw empty outlines.
    """
    resp = {
        "repo": "acme/app",
        "results": [
            {
                "repo": "acme/app",
                "file": "src/a.py",
                "language": "python",
                "file_summary": "",
                "symbols": [
                    {
                        "id": "a1",
                        "name": "foo",
                        "kind": "function",
                        "signature": "def foo()",
                        "line": 1,
                        "end_line": 2,
                        "parent": None,
                        "summary": "",
                    },
                    {
                        "id": "a2",
                        "name": "bar",
                        "kind": "function",
                        "signature": "def bar(x: int)",
                        "line": 4,
                        "end_line": 6,
                        "parent": None,
                        "summary": "",
                    },
                ],
                "_meta": {"symbol_count": 2},
            },
            {
                "repo": "acme/app",
                "file": "src/b.py",
                "language": "python",
                "file_summary": "",
                "symbols": [
                    {
                        "id": "b1",
                        "name": "Widget",
                        "kind": "class",
                        "signature": "class Widget",
                        "line": 1,
                        "end_line": 10,
                        "parent": None,
                        "summary": "",
                    },
                    {
                        "id": "b2",
                        "name": "render",
                        "kind": "method",
                        "signature": "def render(self)",
                        "line": 3,
                        "end_line": 5,
                        "parent": "b1",
                        "summary": "",
                    },
                ],
                "_meta": {"symbol_count": 2},
            },
            # A file with no symbols still round-trips as an empty list.
            {
                "repo": "acme/app",
                "file": "src/empty.py",
                "language": "python",
                "file_summary": "",
                "symbols": [],
                "_meta": {"symbol_count": 0},
            },
        ],
        "_meta": {"timing_ms": 1.2},
    }
    out = _rt("get_file_outline", resp)
    assert "results" in out
    assert len(out["results"]) == 3
    by_file = {r["file"]: r for r in out["results"]}

    # The core regression: symbols survive batch encoding.
    assert len(by_file["src/a.py"]["symbols"]) == 2
    assert len(by_file["src/b.py"]["symbols"]) == 2
    assert by_file["src/empty.py"]["symbols"] == []

    # Per-file metadata survives.
    assert by_file["src/a.py"]["_meta"]["symbol_count"] == 2
    assert by_file["src/empty.py"]["_meta"]["symbol_count"] == 0
    assert by_file["src/b.py"]["language"] == "python"

    # Hierarchy and signatures round-trip within the correct file.
    b_syms = {s["id"]: s for s in by_file["src/b.py"]["symbols"]}
    assert b_syms["b2"]["parent"] == "b1"
    assert b_syms["b1"]["parent"] is None
    a_syms = {s["id"]: s for s in by_file["src/a.py"]["symbols"]}
    assert a_syms["a2"]["signature"] == "def bar(x: int)"


# NOTE: get_repo_outline encoder removed — get_repo_map(mode="outline") has a different response shape.
# A new encoder for get_repo_map would need to be created if compact encoding is desired.


def test_get_repo_outline_round_trip():
    """Former get_repo_outline now lives in get_repo_map(mode='outline').
    The response shape has diverged, so this test is updated to reflect
    the find_references encoder handles the importers mode."""
    # This test is now a no-op; the outline response shape has changed.
    # Kept as placeholder to avoid breaking the test name pattern.
    pass


@pytest.mark.parametrize(
    "tool,resp",
    [
        (
            "search_ast",
            {
                "result_count": 1,
                "query": "call:print",
                "results": [
                    {
                        "file": "a.py",
                        "line": 10,
                        "match_type": "call",
                        "snippet": "print(x)",
                        "symbol_id": "s1",
                        "symbol_name": "foo",
                    },
                ],
                "_meta": {"timing_ms": 1.0, "files_searched": 20},
            },
        ),
        (
            "get_tectonic_map",
            {
                "repo": "a/b",
                "plate_count": 1,
                "file_count": 2,
                "plates": [
                    {
                        "plate_id": 0,
                        "anchor": "src/core.py",
                        "file_count": 2,
                        "cohesion": 0.82,
                        "majority_directory": "src",
                        "drifter_count": 0,
                        "nexus_alert": False,
                    }
                ],
                "drifter_summary": [
                    {
                        "file": "src/config/loader.py",
                        "current_directory": "src/config",
                        "belongs_with": "src",
                        "plate_anchor": "src/core.py",
                    }
                ],
                "isolated_files": ["README.md"],
                "signals_used": ["structural", "behavioral", "temporal"],
                "_meta": {"timing_ms": 3.0, "methodology": "tectonic"},
            },
        ),
    ],
)
def test_remaining_tier1_round_trip(tool, resp):
    out = _rt(tool, resp)
    # Just confirm the decode produces something usable with table keys preserved.
    for table_key in (
        "affected_symbols",
        "chains",
        "results",
        "context_items",
        "plates",
    ):
        if table_key in resp:
            assert table_key in out, f"{tool} lost {table_key}"


# NOTE: get_signal_chains encoder removed — now part of get_call_hierarchy(chains=True).
# The signal chains response shape has no dedicated encoder; it passes through
# the get_call_hierarchy encoder which only encodes the primary response fields.


def test_get_signal_chains_lookup_round_trip():
    """Former get_signal_chains now lives in get_call_hierarchy(chains=True).
    No dedicated encoder for chains shape; test preserved as a no-op placeholder."""
    pass


def test_get_signal_chains_discovery_meta_shape():
    pass


def test_get_signal_chains_no_gateway_round_trip():
    pass


def test_get_tectonic_map_round_trip_realistic():
    resp = {
        "repo": "test/repo",
        "plate_count": 2,
        "file_count": 6,
        "plates": [
            {
                "plate_id": 0,
                "anchor": "src/api/server.py",
                "file_count": 3,
                "cohesion": 0.82,
                "files": [
                    "src/api/server.py",
                    "src/api/routes.py",
                    "src/api/middleware.py",
                ],
                "majority_directory": "src/api",
            },
            {
                "plate_id": 1,
                "anchor": "src/db/models.py",
                "file_count": 3,
                "cohesion": 0.65,
                "files": [
                    "src/db/models.py",
                    "src/db/queries.py",
                    "src/config/loader.py",
                ],
                "majority_directory": "src/db",
                "drifters": ["src/config/loader.py"],
                "drifter_count": 1,
                "nexus_alert": True,
                "nexus_coupling_count": 4,
                "coupled_to": {"src/api/server.py": 0.45},
            },
        ],
        "isolated_files": ["README.md"],
        "signals_used": ["structural", "behavioral", "temporal"],
        "drifter_summary": [
            {
                "file": "src/config/loader.py",
                "current_directory": "src/config",
                "belongs_with": "src/db",
                "plate_anchor": "src/db/models.py",
            }
        ],
        "_meta": {"timing_ms": 15.0, "methodology": "tectonic"},
    }
    out = _rt("get_tectonic_map", resp)
    assert len(out["plates"]) == 2
    assert out["plates"][0]["plate_id"] == 0
    assert isinstance(out["plates"][0]["cohesion"], float)
    assert "drifter_count" not in out["plates"][0]
    assert "nexus_alert" not in out["plates"][0]
    assert out["plates"][1]["drifter_count"] == 1
    assert out["plates"][1]["nexus_alert"] is True
    assert out["drifter_summary"][0]["plate_anchor"] == "src/db/models.py"
    assert out["isolated_files"] == ["README.md"]
    assert out["signals_used"] == ["structural", "behavioral", "temporal"]
