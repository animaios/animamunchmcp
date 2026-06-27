"""Godot scene/resource parser (.tscn, .tres) for jdocmunch-mcp.

Converts Godot's text-based scene/resource format into Markdown sections
suitable for structural navigation by agents.

.tscn format: [gd_scene ...] header, then [ext_resource ...],
[sub_resource ...], and [node ...] blocks, each followed by property lines.

.tres format: [gd_resource ...] header, then [ext_resource ...],
[sub_resource ...], and a [resource] block with property lines.
"""

import os
import re

# Matches a full section header: [tag key=val key="val" ...]
# Attribute values are either quoted strings or bare tokens (no spaces)
_HEADER_RE = re.compile(r'^\[(\w+)((?:\s+\w+\s*=\s*(?:"[^"]*"|\S+))*)\s*\]$')

# Matches one key=value pair inside an attribute string
_ATTR_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([\w./:+-]+))')

# Matches a property assignment line (not a section header)
_PROP_RE = re.compile(r'^([\w/.]+(?:\[[\w"]+\])?)\s*=\s*(.+)$')

_MAX_PROPS = 25  # cap properties shown per block to keep output scannable


def _parse_attrs(attr_str: str) -> dict:
    """Parse key=value pairs from a section header attribute string."""
    attrs = {}
    for m in _ATTR_RE.finditer(attr_str):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        attrs[key] = val
    return attrs


def _format_props(props: list) -> str:
    """Format property lines as a bullet list, capped at _MAX_PROPS."""
    shown = props[:_MAX_PROPS]
    lines = [f"- `{p}`" for p in shown]
    if len(props) > _MAX_PROPS:
        lines.append(f"- *({len(props) - _MAX_PROPS} more properties)*")
    return "\n".join(lines)


def _node_depth(attrs: dict) -> int:
    """Return the Markdown heading depth (3–6) for a [node] block.

    Depth reflects position in the scene tree:
      - scene root (no parent attr): 3
      - parent=".": 4  (direct child of root)
      - parent="A": 5  (grandchild — A is a child of root)
      - parent="A/B": 6  (great-grandchild)
    Capped at 6 to stay within ATX heading limits.
    """
    parent = attrs.get("parent")
    if parent is None:
        return 3
    if parent == ".":
        return 4
    return min(4 + parent.count("/") + 1, 6)


def convert_godot(content: str, doc_path: str = "") -> str:
    """Convert a Godot .tscn or .tres file to a Markdown representation.

    Parses section headers ([node ...], [sub_resource ...], etc.) and their
    trailing property blocks into a hierarchical Markdown document.

    Args:
        content: Raw .tscn or .tres file content.
        doc_path: Relative file path, used to derive the document title.

    Returns:
        Markdown string with # headings suitable for parse_markdown(),
        or "" on parse failure / empty input.
    """
    filename = os.path.basename(doc_path) if doc_path else "scene"
    stem = os.path.splitext(filename)[0] or "scene"
    ext = os.path.splitext(doc_path)[1].lower() if doc_path else ".tscn"
    is_scene = ext == ".tscn"

    # --- Parse blocks ---
    blocks = []          # list of (tag, attrs_dict, prop_lines)
    current_tag = None
    current_attrs: dict = {}
    current_props: list = []

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        # Skip blank lines and comments (;)
        if not line or line.startswith(";"):
            continue

        m = _HEADER_RE.match(line)
        if m:
            if current_tag is not None:
                blocks.append((current_tag, current_attrs, current_props))
            current_tag = m.group(1)
            current_attrs = _parse_attrs(m.group(2))
            current_props = []
        elif current_tag is not None:
            pm = _PROP_RE.match(line)
            if pm:
                current_props.append(f"{pm.group(1)} = {pm.group(2).strip()}")

    if current_tag is not None:
        blocks.append((current_tag, current_attrs, current_props))

    if not blocks:
        return ""

    # --- Render Markdown ---
    out = []
    out.append(f"# {stem}")
    out.append(f"\nGodot {'scene' if is_scene else 'resource'} file: `{filename}`")

    # File header metadata (gd_scene / gd_resource)
    header = next(
        ((tag, attrs, props) for tag, attrs, props in blocks
         if tag in ("gd_scene", "gd_resource")),
        None,
    )
    if header:
        _, attrs, _ = header
        meta = []
        for key in ("format", "load_steps", "uid"):
            val = attrs.get(key)
            if val:
                meta.append(f"- `{key}`: {val}")
        if meta:
            out.append("\n## File Metadata")
            out.extend(meta)

    # External Resources
    ext_res = [(tag, attrs, props) for tag, attrs, props in blocks if tag == "ext_resource"]
    if ext_res:
        out.append("\n## External Resources")
        for _, attrs, props in ext_res:
            res_type = attrs.get("type", "Resource")
            path = attrs.get("path", "")
            res_id = attrs.get("id", "")
            label = res_type
            if path:
                label += f": `{path}`"
            out.append(f"\n### {label}")
            if res_id:
                out.append(f"- `id`: {res_id}")
            if props:
                out.append(_format_props(props))

    # Sub-Resources
    sub_res = [(tag, attrs, props) for tag, attrs, props in blocks if tag == "sub_resource"]
    if sub_res:
        out.append("\n## Sub-Resources")
        for _, attrs, props in sub_res:
            res_type = attrs.get("type", "Resource")
            res_id = attrs.get("id", "")
            label = res_type
            if res_id:
                label += f" ({res_id})"
            out.append(f"\n### {label}")
            if props:
                out.append(_format_props(props))

    # Scene Tree nodes
    nodes = [(tag, attrs, props) for tag, attrs, props in blocks if tag == "node"]
    if nodes:
        out.append("\n## Scene Tree")
        for _, attrs, props in nodes:
            name = attrs.get("name", "unnamed")
            node_type = attrs.get("type", "")
            parent = attrs.get("parent")
            instance = attrs.get("instance", "")

            depth = _node_depth(attrs)
            hashes = "#" * depth

            label = name
            if node_type:
                label += f" ({node_type})"
            elif instance:
                label += " [instanced]"

            out.append(f"\n{hashes} {label}")

            meta = []
            if parent and parent not in (".",):
                meta.append(f"- `parent`: {parent}")
            if instance:
                meta.append(f"- `instance`: {instance}")
            if meta:
                out.extend(meta)

            if props:
                out.append(_format_props(props))

    # .tres [resource] block
    res_blocks = [(tag, attrs, props) for tag, attrs, props in blocks if tag == "resource"]
    if res_blocks:
        out.append("\n## Resource Properties")
        for _, _, props in res_blocks:
            if props:
                out.append(_format_props(props))

    return "\n".join(out)
