"""Claude Code hook handlers for jDocMunch enforcement.

PreToolUse  -- intercept Read on large doc files, suggest jDocMunch tools.
PostToolUse -- auto-reindex after Edit/Write on doc files to keep the index fresh.
PreCompact  -- emit a session snapshot so doc orientation survives context compaction.

All read JSON from stdin and write JSON to stdout per the Claude Code hooks spec.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Doc extensions that benefit from jDocMunch structured retrieval.
# Mirrors parser.ALL_EXTENSIONS.
_DOC_EXTENSIONS: set[str] = {
    ".md", ".markdown", ".mdx",
    ".txt",
    ".rst",
    ".adoc", ".asciidoc", ".asc",
    ".ipynb",
    ".html", ".htm",
    ".yaml", ".yml",
    ".json", ".jsonc",
    ".xml", ".svg", ".xhtml",
    ".tscn", ".tres",
}

# Minimum file size (bytes) to trigger the jDocMunch suggestion.
# Override with JDOCMUNCH_HOOK_MIN_SIZE env var.
_MIN_SIZE_BYTES = int(os.environ.get("JDOCMUNCH_HOOK_MIN_SIZE", "2048"))


def run_pretooluse() -> int:
    """PreToolUse hook: intercept Read calls on large doc files.

    Reads hook JSON from stdin.  If the target is a doc file above the
    size threshold, prints a stderr hint directing Claude to use
    jDocMunch tools instead.

    Small files, non-doc files, and unreadable paths are silently allowed.

    Returns exit code (always 0 -- errors are swallowed to avoid blocking).
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    file_path: str = data.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return 0

    _, ext = os.path.splitext(file_path)
    if ext.lower() not in _DOC_EXTENSIONS:
        return 0

    try:
        size = os.path.getsize(file_path)
    except OSError:
        return 0

    if size < _MIN_SIZE_BYTES:
        return 0

    # Targeted reads (offset/limit set) are likely pre-edit -- allow silently.
    tool_input = data.get("tool_input", {})
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return 0

    # Full-file exploratory read on a large doc file -- warn but allow.
    # Hard deny breaks the Edit workflow (Claude Code requires Read before Edit).
    print(
        f"jDocMunch hint: this is a {size:,}-byte doc file. "
        "Prefer search_sections + get_section for exploration. "
        "Use Read only when you need exact line numbers for Edit.",
        file=sys.stderr,
    )
    return 0


def run_posttooluse() -> int:
    """PostToolUse hook: auto-reindex doc files after Edit/Write.

    Reads hook JSON from stdin, extracts the file path, and spawns
    ``jdocmunch-mcp index-local --path <dir>`` as a fire-and-forget
    background process to keep the index fresh.

    Non-doc files are skipped.  Errors are swallowed silently.

    Returns exit code (always 0).
    """
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    file_path: str = data.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return 0

    _, ext = os.path.splitext(file_path)
    if ext.lower() not in _DOC_EXTENSIONS:
        return 0

    # Fire-and-forget: spawn index-file for the single edited file.
    resolved = str(Path(file_path).resolve())
    try:
        kwargs: dict = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        subprocess.Popen(
            ["jdocmunch-mcp", "index-file", resolved],
            **kwargs,
        )
    except (OSError, FileNotFoundError):
        pass  # jdocmunch-mcp not in PATH -- skip silently

    return 0


def run_precompact() -> int:
    """PreCompact hook: generate session snapshot before context compaction.

    Reads hook JSON from stdin. Builds a compact snapshot of the current
    doc session state and returns it as a systemMessage for context injection.

    Returns exit code (always 0 -- errors are swallowed to avoid blocking).
    """
    try:
        data = json.load(sys.stdin)  # Validate stdin is valid JSON
    except (json.JSONDecodeError, ValueError):
        return 0

    cwd = data.get("cwd") if isinstance(data, dict) else None

    try:
        snapshot = _build_snapshot(cwd=cwd)
    except Exception:
        return 0

    if not snapshot:
        return 0

    result = {"systemMessage": snapshot}
    json.dump(result, sys.stdout)
    return 0


def _hook_include_source_roots() -> bool:
    return os.environ.get("JDOCMUNCH_HOOK_INCLUDE_SOURCE_ROOTS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _repo_matches_cwd(source_root: str, cwd: str) -> bool:
    """True when cwd and the repo's source_root are on the same path branch."""
    if not source_root or not cwd:
        return False
    try:
        c = os.path.normcase(os.path.abspath(cwd))
        s = os.path.normcase(os.path.abspath(source_root))
    except Exception:
        return False
    return c == s or c.startswith(s + os.sep) or s.startswith(c + os.sep)


def _build_snapshot(cwd: "str | None" = None) -> str:
    """Build a compact, path-safe session snapshot from indexed doc repos.

    A compaction hook is injected into agent context at a high-pressure moment,
    so it should preserve the most relevant orientation with the least unrelated
    corpus and path exposure (jdoc#66). When a `cwd` hint is available, repos on
    the same path branch are surfaced first and the rest are summarized as
    omitted. Absolute source roots are hidden by default; set
    `JDOCMUNCH_HOOK_INCLUDE_SOURCE_ROOTS=1` to restore them for local-only use.
    """
    from ..tools.list_repos import list_repos

    repos_result = list_repos()
    repos = repos_result.get("repos", [])

    if not repos:
        return ""

    relevant = [
        r for r in repos
        if cwd and _repo_matches_cwd(r.get("source_root", r.get("source", "")), cwd)
    ]
    cap = 3
    shown = (relevant or repos)[:cap]
    omitted = len(repos) - len(shown)
    include_roots = _hook_include_source_roots()

    lines = ["## jDocMunch Session Snapshot", ""]
    lines.append("Current workspace doc indexes:" if relevant else "Indexed doc repos:")
    for r in shown:
        name = r.get("repo_at_sha", r.get("name", r.get("repo", "?")))
        sections = r.get("section_count", r.get("sections", "?"))
        docs = r.get("doc_count", r.get("documents", "?"))
        line = f"- **{name}**: {docs} docs, {sections} sections"
        if include_roots:
            source = r.get("source_root", r.get("source", ""))
            if source:
                line += f" ({source})"
        lines.append(line)

    if omitted > 0:
        lines.append("")
        lines.append(
            f"Other indexed doc repos: {omitted} omitted. "
            "Use `doc_list_repos` if needed."
        )

    lines.append("")
    lines.append(
        "Use `search_sections` + `get_section` for doc navigation. "
        "Use `Read` only when you need exact line numbers for `Edit`."
    )
    return "\n".join(lines)
