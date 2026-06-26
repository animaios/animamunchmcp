"""Preflight check: is it safe to delete or edit this symbol?

Merges the former ``check_delete_safe`` and ``check_edit_safe`` into a single
dispatch surface.  ``mode='delete'`` answers "who breaks if this disappears?";
``mode='edit'`` answers "what regression risk if I modify it?".  Both share the
same signal gathering (importers, references, runtime, cross-repo) but diverge
in the blockers they build and the verdict tiers they return.

Read-only — never mutates the codebase.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from ..storage import IndexStore, cost_avoided, estimate_savings, record_savings
from ._utils import index_status_to_tool_error, resolve_repo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity scoring for individual blockers (1-5, higher = more dangerous)
# ---------------------------------------------------------------------------
_SEVERITY_CROSS_REPO = 5
_SEVERITY_RUNTIME = 5
_SEVERITY_ENTRY_POINT = 5
_SEVERITY_EXTERNAL_IMPORT = 4
_SEVERITY_COMPLEXITY = 3
_SEVERITY_INTERNAL_REF = 3
_SEVERITY_TEST_ONLY = 2
_SEVERITY_UNTESTED = 2

# Cyclomatic complexity at/above this is "high" — matches the low/medium/high
# bands used by get_symbol_complexity (1-4 low, 5-10 medium, 11+ high).
_COMPLEXITY_HIGH = 11

# Decorator patterns suggesting external invocation
_ENTRY_DECORATOR_RE = re.compile(
    r"\b(route|get|post|put|patch|delete|command|task|signal|"
    r"event|listener|handler|subscribe|on|receiver|websocket|"
    r"endpoint|api|view|mount|app|cli|main|fixture)\b",
    re.IGNORECASE,
)

_TEST_FILE_RE = re.compile(
    r"(^|[/\\])(test_|tests?[/\\]|_test\.|conftest\.py)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _is_test_file(file_path: str) -> bool:
    return bool(_TEST_FILE_RE.search(file_path or ""))


def _resolve_target(index, symbol: str) -> Optional[dict]:
    """Resolve a symbol id or name to one symbol dict."""
    for sym in index.symbols:
        if sym.get("id") == symbol:
            return sym
    candidates = [s for s in index.symbols if s.get("name") == symbol]
    if not candidates:
        return None
    # Prefer non-import kinds with the largest body
    candidates.sort(
        key=lambda s: (
            s.get("kind") == "import",
            -int(s.get("byte_length", 0) or 0),
        )
    )
    return candidates[0]


def _detect_entry_point(target: dict) -> Optional[str]:
    """Return the matched entry-point indicator if target looks like one."""
    decorators = target.get("decorators") or []
    if isinstance(decorators, str):
        decorators = [decorators]
    for dec in decorators:
        dec_str = str(dec) if not isinstance(dec, dict) else (dec.get("name") or "")
        if dec_str and _ENTRY_DECORATOR_RE.search(dec_str):
            return f"decorator:{dec_str}"
    # __main__ / main heuristics
    name = (target.get("name") or "").lower()
    if name in {"main", "__main__", "run", "serve", "cli", "app"}:
        return f"name:{name}"
    return None


def _runtime_hits(
    store: IndexStore, owner: str, name: str, symbol_id: str
) -> Optional[int]:
    """Best-effort runtime hit count over the indexed trace window."""
    try:
        import sqlite3  # noqa: PLC0415

        db_path = store._sqlite._db_path(owner, name)
        if not db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            cur = conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM runtime_calls WHERE symbol_id = ?",
                (symbol_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("check_safe: runtime hits skipped: %s", exc, exc_info=True)
        return None


def _runtime_data_present(store: IndexStore, owner: str, name: str) -> bool:
    """Has *any* runtime trace been ingested for this repo?"""
    try:
        import sqlite3  # noqa: PLC0415

        db_path = store._sqlite._db_path(owner, name)
        if not db_path.exists():
            return False
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            row = conn.execute("SELECT 1 FROM runtime_calls LIMIT 1").fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("check_safe: runtime probe skipped: %s", exc, exc_info=True)
        return False


def _check_dead_code_conf(
    repo: str, target_id: str, storage_path: Optional[str]
) -> float:
    """Look up get_dead_code_v2's confidence score for this symbol."""
    try:
        from .get_dead_code_v2 import get_dead_code_v2  # noqa: PLC0415

        out = get_dead_code_v2(
            repo,
            min_confidence=0.0,
            include_tests=False,
            storage_path=storage_path,
        )
        entries = out.get("dead_symbols") or out.get("results") or []
        for e in entries:
            if e.get("symbol_id") == target_id:
                return float(e.get("confidence", 0.0))
    except Exception as exc:  # noqa: BLE001
        logger.debug("check_safe: dead-code lookup skipped: %s", exc, exc_info=True)
    return 0.0


