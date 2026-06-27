"""Retrieval confidence score (v1.16.0).

Returns a 0–1 number that an agent can use to decide:

- top1 ≈ 1.0  → trust the top result; no need to read more.
- top1 ≈ 0.6  → top result is the best of several similar candidates.
- top1 < 0.4  → query is ambiguous or the index doesn't cover the topic.

Weighted geometric mean of four sub-signals:

- **gap** (0.35): ``(top1 - top2) / top1`` — bigger gap = more decisive.
- **strength** (0.35): ``1 - exp(-top1 / 4)`` — saturates around top1=12.
- **identity** (0.15): exact title match in top-3 → 1.0; else 0.7 (no penalty).
- **freshness** (0.15): 1.0 fresh / 0.6 stale (any non-fresh in top-3).

Geometric mean (rather than arithmetic) means a near-zero sub-signal
drags the whole score down — appropriate for "is this trustworthy."
"""

from __future__ import annotations

import math

WEIGHTS = {
    "gap": 0.35,
    "strength": 0.35,
    "identity": 0.15,
    "freshness": 0.15,
}


def _gap(top1: float, top2: float) -> float:
    if top1 <= 0:
        return 0.0
    return max(0.0, min(1.0, (top1 - top2) / top1))


def _strength(top1: float) -> float:
    if top1 <= 0:
        return 0.0
    return 1.0 - math.exp(-top1 / 4.0)


def _identity(query: str, results: list) -> float:
    """Return 1.0 if any of the top-3 has an exact (case-insensitive)
    title match for the query string. Otherwise 0.7 — no penalty for
    legitimate paraphrase matches."""
    q = (query or "").strip().lower()
    if not q:
        return 0.7
    for sec in results[:3]:
        if (sec.get("title", "") or "").strip().lower() == q:
            return 1.0
    return 0.7


def _freshness(results: list) -> float:
    """0.6 when any non-fresh marker appears in top-3, else 1.0."""
    for sec in results[:3]:
        bucket = sec.get("_freshness")
        if bucket and bucket != "fresh":
            return 0.6
    return 1.0


def compute_confidence(
    query: str,
    results: list,
    score_field: str = "_score",
) -> dict:
    """Return ``{value, components}`` ∈ [0, 1].

    ``score_field`` is the per-result score key (defaults to ``_score`` —
    the BM25/RRF value the search code attaches before confidence runs).
    """
    if not results:
        return {
            "value": 0.0,
            "components": {"gap": 0.0, "strength": 0.0, "identity": 0.7, "freshness": 1.0},
        }

    scores = []
    for sec in results[:5]:
        s = sec.get(score_field)
        if isinstance(s, (int, float)):
            scores.append(float(s))
    top1 = scores[0] if len(scores) >= 1 else 0.0
    top2 = scores[1] if len(scores) >= 2 else 0.0

    components = {
        "gap": _gap(top1, top2),
        "strength": _strength(top1),
        "identity": _identity(query, results),
        "freshness": _freshness(results),
    }

    # Weighted geometric mean. Floor sub-signals at 1e-6 so log isn't
    # negative-infinity when a sub-signal is exactly zero.
    log_sum = 0.0
    for key, val in components.items():
        v = max(1e-6, min(1.0, float(val)))
        log_sum += WEIGHTS[key] * math.log(v)
    value = math.exp(log_sum)
    return {"value": round(max(0.0, min(1.0, value)), 4), "components": {k: round(v, 4) for k, v in components.items()}}


def attach_confidence(
    query: str,
    results: list,
    meta: dict,
    *,
    include_components: bool = False,
    score_field: str = "_score",
) -> None:
    """Mutate ``meta`` in place with ``confidence`` (and optionally
    ``confidence_components``)."""
    out = compute_confidence(query, results, score_field=score_field)
    meta["confidence"] = out["value"]
    if include_components:
        meta["confidence_components"] = out["components"]
