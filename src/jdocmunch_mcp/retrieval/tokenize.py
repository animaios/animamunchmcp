"""Markdown-aware tokenizer for the BM25 engine.

Produces a flat list of lowercase tokens suitable for indexing or scoring:

- Strips top-of-text frontmatter (YAML --- / TOML +++) and fenced code
  blocks. (Section content is byte-accurate and retains both; this is the
  shared prose view both the lexical and semantic channels score over, so
  fusion compares the same text — see prose_view and #58.)
- Strips inline backticks but keeps their contents.
- URLs collapse to host + path tokens (so ``https://api.example.com/v2/users``
  yields ``api`` ``example`` ``com`` ``v2`` ``users``).
- Splits on Unicode word boundaries; further splits CamelCase
  (``DocStore`` → ``Doc Store``) and snake_case / kebab-case
  (``embed_query`` → ``embed query``).
- Drops English stop-words and tokens shorter than two characters.

This module has no side effects and no I/O — safe to call from index time
and from query time. Stop-word list is intentionally short; over-aggressive
stop-word removal hurts technical doc retrieval.
"""

from __future__ import annotations

import re
from typing import Iterable

# A tight English stop-word list. Picked for technical-doc retrieval — words
# that almost never disambiguate a query. NOT exhaustive; we'd rather pass
# a noisy token through than drop a meaningful one.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
        "the", "this", "to", "was", "were", "will", "with",
    }
)

# Top-of-text frontmatter: a --- or +++ opener at offset 0 closed by the SAME
# delimiter (backreference), so a YAML/TOML metadata block doesn't flood the
# scoring text (acutely the capped embed window — #58).
_FRONTMATTER_RE = re.compile(
    r"\A(---|\+\+\+)[ \t]*\r?\n.*?^\1[ \t]*\r?\n?", re.DOTALL | re.MULTILINE
)
# ``` or ~~~ fenced code, dotall to span newlines.
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)


def prose_view(text: str) -> str:
    """Front-matter-free, fence-free view of section content (#58).

    The shared derivation for both scoring channels so hybrid fusion compares
    the same text. Does not touch Section.content / content_hash.
    """
    return _FENCE_RE.sub(" ", _FRONTMATTER_RE.sub(" ", text))
# Inline code: drop the backticks but keep contents (often valuable identifiers).
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# URLs: capture and replace with their host + path tokens.
_URL_RE = re.compile(r"https?://[^\s)\]'\"<>]+")
# Markdown links and images: keep the link text, drop the URL.
_MD_LINK_RE = re.compile(r"!?\[([^\]]+)\]\([^)]*\)")
# Splits CamelCase / PascalCase: insert a space before any uppercase that
# follows a lowercase OR a sequence-of-uppercase-then-lowercase boundary.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# After de-camel + lowercasing, anything that isn't alphanumeric becomes a
# separator. Underscores, hyphens, dots, slashes — all split.
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _expand_url(url: str) -> str:
    """Convert a URL to whitespace-separated host+path tokens.

    ``https://api.example.com/v2/users?x=1`` → ``api example com v2 users``.
    Query string and fragment are dropped — they're rarely informative.
    """
    # Strip scheme.
    body = url.split("://", 1)[-1]
    # Drop query + fragment.
    body = body.split("?", 1)[0].split("#", 1)[0]
    return body.replace("/", " ").replace(".", " ").replace(":", " ")


def tokenize(text: str) -> list[str]:
    """Tokenize ``text`` into a list of lowercase BM25-ready tokens.

    Order is preserved (matters for term-frequency counts but not for ordering
    semantics — BM25 is a bag-of-words model). Empty input returns ``[]``.
    """
    if not text:
        return []

    # 1. Scrub frontmatter, fenced code, then markdown links, then URLs.
    text = _FRONTMATTER_RE.sub(" ", text)
    text = _FENCE_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(lambda m: " " + m.group(1) + " ", text)
    text = _URL_RE.sub(lambda m: " " + _expand_url(m.group(0)) + " ", text)
    text = _INLINE_CODE_RE.sub(lambda m: " " + m.group(1) + " ", text)

    # 2. Insert spaces at CamelCase boundaries BEFORE lowercasing.
    text = _CAMEL_BOUNDARY_RE.sub(" ", text)

    # 3. Lowercase and split on non-alphanumeric runs.
    raw = _SPLIT_RE.split(text.lower())

    out: list[str] = []
    for tok in raw:
        if len(tok) < 2:
            continue
        if tok in STOP_WORDS:
            continue
        out.append(tok)
    return out


def tokenize_unique(text: str) -> set[str]:
    """Tokenize and deduplicate. Useful for posting-list lookups."""
    return set(tokenize(text))


def term_frequencies(tokens: Iterable[str]) -> dict[str, int]:
    """Return ``{term: count}`` from a token list. O(n)."""
    out: dict[str, int] = {}
    for t in tokens:
        out[t] = out.get(t, 0) + 1
    return out
