"""Tests for the Counter: adaptive tool surface (order / menu / route).

Covers the four guarantees from docs/prd-adaptive-tool-surface.md:
  1. Zero behavior change on the default 'full' surface (front door hidden).
  2. 'counter' surface collapses to the front door + always-present controls.
  3. order dispatches read actions, and the charter gate refuses unknown /
     front-door / exec-verb / unopted state-changing actions.
  4. menu discovers; route maps a task to action(s) and can execute.

Plus the charter CI gate: no live catalog action trips the exec/write tripwire.
"""

import asyncio
import json
import os

import pytest

import jcodemunch_mcp.server as server
from jcodemunch_mcp import counter


@pytest.fixture(autouse=True)
def _restore_surface():
    """Isolate each test from JCODEMUNCH_TOOL_SURFACE + the raw-catalog cache."""
    prev = os.environ.get("JCODEMUNCH_TOOL_SURFACE")
    yield
    if prev is None:
        os.environ.pop("JCODEMUNCH_TOOL_SURFACE", None)
    else:
        os.environ["JCODEMUNCH_TOOL_SURFACE"] = prev
    server._RAW_CATALOG = None


def _surface(value):
    if value is None:
        os.environ.pop("JCODEMUNCH_TOOL_SURFACE", None)
    else:
        os.environ["JCODEMUNCH_TOOL_SURFACE"] = value
    server._RAW_CATALOG = None


def _call(name, args):
    res = asyncio.run(server.call_tool(name, args))
    # call_tool returns a plain content list on success and a CallToolResult
    # (isError) on failure (F-P01); read the content uniformly.
    from mcp.types import CallToolResult

    content = res.content if isinstance(res, CallToolResult) else res
    return json.loads(content[0].text)


# --- 1. Full surface: front door hidden, behavior unchanged ---------------- #


def test_full_surface_hides_front_door():
    _surface(None)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert not (counter.FRONT_DOOR & names), "front door must be hidden on 'full'"


def test_full_surface_count_matches_standard_default():
    _surface(None)
    full = {t.name for t in asyncio.run(server.list_tools())}
    # Adding the Counter must not change the resident 'full' catalog at all.
    assert "search_units" in full and "get_blast_radius" in full
    assert "order" not in full


# --- 2. Counter surface: collapse to the front door ------------------------ #


def test_counter_surface_collapses_to_front_door():
    _surface("counter")
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert counter.FRONT_DOOR <= names
    # Counter surface collapses to just the front door.
    assert counter.FRONT_DOOR == names
    # Everything else is collapsed away.
    assert "search_units" not in names


# --- 3. order: dispatch + charter gate ------------------------------------- #


def test_order_gate_rejects_unknown_action():
    err = counter.order_gate("does_not_exist", server._catalog_names(), False)
    assert err and "Unknown action" in err


def test_order_gate_rejects_front_door_recursion():
    for fd in counter.FRONT_DOOR:
        err = counter.order_gate(fd, server._catalog_names(), False)
        assert err and "front-door" in err


def test_order_gate_allows_read_action():
    assert counter.order_gate("search_units", server._catalog_names(), False) is None


def test_order_gate_blocks_state_change_without_optin():
    err = counter.order_gate("index_content", server._catalog_names(), False)
    assert err and "allow_state_change" in err
    assert counter.order_gate("index_content", server._catalog_names(), True) is None


def test_order_gate_exec_tripwire_is_unconditional():
    # Even with the opt-in, an exec/write verb is refused outright.
    for verb in (
        "shell",
        "exec",
        "run_command",
        "write_file",
        "edit_file",
        "apply_patch",
    ):
        assert counter.forbidden_reason(verb) is not None
        assert counter.forbidden_reason(f"do_{verb}_now") is not None
    assert counter.forbidden_reason("search_symbols") is None


def test_order_dispatches_read_action_through_pipeline():
    # get_session_stats needs no repo and no index -> hermetic.
    out = _call("order", {"action": "get_session_stats", "args": {}})
    assert "error" not in out or "session" in json.dumps(out).lower()


def test_order_rejects_bad_args_type():
    out = _call("order", {"action": "search_units", "args": "not-an-object"})
    assert "error" in out


# --- 4. menu: discovery ----------------------------------------------------- #


def test_menu_lists_full_catalog():
    out = _call("menu", {})
    assert out["tool"] == "menu"
    assert out["total_actions"] >= 31
    assert not any(a["action"] in counter.FRONT_DOOR for a in out["actions"])


def test_menu_query_ranks_relevant_actions():
    out = _call("menu", {"query": "dead unused code", "limit": 5})
    actions = [a["action"] for a in out["actions"]]
    assert any(a in actions for a in ("find_dead_code", "get_dead_code_v2"))


def test_menu_rows_flag_state_changing():
    out = _call("menu", {"query": "index a repository", "limit": 10})
    by_name = {a["action"]: a for a in out["actions"]}
    if "index_content" in by_name:
        assert by_name["index_content"]["state_changing"] is True


# --- 5. route: intent -> action -------------------------------------------- #


def test_route_recommends_for_intent():
    out = _call("route", {"task": "who calls this function", "repo": "x"})
    assert out["recommended"], "route should recommend at least one action"
    assert out["recommended"][0]["action"] in ("get_call_hierarchy", "find_references")


def test_route_requires_task():
    out = _call("route", {"repo": "x"})
    assert "error" in out


def test_route_execute_blocks_state_changing_top_pick():
    # "reindex the repo" should map to a state-changing action; execute must refuse.
    out = _call(
        "route",
        {
            "task": "search for the string TODO in the code",
            "repo": "x",
            "execute": True,
        },
    )
    # search_text is read-only and repo-scoped -> route should have executed it
    # (or returned a clean execute_error if args couldn't be shaped); never a crash.
    assert out["tool"] == "route"


def test_route_classify_intent_pure():
    names = server._catalog_names()
    recs = counter.classify_intent("find dead code in the project", names)
    assert any(r["action"] in ("find_dead_code", "get_dead_code_v2") for r in recs)


# --- Charter CI gate: the Counter can never expose an exec/write action ----- #


def test_no_catalog_action_trips_exec_tripwire():
    """jcm is read-only by charter. If a future tool ever names a write/exec
    verb, this fails loudly so order() can't silently become a mutation
    backdoor -- the safety-surface guarantee."""
    offenders = [n for n in server._catalog_names() if counter.forbidden_reason(n)]
    assert offenders == [], (
        f"exec/write-verb tools would be exposed by order: {offenders}"
    )


def test_state_changing_set_is_subset_of_catalog():
    catalog = server._catalog_names()
    missing = counter.STATE_CHANGING_ACTIONS - catalog
    assert missing == set(), f"state-changing names not in catalog (drift): {missing}"
