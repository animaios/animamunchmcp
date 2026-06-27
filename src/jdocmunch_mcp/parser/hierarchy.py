"""Stack-based parent/child hierarchy wiring for parsed sections."""


def wire_hierarchy(sections: list) -> list:
    """Wire parent_id and children fields on a list of Section objects.

    Uses a stack of open ancestors. For each section:
    - Pop entries from the stack whose level >= current section's level.
    - The top of the remaining stack is the parent.
    - Add current section as a child of its parent.
    - Push current section onto the stack.

    Level-0 (pre-heading root) sections are always top-level.

    Args:
        sections: List of Section objects in document order (no hierarchy).

    Returns:
        The same list with parent_id and children fields populated.
    """
    stack = []  # list of Section objects, ordered ancestor-first

    for sec in sections:
        if sec.level == 0:
            # Root section: always top-level, no parent
            sec.parent_id = ""
            stack = [sec]
            continue

        # Pop all stack entries with level >= current
        while stack and stack[-1].level >= sec.level:
            stack.pop()

        if stack:
            parent = stack[-1]
            sec.parent_id = parent.id
            parent.children.append(sec.id)
        else:
            sec.parent_id = ""

        stack.append(sec)

    return sections
