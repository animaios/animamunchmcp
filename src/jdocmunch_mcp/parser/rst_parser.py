"""RST parser: heading-adornment section splitter with byte offsets.

RST headings use underline (and optionally matching overline) adornment characters.
Any printable non-alphanumeric ASCII character is valid. Heading levels are
determined by the ORDER in which adornment styles first appear in the document,
not by the specific character used.

Two distinct styles are recognized:
  - Overline + title + underline  (e.g. === Title ===)
  - Title + underline only        (e.g. Title\\n===)

The same character used with and without an overline counts as two distinct levels.
"""

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

# Any printable non-alphanumeric ASCII character is a valid RST adornment char.
_ADORNMENT_CHARS = frozenset('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')


def _is_adornment(line: str) -> bool:
    """Return True if line is a valid RST adornment: all same punctuation char, len >= 2."""
    s = line.rstrip("\n").rstrip("\r")
    if len(s) < 2:
        return False
    char = s[0]
    return char in _ADORNMENT_CHARS and all(c == char for c in s)


def _adornment_char(line: str) -> str:
    return line.rstrip("\n").rstrip("\r")[0]


def parse_rst(content: str, doc_path: str, repo: str) -> list:
    """Parse an RST file into Section objects.

    Detects both overline+title+underline and title+underline heading styles.
    Content before the first heading becomes a level-0 root section.
    Heading lines are included in the section's byte range and content body.

    Args:
        content: Raw RST content.
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

    # Maps (char, has_overline) -> assigned level (1-indexed, in order of first appearance)
    adornment_levels: dict = {}

    def _get_or_assign_level(char: str, has_overline: bool) -> int:
        key = (char, has_overline)
        if key not in adornment_levels:
            adornment_levels[key] = len(adornment_levels) + 1
        return adornment_levels[key]

    # Pre-compute byte offset of each line start
    byte_offsets = []
    cursor = 0
    for line in lines:
        byte_offsets.append(cursor)
        cursor += len(line.encode("utf-8"))
    total_bytes = cursor

    # Current open section state
    current_title: str = stem
    current_level: int = 0
    current_slug: str = ""
    current_byte_start: int = 0
    current_lines: list = []

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

    i = 0
    n = len(lines)

    while i < n:
        stripped = lines[i].rstrip("\n").rstrip("\r")

        # --- Overline + title + underline (3-line heading) ---
        if i + 2 < n and _is_adornment(stripped):
            title_raw = lines[i + 1].rstrip("\n").rstrip("\r")
            under_raw = lines[i + 2].rstrip("\n").rstrip("\r")
            if (
                title_raw.strip()
                and not _is_adornment(title_raw)
                and _is_adornment(under_raw)
                and _adornment_char(stripped) == _adornment_char(under_raw)
                and len(stripped) >= len(title_raw.strip())
            ):
                char = _adornment_char(stripped)
                heading_text = title_raw.strip()
                level = _get_or_assign_level(char, has_overline=True)

                _finalize_section(byte_offsets[i])

                current_title = heading_text
                current_level = level
                current_slug = make_hierarchical_slug(heading_text, level, slug_stack, used_slugs)
                current_byte_start = byte_offsets[i]
                current_lines = [lines[i], lines[i + 1], lines[i + 2]]

                i += 3
                continue

        # --- Title + underline (2-line heading) ---
        if i + 1 < n and stripped.strip() and not _is_adornment(stripped):
            under_raw = lines[i + 1].rstrip("\n").rstrip("\r")
            if _is_adornment(under_raw) and len(under_raw) >= len(stripped.strip()):
                char = _adornment_char(under_raw)
                heading_text = stripped.strip()
                level = _get_or_assign_level(char, has_overline=False)

                _finalize_section(byte_offsets[i])

                current_title = heading_text
                current_level = level
                current_slug = make_hierarchical_slug(heading_text, level, slug_stack, used_slugs)
                current_byte_start = byte_offsets[i]
                current_lines = [lines[i], lines[i + 1]]

                i += 2
                continue

        # Normal content line
        current_lines.append(lines[i])
        i += 1

    _finalize_section(total_bytes)

    return sections
