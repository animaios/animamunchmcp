"""get_doc_pr_risk_profile — composite risk assessment for a doc-set change (v1.63.0).

Mirrors jcm's ``get_pr_risk_profile`` and rounds out the Phase-2
sibling-parity work. Fuses five orthogonal signals over a set of changed
sections into a single 0-1 ``risk_score`` plus an overall
``risk_level`` (low / medium / high / critical) and a ranked top-5
list of contributing blockers.

Signals:

  1. **volume**             count of changed sections normalised by repo size
  2. **blast_radius**       mean blast_score across modified + deleted sections
                            (delegates to get_section_blast_radius)
  3. **backlink_burden**    total inbound references to changed sections,
                            normalised by section count
  4. **tutorial_disruption** fraction of changed sections sitting on
                            tutorial chains (Next/Prev / toctree)
  5. **role_weight**        % of changes hitting tutorial / reference roles
                            (high-stakes content) vs internal / notes

The caller supplies the change list — this tool does NOT diff anything
itself. Pair with ``get_recent_changes`` or a CI step that computes
the section_id set from a git diff.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from ..storage.doc_store import DocStore
from .get_backlinks import get_backlinks
from .get_section_blast_radius import get_section_blast_radius
from .get_tutorial_path import get_tutorial_path


# Weights chosen so each signal hits a plausible ceiling. Sum to 1.0.
_W_VOLUME = 0.15
_W_BLAST = 0.30
_W_BACKLINKS = 0.20
_W_TUTORIAL = 0.20
_W_ROLE = 0.15

_RISK_THRESHOLDS = {
    "low": 0.25,
    "medium": 0.50,
    "high": 0.75,
    # > 0.75 -> critical
}

_HIGH_STAKES_ROLES = {"tutorial", "reference", "concept", "guide"}

_VALID_KINDS = {"added", "modified", "deleted"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _risk_level(score: float) -> str:
    if score <= _RISK_THRESHOLDS["low"]:
        return "low"
    if score <= _RISK_THRESHOLDS["medium"]:
        return "medium"
    if score <= _RISK_THRESHOLDS["high"]:
        return "high"
    return "critical"


def _normalise_changes(changes: Iterable) -> list[dict]:
    """Normalise the input shape into [{section_id, kind}].

    Accepts:
      * a list of strings (section IDs; kind defaults to 'modified'); or
      * a list of {'section_id': ..., 'kind': ...} dicts.
    """
    out: list[dict] = []
    for entry in changes or []:
        if isinstance(entry, str):
            out.append({"section_id": entry, "kind": "modified"})
        elif isinstance(entry, dict) and "section_id" in entry:
            kind = entry.get("kind") or "modified"
            if kind not in _VALID_KINDS:
                kind = "modified"
            out.append({"section_id": entry["section_id"], "kind": kind})
    return out


def get_doc_pr_risk_profile(
    repo: str,
    changed_sections: list,
    storage_path: Optional[str] = None,
) -> dict:
    """Composite risk profile for a set of changed doc sections.

    Args:
        repo: Doc repo identifier (``owner/name`` or bare name resolving via DocStore).
        changed_sections: List of changed sections. Accepts either bare
            section IDs (str) or dicts ``{section_id, kind}`` where
            ``kind`` in {'added', 'modified', 'deleted'}. Bare strings
            default to ``modified``.
        storage_path: Custom doc-index root.

    Returns:
        ``{result: {risk_score, risk_level, signals, top_blockers,
        recommended_action, changes_evaluated}, _meta}``.
    """
    t0 = time.perf_counter()
    changes = _normalise_changes(changed_sections)
    if not changes:
        return {
            "error": "No changes supplied.",
            "reason": "no_changes",
            "hint": (
                "Pass changed_sections as a list of section IDs or "
                "{section_id, kind} dicts. Compute the list from a git diff "
                "or pair with get_recent_changes."
            ),
        }

    store = DocStore(base_path=storage_path)
    try:
        owner, name = store._resolve_repo(repo)
    except Exception:
        return {"error": f"Repo not found: {repo}", "reason": "repo_not_found"}
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not indexed: {repo}", "reason": "repo_not_indexed"}

    repo_full = f"{owner}/{name}"
    section_lookup = {s["id"]: s for s in index.sections}
    total_sections = len(index.sections) or 1

    # Direct-signal failures are surfaced in result.diagnostics instead of
    # being silently scored as zero-risk. A signal that cannot be computed
    # (section not found, downstream tool raised, or returned an unexpected
    # shape) is recorded here so an aggregate "low risk" can be told apart
    # from "we never actually measured it".
    signal_failures: list[dict] = []

    # ── Signal 1: volume ─────────────────────────────────────────────
    volume_raw = len(changes)
    volume_pct = volume_raw / total_sections
    # 0% -> 0, >=10% -> 1.0
    volume_score = _clamp01(volume_pct * 10.0)

    # ── Signal 2: blast_radius (modified + deleted only) ─────────────
    blast_targets = [
        c for c in changes if c["kind"] in ("modified", "deleted")
    ]
    blast_scores: list[tuple[str, float]] = []
    for c in blast_targets:
        sid = c["section_id"]
        try:
            br = get_section_blast_radius(
                repo=repo_full, section_id=sid, storage_path=storage_path
            )
        except Exception as exc:
            signal_failures.append({
                "signal": "blast_radius",
                "section_id": sid,
                "reason": f"get_section_blast_radius_raised: {type(exc).__name__}",
            })
            continue
        if isinstance(br, dict) and "result" in br:
            bs = float(br["result"].get("blast_score") or 0.0)
            blast_scores.append((sid, bs))
        else:
            signal_failures.append({
                "signal": "blast_radius",
                "section_id": sid,
                "reason": "unexpected_blast_shape",
            })
    if blast_scores:
        blast_score = sum(s for _, s in blast_scores) / len(blast_scores)
    else:
        blast_score = 0.0
    blast_score = _clamp01(blast_score)

    # ── Signal 3: backlink_burden ────────────────────────────────────
    # get_backlinks is document-level: given a target doc_path it returns
    # every inbound link to the whole document. We therefore resolve each
    # changed section to its doc_path (get_backlinks takes doc_path, NOT
    # section_id) and query once per unique document, caching the result so
    # several changed sections in the same doc don't re-query. The per-section
    # count is kept for blocker surfacing, but the aggregate burden counts
    # each unique document once so co-located changes don't inflate it.
    backlink_totals: list[tuple[str, int]] = []
    backlink_doc_cache: dict[str, int] = {}
    counted_docs: set[str] = set()
    total_backlinks = 0
    for c in changes:
        sid = c["section_id"]
        if c["kind"] == "added":
            continue  # newly added sections cannot have inbound refs yet
        sec = section_lookup.get(sid)
        doc_path = (sec or {}).get("doc_path") or ""
        if not doc_path:
            signal_failures.append({
                "signal": "backlink_burden",
                "section_id": sid,
                "reason": "section_not_found" if sec is None else "no_doc_path",
            })
            continue
        if doc_path in backlink_doc_cache:
            cnt = backlink_doc_cache[doc_path]
        else:
            try:
                bl = get_backlinks(
                    repo=repo_full, doc_path=doc_path, storage_path=storage_path
                )
            except Exception as exc:
                signal_failures.append({
                    "signal": "backlink_burden",
                    "section_id": sid,
                    "doc_path": doc_path,
                    "reason": f"get_backlinks_raised: {type(exc).__name__}",
                })
                continue
            if not (isinstance(bl, dict) and "result" in bl):
                signal_failures.append({
                    "signal": "backlink_burden",
                    "section_id": sid,
                    "doc_path": doc_path,
                    "reason": "unexpected_backlinks_shape",
                })
                continue
            cnt = int(bl["result"].get("backlink_count") or 0)
            backlink_doc_cache[doc_path] = cnt
        backlink_totals.append((sid, cnt))
        if doc_path not in counted_docs:
            counted_docs.add(doc_path)
            total_backlinks += cnt
    # 0 backlinks -> 0; >=5 per changed section average -> 1.0
    avg_backlinks = total_backlinks / max(1, len(changes))
    backlink_score = _clamp01(avg_backlinks / 5.0)

    # ── Signal 4: tutorial_disruption ────────────────────────────────
    tutorial_hits: list[str] = []
    for c in changes:
        sid = c["section_id"]
        try:
            tp = get_tutorial_path(
                repo=repo_full, section_id=sid, storage_path=storage_path
            )
        except Exception as exc:
            signal_failures.append({
                "signal": "tutorial_disruption",
                "section_id": sid,
                "reason": f"get_tutorial_path_raised: {type(exc).__name__}",
            })
            continue
        if not isinstance(tp, dict):
            signal_failures.append({
                "signal": "tutorial_disruption",
                "section_id": sid,
                "reason": "unexpected_tutorial_shape",
            })
            continue
        if "error" in tp:
            signal_failures.append({
                "signal": "tutorial_disruption",
                "section_id": sid,
                "reason": f"get_tutorial_path_error: {tp.get('error')}",
            })
            continue
        # get_tutorial_path returns `chain` at the TOP level (not under a
        # "result" wrapper). The prior `tp["result"]["chain"]` guard never
        # matched, so this signal silently scored zero on every call.
        chain = tp.get("chain") or []
        if len(chain) > 1:
            tutorial_hits.append(sid)
    tutorial_score = _clamp01(len(tutorial_hits) / max(1, len(changes)))

    # ── Signal 5: role_weight ────────────────────────────────────────
    high_stakes_hits = 0
    role_breakdown: dict[str, int] = {}
    for c in changes:
        sec = section_lookup.get(c["section_id"])
        if not sec:
            continue
        role = ((sec.get("metadata") or {}).get("role") or "unknown").lower()
        role_breakdown[role] = role_breakdown.get(role, 0) + 1
        if role in _HIGH_STAKES_ROLES:
            high_stakes_hits += 1
    role_score = _clamp01(high_stakes_hits / max(1, len(changes)))

    # ── Composite ────────────────────────────────────────────────────
    composite = (
        _W_VOLUME * volume_score
        + _W_BLAST * blast_score
        + _W_BACKLINKS * backlink_score
        + _W_TUTORIAL * tutorial_score
        + _W_ROLE * role_score
    )
    composite = _clamp01(composite)
    level = _risk_level(composite)

    # ── Top blockers ─────────────────────────────────────────────────
    blockers: list[dict] = []

    top_blast = sorted(blast_scores, key=lambda x: x[1], reverse=True)[:3]
    for sid, bs in top_blast:
        if bs >= 0.3:
            blockers.append({
                "kind": "blast_radius",
                "section_id": sid,
                "score": round(bs, 3),
                "detail": f"Downstream impact reaches blast_score={bs:.2f}",
            })

    top_backlinks = sorted(backlink_totals, key=lambda x: x[1], reverse=True)[:3]
    for sid, cnt in top_backlinks:
        if cnt >= 3:
            blockers.append({
                "kind": "backlinks",
                "section_id": sid,
                "score": cnt,
                "detail": f"{cnt} inbound references; edits propagate",
            })

    for sid in tutorial_hits[:3]:
        blockers.append({
            "kind": "tutorial_path",
            "section_id": sid,
            "detail": "Section sits on a Next/Prev or toctree tutorial chain",
        })

    # Rank: blast > backlinks > tutorial; keep top 5.
    rank_order = {"blast_radius": 0, "backlinks": 1, "tutorial_path": 2}
    blockers.sort(key=lambda b: (rank_order.get(b["kind"], 9),
                                 -float(b.get("score") or 0)))
    blockers = blockers[:5]

    # ── Recommended action ───────────────────────────────────────────
    if level == "critical":
        action = (
            "CRITICAL doc PR risk. Multiple high-impact sections changed — "
            "request a doc-team review, run a fresh `data_health_radar` "
            "(or `doc_health_radar`) on base vs. branch, and verify no "
            "tutorial chain is broken before merging."
        )
    elif level == "high":
        action = (
            "HIGH doc PR risk. Verify tutorial paths and run "
            "diff_doc_health_radar against the baseline before merging."
        )
    elif level == "medium":
        action = (
            "Moderate doc PR risk. Spot-check the top blockers and verify "
            "any orphaned references were either updated or intentionally "
            "removed."
        )
    else:
        action = "Low-risk doc PR. Standard review applies."

    return {
        "result": {
            "repo": repo_full,
            "risk_score": round(composite, 3),
            "risk_level": level,
            "signals": {
                "volume": round(volume_score, 3),
                "blast_radius": round(blast_score, 3),
                "backlink_burden": round(backlink_score, 3),
                "tutorial_disruption": round(tutorial_score, 3),
                "role_weight": round(role_score, 3),
            },
            "signal_details": {
                "changes_evaluated": len(changes),
                "total_sections_in_repo": total_sections,
                "total_backlinks_on_changed": total_backlinks,
                "tutorial_chain_sections": len(tutorial_hits),
                "high_stakes_role_hits": high_stakes_hits,
                "role_breakdown": role_breakdown,
            },
            "top_blockers": blockers,
            "recommended_action": action,
            "diagnostics": {
                "signal_failures": signal_failures,
            },
        },
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "signal_failure_count": len(signal_failures),
        },
    }
