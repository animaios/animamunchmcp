"""get_repo_health — one-call triage snapshot of a repository.

Aggregates results from individual tools to produce a structured health summary:
  - Symbol and file counts
  - Dead-code estimate (% of functions/methods with high dead-code confidence)
  - Average cyclomatic complexity
  - Top N hotspots (complexity × churn)
  - Dependency cycle count
  - Unstable module count (instability > 0.7 in the import graph)

When ``detailed=True`` the response expands to include the full output of
every sub-tool (cycles list, coupling metrics, extraction candidates,
layer violations, untested symbols, churn data) under a ``details`` key.
Sub-tool failures are captured gracefully so the core summary always
succeeds.

Designed to be the *first* tool called in a new session.  One call gives a
complete triage picture with no follow-up needed.  All heavy lifting is
delegated to individual tools — no logic is duplicated here.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from typing import Any, Optional

from ..storage import IndexStore
from ._graph_utils import build_adjacency
from ._utils import get_file_churn, index_status_to_tool_error, resolve_repo, run_git
from .get_dead_code_v2 import get_dead_code_v2

logger = logging.getLogger(__name__)


# Internal time budget for detailed mode (leave 15s buffer for 60s MCP timeout)
_DETAIL_TIME_BUDGET_SECONDS = 45.0


def _check_time_budget(t0: float) -> bool:
    """Return True if we've exceeded the detailed-mode time budget."""
    return (time.perf_counter() - t0) > _DETAIL_TIME_BUDGET_SECONDS


# ---------------------------------------------------------------------------
# Internal helpers absorbed from merged tools
# ---------------------------------------------------------------------------


def _avg_complexity(index) -> float:
    """Mean cyclomatic complexity across all functions/methods with data."""
    values = [
        s.get("cyclomatic") or 0
        for s in index.symbols
        if s.get("kind") in ("function", "method") and (s.get("cyclomatic") or 0) > 0
    ]
    return round(sum(values) / len(values), 2) if values else 0.0


# Directory names that hold non-production code: tests, benchmarks, scripts,
# examples. Files in these directories have Ca=0 by construction (pytest
# collects tests, benchmarks/scripts run from the shell, examples are
# illustrative) so they trivially meet the instability > 0.7 threshold and
# would otherwise dominate the coupling axis for any well-tested project.
# Exclusion applies to both the iteration AND the denominator — see
# _count_unstable_modules below.
_NON_PRODUCTION_DIR_NAMES = frozenset(
    {
        "tests",
        "test",
        "benchmarks",
        "examples",
        "scripts",
    }
)

# Filename suffixes for ecosystems that co-locate tests with source rather
# than placing them under a tests/ directory:
#   Go:        foo_test.go
#   Jest:      foo.test.{js,jsx,ts,tsx}
#   Jasmine/Karma/Angular/NestJS:  foo.spec.{js,jsx,ts,tsx}
#   RSpec:     foo_spec.rb
#   JUnit:     FooTest.java
# Inline conventions like Rust's #[cfg(test)] mod tests cannot be detected
# by path alone and are out of scope — would need an AST-aware approach.
_NON_PRODUCTION_FILENAME_RE = re.compile(
    r"(?:_test\.go|\.(?:test|spec)\.[jt]sx?|_spec\.rb|Test\.java)$",
    re.IGNORECASE,
)


def _is_production_path(path: str) -> bool:
    """True when the path is neither under a non-production directory nor
    a test-suffix file. See module-level constants for the exact rules."""
    norm = path.replace("\\", "/")
    if any(p in _NON_PRODUCTION_DIR_NAMES for p in norm.split("/")):
        return False
    if _NON_PRODUCTION_FILENAME_RE.search(norm):
        return False
    return True


def _count_unstable_modules(index) -> tuple[int, int]:
    """Return ``(unstable_count, production_total)``.

    Counts files with instability > 0.7 (Ce-dominated) among production
    code only — tests, benchmarks, scripts, and examples are excluded
    from both the numerator AND the denominator. Including them would
    structurally penalize any project with a real test suite.

    Inbound references *from* test files still count toward production
    files' Ca (so well-tested code looks more stable, which is correct).
    """
    if not index.imports:
        return 0, 0
    source_files = frozenset(index.source_files)
    alias_map = getattr(index, "alias_map", None)
    fwd = build_adjacency(
        index.imports,
        source_files,
        alias_map,
        getattr(index, "psr4_map", None),
        expand_barrels=True,
    )

    # Build reverse (importers per file). The graph still includes test
    # imports — they credit production Ca and that's the correct shape.
    rev: dict[str, list[str]] = {}
    for src, targets in fwd.items():
        for tgt in targets:
            rev.setdefault(tgt, []).append(src)

    production_files = [f for f in index.source_files if _is_production_path(f)]
    unstable = 0
    for f in production_files:
        ca = len(rev.get(f, []))
        ce = len(fwd.get(f, []))
        total = ca + ce
        if total > 0 and (ce / total) > 0.7:
            unstable += 1
    return unstable, len(production_files)


