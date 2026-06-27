"""Six-axis doc-health radar + diff helper (v1.62.0).

Normalises the signals `get_doc_health` already aggregates into 0-100
scores per axis plus a composite + letter grade. Same shape as jcm's
`health_radar.py` and jData's `data_health_radar.py` — the third leg of
the suite-wide radar pattern.

| Axis                | Source                                              |
|---------------------|-----------------------------------------------------|
| freshness           | fresh / (fresh + edited + stale) x 100              |
| link_integrity      | penalty per broken link relative to section count   |
| orphan_health       | penalty per orphan section                          |
| embedding_coverage  | embedded sections / total sections x 100            |
| role_coverage       | % sections with non-unknown role                    |
| drift_health        | canary clean -> 100; alarm -> 0; no canary -> omit  |

`drift_health` is omitted when no embedding-drift canary has been
captured. Mirrors the jcm convention so the composite stays comparable
across repos with different setup states.
"""

from __future__ import annotations

from typing import Optional


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _score_freshness(fresh: int, edited: int, stale: int) -> Optional[float]:
    total = fresh + edited + stale
    if total <= 0:
        return None
    return _clamp(100.0 * fresh / total)


def _score_link_integrity(broken_links: int, section_count: int) -> float:
    """0 broken -> 100; 10% broken -> 0. Linear in between."""
    if section_count <= 0:
        return 100.0
    ratio = broken_links / section_count
    return _clamp(100.0 - 1000.0 * ratio)


def _score_structural_integrity(structural_warnings: int, section_count: int) -> float:
    """0 warnings -> 100; 10% of sections warned -> 0 (#54). Structural damage
    (fence accidents swallowing sections, level skips) is severe, so it uses the
    same steep slope as link_integrity."""
    if section_count <= 0:
        return 100.0
    ratio = structural_warnings / section_count
    return _clamp(100.0 - 1000.0 * ratio)


def _score_orphan_health(orphan_count: int, section_count: int) -> float:
    """0% orphans -> 100; 50% orphans -> 0."""
    if section_count <= 0:
        return 100.0
    pct = 100.0 * orphan_count / section_count
    return _clamp(100.0 - 2.0 * pct)


def _score_embedding_coverage(embedded: int, section_count: int) -> float:
    if section_count <= 0:
        return 100.0
    return _clamp(100.0 * embedded / section_count)


def _score_role_coverage(role_distribution: dict[str, int], section_count: int) -> float:
    if section_count <= 0:
        return 100.0
    unknown = role_distribution.get("unknown", 0)
    return _clamp(100.0 * (section_count - unknown) / section_count)


def _score_drift_health(has_canary: bool, alarm: Optional[bool]) -> Optional[float]:
    if not has_canary or alarm is None:
        return None
    return 0.0 if alarm else 100.0


def _letter_grade(composite: float) -> str:
    if composite >= 90:
        return "A"
    if composite >= 80:
        return "B"
    if composite >= 70:
        return "C"
    if composite >= 60:
        return "D"
    return "F"


def compute_radar(
    *,
    fresh: int,
    edited: int,
    stale: int,
    broken_links: int,
    orphan_count: int,
    embedded_sections: int,
    section_count: int,
    role_distribution: dict[str, int],
    has_canary: bool = False,
    drift_alarm: Optional[bool] = None,
    structural_warnings: int = 0,
) -> dict:
    """Compute the six-axis radar from raw doc signals.

    Args:
        fresh / edited / stale: Freshness counts from FreshnessProbe.
        broken_links: Count from get_broken_links. Pass 0 (or known-bad
            value) when delegate fails — radar will omit the axis only
            when ``section_count == 0``.
        orphan_count: Count from get_orphan_sections.
        embedded_sections: Count of sections with embedding vectors.
        section_count: Total indexed sections.
        role_distribution: Counts per metadata.role (with 'unknown' for
            sections missing the field).
        has_canary: Whether an embedding-drift canary has been captured.
        drift_alarm: True when current drift exceeds threshold.

    Returns:
        ``{axes, composite, grade, omitted_axes}``.
    """
    axes: dict[str, dict] = {
        "link_integrity": {
            "score": _score_link_integrity(broken_links, section_count),
            "raw_broken": broken_links,
            "raw_sections": section_count,
        },
        "orphan_health": {
            "score": _score_orphan_health(orphan_count, section_count),
            "raw_orphans": orphan_count,
        },
        "embedding_coverage": {
            "score": _score_embedding_coverage(embedded_sections, section_count),
            "raw_embedded": embedded_sections,
        },
        "role_coverage": {
            "score": _score_role_coverage(role_distribution, section_count),
            "raw_unknown": role_distribution.get("unknown", 0),
        },
        "structural_integrity": {
            "score": _score_structural_integrity(structural_warnings, section_count),
            "raw_warnings": structural_warnings,
        },
    }

    omitted: list[str] = []

    freshness_score = _score_freshness(fresh, edited, stale)
    if freshness_score is not None:
        axes["freshness"] = {
            "score": freshness_score,
            "raw_fresh": fresh,
            "raw_edited": edited,
            "raw_stale": stale,
        }
    else:
        omitted.append("freshness")

    drift_score = _score_drift_health(has_canary, drift_alarm)
    if drift_score is not None:
        axes["drift_health"] = {"score": drift_score, "raw_alarm": drift_alarm}
    else:
        omitted.append("drift_health")

    scored_values = [a["score"] for a in axes.values()]
    composite = round(sum(scored_values) / len(scored_values), 1) if scored_values else 0.0

    return {
        "axes": axes,
        "composite": composite,
        "grade": _letter_grade(composite),
        "omitted_axes": omitted,
    }


