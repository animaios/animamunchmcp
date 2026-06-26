"""Find files that reference, import, or are related to a given identifier.

Supports three modes (``mode`` parameter):
  - ``"refs"`` (default): find who references an identifier — the original
    find_references behaviour, with full and quick sub-modes.
  - ``"importers"``: find which files import a given file — the former
    find_importers tool (singular or batch).
  - ``"related"``: find related symbols using heuristic clustering — the
    former get_related_symbols tool.
"""

import posixpath
import re
import time
from typing import Optional

from ..storage import IndexStore, result_cache_get, result_cache_put
from ._utils import index_status_to_tool_error, resolve_repo

# ═══════════════════════════════════════════════════════════════════════════
# Mode "refs" — original find_references logic
# ═══════════════════════════════════════════════════════════════════════════


def _quick_check_single(
    identifier: str,
    index,
    search_content: bool,
    max_content_results: int,
    owner: str,
    name: str,
    store: "IndexStore",
    start: float,
) -> dict:
    """Quick-mode logic for a single identifier: import + content counts only."""
    ident_lower = identifier.lower()

    # ── Import-level check ──────────────────────────────────────────────────
    import_references = []
    if index.imports is not None:
        for src_file, file_imports in index.imports.items():
            matches = []
            for imp in file_imports:
                named_match = any(
                    n.lower() == ident_lower for n in imp.get("names", [])
                )
                spec = imp["specifier"]
                spec_stem = posixpath.splitext(posixpath.basename(spec))[0].lower()
                stem_match = spec_stem == ident_lower

                if named_match or stem_match:
                    matches.append(
                        {
                            "specifier": spec,
                            "names": imp.get("names", []),
                            "match_type": "named" if named_match else "specifier_stem",
                        }
                    )

            if matches:
                import_references.append({"file": src_file, "matches": matches})

    import_count = len(import_references)

    # ── Content-level check ─────────────────────────────────────────────────
    # Find files where this identifier is *defined* (via symbol index)
    # so we can skip them — finding the name in the defining file is not a "reference".
    defining_files: set[str] = set()
    for sym in index.symbols:
        if sym.get("name", "").lower() == ident_lower:
            file_path = sym.get("file", "")
            if file_path:
                defining_files.add(file_path)

    content_references = []

    if search_content:
        content_dir = store._content_dir(owner, name)
        for file_path in index.source_files:
            if file_path in defining_files:
                continue

            full_path = store._safe_content_path(content_dir, file_path)
            if not full_path or not full_path.exists():
                continue

            try:
                with open(
                    full_path, "r", encoding="utf-8", errors="replace", newline=""
                ) as f:
                    content = f.read()
            except OSError:
                continue

            file_matches = []
            for line_index, line in enumerate(content.split("\n")):
                if ident_lower in line.lower():
                    file_matches.append(
                        {
                            "line": line_index + 1,
                            "text": line.rstrip()[:200],
                        }
                    )

            if file_matches:
                content_references.append({"file": file_path, "matches": file_matches})
                # Stop after N files, not N lines
                if len(content_references) >= max_content_results:
                    break

    content_count = len(content_references)

    elapsed = (time.perf_counter() - start) * 1000
    is_referenced = import_count > 0 or content_count > 0

    result = {
        "repo": f"{owner}/{name}",
        "identifier": identifier,
        "is_referenced": is_referenced,
        "import_count": import_count,
        "import_references": import_references,
        "content_count": content_count,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }

    if search_content:
        result["content_references"] = content_references

    return result


def _quick_check_batch(
    identifiers: list[str],
    index,
    search_content: bool,
    max_content_results: int,
    owner: str,
    name: str,
    store: "IndexStore",
    start: float,
) -> dict:
    """Batch quick-mode: loop over identifiers, return grouped results array."""
    results = []
    for identifier in identifiers:
        result = _quick_check_single(
            identifier=identifier,
            index=index,
            search_content=search_content,
            max_content_results=max_content_results,
            owner=owner,
            name=name,
            store=store,
            start=start,
        )
        # Strip envelope fields for consistency with other batch tools
        result.pop("repo", None)
        result.pop("_meta", None)
        results.append(result)

    elapsed = (time.perf_counter() - start) * 1000
    return {
        "repo": f"{owner}/{name}",
        "results": results,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "identifiers_checked": len(identifiers),
        },
    }


