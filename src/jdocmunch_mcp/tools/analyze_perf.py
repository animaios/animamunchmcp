"""analyze_perf — surface per-tool latency from the in-memory ring or SQLite sink."""

from __future__ import annotations

from typing import Optional

from ..storage.token_tracker import (
    _telemetry_enabled,
    latency_db_query,
    latency_stats,
)


def analyze_perf(
    window: str = "session",
    storage_path: Optional[str] = None,
) -> dict:
    """Return per-tool latency stats over the requested window.

    ``window``:
      - ``"session"`` (default): in-memory ring (last 512 calls per tool).
        Always available; no opt-in needed.
      - ``"1h"`` / ``"24h"`` / ``"7d"`` / ``"all"``: persistent SQLite sink.
        Requires ``JDOCMUNCH_PERF_TELEMETRY=1`` (otherwise no rows persisted
        and the response notes ``telemetry_enabled=false``).

    Output:
        {
          window: "session" | "1h" | ...,
          telemetry_enabled: bool,
          source: "memory" | "sqlite",
          per_tool: {tool: {count, p50_ms, p95_ms, max_ms, errors, error_rate}},
        }
    """
    enabled = _telemetry_enabled()

    if window == "session":
        return {
            "window": window,
            "telemetry_enabled": enabled,
            "source": "memory",
            "per_tool": latency_stats(),
        }

    if window not in {"1h", "24h", "7d", "all"}:
        return {
            "error": f"Unknown window: {window!r}. Use 'session', '1h', '24h', '7d', or 'all'.",
        }

    if not enabled:
        return {
            "window": window,
            "telemetry_enabled": False,
            "source": "sqlite",
            "per_tool": {},
            "hint": "Set JDOCMUNCH_PERF_TELEMETRY=1 to persist tool-call latencies to ~/.doc-index/telemetry.db.",
        }

    return {
        "window": window,
        "telemetry_enabled": True,
        "source": "sqlite",
        "per_tool": latency_db_query(window, base_path=storage_path),
    }