# ---------------------------------------------------------------------------
# Helpers absorbed from get_dependency_cycles
# ---------------------------------------------------------------------------


def _find_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    """Find SCCs of size > 1 using Kosaraju's algorithm (iterative).

    Each returned SCC represents a set of files involved in a circular
    import chain.  The members are sorted for deterministic output.
    """
    # Collect all nodes
    all_nodes: set[str] = set(adj.keys())
    for targets in adj.values():
        all_nodes.update(targets)

    # Pass 1: DFS on original graph — record finish order
    visited: set[str] = set()
    finish_order: list[str] = []

    for start in all_nodes:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, Any]] = [(start, iter(adj.get(start, [])))]
        while stack:
            node, it = stack[-1]
            try:
                w = next(it)
                if w not in visited:
                    visited.add(w)
                    stack.append((w, iter(adj.get(w, []))))
            except StopIteration:
                stack.pop()
                finish_order.append(node)

    # Build transpose graph
    rev_adj: dict[str, list[str]] = {}
    for src, targets in adj.items():
        for tgt in targets:
            rev_adj.setdefault(tgt, []).append(src)

    # Pass 2: DFS on transpose in reverse finish order
    visited2: set[str] = set()
    sccs: list[list[str]] = []

    for start in reversed(finish_order):
        if start in visited2:
            continue
        scc: list[str] = []
        work = [start]
        visited2.add(start)
        while work:
            node = work.pop()
            scc.append(node)
            for w in rev_adj.get(node, []):
                if w not in visited2:
                    visited2.add(w)
                    work.append(w)
        if len(scc) > 1:
            sccs.append(sorted(scc))

    return sccs


# ---------------------------------------------------------------------------
# Helpers absorbed from get_hotspots
# ---------------------------------------------------------------------------


def _get_hotspots(
    index,
    owner: str,
    name: str,
    days: int,
    top_n: int,
    min_complexity: int,
    storage_path,
) -> dict[str, Any]:
    """Compute hotspots inline (absorbed from get_hotspots)."""
    t0 = time.perf_counter()
    git_available = False
    file_churn: dict[str, int] = {}
    if index.source_root:
        rc_check, _, _ = run_git(["rev-parse", "--git-dir"], cwd=index.source_root)
        if rc_check == 0:
            git_available = True
            file_churn = get_file_churn(index.source_root, days)

    file_churn_norm = {k.replace("\\", "/"): v for k, v in file_churn.items()}

    candidates: list[dict[str, Any]] = []
    for sym in index.symbols:
        if sym.get("kind") not in ("function", "method"):
            continue
        cyclomatic = sym.get("cyclomatic") or 0
        if cyclomatic < min_complexity:
            continue

        file_path = sym.get("file", "")
        file_norm = file_path.replace("\\", "/")
        churn = file_churn_norm.get(file_norm, 0)
        hotspot_score = round(cyclomatic * math.log1p(churn), 4)

        if hotspot_score > 10:
            assessment = "high"
        elif hotspot_score > 3:
            assessment = "medium"
        else:
            assessment = "low"

        candidates.append(
            {
                "symbol_id": sym.get("id", ""),
                "name": sym.get("name", ""),
                "kind": sym.get("kind", ""),
                "file": file_path,
                "line": sym.get("line") or 0,
                "cyclomatic": cyclomatic,
                "max_nesting": sym.get("max_nesting") or 0,
                "param_count": sym.get("param_count") or 0,
                "churn": churn,
                "hotspot_score": hotspot_score,
                "assessment": assessment,
            }
        )

    candidates.sort(key=lambda x: -x["hotspot_score"])
    top = candidates[: max(1, top_n)]

    has_complexity_data = any(c.get("cyclomatic", 0) > 0 for c in top)
    note = None
    if not has_complexity_data:
        note = (
            "No complexity data found — re-index with jcodemunch-mcp >= 1.16 "
            "to populate cyclomatic complexity metrics."
        )

    result: dict[str, Any] = {
        "repo": f"{owner}/{name}",
        "top_n": top_n,
        "days": days,
        "git_available": git_available,
        "hotspots": top,
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "methodology": "complexity_x_churn",
            "confidence_level": "medium",
        },
    }
    if note:
        result["note"] = note
    return result