def _build_import_name_index(index) -> dict[str, list[tuple[str, dict]]]:
    """Build inverted index: lowered_name -> [(src_file, imp), ...].

    Indexes both named imports and specifier stems so lookups are O(1).
    Built once per CodeIndex, cached on index._import_name_index.
    """
    inv: dict[str, list[tuple[str, dict]]] = {}
    for src_file, file_imports in index.imports.items():
        for imp in file_imports:
            for n in imp.get("names", []):
                inv.setdefault(n.lower(), []).append((src_file, imp))
            spec_stem = posixpath.splitext(posixpath.basename(imp["specifier"]))[
                0
            ].lower()
            if spec_stem:
                inv.setdefault(spec_stem, []).append((src_file, imp))
    return inv


def _get_import_index(index) -> dict[str, list[tuple[str, dict]]]:
    """Return the cached import name index, building it on first call."""
    if index._import_name_index is None:
        index._import_name_index = _build_import_name_index(index)
    return index._import_name_index


def _find_import_line(content: str, specifier: str) -> Optional[int]:
    """Return the 1-based line number where ``specifier`` first appears in
    quotes (single, double, or backtick) in ``content``.

    Heuristic — matches ``from 'X' import``, ``import 'X'``, ``require('X')``,
    ``import('X')``, ``from "X"``, etc. Robust to whitespace and quote style.
    Returns None if the specifier isn't found in any quoting context.
    """
    if not content or not specifier:
        return None
    needles = (f'"{specifier}"', f"'{specifier}'", f"`{specifier}`")
    for idx, line in enumerate(content.splitlines(), start=1):
        for needle in needles:
            if needle in line:
                return idx
    return None


def _calling_symbols_in_file(
    index,
    store,
    owner: str,
    repo_name: str,
    src_file: str,
    identifier: str,
) -> list[dict]:
    """Return symbols in *src_file* whose bodies mention *identifier*.

    Used to populate the optional ``calling_symbols`` field when
    ``include_call_chain=True``.  Each result is ``{id, name, kind, line}``.
    """
    from ._call_graph import _symbol_body, _word_match

    # Lazy: build symbols_by_file only once per call_chain enrichment pass.
    # We do it here inline to keep the function self-contained; callers can
    # pass a pre-built map if they need efficiency across multiple files.
    file_content = store.get_file_content(owner, repo_name, src_file)
    if not file_content:
        return []
    if not _word_match(file_content, identifier):
        return []

    file_lines = file_content.splitlines()
    syms_in_file = [s for s in index.symbols if s.get("file") == src_file]
    results: list[dict] = []
    seen: set[str] = set()

    for sym in syms_in_file:
        sid = sym.get("id", "")
        if not sid or sid in seen or not sym.get("line"):
            continue
        body = _symbol_body(file_lines, sym)
        if body and _word_match(body, identifier):
            seen.add(sid)
            results.append(
                {
                    "id": sid,
                    "name": sym.get("name", ""),
                    "kind": sym.get("kind", ""),
                    "line": sym.get("line", 0),
                }
            )

    return results


