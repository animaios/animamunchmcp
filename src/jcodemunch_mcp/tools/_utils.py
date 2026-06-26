"""Shared helpers for tool modules."""

import logging
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional, Union

from ..storage import IndexStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Git subprocess helper
# ---------------------------------------------------------------------------


def run_git(
    args: list[str],
    cwd: Union[str, Path],
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a git command; return (returncode, stdout, stderr).

    *stdout* and *stderr* are stripped of trailing newlines.
    On error the return code is negative:
      -1  git not found on PATH (FileNotFoundError)
      -2  git command timed out (TimeoutExpired)
      -3  other subprocess error
    """
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return -2, "", "git command timed out"
    except Exception as exc:  # pragma: no cover
        logger.debug("git subprocess error: %s", exc, exc_info=True)
        return -3, "", str(exc)


# ---------------------------------------------------------------------------
# Git file-churn helper (shared by search_symbols and get_repo_health)
# ---------------------------------------------------------------------------


def get_file_churn(cwd: str, days: int) -> dict[str, int]:
    """Return {relative_file_path: commit_count} for the last *days* days.

    Wraps ``git log --since=... --name-only``.  Returns an empty dict when
    git is unavailable (no repo, git not on PATH, timeout, etc.) so callers
    can degrade gracefully.
    """
    rc, out, _ = run_git(
        ["log", f"--since={days} days ago", "--name-only", "--format="],
        cwd=cwd,
        timeout=60,
    )
    if rc not in (0, 128) or not out:
        return {}
    counts: dict[str, int] = defaultdict(int)
    for line in out.splitlines():
        line = line.strip()
        if line:
            counts[line] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Bare-name resolution cache (P5)
# ---------------------------------------------------------------------------
# Keyed by storage base_path string.
# Value: (dir_mtime: float, mapping: dict[bare_name -> sorted list of owner/name])
# Invalidated whenever the base_path directory mtime changes (repo added/removed).
# ---------------------------------------------------------------------------
_bare_name_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
_BARE_NAME_LOCK = threading.Lock()


def _get_bare_name_map(store: IndexStore) -> dict[str, list[str]]:
    """Return a cached bare-name -> [owner/name] mapping for the store's base_path.

    Rebuilds when the directory mtime changes (repo indexed or cache invalidated).
    Cost when warm: one stat() call instead of N db reads.
    """
    path_str = str(store.base_path)
    try:
        mtime = store.base_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    with _BARE_NAME_LOCK:
        cached = _bare_name_cache.get(path_str)
        if cached and cached[0] == mtime:
            return cached[1]

    # Miss: rebuild without holding the lock (list_repos does I/O)
    mapping: dict[str, list[str]] = {}
    for repo_entry in store.list_repos():
        owner_name = repo_entry["repo"]
        if not owner_name or "/" not in owner_name:
            continue
        _, repo_name = owner_name.split("/", 1)
        for key in (repo_name, repo_entry.get("display_name")):
            if key:
                mapping.setdefault(key, []).append(owner_name)

    # Deduplicate and sort so output is deterministic
    mapping = {k: sorted(set(v)) for k, v in mapping.items()}
    with _BARE_NAME_LOCK:
        _bare_name_cache[path_str] = (mtime, mapping)
    return mapping


def resolve_repo(repo: str, storage_path: Optional[str] = None) -> tuple[str, str]:
    """Resolve a repo identifier to (owner, name).

    Accepts:
      - "owner/name" -> (owner, name)
      - "name"      -> looks up the bare name in the index cache
        and returns the match if unique.
      Raises ValueError if the repo is not found or the bare name is ambiguous.
    """
    if not repo:
        return "", ""
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name

    # Bare name: look it up in the index cache
    store = IndexStore(base_path=storage_path)
    bare_map = _get_bare_name_map(store)
    matches = bare_map.get(repo, [])
    if not matches:
        raise ValueError(f"Repository not found: {repo}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous repository name: {repo}. Use one of: {', '.join(matches)}"
        )
    owner, name = matches[0].split("/", 1)
    return owner, name


def index_status_to_tool_error(status) -> dict:
    """Convert an index status probe into a consistent tool error."""
    hint = status.hint or "Re-index this repository to rebuild the index."
    return {
        "error": f"Repository index is not loadable: {status.repo}",
        "repo": status.repo,
        "index_present": status.index_present,
        "loadable": status.loadable,
        "status": status.status,
        "load_error": status.load_error or status.status,
        "hint": hint,
    }


def load_repo_index_or_error(
    repo: str,
    storage_path: Optional[str] = None,
    branch: str = "",
) -> tuple[Optional[object], Optional[dict], Optional[object]]:
    """Resolve and load a repo index, returning a structured error on failure."""
    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return None, {"error": str(e)}, None

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name, branch=branch)
    if index is not None:
        return index, None, None

    status = store.inspect_index(owner, name, branch=branch)
    return None, index_status_to_tool_error(status), status


def resolve_fqn(
    repo: str, fqn: str, storage_path: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a PHP FQN to a jcodemunch symbol_id.

    Returns ``(symbol_id, None)`` on success or ``(None, error_message)`` on failure.
    """
    from ..parser.fqn import fqn_to_symbol
    from ..parser.imports import build_psr4_map

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return None, f"Repository not found: {e}"
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        status = store.inspect_index(owner, name)
        err = index_status_to_tool_error(status)
        return None, f"{err['error']} ({err['load_error']}). {err['hint']}"
    if not getattr(index, "source_root", None):
        return (
            None,
            "Index has no source_root (remote indexes don't support FQN resolution)",
        )
    psr4 = build_psr4_map(index.source_root)
    if not psr4:
        return None, "No PSR-4 autoload config found in composer.json"
    resolved = fqn_to_symbol(fqn, psr4, frozenset(index.source_files))
    if not resolved:
        return (
            None,
            f"FQN '{fqn}' could not be resolved. File not in index or namespace mismatch.",
        )
    return resolved, None