# ---------------------------------------------------------------------------
# Helpers absorbed from get_layer_violations
# ---------------------------------------------------------------------------


def _file_to_layer(file_path: str, layers: list[dict]) -> Optional[str]:
    """Return the layer name for *file_path*, or None if unassigned."""
    for layer in layers:
        for prefix in layer.get("paths", []):
            norm_file = file_path.replace("\\", "/")
            norm_prefix = prefix.rstrip("/")
            if norm_file.startswith(norm_prefix + "/") or norm_file == norm_prefix:
                return layer["name"]
    return None


def _resolve_layers(
    rules: Optional[list[dict]],
    repo: str,
    index_source_root: Optional[str],
) -> list[dict]:
    """Return the layer definitions to use.

    Priority: 1) explicit rules, 2) .jcodemunch.jsonc, 3) empty list.
    """
    if rules is not None:
        return rules

    try:
        from .. import config as _cfg

        arch = _cfg.get("architecture", {}, repo=repo)
        if isinstance(arch, dict):
            layers = arch.get("layers", [])
            if layers:
                return layers
    except Exception:
        logger.debug("Failed to read architecture config", exc_info=True)

    return []


# ---------------------------------------------------------------------------
# Helpers absorbed from get_coupling_metrics
# ---------------------------------------------------------------------------