def _find_references_single(
    identifier: str,
    index,
    max_results: int,
    owner: str,
    name: str,
    start: float,
    include_call_chain: bool = False,
    store=None,
) -> dict:
    """Core logic for a single identifier query. Returns the original flat shape."""
    if index.imports is None:
        return {
            "repo": f"{owner}/{name}",
            "identifier": identifier,
            "references": [],
            "note": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 to enable find_references.",
            "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
        }

    ident_lower = identifier.lower()
    inv = _get_import_index(index)
    entries = inv.get(ident_lower, [])

    # Group by file, dedup, and classify match type
    file_matches: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()  # (src_file, specifier) dedup
    for src_file, imp in entries:
        spec = imp["specifier"]
        key = (src_file, spec)
        if key in seen:
            continue
        seen.add(key)

        named_match = any(n.lower() == ident_lower for n in imp.get("names", []))
        spec_stem = posixpath.splitext(posixpath.basename(spec))[0].lower()
        stem_match = spec_stem == ident_lower

        if named_match or stem_match:
            file_matches.setdefault(src_file, []).append(
                {
                    "specifier": spec,
                    "names": imp.get("names", []),
                    "match_type": "named" if named_match else "specifier_stem",
                }
            )

    results = [{"file": f, "matches": m} for f, m in file_matches.items()]
    results.sort(key=lambda r: r["file"])

    # Enrich each match with the line number of its import statement so
    # downstream consumers (regex harvesters, IDE deeplinks, code review
    # bots) can jump straight to the import site instead of opening the
    # file and grepping. Heuristic: first line where the specifier
    # appears quoted. Skipped silently when file content is unavailable
    # (remote-only indexes); existing callers see additive `line` field.
    if store is not None:
        for ref in results:
            try:
                content = store.get_file_content(owner, name, ref["file"])
            except Exception:
                content = None
            if not content:
                continue
            for match in ref["matches"]:
                line = _find_import_line(content, match.get("specifier", ""))
                if line is not None:
                    match["line"] = line

    # Optional: enrich each reference with which symbols in that file call the identifier
    if include_call_chain and store is not None:
        for ref in results:
            ref["calling_symbols"] = _calling_symbols_in_file(
                index, store, owner, name, ref["file"], identifier
            )

    elapsed = (time.perf_counter() - start) * 1000
    return {
        "repo": f"{owner}/{name}",
        "identifier": identifier,
        "reference_count": len(results),
        "references": results[:max_results],
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "truncated": len(results) > max_results,
            "tip": "Tip: use identifiers=[...] to query multiple identifiers in one call. "
            "For usage-site matching beyond imports, also try search_text or find_references(quick=True).",
        },
    }


def _find_references_batch(
    identifiers: list[str],
    index,
    max_results: int,
    owner: str,
    name: str,
    start: float,
) -> dict:
    """Batch logic: loop over identifiers, return grouped results array."""
    if index.imports is None:
        return {
            "repo": f"{owner}/{name}",
            "results": [
                {
                    "identifier": ident,
                    "references": [],
                    "note": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 to enable find_references.",
                }
                for ident in identifiers
            ],
            "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
        }

    inv = _get_import_index(index)

    # Pre-compute lowercased query identifiers
    query_stems = {ident.lower() for ident in identifiers}

    # Build reverse map: identifier_lower -> dict of file -> entry (deduped per file)
    # First-match-wins on match_type: "named" takes priority since it represents
    # a more specific import name match.
    ident_map: dict[str, dict[str, dict]] = {}
    for ident_lower in query_stems:
        entries = inv.get(ident_lower, [])
        for src_file, imp in entries:
            if src_file not in ident_map.get(ident_lower, {}):
                named_match = any(
                    n.lower() == ident_lower for n in imp.get("names", [])
                )
                match_type = "named" if named_match else "specifier_stem"
                ident_map.setdefault(ident_lower, {})[src_file] = {
                    "file": src_file,
                    "specifier": imp["specifier"],
                    "match_type": match_type,
                }

    results = []
    for identifier in identifiers:
        ident_lower = identifier.lower()
        file_results = list(ident_map.get(ident_lower, {}).values())
        file_results.sort(key=lambda r: r["file"])
        results.append(
            {
                "identifier": identifier,
                "reference_count": len(file_results),
                "references": file_results[:max_results],
            }
        )

    return {
        "repo": f"{owner}/{name}",
        "results": results,
        "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
    }


