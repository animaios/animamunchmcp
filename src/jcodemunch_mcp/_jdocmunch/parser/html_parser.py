"""HTML parser: converts HTML to a clean text representation for section indexing.

Uses the stdlib html.parser.HTMLParser to:
- Convert <h1>–<h6> tags to Markdown-style # headings
- Strip scripts, styles, and navigation chrome
- Emit readable paragraph text with appropriate whitespace

The resulting text is parsed by the Markdown parser, so heading structure
in the HTML drives section boundaries. The text representation (not the
original HTML) is stored as the raw file so byte-offset reads return
readable content.
"""

import re
from html.parser import HTMLParser


# Tags whose content should be silently discarded
_SKIP_TAGS = frozenset([
    "script", "style", "nav", "header", "footer", "aside",
    "form", "button", "select", "option", "noscript",
    "iframe", "svg", "figure", "figcaption",
])

# Block-level tags that should introduce a paragraph break
_BLOCK_TAGS = frozenset([
    "p", "div", "section", "article", "main", "blockquote",
    "ul", "ol", "dl", "table", "thead", "tbody", "tfoot",
    "tr", "th", "td", "dd", "dt", "li",
])

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class _HTMLToTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth: int = 0
        self._heading_level: int = 0
        self._in_pre: bool = False

    def handle_starttag(self, tag: str, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._heading_level = level
            self._parts.append(f"\n\n{'#' * level} ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag == "pre":
            self._in_pre = True
            self._parts.append("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self._parts.append("`")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "hr":
            self._parts.append("\n\n---\n\n")

    def handle_endtag(self, tag: str):
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = 0
            self._parts.append("\n")
        elif tag == "pre":
            self._in_pre = False
            self._parts.append("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self._parts.append("`")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n\n")

    def handle_data(self, data: str):
        if self._skip_depth:
            return
        if self._in_pre:
            self._parts.append(data)
        else:
            # Collapse whitespace but preserve newlines for inline content
            cleaned = _WHITESPACE_RE.sub(" ", data)
            self._parts.append(cleaned)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse 3+ blank lines to 2
        text = _BLANK_LINES_RE.sub("\n\n", text)
        return text.strip()


def convert_html(html_str: str) -> str:
    """Convert an HTML string to a clean text representation.

    <h1>–<h6> become Markdown # headings. Script, style, and nav content
    is stripped. Block elements introduce paragraph breaks.

    Args:
        html_str: Raw HTML content.

    Returns:
        Clean text string with Markdown headings, suitable for parse_markdown().
        Returns empty string on parse failure.
    """
    try:
        parser = _HTMLToTextParser()
        parser.feed(html_str)
        return parser.get_text()
    except Exception:
        return ""
