"""Jupyter Notebook parser: converts .ipynb JSON to a Markdown text representation.

Markdown cells are included as-is. Code cells are wrapped in fenced code blocks
followed by their persisted outputs (text/plain, text/html stripped to text,
truncated image data). The resulting text is parsed by the standard Markdown
parser, so heading structure in markdown cells drives section boundaries AND
each code cell's output appears in the indexed body — searchable, retrievable,
and visible to BM25.

v1.25.0: previously outputs were dropped entirely. Tutorials' teaching value
(the printed output, the rendered table, the error traceback) is now preserved
in the indexed body so search_sections finds it.

The text representation (not the original JSON) is stored as the raw file so
byte-offset content retrieval works correctly.
"""

import json
import re

# Cap each output rendering so a single noisy cell can't blow up the index.
_OUTPUT_TEXT_CHARS = 800
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _get_kernel_language(notebook: dict) -> str:
    """Extract the kernel language from notebook metadata, defaulting to 'python'."""
    meta = notebook.get("metadata", {})
    lang = (
        meta.get("language_info", {}).get("name")
        or meta.get("kernelspec", {}).get("language")
        or "python"
    )
    return lang.lower()


def _cell_source(cell: dict) -> str:
    """Join cell source lines into a single string."""
    source = cell.get("source", [])
    if isinstance(source, list):
        return "".join(source)
    return source


def _join_text(value) -> str:
    """ipynb output payloads can be a string OR a list of lines."""
    if isinstance(value, list):
        return "".join(value)
    return value or ""


def _truncate(text: str, limit: int = _OUTPUT_TEXT_CHARS) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _strip_html(text: str) -> str:
    """Cheap HTML→text conversion. Drops tags + collapses whitespace."""
    no_tags = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _as_quote_block(text: str) -> str:
    """Render text as a markdown blockquote so the BM25 tokenizer (which
    strips fenced code) can still index every word. Each line gets a
    leading ``> ``. Visually distinct from prose without hiding tokens."""
    lines = text.splitlines() or [text]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


def _render_output(out: dict) -> str:
    """Render one ipynb output entry into a markdown fragment.

    Handles four common output_types:

      stream            — stdout/stderr text (most common)
      execute_result    — display_data with a .data dict by mime type
      display_data      — same as execute_result for our purposes
      error             — traceback list

    Plain-text outputs are rendered as blockquotes (not fenced code) so
    the BM25 tokenizer — which deliberately strips ``` fences — still
    indexes every word. Image outputs collapse to a truncated marker
    line. JSON outputs use a fenced ``json`` block (the structure is
    valuable; the tokens within are usually noise).
    """
    output_type = out.get("output_type", "")
    if output_type == "stream":
        text = _truncate(_join_text(out.get("text", "")))
        if not text:
            return ""
        return _as_quote_block(text)

    if output_type == "error":
        tb = out.get("traceback") or []
        text = _truncate(_join_text(tb)) if tb else (out.get("ename", "") + ": " + out.get("evalue", "")).strip()
        if not text:
            return ""
        return _as_quote_block(text)

    data = out.get("data") or {}
    if isinstance(data, dict):
        if "text/plain" in data:
            text = _truncate(_join_text(data["text/plain"]))
            if text:
                return _as_quote_block(text)
        if "text/html" in data:
            text = _strip_html(_join_text(data["text/html"]))
            if text:
                return _truncate(text)
        for mime in ("image/png", "image/jpeg", "image/svg+xml"):
            if mime in data:
                return f"_[{mime} image; output preserved at index time]_"
        if "application/json" in data:
            try:
                text = _truncate(json.dumps(data["application/json"], indent=2))
            except Exception:
                text = ""
            if text:
                return f"```json\n{text}\n```"

    return ""


def _render_outputs(outputs: list) -> str:
    """Render a code cell's outputs[] list into a markdown block.

    Returns empty string when the cell produced no preservable outputs.
    """
    rendered = []
    for out in outputs or []:
        if not isinstance(out, dict):
            continue
        chunk = _render_output(out)
        if chunk:
            rendered.append(chunk)
    if not rendered:
        return ""
    return "**Output:**\n\n" + "\n\n".join(rendered)


def convert_notebook(json_str: str) -> str:
    """Convert a Jupyter notebook JSON string to a Markdown text representation.

    Args:
        json_str: Raw .ipynb file content.

    Returns:
        Markdown string suitable for parse_markdown(). Returns empty string on
        parse failure (the caller will skip the file).
    """
    try:
        nb = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return ""

    cells = nb.get("cells", [])
    lang = _get_kernel_language(nb)
    parts = []

    for cell in cells:
        cell_type = cell.get("cell_type", "")
        source = _cell_source(cell).strip()
        if not source:
            continue

        if cell_type == "markdown":
            parts.append(source)
        elif cell_type == "code":
            block = f"```{lang}\n{source}\n```"
            outputs_md = _render_outputs(cell.get("outputs") or [])
            if outputs_md:
                block = block + "\n\n" + outputs_md
            parts.append(block)
        else:
            # raw or unknown — include as plain text
            parts.append(source)

    return "\n\n".join(parts)