def _attach_runtime_to_response(response: dict, store, owner: str, name: str) -> dict:
    """Phase 2: stamp file-level runtime confidence on every reference in
    a find_references response (single or batch mode). No-op when no traces.
    """
    from ..runtime.confidence import attach_runtime_confidence_by_file

    refs: list[dict] = []
    if "references" in response:
        # Singular mode
        refs.extend(response["references"])
    if "results" in response:
        # Batch mode
        for entry in response.get("results", []):
            refs.extend(entry.get("references", []))
    if not refs:
        return response
    summary = attach_runtime_confidence_by_file(
        refs,
        str(store._sqlite._db_path(owner, name)),
        file_field="file",
    )
    if summary:
        response.setdefault("_meta", {})["runtime_freshness"] = summary
    return response


# ═══════════════════════════════════════════════════════════════════════════
# Mode "importers" — former find_importers logic
# ═══════════════════════════════════════════════════════════════════════════


def _importers_resolve_to_leaves(
    imp: dict,
    src_file: str,
    source_files: frozenset,
    alias_map,
    psr4_map,
    wildcard_map: dict[str, list[str]],
    named_map: dict[str, dict[str, tuple[str, str]]],
) -> set[str]:
    """Resolve `imp` to its direct target plus every leaf reachable through
    re-export chains, with per-name routing for selective re-exports.

    For `export { Foo } from './foo'` in a barrel, only consumers that
    actually import `Foo` from the barrel credit `./foo`.
    """
    from ..parser.imports import expand_barrel_leaves
    from ..parser.imports import resolve_specifier as _rs

    direct = _rs(imp["specifier"], src_file, source_files, alias_map, psr4_map)
    if not direct:
        return set()
    return expand_barrel_leaves(direct, imp.get("names", []), wildcard_map, named_map)


def _importers_find_single(
    file_path: str,
    index,
    max_results: int,
    owner: str,
    name: str,
    start: float,
) -> dict:
    """Core logic for a single file_path query. Returns the original flat shape."""
    from ..parser.imports import build_re_export_maps

    if index.imports is None:
        return {
            "repo": f"{owner}/{name}",
            "file_path": file_path,
            "importers": [],
            "note": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 to enable find_importers.",
            "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
        }

    source_files = frozenset(index.source_files)
    alias_map = index.alias_map
    psr4_map = getattr(index, "psr4_map", None)

    # v1.94.0: symbol-aware barrel resolution.  Wildcard re-exports
    # (`export * from`) credit every leaf to anyone importing the barrel
    # (v1.93 behavior).  Selective re-exports (`export { Foo } from`)
    # credit only consumers that actually import `Foo` from the barrel.
    # Old indexes without `re_export_kind` default to wildcard semantics.
    wildcard_map, named_map = build_re_export_maps(
        index.imports,
        source_files,
        alias_map,
        psr4_map,
    )

    # Build a set of all files that are imported by at least one other file
    # (used for has_importers). Counts barrel-expanded leaves so a leaf
    # definition file isn't flagged as orphan just because its only
    # importers reach it via re-exports.
    files_that_are_imported: set[str] = set()
    for src_file, file_imports in index.imports.items():
        for imp in file_imports:
            if imp.get("is_re_export"):
                continue  # re-exports forward, not consume
            files_that_are_imported.update(
                _importers_resolve_to_leaves(
                    imp,
                    src_file,
                    source_files,
                    alias_map,
                    psr4_map,
                    wildcard_map,
                    named_map,
                )
            )

    results = []

    for src_file, file_imports in index.imports.items():
        if src_file == file_path:
            continue
        for imp in file_imports:
            if imp.get("is_re_export"):
                continue  # re-export-only files are forwarders, not consumers
            leaves = _importers_resolve_to_leaves(
                imp,
                src_file,
                source_files,
                alias_map,
                psr4_map,
                wildcard_map,
                named_map,
            )
            if file_path in leaves:
                results.append(
                    {
                        "file": src_file,
                        "specifier": imp["specifier"],
                        "names": imp.get("names", []),
                        "has_importers": src_file in files_that_are_imported,
                    }
                )
                break  # one match per file is enough

    results.sort(key=lambda r: r["file"])

    elapsed = (time.perf_counter() - start) * 1000
    truncated = len(results) > max_results
    return {
        "repo": f"{owner}/{name}",
        "file_path": file_path,
        "importer_count": len(results),
        "importers": results[:max_results],
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "truncated": truncated,
            "tip": "Tip: use file_paths=['{0}','...'] to query multiple files in one call.".format(
                file_path
            )
            if truncated
            else "Tip: use file_paths=['{0}','...'] to query multiple files in one call. "
            "For usage-site matching beyond imports, also try find_references(quick=True).".format(
                file_path
            ),
        },
    }


