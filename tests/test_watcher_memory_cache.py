"""Tests for WatcherChange NamedTuple, watcher memory cache, and fast-path integration."""

import os
import tempfile

import pytest
from pathlib import Path

from jcodemunch_mcp.reindex_state import WatcherChange


class TestWatcherChangeFormat:
    def test_watcher_change_properties(self):
        wc = WatcherChange("modified", "/path/to/file.py", "abc123")
        assert wc.change_type == "modified"
        assert wc.path == "/path/to/file.py"
        assert wc.old_hash == "abc123"

    def test_watcher_change_tuple_access(self):
        wc = WatcherChange("added", "/path/to/file.py", "")
        assert wc[0] == "added"
        assert wc[1] == "/path/to/file.py"
        assert wc[2] == ""

    def test_watcher_change_default_old_hash(self):
        wc = WatcherChange("added", "/path/to/file.py")
        assert wc.old_hash == ""


class TestWatcherMemoryCache:
    def test_watcher_change_with_old_hash(self):
        wc = WatcherChange("modified", "/path/to/file.py", "old_hash_value")
        assert wc.old_hash == "old_hash_value"
        assert wc.change_type == "modified"
        assert wc.path == "/path/to/file.py"


class TestBuildHashCacheIntegration:
    """Verify that _build_hash_cache can actually load an index via IndexStore.

    This catches the bug where _local_repo_id returns 'local/name-hash'
    but store.load_index(owner, name) rejects '/' in the name parameter.
    """

    def test_load_index_with_split_repo_id(self, tmp_path):
        """Simulate what _build_hash_cache does: split repo_id and call load_index."""
        from jcodemunch_mcp.storage.index_store import IndexStore
        from jcodemunch_mcp.watcher import _local_repo_id

        folder_path = str(tmp_path)
        repo_id = _local_repo_id(folder_path)
        assert "/" in repo_id, "repo_id must contain 'local/' prefix"

        repo_owner, repo_store_name = repo_id.split("/", 1)
        store = IndexStore(base_path=str(tmp_path / ".code-index"))

        # Must not raise ValueError — this is the exact call _build_hash_cache makes
        result = store.load_index(repo_owner, repo_store_name)
        assert result is None  # no index yet, but no crash

    def test_load_index_rejects_unsplit_repo_id(self, tmp_path):
        """Passing the full repo_id as name must raise (validates the bug existed)."""
        from jcodemunch_mcp.storage.index_store import IndexStore
        from jcodemunch_mcp.watcher import _local_repo_id

        folder_path = str(tmp_path)
        repo_id = _local_repo_id(folder_path)  # "local/name-hash"
        store = IndexStore(base_path=str(tmp_path / ".code-index"))

        with pytest.raises(ValueError, match="Path separator"):
            store.load_index("local", repo_id)  # <-- the old bug


