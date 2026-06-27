"""Three-tier summarization for doc sections: heading > AI > title fallback."""

import logging
import os
import re
from typing import Optional

from ..parser.sections import Section

_SUMMARY_LINE_RE = re.compile(r"^(\d+)\.\s+(.+)")
logger = logging.getLogger(__name__)


def _build_prompt(sections: list) -> str:
    lines = [
        "Summarize each documentation section in ONE short sentence (max 15 words).",
        "Focus on what the section covers.",
        "",
        "Input:",
    ]
    for i, sec in enumerate(sections, 1):
        snippet = sec.content[:200].replace("\n", " ")
        lines.append(f"{i}. [{sec.title}] {snippet}")
    lines.extend([
        "",
        "Output format: NUMBER. SUMMARY",
        "Example: 1. Explains how to install the package via pip.",
        "",
        "Summaries:",
    ])
    return "\n".join(lines)


def _parse_response(text: str, expected_count: int) -> list:
    """Parse numbered summary lines from AI response."""
    summaries = [""] * expected_count
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _SUMMARY_LINE_RE.match(line)
        if m:
            num = int(m.group(1))
            if 1 <= num <= expected_count:
                summaries[num - 1] = m.group(2).strip()
    return summaries


def heading_summary(section: Section) -> str:
    """Tier 1: Use heading text as a natural summary (free, deterministic).

    For sections whose title is descriptive, the heading IS the summary.
    Returns up to 120 chars of the title.
    """
    return section.title[:120]


def title_fallback(section: Section) -> str:
    """Tier 3: Generate a summary from the section title when all else fails."""
    level_label = {0: "Root", 1: "Section", 2: "Subsection"}.get(section.level, "Section")
    return f"{level_label}: {section.title[:100]}"


# ---------------------------------------------------------------------------
# Base class + provider implementations
# ---------------------------------------------------------------------------

class _BaseSummarizer:
    """Base for all AI summarizer providers."""

    max_tokens_per_batch: int = 600

    def summarize_batch(self, sections: list, batch_size: int = 8) -> list:
        """Summarize sections that don't yet have summaries."""
        to_summarize = [s for s in sections if not s.summary]
        for i in range(0, len(to_summarize), batch_size):
            batch = to_summarize[i:i + batch_size]
            self._summarize_one_batch(batch)
        return sections

    def _summarize_one_batch(self, batch: list):
        prompt = _build_prompt(batch)
        try:
            text = self._call_api(prompt)
            summaries = _parse_response(text, len(batch))
            for sec, summary in zip(batch, summaries):
                sec.summary = summary if summary else title_fallback(sec)
        except Exception:
            for sec in batch:
                if not sec.summary:
                    sec.summary = title_fallback(sec)

    def _call_api(self, prompt: str) -> str:
        """Send prompt to AI and return raw text. Subclasses must override."""
        raise NotImplementedError