def _coupling_metrics(index, module_path: str, owner: str, name: str) -> dict[str, Any]:
    """Return coupling metrics for a single module (absorbed from get_coupling_metrics)."""
    start = time.perf_counter()

    if not index.imports:
        return {
            "error": (
                "No import data available. "
                "Re-index with jcodemunch-mcp >= 1.3.0 to enable coupling analysis."
            )
        }

    if module_path not in index.source_files:
        return {"error": f"File not found in index: {module_path}"}

    source_files = frozenset(index.source_files)
    alias_map = getattr(index, "alias_map", None)
    fwd = build_adjacency(
        index.imports,
        source_files,
        alias_map,
        getattr(index, "psr4_map", None),
        expand_barrels=True,
    )

    rev: dict[str, list[str]] = {}
    for src, targets in fwd.items():
        for tgt in targets:
            rev.setdefault(tgt, []).append(src)

    importers: list[str] = sorted(rev.get(module_path, []))
    dependencies: list[str] = sorted(fwd.get(module_path, []))

    ca = len(importers)
    ce = len(dependencies)
    total = ca + ce

    if total == 0:
        instability = None
        assessment = "isolated"
    else:
        instability = round(ce / total, 4)
        if instability <= 0.3:
            assessment = "stable"
        elif instability <= 0.7:
            assessment = "neutral"
        else:
            assessment = "unstable"

    elapsed = (time.perf_counter() - start) * 1000
    return {
        "repo": f"{owner}/{name}",
        "module": module_path,
        "ca": ca,
        "ce": ce,
        "instability": instability,
        "assessment": assessment,
        "importers": importers,
        "dependencies": dependencies,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


# ---------------------------------------------------------------------------
# Helpers absorbed from get_extraction_candidates
# ---------------------------------------------------------------------------


def _extraction_candidates(
    index,
    store: IndexStore,
    owner: str,
    name: str,
    file_path: str,
    min_complexity: int,
    min_callers: int,
) -> dict[str, Any]:
    """Find extraction candidates (absorbed from get_extraction_candidates)."""
    from ..parser.imports import resolve_specifier
    from ._call_graph import _word_match

    t0 = time.monotonic()

    if not index.has_source_file(file_path):
        matches = [
            f
            for f in index.source_files
            if f.endswith(file_path) or f.endswith(file_path.replace("\\", "/"))
        ]
        if len(matches) == 1:
            file_path = matches[0]
        elif len(matches) > 1:
            return {
                "error": f"Ambiguous file path {file_path!r}. Be more specific.",
                "candidates_paths": matches[:10],
            }
        else:
            return {"error": f"File {file_path!r} not found in index."}

    target_syms = [
        s
        for s in index.symbols
        if s.get("file") == file_path
        and s.get("kind") in ("function", "method")
        and (s.get("cyclomatic") or 0) >= min_complexity
    ]

    if not target_syms:
        return {
            "repo": f"{owner}/{name}",
            "file": file_path,
            "candidates": [],
            "min_complexity": min_complexity,
            "min_callers": min_callers,
            "note": (
                f"No functions with cyclomatic >= {min_complexity} found. "
                "If this file was indexed before v1.16, complexity data is not "
                "available — re-index the repo to populate complexity metrics."
            ),
            "_meta": {"timing_ms": round((time.monotonic() - t0) * 1000, 1)},
        }

    source_files_fs = frozenset(index.source_files)
    alias_map = getattr(index, "alias_map", {}) or {}
    psr4_map = getattr(index, "psr4_map", None)
    rev: dict[str, list[str]] = {}
    if index.imports:
        for src_file, file_imports in index.imports.items():
            for imp in file_imports:
                target = resolve_specifier(
                    imp["specifier"], src_file, source_files_fs, alias_map, psr4_map
                )
                if target and target != src_file:
                    rev.setdefault(target, []).append(src_file)

    importer_files = list(dict.fromkeys(rev.get(file_path, [])))

    candidates: list[dict[str, Any]] = []
    for sym in target_syms:
        sym_name = sym.get("name", "")
        caller_files: list[str] = []
        for imp_file in importer_files:
            content = store.get_file_content(owner, name, imp_file)
            if content and _word_match(content, sym_name):
                caller_files.append(imp_file)

        if len(caller_files) >= min_callers:
            cyclomatic = sym.get("cyclomatic") or 0
            candidates.append(
                {
                    "id": sym["id"],
                    "name": sym_name,
                    "kind": sym.get("kind", ""),
                    "line": sym.get("line", 0),
                    "cyclomatic": cyclomatic,
                    "max_nesting": sym.get("max_nesting") or 0,
                    "param_count": sym.get("param_count") or 0,
                    "caller_count": len(caller_files),
                    "caller_files": caller_files,
                    "score": cyclomatic * len(caller_files),
                }
            )

    candidates.sort(key=lambda x: -x["score"])

    timing_ms = round((time.monotonic() - t0) * 1000, 1)
    return {
        "repo": f"{owner}/{name}",
        "file": file_path,
        "candidates": candidates,
        "min_complexity": min_complexity,
        "min_callers": min_callers,
        "_meta": {"timing_ms": timing_ms},
    }


# ---------------------------------------------------------------------------
# Helpers absorbed from get_churn_rate
# ---------------------------------------------------------------------------


def _churn_rate(index, target: str, days: int, owner: str, name: str) -> dict[str, Any]:
    """Return git churn metrics for a file or symbol (absorbed from get_churn_rate)."""
    t0 = time.perf_counter()
    days = max(1, days)

    if not index.source_root:
        return {
            "error": (
                "Churn analysis requires a locally indexed repo (index_folder). "
                "GitHub-indexed repos (index_repo) do not have a local git working tree."
            )
        }
    cwd = index.source_root

    target_type = "file"
    file_path = target
    sym_name = None

    sym = next((s for s in index.symbols if s.get("id") == target), None)
    if sym is not None:
        file_path = sym.get("file", "")
        sym_name = sym.get("name", "")
        target_type = "symbol"
        if not file_path:
            return {"error": f"Symbol {target!r} has no file in index."}

    rc, _, err = run_git(["rev-parse", "--git-dir"], cwd=cwd)
    if rc != 0:
        if rc == -1:
            return {"error": "git not found on PATH."}
        return {"error": f"Not a git repository: {err}"}

    rc2, log_out, log_err = run_git(
        [
            "log",
            "--follow",
            f"--since={days} days ago",
            "--format=%H|%ae|%aI",
            "--",
            file_path,
        ],
        cwd=cwd,
        timeout=30,
    )
    if rc2 not in (0, 128):
        return {"error": f"git log failed: {log_err}"}

    commits_raw = (
        [line for line in log_out.splitlines() if line.strip()] if log_out else []
    )
    commit_count = len(commits_raw)

    authors: list[str] = sorted(
        {parts[1] for line in commits_raw if len((parts := line.split("|"))) >= 2}
    )
    dates = [
        parts[2]
        for line in commits_raw
        if len((parts := line.split("|"))) >= 3 and parts[2]
    ]
    last_modified = dates[0] if dates else None

    rc3, first_out, _ = run_git(
        ["log", "--follow", "--diff-filter=A", "--format=%aI", "--", file_path],
        cwd=cwd,
        timeout=30,
    )
    first_seen: Optional[str] = None
    if rc3 == 0 and first_out:
        first_seen = first_out.splitlines()[-1].strip() or None

    churn_per_week = round(commit_count / (days / 7), 2) if days > 0 else 0.0
    if churn_per_week <= 1.0:
        assessment = "stable"
    elif churn_per_week <= 3.0:
        assessment = "active"
    else:
        assessment = "volatile"

    result: dict[str, Any] = {
        "repo": f"{owner}/{name}",
        "target": target,
        "target_type": target_type,
        "file": file_path,
        "commits": commit_count,
        "authors": authors,
        "first_seen": first_seen,
        "last_modified": last_modified,
        "days": days,
        "churn_per_week": churn_per_week,
        "assessment": assessment,
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "methodology": "git_log",
            "confidence_level": "high",
        },
    }
    if sym_name:
        result["symbol_name"] = sym_name
    return result