class TestFastPathDeletedFiles:
    """Verify that deleted files are processed on the memory-cache fast path."""

    def test_deleted_file_with_memory_cache(self, tmp_path):
        """When use_memory_hash_cache=True, deleted files must still be removed from the index."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        # Create a test file and index it
        test_file = tmp_path / "hello.py"
        test_file.write_text("def hello():\n    return 'world'\n")

        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=str(tmp_path / ".code-index"),
            incremental=False,
        )
        assert result["success"]
        assert result["symbol_count"] >= 1

        # Now delete the file and call index_folder with changed_paths simulating
        # a watcher delete event with old_hash (memory cache path)
        abs_path = str(test_file.resolve())
        test_file.unlink()

        watcher_changes = [WatcherChange("deleted", abs_path, "some_old_hash")]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=str(tmp_path / ".code-index"),
            incremental=True,
            changed_paths=watcher_changes,
        )
        assert result2["success"]
        assert result2.get("deleted", 0) >= 1, (
            f"Expected at least 1 deleted file, got {result2}"
        )


class TestHashCacheMissFallback:
    """Regression tests for the hash-cache miss handling in _watch_single.

    Previously, a cache miss caused the watcher to read the file from disk to
    compute old_hash — but by the time watchfiles delivers the event the file
    already has new content, so old_hash == new_hash and the change is silently
    skipped as "unchanged". The fix replaces this with a sentinel "__cache_miss__"
    that is guaranteed never to match any real content hash, forcing re-parse.
    """

    def test_cache_miss_forces_reindex(self, tmp_path):
        """A modified file whose hash is absent from the memory cache must be re-indexed.

        Simulate the scenario: file is indexed, then modified, but the watcher's
        in-memory hash cache is empty (e.g. cold start).  Pass old_hash="__cache_miss__"
        (the sentinel) and verify index_folder re-parses the file.
        """
        from jcodemunch_mcp.tools.index_folder import index_folder
        from jcodemunch_mcp.reindex_state import WatcherChange

        test_file = tmp_path / "module.py"
        test_file.write_text("def original(): pass\n")

        # Initial index
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=str(tmp_path / ".code-index"),
            incremental=False,
        )
        assert result["success"]

        # Simulate file change + cache miss (sentinel old_hash)
        test_file.write_text("def updated(): return 42\n")
        abs_path = str(test_file.resolve())
        watcher_changes = [WatcherChange("modified", abs_path, "__cache_miss__")]

        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=str(tmp_path / ".code-index"),
            incremental=True,
            changed_paths=watcher_changes,
        )
        assert result2["success"]
        # Must NOT return "No changes detected" — the file change must be processed
        assert result2.get("message") != "No changes detected", (
            "Cache-miss sentinel must force re-parse, not skip the file"
        )
        assert result2.get("changed", 0) >= 1 or result2.get("new", 0) >= 1, (
            f"Expected at least 1 changed/new file, got {result2}"
        )

    def test_sentinel_never_equals_real_hash(self):
        """__cache_miss__ must not be a valid SHA-256 hex digest."""
        import hashlib
        sentinel = "__cache_miss__"
        # Real hashes are 64-char hex strings; the sentinel is neither
        assert not all(c in "0123456789abcdef" for c in sentinel), (
            "Sentinel must be distinguishable from a real content hash"
        )
        assert len(sentinel) != 64


class TestFastPathExtraIgnorePatterns:
    """Regression: #300 follow-up (reported by @domis86 on v1.108.18). The
    watcher fast path in index_folder skipped `discover_local_files`, which
    is where extra_ignore_patterns get applied. A file under an ignored
    prefix that was correctly absent from the initial index would slip
    back in on the next modify event.
    """

    def test_modified_file_under_ignore_pattern_stays_unindexed(self, tmp_path):
        """An 'modified' watcher event on a file matching extra_ignore_patterns
        must be skipped on the fast path."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        # Project shape: one file under docs/legacy/ (ignored), one outside.
        legacy_dir = tmp_path / "docs" / "legacy"
        legacy_dir.mkdir(parents=True)
        ignored_file = legacy_dir / "file123.py"
        ignored_file.write_text("def in_ignored():\n    return 1\n")
        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")

        storage = str(tmp_path / ".code-index")

        # Initial full index with extra_ignore_patterns excluding docs/legacy/.
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
            extra_ignore_patterns=["docs/legacy/"],
        )
        assert result["success"]
        # Sanity: the ignored file is not in the index.
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index = store.load_index(owner, name)
        files = set(index.file_hashes.keys()) if index.file_hashes else set()
        assert "docs/legacy/file123.py" not in files, (
            "initial index leaked an ignored file"
        )

        # Modify the ignored file and trigger the watcher fast-path reindex.
        ignored_file.write_text("def in_ignored():\n    return 2  # changed\n")
        watcher_changes = [
            WatcherChange("modified", str(ignored_file.resolve()), "__cache_miss__"),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            extra_ignore_patterns=["docs/legacy/"],
            changed_paths=watcher_changes,
        )
        assert result2["success"]

        # Re-load index and assert the ignored file is STILL not in it.
        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "docs/legacy/file123.py" not in files_after, (
            "watcher fast path re-indexed an ignored file (#300 follow-up); "
            f"index files: {sorted(files_after)}"
        )

    def test_added_file_under_ignore_pattern_stays_unindexed(self, tmp_path):
        """An 'added' watcher event on a new file matching extra_ignore_patterns
        must also be skipped."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        legacy_dir = tmp_path / "docs" / "legacy"
        legacy_dir.mkdir(parents=True)
        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")

        storage = str(tmp_path / ".code-index")

        # Initial index without the ignored file present.
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
            extra_ignore_patterns=["docs/legacy/"],
        )
        assert result["success"]

        # Now create a file under the ignored prefix and fire an "added" event.
        new_file = legacy_dir / "newcomer.py"
        new_file.write_text("def newcomer():\n    return 1\n")
        watcher_changes = [
            WatcherChange("added", str(new_file.resolve()), ""),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            extra_ignore_patterns=["docs/legacy/"],
            changed_paths=watcher_changes,
        )
        assert result2["success"]

        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "docs/legacy/newcomer.py" not in files_after, (
            "watcher fast path indexed a newly-added ignored file; "
            f"index files: {sorted(files_after)}"
        )

    def test_modified_file_outside_ignore_still_indexed(self, tmp_path):
        """Sanity: non-ignored files should still flow through the fast path
        normally. The filter must not over-match."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")

        storage = str(tmp_path / ".code-index")
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
            extra_ignore_patterns=["docs/legacy/"],
        )
        assert result["success"]

        # Modify the kept file; watcher fast path should re-index it.
        kept_file.write_text("def kept():\n    return 99  # updated\n")
        watcher_changes = [
            WatcherChange("modified", str(kept_file.resolve()), "__cache_miss__"),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            extra_ignore_patterns=["docs/legacy/"],
            changed_paths=watcher_changes,
        )
        assert result2["success"]
        # changed should be >=1 since the file content actually changed.
        assert result2.get("changed", 0) >= 1, (
            f"non-ignored file should have been re-indexed: {result2}"
        )