def _importers_find_batch(
    file_paths: list[str],
    index,
    max_results: int,
    owner: str,
    name: str,
    start: float,
) -> dict:
    """Batch logic: loop over file_paths, return grouped results array."""
    from ..parser.imports import build_re_export_maps

    if index.imports is None:
        return {
            "repo": f"{owner}/{name}",
            "results": [
                {
                    "file_path": fp,
                    "importers": [],
                    "note": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 to enable find_importers.",
                }
                for fp in file_paths
            ],
            "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
        }

    source_files = frozenset(index.source_files)
    alias_map = index.alias_map
    psr4_map = getattr(index, "psr4_map", None)

    # v1.94.0: symbol-aware barrel resolution. See _importers_find_single.
    wildcard_map, named_map = build_re_export_maps(
        index.imports,
        source_files,
        alias_map,
        psr4_map,
    )

    # Pass 1: build files_that_are_imported (counts barrel-expanded leaves)
    files_that_are_imported: set[str] = set()
    for src_file, file_imports in index.imports.items():
        for imp in file_imports:
            if imp.get("is_re_export"):
                continue
            files_that_are_imported.update(
                _importers_resolve_to_leaves(
                    imp,
                    src_file,
                    source_files,
                    alias_map,
                    psr4_map,
                    wildcard_map,
                    named_map,
                )
            )

    # Pass 2: build import_map. Each importer is recorded under every leaf
    # its specifier reaches (including transitively through barrels).
    import_map: dict[str, list[dict]] = {}
    for src_file, file_imports in index.imports.items():
        for imp in file_imports:
            if imp.get("is_re_export"):
                continue
            leaves = _importers_resolve_to_leaves(
                imp,
                src_file,
                source_files,
                alias_map,
                psr4_map,
                wildcard_map,
                named_map,
            )
            if not leaves:
                continue
            entry = {
                "file": src_file,
                "specifier": imp["specifier"],
                "names": imp.get("names", []),
                "has_importers": src_file in files_that_are_imported,
            }
            for leaf in leaves:
                import_map.setdefault(leaf, []).append(entry)

    results = []
    for file_path in file_paths:
        file_results = import_map.get(file_path, [])  # O(1) lookup
        file_results.sort(key=lambda r: r["file"])
        results.append(
            {
                "file_path": file_path,
                "importer_count": len(file_results),
                "importers": file_results[:max_results],
            }
        )

    return {
        "repo": f"{owner}/{name}",
        "results": results,
        "_meta": {"timing_ms": round((time.perf_counter() - start) * 1000, 1)},
    }


def _importers_find_cross_repo(
    file_path: str,
    repo_id: str,
    all_repos: list[dict],
    store: IndexStore,
    owner: str,
    name: str,
) -> list[dict]:
    """Search other indexed repos for files that import from this repo's package."""
    from .package_registry import extract_root_package_from_specifier

    # Look up this repo's package names from its index
    current_index = store.load_index(owner, name)
    if not current_index:
        return []
    pkg_names = getattr(current_index, "package_names", []) or []
    if not pkg_names:
        return []

    cross_results: list[dict] = []

    for repo_entry in all_repos:
        other_repo_id = repo_entry.get("repo", "")
        if not other_repo_id or other_repo_id == repo_id or "/" not in other_repo_id:
            continue
        other_owner, other_name = other_repo_id.split("/", 1)
        other_index = store.load_index(other_owner, other_name)
        if not other_index or not other_index.imports:
            continue

        other_source_files = frozenset(other_index.source_files)

        for src_file, file_imports in other_index.imports.items():
            for imp in file_imports:
                specifier = imp.get("specifier", "")
                # Determine language from file extension
                lang = other_index.file_languages.get(src_file, "")
                root_pkg = extract_root_package_from_specifier(specifier, lang)
                if root_pkg and root_pkg in pkg_names:
                    cross_results.append(
                        {
                            "file": src_file,
                            "specifier": specifier,
                            "names": imp.get("names", []),
                            "has_importers": True,  # cross-repo — not analyzed further
                            "cross_repo": True,
                            "source_repo": other_repo_id,
                        }
                    )
                    break  # one match per file per other-repo is enough

    return cross_results


