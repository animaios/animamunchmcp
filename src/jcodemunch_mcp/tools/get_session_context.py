"""Get session context — files accessed, searches performed, edits registered.

Merges the former ``get_session_snapshot`` and ``get_session_stats`` into this
single tool.  ``format='json'`` returns raw structured data; ``format='compact'``
returns a ~200 token markdown summary suited for context compaction recovery.
"""

from __future__ import annotations

import time
from typing import Optional

# ---------------------------------------------------------------------------
# Snapshot rendering helpers (migrated from get_session_snapshot.py)
# ---------------------------------------------------------------------------


def _truncate_path(path: str, max_len: int = 50) -> str:
    """Truncate long paths to save tokens."""
    if len(path) <= max_len:
        return path
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) <= 2:
        return path
    return f".../{'/'.join(parts[-2:])}"


def _render_snapshot(
    context: dict,
    neg_log: list,
    max_files: int = 10,
    max_searches: int = 5,
    max_edits: int = 10,
    include_negative_evidence: bool = True,
) -> dict:
    """Render a snapshot ({snapshot, structured}) from a journal context dict.

    Shared by the in-process tool and the out-of-process PreCompact hook
    (snapshot_from_live), so both produce byte-identical output.
    """
    files_accessed = context.get("files_accessed", [])[:max_files]
    recent_searches = context.get("recent_searches", [])[:max_searches]
    files_edited = context.get("files_edited", [])[:max_edits]
    total_files = context.get(
        "total_unique_files", len(context.get("files_accessed", []))
    )
    total_queries = context.get(
        "total_unique_queries", len(context.get("recent_searches", []))
    )
    duration_s = context.get("session_duration_s", 0)

    duration_mins = int(duration_s / 60)
    duration_str = f"{duration_mins}m" if duration_mins > 0 else f"{int(duration_s)}s"

    snapshot_parts = [
        "## Session Snapshot (jCodemunch)",
        f"**Duration:** {duration_str} | **Files explored:** {total_files} | **Searches:** {total_queries}",
    ]

    # Focus files (most accessed)
    if files_accessed:
        snapshot_parts.append("\n### Focus files (most accessed)")
        for item in files_accessed:
            truncated_file = _truncate_path(item["file"])
            snapshot_parts.append(
                f"- {truncated_file} ({item['reads']} reads, last: {item['last_tool']})"
            )

    # Edited files
    if files_edited:
        snapshot_parts.append("\n### Edited files")
        for item in files_edited:
            truncated_file = _truncate_path(item["file"])
            snapshot_parts.append(f"- {truncated_file} ({item['edits']} edits)")

    # Key searches
    if recent_searches:
        snapshot_parts.append("\n### Key searches")
        for item in recent_searches:
            snapshot_parts.append(
                f'- "{item["query"]}" → {item["result_count"]} results'
            )

    # Dead ends (negative evidence)
    dead_ends = []
    if include_negative_evidence:
        recent_neg_log = neg_log[-max_searches:] if neg_log else []
        dead_ends.extend(
            [
                {"query": entry["query"], "verdict": entry["verdict"]}
                for entry in recent_neg_log
            ]
        )
        if recent_neg_log:
            snapshot_parts.append("\n### Dead ends (don't re-search)")
            for entry in recent_neg_log:
                verdict_display = entry["verdict"].replace("_", " ")
                if "scanned_symbols" in entry:
                    snapshot_parts.append(
                        f'- "{entry["query"]}" → {verdict_display} (scanned {entry["scanned_symbols"]} symbols)'
                    )
                else:
                    snapshot_parts.append(f'- "{entry["query"]}" → {verdict_display}')

    structured = {
        "focus_files": [
            {
                "file": item["file"],
                "reads": item["reads"],
                "last_tool": item["last_tool"],
            }
            for item in files_accessed
        ],
        "edited_files": [
            {"file": item["file"], "edits": item["edits"]} for item in files_edited
        ],
        "key_searches": [
            {
                "query": item["query"],
                "count": item["count"],
                "result_count": item["result_count"],
            }
            for item in recent_searches
        ],
        "dead_ends": dead_ends,
        "session_duration_s": duration_s,
        "total_files_explored": total_files,
        "total_searches": total_queries,
    }

    return {
        "snapshot": "\n".join(snapshot_parts),
        "structured": structured,
    }


