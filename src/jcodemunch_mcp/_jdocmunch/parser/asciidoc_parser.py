"""AsciiDoc parser: ATX-style heading section splitter with byte offsets.

AsciiDoc headings use leading '=' characters followed by a space and title text:

    = Document Title   (level 1)
    == Section         (level 2)
    === Subsection     (level 3)
    ==== Level 4       (level 4)
    ===== Level 5      (level 5)
    ====== Level 6     (level 6)

Block delimiters (==== with no text) are not headings and are ignored.
Document attribute entries (:attr: value) are treated as preamble content.
"""

import re
from pathlib import Path

from .sections import (
    Section,
    slugify,
    make_section_id,
    make_hierarchical_slug,
    compute_content_hash,
    extract_references,
    extract_tags,
)

_HEADING_RE = re.compile(r"^(={1,6})\s+(.+?)(?:\s+=+\s*)?$")


def parse_asciidoc(content: str, doc_path: str, repo: str) -> list:
    """Parse an AsciiDoc file into Section objects.

    Detects ATX-style AsciiDoc headings (= through ======). Content before
    the first heading becomes a level-0 root section. Heading lines are
    included in the section's byte range and content body.

    Args:
        content: Raw AsciiDoc content.
        doc_path: Relative path of the document.
        repo: Repository identifier.

    Returns:
        List of Section objects in document order, without hierarchy wiring.
    """
    stem = Path(doc_path).stem
    lines = content.splitlines(keepends=True)
    used_slugs: dict = {}
    slug_stack: list = []
    sections = []

    # Current open section state
    current_title: str = stem
    current_level: int = 0
    current_slug: str = ""
    current_byte_start: int = 0
    current_lines: list = []
    byte_cursor: int = 0

    def _finalize_section(byte_end: int) -> None:
        body = "".join(current_lines)
        slug = current_slug or slugify(current_title)
        section_id = make_section_id(repo, doc_path, slug, current_level)
        sec = Section(
            id=section_id,
            repo=repo,
            doc_path=doc_path,
            title=current_title,
            content=body,
            level=current_level,
            parent_id="",
            children=[],
            byte_start=current_byte_start,
            byte_end=byte_end,
            summary="",
        )
        sec.content_hash = compute_content_hash(body)
        sec.references = extract_references(body)
        sec.tags = extract_tags(body)
        sections.append(sec)

    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        stripped = line.rstrip("\n").rstrip("\r")
        match = _HEADING_RE.match(stripped)

        if match:
            level = len(match.group(1))
            heading_text = match.group(2).strip()

            _finalize_section(byte_cursor)

            current_title = heading_text
            current_level = level
            current_slug = make_hierarchical_slug(heading_text, level, slug_stack, used_slugs)
            current_byte_start = byte_cursor
            current_lines = [line]
        else:
            current_lines.append(line)

        byte_cursor += line_bytes

    _finalize_section(byte_cursor)

    return sections
