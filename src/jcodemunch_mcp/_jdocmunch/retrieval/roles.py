"""Section role classifier (v1.19.0).

Lightweight, deterministic, AI-optional. Classifies each section into a
small fixed enum so agents can request "show me only troubleshooting near
'connection refused'" or "tutorial sections explaining auth tokens."

Roles:

  concept         conceptual / explanatory text (default fallback)
  tutorial        step-by-step walkthroughs ("Tutorial: …", "Quickstart")
  how_to          task-oriented ("How to …")
  reference       API / config reference material ("Reference", "API")
  api             explicit API doc sections (often overlaps reference)
  example         code-heavy example sections ("Example", "Examples")
  troubleshooting error / problem-solving ("Troubleshooting", "Errors", "FAQ")
  changelog       release notes / history
  faq             question-and-answer
  other           explicit "other / misc" markers

Strategy: heading regex first; if no match, density heuristics over the
section body (code-block ratio, imperative-verb count, ordered-list
density, "Q:" / question-mark density). When all signals are weak the
section keeps role="concept" (the conservative default).

The classifier is pure-Python and side-effect-free. AI fallback is left as
a follow-up — heuristics turn out to cover ~85% of real docs.
"""

from __future__ import annotations

import re

ROLES = (
    "concept",
    "tutorial",
    "how_to",
    "reference",
    "api",
    "example",
    "troubleshooting",
    "changelog",
    "faq",
    "other",
)

# Heading-text regex per role. Matched case-insensitively against the
# section title (and against ancestor titles in roles_for() below).
_HEADING_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("changelog", re.compile(r"^(?:change\s*log|release\s*notes?|history|what'?s\s+new|news)$", re.IGNORECASE)),
    ("faq", re.compile(r"^(?:faq|frequently\s+asked\s+questions?|questions?\s*&?\s*answers?|q\s*&\s*a)$", re.IGNORECASE)),
    ("troubleshooting", re.compile(r"\b(?:troubleshoot(?:ing)?|errors?|debug(?:ging)?|fix(?:es)?|known\s+issues?|problems?)\b", re.IGNORECASE)),
    ("tutorial", re.compile(r"\b(?:tutorial|walk\s*through|quick\s*start|getting\s+started|hello\s+world|step\s+by\s+step)\b", re.IGNORECASE)),
    ("how_to", re.compile(r"^how[\s-]+to\b", re.IGNORECASE)),
    ("api", re.compile(r"\b(?:api|endpoint|operation|sdk\s+reference|http\s+reference)\b", re.IGNORECASE)),
    ("reference", re.compile(r"^(?:reference|cli|configuration|config|options?|parameters?|env(?:ironment)?\s+variables?)\b", re.IGNORECASE)),
    ("example", re.compile(r"\b(?:examples?|usage(?:\s+example)?|sample\s*(?:code)?|recipes?)\b", re.IGNORECASE)),
    ("other", re.compile(r"^(?:misc|miscellaneous|other|notes?|appendix)\b", re.IGNORECASE)),
]

# Imperative-mood verb starters, common in how-to + tutorial bodies.
_IMPERATIVE_RE = re.compile(
    r"^\s*(?:install|configure|set|run|create|update|delete|deploy|build|enable|disable|add|remove|verify|check|"
    r"open|close|copy|paste|click|select|navigate|generate|sign|register|connect|export|import)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
_QUESTION_RE = re.compile(r"\?\s*$", re.MULTILINE)


def _heading_role(text: str) -> str:
    """Return the first matching role for the heading text, or '' if none."""
    if not text:
        return ""
    text = text.strip()
    for role, pat in _HEADING_PATTERNS:
        if pat.search(text):
            return role
    return ""


def _density_role(content: str) -> str:
    """Return a role inferred from body-text density signals.

    Returns ``""`` (caller defaults to "concept") when all signals are weak.
    """
    if not content:
        return ""
    n_lines = max(1, content.count("\n") + 1)
    code_fences = len(_CODE_FENCE_RE.findall(content))
    code_block_pairs = code_fences // 2
    numbered_steps = len(_NUMBERED_STEP_RE.findall(content))
    imperatives = len(_IMPERATIVE_RE.findall(content))
    questions = len(_QUESTION_RE.findall(content))

    # FAQ: many lines end in '?'
    if questions >= 3 and questions / n_lines > 0.05:
        return "faq"

    # Tutorial: numbered steps, multiple code blocks.
    if numbered_steps >= 3 and code_block_pairs >= 2:
        return "tutorial"

    # How-to: imperative verbs leading paragraphs, fewer steps than tutorial.
    if imperatives >= 3 and imperatives / n_lines > 0.05:
        return "how_to"

    # Example: dominated by code blocks, little prose.
    if code_block_pairs >= 1 and code_block_pairs / max(1, n_lines / 10) >= 1.0:
        return "example"

    return ""


def classify_section(title: str, content: str, ancestor_titles: list[str] = None) -> tuple[str, str]:
    """Return ``(role, source)`` where source is "heading" / "ancestor" / "density" / "default"."""
    role = _heading_role(title)
    if role:
        return role, "heading"

    for anc in (ancestor_titles or []):
        role = _heading_role(anc)
        if role:
            return role, "ancestor"

    role = _density_role(content)
    if role:
        return role, "density"

    return "concept", "default"


def annotate_sections(sections: list) -> list:
    """Walk a list of Section dataclasses and stamp metadata['role'].

    Recovers ancestor titles by walking the parent chain through the
    section list (which is in document order with parent_id wired).
    Idempotent — overwrites any pre-existing role.
    """
    by_id: dict = {s.id: s for s in sections if hasattr(s, "id")}

    for sec in sections:
        ancestors: list[str] = []
        cur_parent = getattr(sec, "parent_id", "") or ""
        guard = 0
        while cur_parent and guard < 16:
            parent = by_id.get(cur_parent)
            if not parent:
                break
            ancestors.append(getattr(parent, "title", "") or "")
            cur_parent = getattr(parent, "parent_id", "") or ""
            guard += 1

        role, source = classify_section(
            getattr(sec, "title", "") or "",
            getattr(sec, "content", "") or "",
            ancestor_titles=ancestors,
        )
        if not hasattr(sec, "metadata") or sec.metadata is None:
            sec.metadata = {}
        sec.metadata["role"] = role
        sec.metadata["role_source"] = source

    return sections
