"""Shared import-graph adjacency builders.

Provides a single ``build_adjacency`` function that can produce either
forward adjacency (``{src: [imports]}``) or reverse adjacency
(``{target: [importers]}``), with optional barrel/re-export expansion
for correct handling of TypeScript/JavaScript barrel files.

All tool files should import from here instead of maintaining
private copies of adjacency-building logic.
"""

from __future__ import annotations

from typing import Optional

from ..parser.imports import (
    build_re_export_maps,
    expand_barrel_leaves,
    resolve_specifier,
)


def build_adjacency(
    imports: dict,
    source_files: frozenset,
    alias_map: Optional[dict] = None,
    psr4_map: Optional[dict] = None,
    direction: str = "forward",
    expand_barrels: bool = False,
) -> dict[str, list[str]]:
    """Build an adjacency list from raw import data.

    Args:
        imports:       Raw import dict from ``index.imports``.
        source_files:  Frozen set of all source file paths in the index.
        alias_map:     Optional package alias → root mapping.
        psr4_map:      Optional PSR-4 autoloader mapping (PHP).
        direction:     ``"forward"`` returns ``{file: [files_it_imports]}``.
                       ``"reverse"`` returns ``{file: [files_that_import_it]}``.
        expand_barrels: When *True*, resolve barrel/re-export expansion
            (wildcard ``export *`` and selective ``export { X }``). This is
            the more correct but slower path; when *False* (default) a
            simple specifier resolution is used, preserving backward-
            compatible behaviour with earlier tool implementations.

    Returns:
        Deduplicated adjacency dict with insertion-order preserved values.
    """
    if expand_barrels:
        return _build_with_barrels(
            imports, source_files, alias_map, psr4_map, direction
        )

    # --- Simple path: resolve specifiers directly, no barrel expansion ---
    if direction == "reverse":
        rev: dict[str, list[str]] = {}
        for src_file, file_imports in imports.items():
            for imp in file_imports:
                target = resolve_specifier(
                    imp["specifier"], src_file, source_files, alias_map, psr4_map
                )
                if target and target != src_file:
                    rev.setdefault(target, []).append(src_file)
        return {k: list(dict.fromkeys(v)) for k, v in rev.items()}

    # direction == "forward"
    fwd: dict[str, list[str]] = {}
    for src_file, file_imports in imports.items():
        for imp in file_imports:
            target = resolve_specifier(
                imp["specifier"], src_file, source_files, alias_map, psr4_map
            )
            if target and target != src_file and target in source_files:
                fwd.setdefault(src_file, []).append(target)
    return {k: list(dict.fromkeys(v)) for k, v in fwd.items()}


def invert_adjacency(adj: dict[str, list[str]]) -> dict[str, list[str]]:
    """Invert an adjacency list: ``{file: [importers_of_file]}``."""
    inv: dict[str, list[str]] = {}
    for src, targets in adj.items():
        for tgt in targets:
            inv.setdefault(tgt, []).append(src)
    return inv


# -----------------------------------------------------------------------
# Internal: barrel-expansion path
# -----------------------------------------------------------------------


def _build_with_barrels(
    imports: dict,
    source_files: frozenset,
    alias_map: Optional[dict],
    psr4_map: Optional[dict],
    direction: str,
) -> dict[str, list[str]]:
    """Build forward adjacency with barrel/re-export expansion, then
    optionally invert for reverse.

    When an import targets a TypeScript/JavaScript barrel file,
    the importer is also credited with importing the leaf files re-exported
    through that barrel.

    Barrel expansion is per-name.  Wildcard re-exports
    (``export * from``) credit every leaf to anyone importing the barrel.
    Selective re-exports (``export { Foo } from``) credit only the consumers
    that actually import ``Foo`` from the barrel.  Mixed barrels work too —
    names not in the selective table fall back to the wildcard expansion.
    """
    wildcard_map, named_map = build_re_export_maps(
        imports,
        source_files,
        alias_map,
        psr4_map,
    )

    fwd: dict[str, list[str]] = {}
    for src_file, file_imports in imports.items():
        resolved: list[str] = []
        for imp in file_imports:
            direct = resolve_specifier(
                imp["specifier"], src_file, source_files, alias_map, psr4_map
            )
            if not direct or direct == src_file:
                continue
            leaves = expand_barrel_leaves(
                direct,
                imp.get("names", []),
                wildcard_map,
                named_map,
            )
            for leaf in leaves:
                if leaf != src_file:
                    resolved.append(leaf)
        if resolved:
            fwd[src_file] = list(dict.fromkeys(resolved))  # deduplicate, preserve order

    if direction == "forward":
        return fwd

    # direction == "reverse" — invert the forward adjacency
    return invert_adjacency(fwd)
