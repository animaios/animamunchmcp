"""Tests for v1.108.58: language-agnostic framework flow-edge resolver.

Covers the new ``tools/flow_edges.resolve_flow_edges`` resolver and its
integration into ``get_signal_chains`` (string-dispatched handlers become http
gateways; rendered templates attach as a per-chain ``views`` list).
"""

import pytest

from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.get_signal_chains import get_signal_chains
from jcodemunch_mcp.tools.flow_edges import resolve_flow_edges
from jcodemunch_mcp.tools._utils import resolve_repo
from jcodemunch_mcp.storage import IndexStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _index(src, store):
    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store)


def _load(repo, store_path):
    owner, name = resolve_repo(repo, store_path)
    store = IndexStore(base_path=store_path)
    return store.load_index(owner, name), store, owner, name


def _django_string_dispatch(tmp_path):
    """Django-style string dispatch: handler bound by reference, no decorator."""
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "views.py").write_text(
        "def list_users(request):\n"
        "    return render(request, 'users/list.html')\n\n"
        "def ghost_helper():\n"
        "    return 1\n"
    )
    (src / "urls.py").write_text(
        "from views import list_users\n"
        "from django.urls import path\n\n"
        "urlpatterns = [\n"
        "    path('users/', list_users),\n"
        "    path('missing/', views.does_not_exist),\n"
        "]\n"
    )
    return _index(src, store)


def _express_repo(tmp_path):
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "handlers.js").write_text(
        "function listUsers(req, res) {\n"
        "  res.render('dashboard');\n"
        "}\n"
        "module.exports = { listUsers };\n"
    )
    (src / "routes.js").write_text(
        "const { listUsers } = require('./handlers');\n"
        "router.get('/users', listUsers);\n"
    )
    return _index(src, store)


def _plain_repo(tmp_path):
    """No route/render shapes anywhere — the off-path baseline."""
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir()
    store.mkdir()
    (src / "utils.py").write_text(
        "def helper():\n    return 42\n"
    )
    (src / "service.py").write_text(
        "from utils import helper\n\n"
        "def compute():\n    return helper() + 1\n"
    )
    return _index(src, store)


# ---------------------------------------------------------------------------
# resolve_flow_edges — unit
# ---------------------------------------------------------------------------

def test_django_route_handler_resolved(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    edges = resolve_flow_edges(index, store, owner, name)

    routes = [e for e in edges if e["type"] == "route->handler"]
    resolved = [e for e in routes if e["resolution"] == "resolved"]
    assert any(e["dst_name"] == "list_users" for e in resolved)
    hit = next(e for e in resolved if e["dst_name"] == "list_users")
    assert hit["framework_shape"] == "django"
    assert hit["path"] == "users/"
    assert hit["dst_file"].endswith("views.py")
    assert hit["dst_id"]


def test_unresolved_route_is_reported_not_dropped(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    edges = resolve_flow_edges(index, store, owner, name)
    unresolved = [
        e for e in edges
        if e["type"] == "route->handler" and e["resolution"] == "unresolved"
    ]
    # `views.does_not_exist` has no backing symbol — surfaced, not silently lost.
    assert any("does_not_exist" in e["dst_name"] for e in unresolved)


def test_render_view_edge(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    edges = resolve_flow_edges(index, store, owner, name)
    renders = [e for e in edges if e["type"] == "render->view"]
    assert any(
        e["src_name"] == "list_users" and e["dst_name"] == "users/list.html"
        for e in renders
    )


def test_express_route_handler_resolved(tmp_path):
    repo, store_path = _express_repo(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    edges = resolve_flow_edges(index, store, owner, name)
    routes = [
        e for e in edges
        if e["type"] == "route->handler" and e["resolution"] == "resolved"
    ]
    assert any(e["dst_name"] == "listUsers" for e in routes)
    hit = next(e for e in routes if e["dst_name"] == "listUsers")
    assert hit["framework_shape"] == "express"
    assert hit["verb"] == "GET"
    assert hit["path"] == "/users"


def test_kinds_filter(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    only_routes = resolve_flow_edges(index, store, owner, name, kinds=("route",))
    assert only_routes
    assert all(e["type"] == "route->handler" for e in only_routes)
    only_renders = resolve_flow_edges(index, store, owner, name, kinds=("render",))
    assert all(e["type"] == "render->view" for e in only_renders)


def test_plain_repo_no_edges(tmp_path):
    repo, store_path = _plain_repo(tmp_path)
    index, store, owner, name = _load(repo, store_path)
    assert resolve_flow_edges(index, store, owner, name) == []


# ---------------------------------------------------------------------------
# get_signal_chains integration
# ---------------------------------------------------------------------------

def test_string_dispatched_handler_becomes_gateway(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    result = get_signal_chains(repo, storage_path=store_path)

    gw_names = {c["gateway_name"] for c in result["chains"]}
    assert "list_users" in gw_names
    chain = next(c for c in result["chains"] if c["gateway_name"] == "list_users")
    assert chain["kind"] == "http"
    # render->view annotation rides the chain.
    assert chain.get("views") == ["users/list.html"]
    assert result["_meta"]["flow_edges"]["route_gateways"] >= 1
    assert result["_meta"]["flow_edges"]["render_views"] >= 1
    assert result["_meta"]["flow_edges"]["unresolved_routes"] >= 1


def test_include_flow_edges_false_is_pre_feature_behavior(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    off = get_signal_chains(repo, storage_path=store_path, include_flow_edges=False)

    gw_names = {c["gateway_name"] for c in off["chains"]}
    assert "list_users" not in gw_names  # no decorator → not a gateway
    assert "flow_edges" not in off["_meta"]
    assert all("views" not in c for c in off["chains"])


def test_plain_repo_chains_unchanged_by_flag(tmp_path):
    repo, store_path = _plain_repo(tmp_path)
    on = get_signal_chains(repo, storage_path=store_path)
    off = get_signal_chains(repo, storage_path=store_path, include_flow_edges=False)

    # No route/render shapes: chain set is identical with the flag on or off.
    assert on["chains"] == off["chains"]
    assert on["_meta"]["flow_edges"] == {
        "route_gateways": 0, "unresolved_routes": 0, "render_views": 0,
    }
    assert "flow_edges" not in off["_meta"]


def test_lookup_mode_reaches_string_dispatched_handler(tmp_path):
    repo, store_path = _django_string_dispatch(tmp_path)
    result = get_signal_chains(repo, symbol="list_users", storage_path=store_path)
    assert result["chain_count"] >= 1
    assert result["on_no_chain"] is False
    assert "flow_edges" in result["_meta"]