def _mode_importers(
    repo: str,
    file_path: Optional[str],
    file_paths: Optional[list[str]],
    max_results: int,
    storage_path: Optional[str],
    cross_repo: bool,
) -> dict:
    """Dispatch the find_importers logic (singular or batch, with optional cross-repo)."""
    # Normalize: some MCP clients send file_paths=[] alongside file_path when they mean singular mode
    if file_path is not None and file_paths is not None and len(file_paths) == 0:
        file_paths = None
    if (file_path is None and file_paths is None) or (
        file_path is not None and file_paths is not None
    ):
        raise ValueError(
            "Provide exactly one of 'file_path' or 'file_paths', not both and not neither."
        )

    # Resolve cross_repo default from config if not explicitly provided
    if not cross_repo:
        from .. import config as _cfg

        cross_repo = bool(_cfg.get("cross_repo_default", False))

    start = time.perf_counter()
    max_results = max(1, min(max_results, 200))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    repo_id = f"{owner}/{name}"

    if file_paths is not None:
        # Cross-repo importer lookup is package-level (whole-repo, not per-file —
        # _importers_find_cross_repo ignores its file_path arg). Attaching one
        # whole-repo result across a multi-file batch would either drop the
        # evidence (the old `""` path) or imply per-file precision that does not
        # exist. Fail closed and point the caller at singular calls (#339).
        if cross_repo and len(file_paths) > 1:
            return {
                "error": (
                    "cross_repo=true is not supported with a multi-file 'file_paths' "
                    "batch: cross-repo importer lookup is package-level, not per-file. "
                    "Use singular 'file_path' calls for cross-repo evidence."
                ),
                "_meta": {
                    "cross_repo_scope": "package",
                    "file_count": len(file_paths),
                },
            }
        result = _importers_find_batch(
            file_paths, index, max_results, owner, name, start
        )
        if cross_repo:
            # len(file_paths) == 1 here — equivalent to the singular cross-repo path.
            try:
                from .list_repos import list_repos

                all_repos = list_repos(storage_path=storage_path).get("repos", [])
                cross_results = _importers_find_cross_repo(
                    file_paths[0],
                    repo_id,
                    all_repos,
                    store,
                    owner,
                    name,
                )
                if cross_results and "results" in result:
                    # Attach cross-repo results to the batch response
                    result["cross_repo_importers"] = cross_results[:max_results]
            except Exception:
                pass
        return result
    else:
        result = _importers_find_single(
            file_path, index, max_results, owner, name, start
        )
        if cross_repo and "importers" in result:
            try:
                from .list_repos import list_repos

                all_repos = list_repos(storage_path=storage_path).get("repos", [])
                cross_results = _importers_find_cross_repo(
                    file_path,
                    repo_id,
                    all_repos,
                    store,
                    owner,
                    name,
                )
                if cross_results:
                    result["importers"] = (
                        result.get("importers", []) + cross_results[:max_results]
                    )
                    result["cross_repo_importer_count"] = len(cross_results)
            except Exception:
                pass
        return result


# ═══════════════════════════════════════════════════════════════════════════
# Mode "related" — former get_related_symbols logic
# ═══════════════════════════════════════════════════════════════════════════

_W_SAME_FILE = 3.0
_W_SHARED_IMPORT = 1.5
_W_NAME_TOKEN = 0.5  # per overlapping token


