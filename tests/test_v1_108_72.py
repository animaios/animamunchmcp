"""v1.108.72 — relative-path safety for resolve_repo.

A relative `path` (e.g. ".") passed to `resolve_repo` is resolved against the
SERVER process's working directory. Over a detached SSE / streamable-http
transport that is not the caller's directory, so "." silently bound to the
server's CWD and returned the wrong repo with no signal. The resolution itself
is unchanged (backward compatible); a relative input now carries an explicit
`relative_path_warning` + `_meta.relative_path` so the misbinding is visible.
Absolute-path callers get byte-identical output.
"""

from __future__ import annotations

import os
from pathlib import Path

from jcodemunch_mcp.tools.resolve_repo import resolve_repo, _resolve_repo_impl
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.resolve_repo import _compute_repo_id


class TestRelativePathWarning:
    def test_relative_dot_is_flagged(self, tmp_path, monkeypatch):
        """A relative '.' resolves against the server CWD and is flagged."""
        work = tmp_path / "someplace"
        work.mkdir()
        monkeypatch.chdir(work)

        result = resolve_repo(".", storage_path=str(tmp_path / "store"))

        assert "relative_path_warning" in result
        rel = result["_meta"]["relative_path"]
        assert rel["input"] == "."
        # Resolved against the (mocked) server CWD, not some caller dir.
        assert rel["resolved_against_cwd"] == str(work.resolve())
        assert "absolute path" in rel["hint"]

    def test_relative_subpath_is_flagged(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()

        result = resolve_repo("sub", storage_path=str(tmp_path / "store"))

        assert "relative_path_warning" in result
        assert result["_meta"]["relative_path"]["input"] == "sub"

    def test_absolute_path_is_byte_identical(self, tmp_path):
        """Absolute-path callers must see no behavior/output change."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("def hello(): pass\n")
        store_path = str(tmp_path / "store")
        index_folder(
            str(project), use_ai_summaries=False,
            storage_path=store_path, identity_mode="local",
        )

        abs_path = str(project.resolve())
        wrapped = resolve_repo(abs_path, storage_path=store_path)
        impl = _resolve_repo_impl(abs_path, storage_path=store_path)

        assert "relative_path_warning" not in wrapped
        assert "relative_path" not in wrapped.get("_meta", {})
        # Same resolution payload (drop volatile timing before comparing).
        for d in (wrapped, impl):
            d.get("_meta", {}).pop("timing_ms", None)
        assert wrapped == impl

    def test_warning_does_not_change_indexed_resolution(self, tmp_path, monkeypatch):
        """The flag is additive: a relative path that maps to an indexed repo
        still resolves to it, just with the warning attached."""
        project = tmp_path / "proj2"
        project.mkdir()
        (project / "main.py").write_text("def hi(): pass\n")
        store_path = str(tmp_path / "store2")
        index_folder(
            str(project), use_ai_summaries=False,
            storage_path=store_path, identity_mode="local",
        )
        monkeypatch.chdir(project)

        result = resolve_repo(".", storage_path=store_path)

        assert result["indexed"] is True
        assert result["repo"] == _compute_repo_id(project)
        assert "relative_path_warning" in result
