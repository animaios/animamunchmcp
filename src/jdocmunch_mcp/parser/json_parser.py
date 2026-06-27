"""JSON/JSONC parser: converts JSON and JSONC files to Markdown for section indexing.

Handles standard JSON and JSON with comments (JSONC). Converts the JSON
structure to Markdown sections suitable for parse_markdown(). Top-level
keys become ## headings, nested objects create deeper headings (up to depth 4).
"""

import json
import os
import re

# JSONC comment patterns
_JSONC_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
_JSONC_LINE_COMMENT = re.compile(r'(?<!")//(?![^"]*"(?:[^"]*"[^"]*")*[^"]*$).*$', re.MULTILINE)


def _strip_jsonc(content: str) -> str:
    """Strip JSONC comments (/* */ and //) from content."""
    content = _JSONC_BLOCK_COMMENT.sub('', content)
    content = _JSONC_LINE_COMMENT.sub('', content)
    return content


def _extract_label(obj: dict) -> str:
    """Try to extract a human-readable label from a dict."""
    for key in ("name", "title", "id", "key", "label", "type"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _render_leaf(value) -> str:
    """Render a scalar value as a readable string."""
    if value is None:
        return "*null*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    if isinstance(value, str):
        return value[:300] + ("…" if len(value) > 300 else "")
    try:
        s = json.dumps(value, ensure_ascii=False)
        return (s[:300] + "…") if len(s) > 300 else s
    except Exception:
        return str(value)[:300]


def _render_sections(lines: list, data, depth: int, max_depth: int = 4) -> None:
    """Recursively render JSON data as Markdown heading sections."""
    heading = "#" * min(depth, 6)

    if isinstance(data, dict):
        for key, value in data.items():
            lines.append(f"\n{heading} {key}")
            if isinstance(value, (dict, list)) and depth < max_depth:
                _render_sections(lines, value, depth + 1, max_depth)
            else:
                leaf = _render_leaf(value) if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False)[:300]
                if leaf:
                    lines.append(f"\n{leaf}")

    elif isinstance(data, list):
        for i, item in enumerate(data[:50]):
            if isinstance(item, dict):
                label = _extract_label(item) or f"Item {i + 1}"
                lines.append(f"\n{heading} {label}")
                if depth < max_depth:
                    _render_sections(lines, item, depth + 1, max_depth)
            else:
                lines.append(f"- {_render_leaf(item)}")


def convert_json(content: str, doc_path: str = "") -> str:
    """Convert JSON or JSONC content to a Markdown representation.

    Args:
        content: Raw JSON or JSONC string.
        doc_path: Relative file path, used to derive the document title.

    Returns:
        Markdown string with # headings, suitable for parse_markdown().
        Returns empty string on parse failure or if content is not an object/array.
    """
    clean = _strip_jsonc(content)
    try:
        data = json.loads(clean)
    except Exception:
        return ""

    if not isinstance(data, (dict, list)):
        return ""

    title = os.path.basename(doc_path) if doc_path else "JSON Document"
    lines = [f"# {title}"]
    _render_sections(lines, data, depth=2)
    return "\n".join(lines)