class _AnthropicSummarizer(_BaseSummarizer):
    """AI summarization via Anthropic Claude Haiku."""

    model = "claude-haiku-4-5-20251001"

    def __init__(self):
        from anthropic import Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("No ANTHROPIC_API_KEY")
        self._client = Anthropic(api_key=api_key)

    def _call_api(self, prompt: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens_per_batch,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


class _GeminiSummarizer(_BaseSummarizer):
    """AI summarization via Google Gemini Flash."""

    model = "gemini-2.0-flash"

    def __init__(self):
        import google.generativeai as genai
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("No GOOGLE_API_KEY")
        genai.configure(api_key=api_key)
        self._client = genai.GenerativeModel(self.model)

    def _call_api(self, prompt: str) -> str:
        response = self._client.generate_content(prompt)
        return response.text


class _OpenAICompatSummarizer(_BaseSummarizer):
    """AI summarization via any OpenAI-compatible API (OpenAI, MiniMax, GLM-5)."""

    def __init__(self, api_key: str, base_url: str, model: str):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def _call_api(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens_per_batch,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content
        if isinstance(content, str):
            return content
        if content:
            logger.warning(
                "OpenAI-compatible summarizer returned non-string content; coercing to string",
                extra={"model": self.model, "content_type": type(content).__name__},
            )
            return str(content)
        logger.warning(
            "OpenAI-compatible summarizer returned empty content; falling back to empty response",
            extra={"model": self.model},
        )
        return ""


# ---------------------------------------------------------------------------
# Provider detection and factory
# ---------------------------------------------------------------------------

# Auto-detect key mapping: env var -> provider name
_AUTO_DETECT_ORDER = [
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("GOOGLE_API_KEY", "gemini"),
    ("OPENAI_API_KEY", "openai"),
    ("MINIMAX_API_KEY", "minimax"),
    ("ZHIPUAI_API_KEY", "glm"),
]

_VALID_PROVIDERS = {"anthropic", "gemini", "openai", "minimax", "glm", "none"}


def get_provider_name() -> Optional[str]:
    """Return the active summarizer provider name, or None if disabled.

    Priority: explicit JDOCMUNCH_SUMMARIZER_PROVIDER env var > auto-detect by API key.
    Auto-detect order: Anthropic > Gemini > OpenAI > MiniMax > GLM-5.
    """
    explicit = os.environ.get("JDOCMUNCH_SUMMARIZER_PROVIDER", "").lower().strip()
    if explicit in _VALID_PROVIDERS:
        return None if explicit == "none" else explicit

    for env_var, name in _AUTO_DETECT_ORDER:
        if os.environ.get(env_var):
            return name
    return None


def _make_openai_compat(env_var: str, base_url: str, model: str):
    """Factory helper for OpenAI-compatible providers."""
    api_key = os.environ.get(env_var)
    if not api_key:
        raise ValueError(f"No {env_var}")
    return _OpenAICompatSummarizer(api_key=api_key, base_url=base_url, model=model)


_PROVIDERS = {
    "anthropic": lambda: _AnthropicSummarizer(),
    "gemini": lambda: _GeminiSummarizer(),
    "openai": lambda: _make_openai_compat("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
    "minimax": lambda: _make_openai_compat("MINIMAX_API_KEY", "https://api.minimax.io/v1", "minimax-m2.7"),
    "glm": lambda: _make_openai_compat("ZHIPUAI_API_KEY", "https://api.z.ai/api/paas/v4/", "glm-5"),
}


def _create_summarizer() -> Optional[_BaseSummarizer]:
    """Return the appropriate summarizer instance, or None."""
    name = get_provider_name()
    if name and name in _PROVIDERS:
        try:
            return _PROVIDERS[name]()
        except Exception as exc:
            logger.warning(
                "Failed to initialize summarizer provider; AI summaries disabled for this run",
                extra={"provider": name, "error": type(exc).__name__, "detail": str(exc)},
            )
            return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def summarize_sections(sections: list, use_ai: bool = True) -> list:
    """Three-tier summarization for doc sections.

    Tier 1: Heading text (always free — used as initial summary)
    Tier 2: AI batch summarization (Claude Haiku, Gemini Flash, OpenAI, MiniMax, GLM-5)
    Tier 3: title_fallback (always works)
    """
    # Tier 1: seed summary from heading
    for sec in sections:
        if not sec.summary:
            sec.summary = heading_summary(sec)

    # Tier 2: AI for sections where heading is short/uninformative
    if use_ai:
        needs_ai = [s for s in sections if len(s.summary) < 20 and s.content]
        if needs_ai:
            summarizer = _create_summarizer()
            if summarizer:
                # Temporarily clear summaries so batch_summarize processes them
                for sec in needs_ai:
                    sec.summary = ""
                summarizer.summarize_batch(needs_ai)

    # Tier 3: fallback for any still missing
    for sec in sections:
        if not sec.summary:
            sec.summary = title_fallback(sec)

    return sections


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

BatchSummarizer = _AnthropicSummarizer
GeminiBatchSummarizer = _GeminiSummarizer