# ---------------------------------------------------------------------------
# Helpers absorbed from get_untested_symbols
# ---------------------------------------------------------------------------


def _test_files_that_import(file_path: str, rev: dict[str, list[str]]) -> list[str]:
    """Return test files that directly import *file_path*."""
    from .get_dead_code_v2 import _is_test_file

    return [f for f in rev.get(file_path, []) if _is_test_file(f)]


def _symbol_reached_by_tests(
    sym: dict[str, Any],
    test_importers: list[str],
    index,
    store: IndexStore,
    owner: str,
    repo_name: str,
) -> tuple[bool, float, str]:
    """Check whether any test file references *sym* by name."""
    from ._call_graph import _word_match

    sym_name: str = sym.get("name", "")
    sym_file: str = sym.get("file", "")
    if not sym_name or not sym_file:
        return False, 1.0, "unreached"

    if not test_importers:
        return False, 1.0, "unreached"

    get_callers = getattr(index, "get_callers_by_name", None)
    callers_by_name = get_callers() if get_callers else None

    if callers_by_name:
        test_importer_set = frozenset(test_importers)
        for tf in test_importer_set:
            if callers_by_name.get((tf, sym_name)):
                return True, 0.0, "reached"

    for tf in test_importers:
        content = store.get_file_content(owner, repo_name, tf)
        if content and _word_match(content, sym_name):
            return True, 0.0, "reached"

    return False, 0.7, "imported_not_called"


def _get_untested_symbols(
    index,
    store: IndexStore,
    owner: str,
    name: str,
    file_pattern: Optional[str] = None,
    min_confidence: float = 0.5,
    max_results: int = 100,
) -> dict[str, Any]:
    """Find untested symbols (absorbed from get_untested_symbols)."""
    import fnmatch

    from .get_dead_code_v2 import _is_test_file

    start = time.perf_counter()

    if index.imports is None:
        return {
            "error": (
                "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 "
                "to enable untested symbol detection."
            )
        }

    source_files = frozenset(index.source_files)
    rev = build_adjacency(
        index.imports,
        source_files,
        index.alias_map,
        getattr(index, "psr4_map", None),
        direction="reverse",
    )

    test_file_set = frozenset(f for f in index.source_files if _is_test_file(f))
    test_importers_cache: dict[str, list[str]] = {}

    symbols: list[dict[str, Any]] = []
    total_non_test = 0

    for sym in index.symbols:
        kind = sym.get("kind", "")
        if kind not in ("function", "method"):
            continue

        sym_file = sym.get("file", "")
        if not sym_file or sym_file in test_file_set:
            continue

        if file_pattern:
            fp_fwd = sym_file.replace("\\", "/")
            if not (
                fnmatch.fnmatch(fp_fwd, file_pattern)
                or fnmatch.fnmatch(fp_fwd.rsplit("/", 1)[-1], file_pattern)
            ):
                continue

        total_non_test += 1

        if sym_file not in test_importers_cache:
            test_importers_cache[sym_file] = _test_files_that_import(sym_file, rev)
        test_importers = test_importers_cache[sym_file]

        reached, confidence, reason = _symbol_reached_by_tests(
            sym,
            test_importers,
            index,
            store,
            owner,
            name,
        )

        if reached:
            continue

        if confidence < min_confidence:
            continue

        symbols.append(
            {
                "symbol_id": sym.get("id", ""),
                "name": sym.get("name", ""),
                "kind": kind,
                "file": sym_file,
                "line": sym.get("line", 0),
                "confidence": confidence,
                "reason": reason,
            }
        )

    symbols.sort(key=lambda s: (s["file"], s["line"]))
    truncated = len(symbols) > max_results
    symbols = symbols[:max_results]

    untested_count = len(symbols)
    reached_pct = round(
        ((total_non_test - untested_count) / total_non_test * 100)
        if total_non_test > 0
        else 100.0,
        1,
    )

    elapsed = (time.perf_counter() - start) * 1000
    result: dict[str, Any] = {
        "repo": f"{owner}/{name}",
        "untested_count": untested_count,
        "total_non_test_symbols": total_non_test,
        "reached_pct": reached_pct,
        "symbols": symbols,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }
    if truncated:
        result["_meta"]["truncated"] = True
        result["_meta"]["note"] = f"Results capped at max_results={max_results}"
    return result


