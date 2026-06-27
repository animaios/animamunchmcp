"""XML/SVG parser: converts XML and SVG documents to Markdown for section indexing.

Uses stdlib xml.etree.ElementTree to parse XML. Element tag names and
attributes become section headings. SVG files use <title> and <desc> elements
for documentation context. The resulting Markdown is parsed by the Markdown
parser so heading structure drives section boundaries.
"""

import os
import re
import xml.etree.ElementTree as ET

_SVG_NS = "http://www.w3.org/2000/svg"

# Attribute keys too noisy to surface in documentation
_SKIP_ATTRS = frozenset({
    "style", "transform", "d", "points", "xlink:href",
    "xmlns", "class", "fill", "stroke", "opacity",
})


def _strip_ns(tag: str) -> str:
    """Remove XML namespace prefix from a tag name."""
    return re.sub(r'^\{[^}]+\}', '', tag)


def _elem_label(elem: ET.Element) -> str:
    """Derive a readable label from element tag + key attributes."""
    tag = _strip_ns(elem.tag)
    for attr in ("name", "id", "title", "label", "type"):
        val = elem.get(attr, "").strip()
        if val:
            return f"{tag} ({val})"
    return tag


def _text_content(elem: ET.Element) -> str:
    """Return direct text of an element (excluding children), trimmed."""
    text = (elem.text or "").strip()
    return text[:500]


def _render_attributes(elem: ET.Element) -> list:
    """Render element attributes as a bullet list, skipping noisy attrs."""
    lines = []
    for k, v in elem.attrib.items():
        k_clean = _strip_ns(k)
        if k_clean in _SKIP_ATTRS:
            continue
        if len(v) > 120:
            continue
        lines.append(f"- `{k_clean}`: {v}")
    return lines


def _render_xml_sections(lines: list, elem: ET.Element, depth: int, max_depth: int = 4) -> None:
    """Recursively render XML child elements as Markdown heading sections."""
    if depth > max_depth:
        return
    heading = "#" * min(depth, 6)

    for child in elem:
        label = _elem_label(child)
        lines.append(f"\n{heading} {label}")

        attr_lines = _render_attributes(child)
        if attr_lines:
            lines.extend(attr_lines)

        text = _text_content(child)
        if text:
            lines.append(f"\n{text}")

        if list(child):
            _render_xml_sections(lines, child, depth + 1, max_depth)


def _find_svg_text(root: ET.Element, tag_name: str) -> str:
    """Find text content of a named SVG element (namespace-aware)."""
    elem = root.find(f"{{{_SVG_NS}}}{tag_name}")
    if elem is None:
        elem = root.find(tag_name)
    if elem is not None and elem.text:
        return elem.text.strip()
    return ""


def convert_xml(content: str, doc_path: str = "") -> str:
    """Convert XML or SVG content to a Markdown representation.

    Args:
        content: Raw XML/SVG string.
        doc_path: Relative file path, used to derive the document title.

    Returns:
        Markdown string with # headings, suitable for parse_markdown().
        Returns empty string on parse failure.
    """
    try:
        root = ET.fromstring(content.encode("utf-8") if isinstance(content, str) else content)
    except Exception:
        return ""

    tag = _strip_ns(root.tag)
    lines = []

    if tag.lower() == "svg":
        svg_title = _find_svg_text(root, "title") or os.path.basename(doc_path) or "SVG Document"
        lines.append(f"# {svg_title}")

        desc = _find_svg_text(root, "desc")
        if desc:
            lines.append(f"\n{desc}")

        # Viewport metadata
        meta = []
        for attr in ("viewBox", "width", "height"):
            val = root.get(attr, "")
            if val:
                meta.append(f"- `{attr}`: {val}")
        if meta:
            lines.append("\n## Metadata")
            lines.extend(meta)

        lines.append("\n## Elements")
        _render_xml_sections(lines, root, depth=3, max_depth=4)

    else:
        title = os.path.basename(doc_path) or tag
        lines.append(f"# {title}")
        lines.append(f"\nRoot element: `{tag}`")

        attr_lines = _render_attributes(root)
        if attr_lines:
            lines.append("\n## Attributes")
            lines.extend(attr_lines)

        _render_xml_sections(lines, root, depth=2, max_depth=4)

    return "\n".join(lines)
