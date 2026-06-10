"""CLI test for the ``delete-index`` subcommand.

``delete-index`` is the CLI alias for the ``invalidate_cache`` MCP tool (whose
core behavior is covered in ``test_hardening.py``). It exists so the jMunch
Console index panel can delete an index by shelling the CLI. This test exercises
the subcommand end-to-end against an isolated ``CODE_INDEX_PATH`` store: routing
(it must be in ``known_commands`` so the prepend-serve guard doesn't swallow it),
the ``--json`` body, and the exit codes the Console relies on (0 on success so
``_run_cli`` returns stdout; nonzero on a missing repo so it returns ``None``).
"""
import json
import os
import subprocess
import sys

import pytest


def _run(args, storage):
    env = {
        **os.environ,
        "CODE_INDEX_PATH": str(storage),
        "JCODEMUNCH_USE_AI_SUMMARIES": "false",  # hermetic + fast: no provider calls
    }
    return subprocess.run(
        [sys.executable, "-m", "jcodemunch_mcp", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def test_delete_index_cli_roundtrip(tmp_path):
    storage = tmp_path / "store"
    src = tmp_path / "proj"
    src.mkdir()
    (src / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    # Index a throwaway project into the isolated store.
    idx = _run(["index", str(src)], storage)
    assert idx.returncode == 0, idx.stderr

    repos = json.loads(_run(["list-repos", "--json"], storage).stdout)
    assert repos, "expected the throwaway project to be indexed"
    rid = repos[0]["repo_id"]

    # Delete it: success, exit 0, structured JSON body.
    dele = _run(["delete-index", rid, "--json"], storage)
    assert dele.returncode == 0, dele.stderr
    body = json.loads(dele.stdout)
    assert body["success"] is True
    assert body["repo"] == rid

    # It is gone from the cockpit listing.
    after = json.loads(_run(["list-repos", "--json"], storage).stdout)
    assert all(r["repo_id"] != rid for r in after)

    # Deleting again fails: nonzero exit (so _run_cli returns None) + error body.
    again = _run(["delete-index", rid, "--json"], storage)
    assert again.returncode == 1
    assert json.loads(again.stdout)["success"] is False