# ---------------------------------------------------------------------------
# Layer violations (absorbed from get_layer_violations)
# ---------------------------------------------------------------------------


def _get_layer_violations(
    index,
    owner: str,
    name: str,
    rules: Optional[list[dict[str, Any]]],
) -> dict[str, Any]:
    """Check layer boundary violations (absorbed from get_layer_violations)."""
    start = time.perf_counter()

    if not index.imports:
        return {
            "error": (
                "No import data available. "
                "Re-index with jcodemunch-mcp >= 1.3.0 to enable layer analysis."
            )
        }

    source_root = getattr(index, "source_root", None)
    layers = _resolve_layers(rules, f"{owner}/{name}", source_root)

    if not layers:
        return {
            "repo": f"{owner}/{name}",
            "layer_count": 0,
            "violation_count": 0,
            "violations": [],
            "note": (
                "No layer rules defined. Pass 'rules' or add 'architecture.layers' "
                "to .jcodemunch.jsonc."
            ),
            "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
        }

    forbidden: dict[str, set[str]] = {}
    for layer in layers:
        lname = layer.get("name", "")
        mni = layer.get("may_not_import", [])
        if lname and mni:
            forbidden[lname] = set(mni)

    source_files = frozenset(index.source_files)
    alias_map = getattr(index, "alias_map", None)
    fwd = build_adjacency(
        index.imports,
        source_files,
        alias_map,
        getattr(index, "psr4_map", None),
        expand_barrels=True,
    )

    violations: list[dict[str, Any]] = []

    for src_file, targets in fwd.items():
        src_layer = _file_to_layer(src_file, layers)
        if not src_layer or src_layer not in forbidden:
            continue
        disallowed = forbidden[src_layer]
        for tgt_file in targets:
            tgt_layer = _file_to_layer(tgt_file, layers)
            if tgt_layer and tgt_layer in disallowed:
                violations.append(
                    {
                        "file": src_file,
                        "file_layer": src_layer,
                        "import_target": tgt_file,
                        "target_layer": tgt_layer,
                        "rule_violated": f"{src_layer} may_not_import {tgt_layer}",
                    }
                )

    elapsed = (time.perf_counter() - start) * 1000
    return {
        "repo": f"{owner}/{name}",
        "layer_count": len(layers),
        "violation_count": len(violations),
        "violations": violations,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def get_repo_health(
    repo: str,
    days: int = 90,
    detailed: bool = False,
    file_path: Optional[str] = None,
    rules: Optional[list[dict[str, Any]]] = None,
    top_n: int = 5,
    min_confidence: float = 0.5,
    max_results: int = 100,
    storage_path: Optional[str] = None,
) -> dict[str, Any]:
    """Return a one-call triage snapshot of the repository.

    Aggregates: symbol counts, dead code %, avg complexity, top hotspots,
    dependency cycle count, and unstable module count.

    When ``detailed=True`` the response expands with a ``details`` key
    containing full output from each sub-tool: cycles list, coupling metrics
    (if *file_path* given), extraction candidates (if *file_path* given),
    layer violations, untested symbols, and churn data (if *file_path* given).
    Sub-tool failures are captured in ``details._errors`` so the core
    summary always succeeds.

    Args:
        repo:           Repository identifier (owner/repo or bare name).
        days:           Churn look-back window for hotspot calculation (default 90).
        detailed:       When True, include full sub-tool outputs under ``details``.
        file_path:      Optional file path — enables coupling, extraction,
                        and churn sub-reports for that file.
        rules:          Optional layer rules for get_layer_violations.
        top_n:          Number of hotspots to return (default 5).
        min_confidence:  Minimum confidence for untested-symbol inclusion (default 0.5).
        max_results:    Cap on untested symbols returned (default 100).
        storage_path:   Optional index storage path override.

    Returns:
        ``{repo, summary, top_hotspots, cycle_count, cycles_sample,
           unstable_modules, dead_code_pct, avg_complexity,
           total_symbols, total_files, radar, details?, _meta}``
    """
    t0 = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return {"error": f"No index found for {repo!r}. Run index_folder first."}

    total_files = len(index.source_files)
    total_symbols = len(index.symbols)
    fn_method_count = sum(
        1 for s in index.symbols if s.get("kind") in ("function", "method")
    )
    repo_id = f"{owner}/{name}"

    # Dead code estimate (min_confidence=0.67 = 2 of 3 signals)
    dead_result = get_dead_code_v2(
        repo=repo_id, min_confidence=0.67, storage_path=storage_path
    )
    dead_count = len(dead_result.get("dead_symbols", []))
    dead_code_pct = (
        round(dead_count / fn_method_count * 100, 1) if fn_method_count > 0 else 0.0
    )

    # Avg complexity
    avg_complexity = _avg_complexity(index)

    # Top hotspots (now inline)
    hotspot_result = _get_hotspots(
        index,
        owner,
        name,
        days=days,
        top_n=top_n,
        min_complexity=2,
        storage_path=storage_path,
    )
    top_hotspots = hotspot_result.get("hotspots", [])

    # Dependency cycles (now inline)
    if index.imports:
        source_files = frozenset(index.source_files)
        adj = build_adjacency(
            index.imports,
            source_files,
            getattr(index, "alias_map", None),
            getattr(index, "psr4_map", None),
            expand_barrels=True,
        )
        cycles = _find_cycles(adj)
    else:
        cycles = []

    cycle_count = len(cycles)
    cycles_sample = cycles[:3]

    # Unstable modules — `coupling_total` excludes tests/benchmarks/scripts
    # and is the correct denominator for the coupling axis. `total_files`
    # in the response stays as the unfiltered count.
    unstable_count, coupling_total = _count_unstable_modules(index)

    # Build a human-readable summary line
    health_issues: list[str] = []
    if cycle_count > 0:
        health_issues.append(f"{cycle_count} dependency cycle(s)")
    if dead_code_pct >= 10:
        health_issues.append(f"{dead_code_pct}% likely-dead functions")
    if avg_complexity >= 10:
        health_issues.append(f"avg complexity {avg_complexity} (high)")
    elif avg_complexity >= 5:
        health_issues.append(f"avg complexity {avg_complexity} (medium)")
    if unstable_count > 0:
        health_issues.append(f"{unstable_count} unstable module(s)")

    if not health_issues:
        summary = "Healthy — no major issues detected."
    else:
        summary = "Issues found: " + "; ".join(health_issues) + "."

    # Six-axis radar. Test-gap and churn-surface inputs are best-effort —
    # failures degrade gracefully and the relevant axis is omitted from
    # the radar's composite.
    untested_pct: Optional[float] = None
    try:
        untested = _get_untested_symbols(
            index,
            store,
            owner,
            name,
            min_confidence=0.5,
            max_results=1,  # only need the count for radar
        )
        if "error" not in untested:
            reached_pct = float(untested.get("reached_pct", 0))
            untested_pct = max(0.0, 100.0 - reached_pct)
    except Exception:
        pass

    top_hotspot_score: Optional[float] = None
    if top_hotspots:
        try:
            top_hotspot_score = float(top_hotspots[0].get("hotspot_score", 0))
        except (TypeError, ValueError):
            top_hotspot_score = None

    from .health_radar import compute_radar

    radar = compute_radar(
        avg_complexity=avg_complexity,
        dead_code_pct=dead_code_pct,
        cycle_count=cycle_count,
        unstable_modules=unstable_count,
        total_files=coupling_total,  # production-code denominator
        untested_pct=untested_pct,
        top_hotspot_score=top_hotspot_score,
        runtime_coverage_pct=None,
    )

    # Build base response
    result: dict[str, Any] = {
        "repo": repo_id,
        "summary": summary,
        "total_files": total_files,
        "total_symbols": total_symbols,
        "fn_method_count": fn_method_count,
        "avg_complexity": avg_complexity,
        "dead_code_pct": dead_code_pct,
        "dead_count": dead_count,
        "cycle_count": cycle_count,
        "cycles_sample": cycles_sample,
        "unstable_modules": unstable_count,
        "top_hotspots": top_hotspots,
        "radar": radar,
    }

    # ------------------------------------------------------------------
    # Detailed mode: expand with full sub-tool outputs
    # ------------------------------------------------------------------
    if detailed:
        details: dict[str, Any] = {}
        detail_errors: list[dict[str, Any]] = []

        # Full cycles list
        details["cycles"] = cycles

        # Coupling metrics for file_path
        if file_path:
            try:
                details["coupling"] = _coupling_metrics(index, file_path, owner, name)
            except Exception as exc:
                details.setdefault("coupling", {"error": str(exc)})
                detail_errors.append({"sub_tool": "coupling", "error": str(exc)})

            # Extraction candidates for file_path
            try:
                details["extractions"] = _extraction_candidates(
                    index,
                    store,
                    owner,
                    name,
                    file_path=file_path,
                    min_complexity=5,
                    min_callers=2,
                )
            except Exception as exc:
                details.setdefault("extractions", {"error": str(exc)})
                detail_errors.append({"sub_tool": "extractions", "error": str(exc)})

            # Time budget check after extractions
            if _check_time_budget(t0):
                details["_timeout"] = True
                details["_errors"] = detail_errors + [
                    {
                        "sub_tool": "timeout",
                        "error": "Time budget exceeded, aborting remaining detailed checks",
                    }
                ]
                result["details"] = details
                elapsed = (time.perf_counter() - t0) * 1000
                result["_meta"] = {
                    "timing_ms": round(elapsed, 1),
                    "days": days,
                    "detailed": detailed,
                    "methodology": "aggregate",
                    "confidence_level": "medium",
                }
                return result

            # Churn rate for file_path
            try:
                details["churn"] = _churn_rate(
                    index, target=file_path, days=days, owner=owner, name=name
                )
            except Exception as exc:
                details.setdefault("churn", {"error": str(exc)})
                detail_errors.append({"sub_tool": "churn", "error": str(exc)})

            # Time budget check after churn rate
            if _check_time_budget(t0):
                details["_timeout"] = True
                details["_errors"] = detail_errors + [
                    {
                        "sub_tool": "timeout",
                        "error": "Time budget exceeded, aborting remaining detailed checks",
                    }
                ]
                result["details"] = details
                elapsed = (time.perf_counter() - t0) * 1000
                result["_meta"] = {
                    "timing_ms": round(elapsed, 1),
                    "days": days,
                    "detailed": detailed,
                    "methodology": "aggregate",
                    "confidence_level": "medium",
                }
                return result
        else:
            details["coupling"] = None
            details["extractions"] = None
            details["churn"] = None

        # Layer violations
        try:
            details["layer_violations"] = _get_layer_violations(
                index, owner, name, rules
            )
        except Exception as exc:
            details["layer_violations"] = {"error": str(exc)}
            detail_errors.append({"sub_tool": "layer_violations", "error": str(exc)})

        # Time budget check after layer violations
        if _check_time_budget(t0):
            details["_timeout"] = True
            details["_errors"] = detail_errors + [
                {
                    "sub_tool": "timeout",
                    "error": "Time budget exceeded, aborting remaining detailed checks",
                }
            ]
            result["details"] = details
            elapsed = (time.perf_counter() - t0) * 1000
            result["_meta"] = {
                "timing_ms": round(elapsed, 1),
                "days": days,
                "detailed": detailed,
                "methodology": "aggregate",
                "confidence_level": "medium",
            }
            return result

        # Full untested symbols
        try:
            details["untested_symbols"] = _get_untested_symbols(
                index,
                store,
                owner,
                name,
                min_confidence=min_confidence,
                max_results=max_results,
            )
        except Exception as exc:
            details["untested_symbols"] = {"error": str(exc)}
            detail_errors.append({"sub_tool": "untested_symbols", "error": str(exc)})

        # Time budget check after untested symbols
        if _check_time_budget(t0):
            details["_timeout"] = True
            details["_errors"] = detail_errors + [
                {
                    "sub_tool": "timeout",
                    "error": "Time budget exceeded, aborting remaining detailed checks",
                }
            ]
            result["details"] = details
            elapsed = (time.perf_counter() - t0) * 1000
            result["_meta"] = {
                "timing_ms": round(elapsed, 1),
                "days": days,
                "detailed": detailed,
                "methodology": "aggregate",
                "confidence_level": "medium",
            }
            return result

        if detail_errors:
            details["_errors"] = detail_errors

        result["details"] = details

    elapsed = (time.perf_counter() - t0) * 1000
    result["_meta"] = {
        "timing_ms": round(elapsed, 1),
        "days": days,
        "detailed": detailed,
        "methodology": "aggregate",
        "confidence_level": "medium",
    }

    return result