def diff_radar(baseline: dict, current: dict) -> dict:
    """Axis-by-axis deltas between two radar payloads. Pure function."""
    threshold = 3.0
    out_axes: dict[str, dict] = {}
    regressions: list[str] = []
    improvements: list[str] = []

    base_axes = baseline.get("axes", {})
    cur_axes = current.get("axes", {})
    all_axis_names = sorted(set(base_axes.keys()) | set(cur_axes.keys()))

    for axis in all_axis_names:
        b = base_axes.get(axis, {}) or {}
        c = cur_axes.get(axis, {}) or {}
        b_score = b.get("score")
        c_score = c.get("score")
        if b_score is None or c_score is None:
            out_axes[axis] = {
                "from": b_score,
                "to": c_score,
                "delta": None,
                "note": "axis missing from one side",
            }
            continue
        delta = round(c_score - b_score, 1)
        out_axes[axis] = {"from": b_score, "to": c_score, "delta": delta}
        if delta <= -threshold:
            regressions.append(axis)
        elif delta >= threshold:
            improvements.append(axis)

    base_composite = baseline.get("composite", 0.0)
    cur_composite = current.get("composite", 0.0)
    composite_delta = round(cur_composite - base_composite, 1)

    base_grade = baseline.get("grade", "?")
    cur_grade = current.get("grade", "?")
    grade_change = (
        f"{base_grade} -> {cur_grade}" if base_grade != cur_grade else f"{cur_grade} (unchanged)"
    )

    return {
        "axis_deltas": out_axes,
        "composite_from": base_composite,
        "composite_to": cur_composite,
        "composite_delta": composite_delta,
        "grade_change": grade_change,
        "regressions": regressions,
        "improvements": improvements,
        "verdict": _verdict(composite_delta, regressions, improvements),
    }


def _verdict(composite_delta: float, regressions: list[str], improvements: list[str]) -> str:
    if abs(composite_delta) < 1.0 and not regressions and not improvements:
        return "no meaningful change"
    if regressions and not improvements:
        return f"REGRESSION on {len(regressions)} axis/axes (composite {composite_delta:+.1f})"
    if improvements and not regressions:
        return f"improvement on {len(improvements)} axis/axes (composite {composite_delta:+.1f})"
    if regressions and improvements:
        return f"mixed: -{len(regressions)} / +{len(improvements)} axes (composite {composite_delta:+.1f})"
    return f"composite {composite_delta:+.1f}"


def diff_doc_health_radar(baseline: dict, current: dict) -> dict:
    """MCP tool entry point: takes two radar payloads, returns the diff."""
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        return {
            "error": (
                "diff_doc_health_radar requires two radar payload dicts. "
                "Pass the `radar` field from doc_health_radar responses."
            )
        }
    if "axes" not in baseline or "axes" not in current:
        return {
            "error": (
                "Both inputs must be radar payloads (need an `axes` field). "
                "Did you pass the full doc_health_radar response instead of its `radar` sub-field?"
            )
        }
    return diff_radar(baseline, current)
