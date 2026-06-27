"""list_repo_groups + define_repo_group MCP tools (v1.26.0)."""

from __future__ import annotations

import time
from typing import Optional

from ..storage import repo_groups


def list_repo_groups(storage_path: Optional[str] = None) -> dict:
    t0 = time.perf_counter()
    groups = repo_groups.list_groups(storage_path)
    return {
        "groups": [
            {"name": name, "repos": repos, "size": len(repos)}
            for name, repos in sorted(groups.items())
        ],
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "group_count": len(groups),
        },
    }


def define_repo_group(
    name: str,
    repos: list,
    storage_path: Optional[str] = None,
) -> dict:
    """Create, replace, or delete (empty repos list) a repo group."""
    t0 = time.perf_counter()
    if not name or not isinstance(name, str):
        return {"error": "Group name must be a non-empty string."}
    data = repo_groups.define(name, repos or [], base_path=storage_path)
    return {
        "name": name,
        "repos": data["groups"].get(name) or [],
        "deleted": name not in data["groups"],
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "group_count": len(data["groups"]),
        },
    }
