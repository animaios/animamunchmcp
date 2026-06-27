"""Plain text parser: splits .txt files into paragraph-level sections."""

from pathlib import Path

from .sections import (
    Section,
    slugify,
    resolve_slug_collision,
    make_section_id,
    compute_content_hash,
    extract_references,
    extract_tags,
)


def parse_text(content: str, doc_path: str, repo: str) -> list:
    """Parse a plain text file into paragraph-level sections.

    A paragraph is a block of non-empty lines separated by one or more blank
    lines. The first non-empty line of each paragraph becomes the section title.
    Byte offsets are tracked for each paragraph.

    Args:
        content: Raw text content.
        doc_path: Relative path of the document.
        repo: Repository identifier.

    Returns:
        List of Section objects in document order, without hierarchy wiring.
    """
    stem = Path(doc_path).stem
    used_slugs: dict = {}
    sections = []

    lines = content.splitlines(keepends=True)
    byte_cursor = 0
    paragraphs = []  # list of (byte_start, lines_list)

    current_para_lines: list = []
    current_para_start: int = 0
    in_para = False

    for line in lines:
        line_bytes = len(line.encode("utf-8"))
        stripped = line.strip()

        if stripped:
            if not in_para:
                current_para_start = byte_cursor
                current_para_lines = []
                in_para = True
            current_para_lines.append(line)
        else:
            if in_para:
                paragraphs.append((current_para_start, current_para_lines))
                in_para = False
                current_para_lines = []

        byte_cursor += line_bytes

    if in_para and current_para_lines:
        paragraphs.append((current_para_start, current_para_lines))

    if not paragraphs:
        # Empty file: one root section
        sec = Section(
            id=make_section_id(repo, doc_path, slugify(stem), 0),
            repo=repo,
            doc_path=doc_path,
            title=stem,
            content=content,
            level=0,
            parent_id="",
            children=[],
            byte_start=0,
            byte_end=len(content.encode("utf-8")),
        )
        sec.content_hash = compute_content_hash(content)
        sec.references = extract_references(content)
        sec.tags = extract_tags(content)
        return [sec]

    for idx, (byte_start, para_lines) in enumerate(paragraphs):
        body = "".join(para_lines)
        byte_end = byte_start + len(body.encode("utf-8"))

        # Title = first non-empty line, truncated
        first_line = para_lines[0].strip()
        title = first_line[:80] if first_line else f"{stem} paragraph {idx + 1}"

        slug = slugify(title) if title else f"{slugify(stem)}-p{idx + 1}"
        unique_slug = resolve_slug_collision(slug, used_slugs)

        # Paragraphs are all level 1 (flat structure)
        sec = Section(
            id=make_section_id(repo, doc_path, unique_slug, 1),
            repo=repo,
            doc_path=doc_path,
            title=title,
            content=body,
            level=1,
            parent_id="",
            children=[],
            byte_start=byte_start,
            byte_end=byte_end,
        )
        sec.content_hash = compute_content_hash(body)
        sec.references = extract_references(body)
        sec.tags = extract_tags(body)
        sections.append(sec)

    return sections
