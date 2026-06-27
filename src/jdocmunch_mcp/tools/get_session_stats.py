"""get_session_stats — concise session view: latency_per_tool + total tokens saved."""

from __future__ import annotations

from typing import Optional

from ..storage.token_tracker import get_total_saved, latency_stats


def get_session_stats(storage_path: Optional[str] = None) -> dict:
    """Return latency_per_tool (in-memory ring) + cumulative tokens_saved.

    Lightweight wrapper that an agent can call to self-monitor without
    opting into the SQLite sink. For windowed historical analysis use
    ``analyze_perf(window=...)``.
    """
    return {
        "latency_per_tool": latency_stats(),
        "total_tokens_saved": get_total_saved(storage_path),
    }