class TestFastPathHonorsAllDiscoveryFilters:
    """Regression: #306 (audit follow-up to #300). The watcher fast path in
    ``index_folder`` skipped ``discover_local_files`` entirely, so every
    filter applied during a full walk was missing on the fast path. v1.108.19
    fixed ``extra_ignore_patterns`` (covered by ``TestFastPathExtraIgnorePatterns``
    above). #306 extracted ``_should_index_file`` as the single source of
    truth and routed both paths through it. These tests lock the invariant:
    any new filter added to ``_should_index_file`` automatically applies on
    the fast path too — but the High/Medium risks called out in #306
    (gitignore, size cap, symlink protection, skip-dirs) get explicit
    coverage here so regressions surface as test failures, not silent
    re-indexing of files the user expects to be excluded.
    """

    def test_modified_file_under_gitignore_stays_unindexed(self, tmp_path):
        """High-risk per #306: a .gitignore-matched file modified after the
        initial index must not be re-indexed by the fast path."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        # Project shape: root .gitignore excludes build/; one file inside
        # build/ (ignored), one outside (kept).
        (tmp_path / ".gitignore").write_text("build/\n")
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        ignored_file = build_dir / "artifact.py"
        ignored_file.write_text("def artifact():\n    return 1\n")
        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")

        storage = str(tmp_path / ".code-index")

        # Initial full index. The full walk already honours .gitignore, so
        # build/artifact.py is correctly absent.
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
        )
        assert result["success"]
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index = store.load_index(owner, name)
        files = set(index.file_hashes.keys()) if index.file_hashes else set()
        assert "build/artifact.py" not in files, "initial walk leaked a gitignored file"

        # Modify the gitignored file and trigger a fast-path reindex.
        ignored_file.write_text("def artifact():\n    return 2  # changed\n")
        watcher_changes = [
            WatcherChange("modified", str(ignored_file.resolve()), "__cache_miss__"),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            changed_paths=watcher_changes,
        )
        assert result2["success"]

        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "build/artifact.py" not in files_after, (
            "watcher fast path re-indexed a .gitignore-matched file (#306); "
            f"index files: {sorted(files_after)}"
        )

    def test_modified_oversize_file_skipped_by_fast_path(self, tmp_path):
        """Medium-risk per #306: a file that exceeds the size cap must be
        skipped on the fast path, not silently re-indexed."""
        from jcodemunch_mcp.tools.index_folder import index_folder
        from jcodemunch_mcp.tools import index_folder as idx_mod

        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")
        # Pre-create a small placeholder; we'll grow it past the cap below.
        big_file = tmp_path / "big.py"
        big_file.write_text("def small():\n    return 1\n")

        storage = str(tmp_path / ".code-index")

        # Initial index includes big.py at its small size.
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
        )
        assert result["success"]
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)

        # Grow big.py past the size cap. Patch the module-level constant so
        # the fast-path filter config sees a low cap (real cap is 500 KB;
        # writing that many bytes per test is wasteful).
        original_cap = idx_mod.DEFAULT_MAX_FILE_SIZE
        try:
            idx_mod.DEFAULT_MAX_FILE_SIZE = 200  # bytes
            big_file.write_text("# padding\n" * 100)  # ~1000 bytes
            watcher_changes = [
                WatcherChange("modified", str(big_file.resolve()), "__cache_miss__"),
            ]
            result2 = index_folder(
                path=str(tmp_path),
                use_ai_summaries=False,
                storage_path=storage,
                incremental=True,
                changed_paths=watcher_changes,
            )
            assert result2["success"]
            # The modify should NOT have re-parsed big.py — fast path saw
            # too_large and skipped it. The bookkeeping signal: no parse =
            # no "changed" increment.
            assert result2.get("changed", 0) == 0, (
                "watcher fast path re-indexed an oversize file (#306); "
                f"result: {result2}"
            )
        finally:
            idx_mod.DEFAULT_MAX_FILE_SIZE = original_cap

    def test_added_file_under_skip_dir_not_indexed(self, tmp_path):
        """Per #306: watchfiles can emit events under build/cache dirs that
        os.walk would prune. The fast path must apply the skip-dirs regex."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")
        # node_modules is one of the default skip dirs.
        nm_dir = tmp_path / "node_modules" / "left-pad"
        nm_dir.mkdir(parents=True)

        storage = str(tmp_path / ".code-index")
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
        )
        assert result["success"]

        # Fire an "added" event for a file inside node_modules/.
        new_file = nm_dir / "index.js"
        new_file.write_text("module.exports = function(){};\n")
        watcher_changes = [
            WatcherChange("added", str(new_file.resolve()), ""),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            changed_paths=watcher_changes,
        )
        assert result2["success"]

        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "node_modules/left-pad/index.js" not in files_after, (
            "watcher fast path indexed a file under a skipped directory (#306); "
            f"index files: {sorted(files_after)}"
        )

    def test_added_symlinked_file_skipped_when_follow_symlinks_false(self, tmp_path):
        """Per #306: with follow_symlinks=False (the default), an 'added'
        event for a symlink must be skipped on the fast path."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        kept_file = tmp_path / "main.py"
        kept_file.write_text("def kept():\n    return 1\n")

        # Target outside the repo to make the case unambiguous, even if
        # symlink_escape didn't fire first.
        external_target = tmp_path.parent / f"external-{tmp_path.name}.py"
        external_target.write_text("def external():\n    return 1\n")

        storage = str(tmp_path / ".code-index")
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
        )
        assert result["success"]

        symlink_path = tmp_path / "link.py"
        try:
            os.symlink(str(external_target), str(symlink_path))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/user")

        watcher_changes = [
            WatcherChange("added", str(symlink_path), ""),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            follow_symlinks=False,
            changed_paths=watcher_changes,
        )
        assert result2["success"]

        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "link.py" not in files_after, (
            "watcher fast path indexed a symlink with follow_symlinks=False (#306); "
            f"index files: {sorted(files_after)}"
        )

    def test_deleted_event_bypasses_filters(self, tmp_path):
        """Per #306: deletions intentionally bypass the filter set so that
        an entry indexed before an ignore rule existed can still be
        removed. This locks that asymmetry — modifies are filtered, deletes
        are not."""
        from jcodemunch_mcp.tools.index_folder import index_folder

        # Index a file, then later "ignore" it. The delete event should
        # still remove it from the index even though the filter would now
        # reject an "added" or "modified" event for the same path.
        target = tmp_path / "to_remove.py"
        target.write_text("def to_remove():\n    return 1\n")
        kept = tmp_path / "main.py"
        kept.write_text("def kept():\n    return 1\n")

        storage = str(tmp_path / ".code-index")
        result = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=False,
        )
        assert result["success"]
        from jcodemunch_mcp.storage import IndexStore
        store = IndexStore(base_path=storage)
        owner, name = result["repo"].split("/", 1)
        index = store.load_index(owner, name)
        files = set(index.file_hashes.keys()) if index.file_hashes else set()
        assert "to_remove.py" in files, "setup failed: file should be in initial index"

        # Now delete + fire delete event. Use an ignore pattern that would
        # have blocked add/modify, to prove deletes are unaffected.
        target.unlink()
        watcher_changes = [
            WatcherChange("deleted", str((tmp_path / "to_remove.py").resolve()), ""),
        ]
        result2 = index_folder(
            path=str(tmp_path),
            use_ai_summaries=False,
            storage_path=storage,
            incremental=True,
            extra_ignore_patterns=["to_remove.py"],
            changed_paths=watcher_changes,
        )
        assert result2["success"]
        assert result2.get("deleted", 0) == 1, (
            f"delete event should remove the entry even when filter would reject "
            f"an add/modify for the same path: {result2}"
        )

        index_after = store.load_index(owner, name)
        files_after = set(index_after.file_hashes.keys()) if index_after.file_hashes else set()
        assert "to_remove.py" not in files_after, (
            "delete event failed to remove the file from the index"
        )