def _related_tokenize_name(name: str) -> set[str]:
    """Split camelCase/snake_case name into lowercase tokens (≥2 chars)."""
    name = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    return {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", name) if len(t) > 1}


def _related_build_file_importers(
    imports: Optional[dict],
    source_files: frozenset,
    alias_map: Optional[dict] = None,
    psr4_map: Optional[dict] = None,
) -> dict[str, set[str]]:
    """Return {file: {files_that_import_it}} — used for shared-importer signal."""
    from ..parser.imports import resolve_specifier

    if not imports:
        return {}
    rev: dict[str, set[str]] = {}
    for src_file, file_imports in imports.items():
        for imp in file_imports:
            target = resolve_specifier(
                imp["specifier"], src_file, source_files, alias_map, psr4_map
            )
            if target and target != src_file:
                rev.setdefault(target, set()).add(src_file)
    return rev


def _mode_related(
    repo: str,
    symbol_id: str,
    max_results: int,
    storage_path: Optional[str],
) -> dict:
    """Find symbols related to a given symbol using heuristic clustering.

    Three signals are combined:
    * Same file (weight 3.0)
    * Shared importers (weight 1.5)
    * Name token overlap (weight 0.5 per token)
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 50))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    target = index.get_symbol(symbol_id)
    if not target:
        return {"error": f"Symbol not found: {symbol_id}"}

    target_file = target.get("file", "")
    target_tokens = _related_tokenize_name(target.get("name", ""))

    # Build shared-importer map if imports are available
    source_files = frozenset(index.source_files)
    file_importers = _related_build_file_importers(
        index.imports, source_files, index.alias_map, getattr(index, "psr4_map", None)
    )
    target_importers = file_importers.get(target_file, set())

    scores: dict[str, float] = {}

    for sym in index.symbols:
        sid = sym.get("id", "")
        if sid == symbol_id:
            continue

        sym_file = sym.get("file", "")
        score = 0.0

        # Same file
        if sym_file == target_file:
            score += _W_SAME_FILE

        # Shared importers
        elif (
            target_importers and file_importers.get(sym_file, set()) & target_importers
        ):
            score += _W_SHARED_IMPORT

        # Name token overlap
        sym_tokens = _related_tokenize_name(sym.get("name", ""))
        overlap = target_tokens & sym_tokens
        if overlap:
            score += len(overlap) * _W_NAME_TOKEN

        if score > 0:
            scores[sid] = score

    # Sort and take top results
    top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:max_results]

    related = []
    for sid in top_ids:
        sym = index.get_symbol(sid)
        if sym:
            related.append(
                {
                    "id": sym["id"],
                    "name": sym["name"],
                    "kind": sym["kind"],
                    "file": sym["file"],
                    "line": sym["line"],
                    "signature": sym.get("signature", ""),
                    "relatedness_score": round(scores[sid], 2),
                }
            )

    elapsed = (time.perf_counter() - start) * 1000
    return {
        "repo": f"{owner}/{name}",
        "symbol": {
            "id": target["id"],
            "name": target["name"],
            "kind": target["kind"],
            "file": target_file,
        },
        "related_count": len(related),
        "related": related,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


# ═══════════════════════════════════════════════════════════════════════════
# Unified public entry point
# ═══════════════════════════════════════════════════════════════════════════


def find_references(
    repo: str,
    identifier: Optional[str] = None,
    max_results: int = 50,
    storage_path: Optional[str] = None,
    identifiers: Optional[list[str]] = None,
    include_call_chain: bool = False,
    quick: bool = False,
    search_content: bool = True,
    max_content_results: int = 20,
    # ── mode parameter ────────────────────────────────────────────────────
    mode: str = "refs",
    # ── mode="importers" params ──────────────────────────────────────────
    file_path: Optional[str] = None,
    file_paths: Optional[list] = None,
    cross_repo: bool = False,
    # ── mode="related" params ───────────────────────────────────────────
    symbol_id: Optional[str] = None,
) -> dict:
    """Find files that reference, import, or are related to an identifier.

    Three modes are available via the ``mode`` parameter:

    * ``"refs"`` (default): find who references an identifier — the original
      find_references behaviour. Supports singular (``identifier``) and batch
      (``identifiers``), full and quick sub-modes.

    * ``"importers"``: find which files import a given file — the former
      find_importers tool. Use ``file_path`` for singular mode or ``file_paths``
      for batch mode. Set ``cross_repo=True`` to also search other indexed repos.

    * ``"related"``: find related symbols using heuristic clustering — the
      former get_related_symbols tool. Pass ``symbol_id`` to specify the target
      symbol.

    Args:
        repo: Repository identifier (owner/repo or display name).
        mode: ``"refs"`` | ``"importers"`` | ``"related"``. Default ``"refs"``.

    Refs-mode params:
        identifier: The symbol/module name to look for (singular mode).
        identifiers: List of symbol/module names (batch mode).
        max_results: Maximum number of results (full mode only).
        include_call_chain: When True, each reference gains a ``calling_symbols`` list.
        quick: When True, return a lightweight ``is_referenced`` envelope.
        search_content: Also search file contents (quick mode only). Default True.
        max_content_results: Max files per identifier for content search. Default 20.

    Importers-mode params:
        file_path: Target file path within the repo (singular mode).
        file_paths: List of target file paths (batch mode).
        cross_repo: When True, also search other indexed repos for cross-repo importers.

    Related-mode params:
        symbol_id: ID of the symbol to find relatives for.
        max_results: Maximum number of related symbols to return (capped at 50).

    Returns:
        Varies by mode. See individual mode documentation above.

    Raises:
        ValueError: if required mode-specific params are missing or conflicting.
    """
    if mode == "importers":
        return _mode_importers(
            repo=repo,
            file_path=file_path,
            file_paths=file_paths,
            max_results=max_results,
            storage_path=storage_path,
            cross_repo=cross_repo,
        )

    if mode == "related":
        if not symbol_id:
            return {"error": "symbol_id is required when mode='related'."}
        return _mode_related(
            repo=repo,
            symbol_id=symbol_id,
            max_results=max_results,
            storage_path=storage_path,
        )

    # ── mode="refs" (default) ────────────────────────────────────────────────
    if mode != "refs":
        return {
            "error": f"Invalid mode '{mode}'. Must be 'refs', 'importers', or 'related'."
        }

    # Normalize: some MCP clients send identifiers=[] alongside identifier when they mean singular mode
    if identifier is not None and identifiers is not None and len(identifiers) == 0:
        identifiers = None
    if (identifier is None and identifiers is None) or (
        identifier is not None and identifiers is not None
    ):
        raise ValueError(
            "Provide exactly one of 'identifier' or 'identifiers', not both and not neither."
        )

    start = time.perf_counter()
    if not quick:
        max_results = max(1, min(max_results, 200))
    else:
        max_content_results = max(1, min(max_content_results, 100))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    # ── Quick mode (former check_references) ────────────────────────────
    if quick:
        if identifiers is not None:
            return _quick_check_batch(
                identifiers,
                index,
                search_content,
                max_content_results,
                owner,
                name,
                store,
                start,
            )
        else:
            return _quick_check_single(
                identifier=identifier,  # type: ignore[arg-type]
                index=index,
                search_content=search_content,
                max_content_results=max_content_results,
                owner=owner,
                name=name,
                store=store,
                start=start,
            )

    # ── Full mode (original find_references behaviour) ────────────────────
    if identifiers is not None:
        result = _find_references_batch(
            identifiers, index, max_results, owner, name, start
        )
        return _attach_runtime_to_response(result, store, owner, name)
    else:
        repo_key = f"{owner}/{name}"
        specific_key = (identifier, max_results, include_call_chain)
        cached = result_cache_get("find_references", repo_key, specific_key)
        if cached is not None:
            result = dict(cached)
            result["_meta"] = {
                **cached["_meta"],
                "timing_ms": round((time.perf_counter() - start) * 1000, 1),
                "cache_hit": True,
            }
            return _attach_runtime_to_response(result, store, owner, name)
        result = _find_references_single(
            identifier,
            index,
            max_results,
            owner,
            name,
            start,
            include_call_chain=include_call_chain,
            store=store,
        )
        result_cache_put("find_references", repo_key, specific_key, result)
        return _attach_runtime_to_response(result, store, owner, name)