# ---------------------------------------------------------------------------
# Signal gathering — shared between both modes
# ---------------------------------------------------------------------------


class _Signals:
    """Accumulator for the import/reference/runtime signals shared by both modes."""

    def __init__(self) -> None:
        self.external_import_count = 0
        self.test_import_count = 0
        self.cross_repo_count = 0
        self.internal_ref_count = 0
        self.test_ref_count = 0
        self.entry_signal: Optional[str] = None
        self.cyclomatic = 0
        self.has_test_coverage = False
        self.runtime_hits: Optional[int] = None
        self.runtime_data_present = False

    def raw(self) -> dict:
        return {
            "external_import_count": self.external_import_count,
            "test_import_count": self.test_import_count,
            "cross_repo_count": self.cross_repo_count,
            "internal_ref_count": self.internal_ref_count,
            "test_ref_count": self.test_ref_count,
            "cyclomatic": self.cyclomatic,
            "has_test_coverage": self.has_test_coverage,
            "entry_point": self.entry_signal,
        }


def _gather_signals(
    signals: _Signals,
    owner: str,
    name: str,
    target: dict,
    cross_repo: bool,
    include_runtime: bool,
    storage_path: Optional[str],
    blockers: list[dict],
    mode: str,
) -> None:
    """Populate *signals* and *blockers* from import-graph + references + runtime."""
    target_id = target["id"]
    target_name = target.get("name", "")
    target_file = target.get("file", "")
    is_delete = mode == "delete"

    # ── Entry-point (delete only) ────────────────────────────────────────
    if is_delete:
        entry_signal = _detect_entry_point(target)
        signals.entry_signal = entry_signal
        if entry_signal:
            blockers.append(
                {
                    "kind": "entry_point",
                    "detail": entry_signal,
                    "severity": _SEVERITY_ENTRY_POINT,
                }
            )

    # ── File-level importers (cross_repo when requested) ─────────────────
    try:
        from .find_importers import find_importers  # noqa: PLC0415

        importers_out = find_importers(
            repo=f"{owner}/{name}",
            file_path=target_file,
            cross_repo=cross_repo,
            storage_path=storage_path,
        )
        for entry in importers_out.get("importers", []) or []:
            if entry.get("cross_repo"):
                signals.cross_repo_count += 1
                blockers.append(
                    {
                        "kind": "cross_repo_import",
                        "repo": entry.get("source_repo", ""),
                        "file": entry.get("file", ""),
                        "severity": _SEVERITY_CROSS_REPO,
                        "info": "another indexed repo depends on this"
                        if not is_delete
                        else None,
                    }
                )
            else:
                imp_file = entry.get("file", "")
                if imp_file and imp_file != target_file:
                    if _is_test_file(imp_file):
                        signals.test_import_count += 1
                        if is_delete:
                            blockers.append(
                                {
                                    "kind": "test_import",
                                    "file": imp_file,
                                    "severity": _SEVERITY_TEST_ONLY,
                                }
                            )
                    else:
                        signals.external_import_count += 1
                        blockers.append(
                            {
                                "kind": "external_import",
                                "file": imp_file,
                                "severity": _SEVERITY_EXTERNAL_IMPORT,
                                "info": "external caller depends on the current signature"
                                if not is_delete
                                else None,
                            }
                        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("check_safe: find_importers skipped: %s", exc, exc_info=True)

    # ── Identifier text refs ─────────────────────────────────────────────
    try:
        from .check_references import check_references  # noqa: PLC0415

        ref_out = check_references(
            repo=f"{owner}/{name}",
            identifiers=[target_name],
            search_content=True,
            max_content_results=20,
            storage_path=storage_path,
        )
        for entry in ref_out.get("results", []) or []:
            for ref in entry.get("content_references", []) or []:
                ref_file = ref.get("file", "")
                if not ref_file or ref_file == target_file:
                    continue
                if _is_test_file(ref_file):
                    signals.test_ref_count += 1
                    if is_delete and signals.test_ref_count <= 3:
                        blockers.append(
                            {
                                "kind": "test_reference",
                                "file": ref_file,
                                "line": ref.get("line", 0),
                                "severity": _SEVERITY_TEST_ONLY,
                            }
                        )
                else:
                    signals.internal_ref_count += 1
                    if signals.internal_ref_count <= 3:
                        blockers.append(
                            {
                                "kind": "internal_reference",
                                "file": ref_file,
                                "line": ref.get("line", 0),
                                "severity": _SEVERITY_INTERNAL_REF,
                                "info": "internal call site that may rely on current behavior"
                                if not is_delete
                                else None,
                            }
                        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("check_safe: check_references skipped: %s", exc, exc_info=True)

    # ── Test coverage flag ───────────────────────────────────────────────
    signals.has_test_coverage = (signals.test_import_count + signals.test_ref_count) > 0

    # ── Runtime evidence ─────────────────────────────────────────────────
    store = IndexStore(base_path=storage_path)
    if include_runtime:
        signals.runtime_hits = _runtime_hits(store, owner, name, target_id)
        signals.runtime_data_present = _runtime_data_present(store, owner, name)
        if signals.runtime_hits and signals.runtime_hits > 0:
            blockers.append(
                {
                    "kind": "runtime_observed",
                    "hit_count": signals.runtime_hits,
                    "severity": _SEVERITY_RUNTIME,
                    "info": "this symbol executes in production traffic"
                    if not is_delete
                    else None,
                }
            )


# ---------------------------------------------------------------------------
# Verdict logic — mode-specific
# ---------------------------------------------------------------------------


def _delete_verdict(
    signals: _Signals,
    dead_code_conf: float,
    runtime_data_present: bool,
    include_runtime: bool,
) -> tuple[str, float, str]:
    """Return (verdict, confidence, recommended_action) for delete mode."""
    total_test = signals.test_ref_count + signals.test_import_count

    if signals.runtime_hits and signals.runtime_hits > 0:
        verdict = "runtime_observed"
    elif signals.entry_signal:
        verdict = "entry_point"
    elif signals.cross_repo_count > 0:
        verdict = "cross_repo_blocking"
    elif signals.external_import_count > 0:
        verdict = "external_uses_blocking"
    elif signals.internal_ref_count > 0:
        verdict = "internal_uses_blocking"
    elif total_test > 0:
        verdict = "test_coverage_only"
    elif dead_code_conf >= 0.9:
        verdict = "safe_to_delete"
    elif (
        signals.internal_ref_count == 0
        and signals.external_import_count == 0
        and total_test == 0
    ):
        verdict = "safe_to_delete"
    else:
        verdict = "internal_only"

    # Confidence
    confidence = max(0.5, dead_code_conf)
    _CONF = {
        "safe_to_delete": lambda: max(
            confidence, 0.85 if dead_code_conf < 0.9 else 0.95
        ),
        "runtime_observed": lambda: 0.05,
        "cross_repo_blocking": lambda: 0.10,
        "entry_point": lambda: 0.20,
        "external_uses_blocking": lambda: 0.25,
        "internal_uses_blocking": lambda: 0.45,
        "test_coverage_only": lambda: 0.65,
        "internal_only": lambda: 0.55,
    }
    confidence = _CONF.get(verdict, lambda: confidence)()

    # Recommended action
    safe_action = "No callers, refs, or runtime hits found — deletion appears safe."
    if include_runtime and not runtime_data_present:
        safe_action = (
            "No callers or refs found. Static signals only — no runtime traces "
            "ingested for this repo, so production traffic was not consulted. "
            "Run `import_runtime_signal` against representative traffic to strengthen this verdict."
        )
    actions = {
        "safe_to_delete": safe_action,
        "test_coverage_only": "Only tests reference this symbol. Remove the tests alongside it.",
        "internal_only": "Refs exist only in the same file. Safe with local refactor.",
        "internal_uses_blocking": f"{signals.internal_ref_count} internal reference(s) found. Rename/refactor callers first.",
        "external_uses_blocking": f"{signals.external_import_count} other file(s) in this repo import this. Update importers first.",
        "cross_repo_blocking": f"{signals.cross_repo_count} other repo(s) in the suite depend on this. Coordinate a deprecation.",
        "runtime_observed": f"Runtime traces show {signals.runtime_hits} hits — this code runs in production. Investigate why static analysis missed the callers.",
        "entry_point": f"Entry-point indicator ({signals.entry_signal}) — invoked externally by framework/CLI/protocol. Never delete blindly; verify routing config.",
    }

    return verdict, confidence, actions.get(verdict, "Review blockers before deletion.")


def _edit_verdict(
    signals: _Signals,
    cyclomatic: int,
    runtime_hits: Optional[int],
    include_runtime: bool,
) -> tuple[str, float, str]:
    """Return (verdict, confidence, recommended_action) for edit mode."""
    high_complexity = cyclomatic >= _COMPLEXITY_HIGH
    is_referenced = (
        signals.external_import_count
        + signals.cross_repo_count
        + signals.internal_ref_count
    ) > 0
    signature_impact = (signals.external_import_count + signals.cross_repo_count) > 0

    if runtime_hits and runtime_hits > 0:
        verdict = "runtime_critical"
    elif signature_impact:
        verdict = "signature_impact"
    elif high_complexity:
        verdict = "complexity_risk"
    elif is_referenced and not signals.has_test_coverage:
        verdict = "untested"
    else:
        verdict = "safe_to_edit"

    _CONF = {
        "runtime_critical": 0.15,
        "signature_impact": 0.40,
        "complexity_risk": 0.45,
        "untested": 0.55,
        "safe_to_edit": 0.90,
    }
    confidence = _CONF[verdict]
    if verdict == "safe_to_edit" and signals.has_test_coverage:
        confidence = 0.95

    callers = signals.external_import_count + signals.cross_repo_count
    tests_note = (
        ""
        if signals.has_test_coverage
        else " No test coverage detected — add a characterization test first."
    )
    actions = {
        "runtime_critical": (
            f"Runs in production ({runtime_hits} runtime hit(s)). Edit behind a flag, keep "
            f"behavior backward-compatible, and watch monitoring.{tests_note}"
        ),
        "signature_impact": (
            f"{callers} external/cross-repo caller(s) depend on this. Body edits are safe; "
            f"preserve the signature and return contract, or update callers in lockstep.{tests_note}"
        ),
        "complexity_risk": (
            f"High cyclomatic complexity ({cyclomatic}). Edits are regression-prone — change in "
            f"small steps.{tests_note}"
        ),
        "untested": (
            f"Referenced by {signals.internal_ref_count} site(s) with no test coverage. Add a "
            "characterization test before editing to catch regressions."
        ),
        "safe_to_edit": (
            "Low complexity, no external callers — safe to edit."
            + (
                ""
                if signals.has_test_coverage
                else " (No tests reference it; consider adding one.)"
            )
        ),
    }

    return verdict, confidence, actions[verdict]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_safe(
    repo: str,
    symbol: str,
    mode: str = "delete",
    cross_repo: bool = True,
    include_runtime: bool = True,
    storage_path: Optional[str] = None,
) -> dict:
    """Composite preflight: can this symbol be deleted or edited safely?

    ``mode='delete'`` combines find_importers (cross-repo), check_references,
    get_dead_code_v2 confidence, runtime evidence, and entry-point heuristics
    into a single verdict + one-line recommended_action.

    ``mode='edit'`` fuses signature impact (external/cross-repo importers),
    cyclomatic complexity, test-coverage presence, and runtime traffic into a
    single verdict + one-line recommended_action.

    Read-only — never mutates the codebase.
    """
    start = time.perf_counter()

    if mode not in ("delete", "edit"):
        return {"error": f"invalid mode: {mode!r}. Must be 'delete' or 'edit'."}

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    target = _resolve_target(index, symbol)
    if target is None:
        return {"error": f"Symbol not found: {symbol}"}

    target_id = target["id"]
    target_name = target.get("name", "")
    target_file = target.get("file", "")
    cyclomatic = int(target.get("cyclomatic") or 0)

    # ── Gather signals ───────────────────────────────────────────────────
    signals = _Signals()
    blockers: list[dict] = []
    _gather_signals(
        signals,
        owner,
        name,
        target,
        cross_repo=cross_repo,
        include_runtime=include_runtime,
        storage_path=storage_path,
        blockers=blockers,
        mode=mode,
    )

    # ── Mode-specific verdict ─────────────────────────────────────────────
    if mode == "delete":
        dead_code_conf = _check_dead_code_conf(
            f"{owner}/{name}", target_id, storage_path
        )
        verdict, confidence, recommended_action = _delete_verdict(
            signals,
            dead_code_conf,
            signals.runtime_data_present,
            include_runtime,
        )
    else:
        # Edit mode: add complexity and untested blockers
        high_complexity = cyclomatic >= _COMPLEXITY_HIGH
        if high_complexity:
            blockers.append(
                {
                    "kind": "high_complexity",
                    "cyclomatic": cyclomatic,
                    "severity": _SEVERITY_COMPLEXITY,
                    "info": f"cyclomatic complexity {cyclomatic} (high) — edits are regression-prone",
                }
            )
        is_referenced = (
            signals.external_import_count
            + signals.cross_repo_count
            + signals.internal_ref_count
        ) > 0
        if is_referenced and not signals.has_test_coverage:
            blockers.append(
                {
                    "kind": "no_test_coverage",
                    "severity": _SEVERITY_UNTESTED,
                    "info": "symbol is used but no referencing test files were found",
                }
            )

        verdict, confidence, recommended_action = _edit_verdict(
            signals,
            cyclomatic,
            signals.runtime_hits,
            include_runtime,
        )

    # Rank blockers by severity, truncate to top 5
    blockers.sort(key=lambda b: -b.get("severity", 0))
    blockers_out = blockers[:5]

    # Token-savings ledger
    raw_bytes = int(target.get("byte_length", 0) or 0) + 1000
    response_bytes = 800
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="check_safe")

    elapsed = (time.perf_counter() - start) * 1000

    result: dict = {
        "verdict": verdict,
        "confidence": round(confidence, 2),
        "target": {
            "symbol_id": target_id,
            "name": target_name,
            "kind": target.get("kind", ""),
            "file": target_file,
            "line": target.get("line", 0),
        },
        "blockers": blockers_out,
        "recommended_action": recommended_action,
        "mode": mode,
        "signals": signals.raw(),
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }
    if mode == "delete":
        result["signals"]["dead_code_confidence"] = round(dead_code_conf, 3)
    if signals.runtime_hits is not None:
        result["signals"]["runtime_hits"] = signals.runtime_hits
    if include_runtime:
        result["signals"]["runtime_data_present"] = signals.runtime_data_present
    return result