# ---------------------------------------------------------------------------
# Out-of-process hook entry point (migrated from get_session_snapshot.py)
# ---------------------------------------------------------------------------


def snapshot_from_live(
    base_path: Optional[str] = None,
    max_files: int = 10,
    max_searches: int = 5,
    max_edits: int = 10,
    include_negative_evidence: bool = True,
    max_age_minutes: Optional[int] = None,
) -> Optional[dict]:
    """Build a snapshot from the persisted live journal (#334).

    The PreCompact hook runs in a separate process from the MCP server, so its
    in-process journal is empty. This reads the live journal the server writes
    incrementally and renders it with the same formatter the live tool uses.

    Returns the snapshot dict (with ``_context`` for landmark seeding), or None
    when no live journal data is available or it records no activity.
    """
    start = time.perf_counter()
    from .session_state import load_live_journal  # noqa: PLC0415

    data = load_live_journal(base_path=base_path, max_age_minutes=max_age_minutes)
    if not data:
        return None

    context = {
        "files_accessed": data.get("files_accessed", []),
        "recent_searches": data.get("recent_searches", []),
        "files_edited": data.get("files_edited", []),
        "session_duration_s": data.get("session_duration_s", 0),
        "total_unique_files": data.get("total_unique_files", 0),
        "total_unique_queries": data.get("total_unique_queries", 0),
    }
    if (
        not context["total_unique_files"]
        and not context["total_unique_queries"]
        and not context["files_edited"]
    ):
        return None

    neg_log = data.get("negative_evidence_log", []) if include_negative_evidence else []
    result = _render_snapshot(
        context,
        neg_log,
        max_files=max_files,
        max_searches=max_searches,
        max_edits=max_edits,
        include_negative_evidence=include_negative_evidence,
    )
    result["_meta"] = {
        "timing_ms": round((time.perf_counter() - start) * 1000, 1),
        "source": "live_journal",
    }
    result["_context"] = context  # for the hook's landmark enrichment
    return result


# ---------------------------------------------------------------------------
# Public MCP tool entry point
# ---------------------------------------------------------------------------


def get_session_context(
    max_files: int = 50,
    max_queries: int = 20,
    format: str = "json",  # noqa: A002
    include_negative_evidence: bool = True,
    max_searches: int = 5,
    max_edits: int = 10,
    storage_path: Optional[str] = None,  # noqa: ARG001 - for API consistency
) -> dict:
    """Get the current session context.

    Returns information about files accessed, searches performed, and edits
    registered during the current MCP session.

    Args:
        max_files: Maximum number of files to return in files_accessed.
        max_queries: Maximum number of queries to return in recent_searches.
        format: Output format — 'json' returns raw structured data; 'compact'
            returns a ~200 token markdown summary with negative evidence and
            token-savings stats.
        include_negative_evidence: Include dead-end searches in compact format.
        max_searches: Maximum key searches to include (compact format only).
        max_edits: Maximum edited files to include (compact format only).
        storage_path: Ignored (for API consistency with other tools).

    Returns:
        When format='json': dict with files_accessed, recent_searches,
            files_edited, tool_calls, session_duration_s, etc.
        When format='compact': dict with snapshot (markdown string),
            structured (compact dict), and stats (token savings).
    """
    from ..storage.token_tracker import get_session_stats as _get_session_stats
    from .session_journal import get_journal

    start = time.perf_counter()
    journal = get_journal()
    context = journal.get_context(
        max_files=max_files, max_queries=max_queries, max_edits=20
    )

    if format == "compact":
        neg_log = []
        try:
            neg_log = journal.get_negative_evidence_log()
        except Exception:  # noqa: BLE001
            pass
        result = _render_snapshot(
            context,
            neg_log,
            max_files=min(max_files, 10),
            max_searches=max_searches,
            max_edits=max_edits,
            include_negative_evidence=include_negative_evidence,
        )
        # Attach token-savings stats (migrated from get_session_stats)
        try:
            result["stats"] = _get_session_stats(base_path=storage_path)
        except Exception:  # noqa: BLE001
            result["stats"] = {}
        result["_meta"] = {
            "timing_ms": round((time.perf_counter() - start) * 1000, 1),
            "format": "compact",
        }
        return result

    # Default: raw JSON (backward compatible)
    context["_meta"] = {
        "timing_ms": round((time.perf_counter() - start) * 1000, 1),
    }
    return context
