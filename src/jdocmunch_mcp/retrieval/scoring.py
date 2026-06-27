"""Per-result heuristic scores: answerability + quotability (v1.33.0).

Both scores are 0–1 floats attached to each retrieval result so an agent
can decide:

  - **answerability** — "does this section actually contain an answer
    to the query, or is it just a keyword match?"
  - **quotability** — "if I quote from this section, will it stand on
    its own, or does it rely on context from elsewhere?"

Pure-Python heuristics — no AI calls, no external dependencies. The
density signals (imperative verbs, code fences, definition syntax,
numbered lists, "see above" phrases) are deliberately conservative.
False positives are worse than false negatives for these scores: an
agent that sees a low score should consider expanding the search;
seeing a high score should not be trusted blindly. Agents should
combine these scores with v1.16 retrieval confidence.

Both scores expose ``_components`` so callers can inspect what drove
the score, the same pattern v1.16 confidence uses.
"""

from __future__ import annotations

import math
import re

# ---------------------------------------------------------------------------
# Shared regexes — compile once.
# ---------------------------------------------------------------------------

_IMPERATIVE_RE = re.compile(
    r"^\s*(?:install|configure|set|run|create|update|delete|deploy|build|"
    r"enable|disable|add|remove|verify|check|open|close|copy|paste|click|"
    r"select|navigate|generate|sign|register|connect|export|import|use|"
    r"call|invoke|pass|return|ensure|launch|start|stop|restart)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
_DEFINITION_RE = re.compile(
    r"\b(?:is|are|means|refers to|stands for|defined as|denotes|represents)\b",
    re.IGNORECASE,
)
_BOLD_TERM_RE = re.compile(r"\*\*([A-Za-z][A-Za-z0-9 _\-./]{1,80})\*\*\s*[—–\-:]\s*\S")
_QUESTION_RE = re.compile(r"\?\s*$", re.MULTILINE)
_SEE_ABOVE_RE = re.compile(
    r"\b(?:see (?:above|below|the previous section|earlier|the next section)|"
    r"as (?:mentioned|noted|described|shown) (?:above|earlier|previously)|"
    r"as discussed (?:above|earlier))\b",
    re.IGNORECASE,
)
_BARE_CODE_REF_RE = re.compile(r"```", re.MULTILINE)

# Guardrails so a 10-token section doesn't dominate density math.
_MIN_LINES_FOR_DENSITY = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _safe_div(num: float, den: float) -> float:
    return float(num) / den if den > 0 else 0.0


def _saturate(x: float, half: float) -> float:
    """Map a non-negative count to [0, 1) saturating at ``half``."""
    if x <= 0:
        return 0.0
    return 1.0 - math.exp(-x / max(0.001, float(half)))


# ---------------------------------------------------------------------------
# Answerability
# ---------------------------------------------------------------------------

ANSWERABILITY_WEIGHTS = {
    "definition": 0.30,
    "imperative": 0.25,
    "code_block": 0.20,
    "numbered_steps": 0.15,
    "faq_question": 0.10,
}


def compute_answerability(text: str, query: str = "") -> dict:
    """Return ``{value, components}`` ∈ [0, 1].

    Components:
      - definition  — definition phrases ("X is …") or **Term** —
        markdown patterns.
      - imperative  — imperative-verb-led lines (how-to / tutorial signal).
      - code_block  — at least one fenced code block — code is often the
        actual answer in dev docs.
      - numbered_steps — ordered procedural lists.
      - faq_question — line-end question marks (FAQ-style sections).

    Each component contributes its weight times a saturating function of
    the count. Linear-weighted (additive) — a section that hits two of
    five signals shouldn't be artificially capped.
    """
    if not text:
        return {
            "value": 0.0,
            "components": {k: 0.0 for k in ANSWERABILITY_WEIGHTS},
        }

    lines = max(_MIN_LINES_FOR_DENSITY, _line_count(text))
    code_pairs = len(_CODE_FENCE_RE.findall(text)) // 2
    imperatives = len(_IMPERATIVE_RE.findall(text))
    numbered = len(_NUMBERED_STEP_RE.findall(text))
    questions = len(_QUESTION_RE.findall(text))
    defs = len(_DEFINITION_RE.findall(text)) + len(_BOLD_TERM_RE.findall(text))

    components = {
        "definition": _saturate(defs, half=2.0),
        "imperative": _saturate(imperatives, half=3.0),
        "code_block": 1.0 if code_pairs >= 1 else 0.0,
        "numbered_steps": _saturate(numbered, half=3.0),
        "faq_question": _saturate(questions, half=2.0),
    }

    value = 0.0
    for k, w in ANSWERABILITY_WEIGHTS.items():
        value += w * float(components[k])
    value = round(max(0.0, min(1.0, value)), 4)

    return {
        "value": value,
        "components": {k: round(v, 4) for k, v in components.items()},
    }


# ---------------------------------------------------------------------------
# Quotability
# ---------------------------------------------------------------------------

QUOTABILITY_WEIGHTS = {
    "intro_density": 0.35,   # has prose, not just code
    "definition_density": 0.25,
    "self_contained": 0.30,  # not riddled with "see above" / "as noted"
    "length": 0.10,          # very short sections rarely quotable
}


def compute_quotability(text: str) -> dict:
    """Return ``{value, components}`` ∈ [0, 1].

    Heuristics:
      - intro_density   — ratio of prose lines to code-block lines.
        High-prose-low-code sections quote well; pure-code sections rely
        on context the agent doesn't get.
      - definition_density — frequency of definition-style sentences;
        these are the most "quotable" units in technical docs.
      - self_contained  — penalty for "see above" / "as noted earlier"
        phrases. Higher score = fewer such references.
      - length          — saturating function of section length; very
        short sections (1–2 lines) are rarely quotable.
    """
    if not text:
        return {
            "value": 0.0,
            "components": {k: 0.0 for k in QUOTABILITY_WEIGHTS},
        }

    lines = max(_MIN_LINES_FOR_DENSITY, _line_count(text))
    # Approximate prose vs code: each fenced block is roughly 5 lines (typical).
    code_pairs = len(_CODE_FENCE_RE.findall(text)) // 2
    code_lines_estimate = code_pairs * 5
    prose_lines = max(0, _line_count(text) - code_lines_estimate)
    intro_density = _safe_div(prose_lines, lines)

    defs = len(_DEFINITION_RE.findall(text)) + len(_BOLD_TERM_RE.findall(text))
    definition_density = _saturate(defs, half=2.0)

    see_above_count = len(_SEE_ABOVE_RE.findall(text))
    # Self-contained = 1.0 with zero references; saturating penalty.
    self_contained = max(0.0, 1.0 - _saturate(see_above_count, half=1.5))

    length_score = _saturate(_line_count(text), half=8.0)

    components = {
        "intro_density": round(min(1.0, intro_density), 4),
        "definition_density": round(definition_density, 4),
        "self_contained": round(self_contained, 4),
        "length": round(length_score, 4),
    }

    value = 0.0
    for k, w in QUOTABILITY_WEIGHTS.items():
        value += w * float(components[k])
    value = round(max(0.0, min(1.0, value)), 4)

    return {
        "value": value,
        "components": components,
    }


# ---------------------------------------------------------------------------
# Attach helper — mutates a single result dict in place
# ---------------------------------------------------------------------------

def attach_scores(
    result: dict,
    *,
    text_loader=None,
    query: str = "",
    include_components: bool = False,
) -> None:
    """Mutate ``result`` with answerability + quotability scores.

    ``text_loader`` is a callable returning the section text for this
    result; required because the result dict often lacks inline content
    (Section.to_dict drops it). Falls back to result.get('content','')
    when loader is None.
    """
    text = result.get("content") or ""
    if not text and text_loader is not None:
        try:
            text = text_loader(result) or ""
        except Exception:
            text = ""

    ans = compute_answerability(text, query=query)
    quo = compute_quotability(text)

    result["_answerability"] = ans["value"]
    result["_quotability"] = quo["value"]
    if include_components:
        result["_answerability_components"] = ans["components"]
        result["_quotability_components"] = quo["components"]
