"""get_call_hierarchy: callers and callees for any indexed symbol, N levels deep."""

import time
from collections import defaultdict
from typing import Optional

from ..storage import IndexStore
from ._call_graph import (
    bfs_callees,
    bfs_callers,
    build_symbols_by_file,
    find_direct_callers,
)
from ._graph_utils import build_adjacency
from ._utils import index_status_to_tool_error, resolve_repo
from .decision_context import resolve_decision_context
from .get_blast_radius import _find_symbol

# Max traversal depth for transitive impact walk (bounds compute on pathological graphs)
_IMPACT_MAX_DEPTH = 5


def _compute_impact(
    index,
    store,
    owner,
    name,
    sym,
    reverse_adj,
    symbols_by_file,
    include_decisions: bool = False,
) -> dict:
    """Walk the call graph transitively from callers — the get_impact_preview logic.

    Returns the impact sub-dict (affected_files, affected_symbol_count,
    affected_symbols, affected_by_file, call_chains, _meta, and optionally
    decisions).
    """
    sym_id = sym.get("id", "")
    symbol_index: dict[str, dict] = getattr(index, "_symbol_index", {})

    # DFS collecting call chains.
    # visited maps symbol_id → chain that reached it (shortest first-seen).
    # chain = [sym_id (target), ..., caller_id]
    visited: dict[str, list[str]] = {sym_id: [sym_id]}
    affected_symbols: list[dict] = []

    # Stack entries: (sym_dict, chain_up_to_this_sym)
    stack: list[tuple[dict, list[str]]] = [(sym, [sym_id])]

    while stack:
        curr_sym, curr_chain = stack.pop()

        if len(curr_chain) > _IMPACT_MAX_DEPTH:
            continue

        callers = find_direct_callers(
            index, store, owner, name, curr_sym, reverse_adj, symbols_by_file
        )

        for caller in callers:
            cid = caller["id"]
            if cid in visited:
                continue
            new_chain = curr_chain + [cid]
            visited[cid] = new_chain

            affected_symbols.append(
                {
                    "id": cid,
                    "name": caller["name"],
                    "kind": caller["kind"],
                    "file": caller["file"],
                    "line": caller["line"],
                    "call_chain": new_chain,
                }
            )

            caller_full = symbol_index.get(cid)
            if caller_full:
                stack.append((caller_full, new_chain))

    # Group by file
    by_file: dict[str, list[dict]] = defaultdict(list)
    for entry in affected_symbols:
        by_file[entry["file"]].append(entry)

    # Determine methodology based on available data
    get_callers_fn = getattr(index, "get_callers_by_name", None)
    callers_by_name = get_callers_fn() if get_callers_fn else None
    has_call_data = bool(callers_by_name)
    if has_call_data:
        impact_methodology = "ast_call_references"
        impact_confidence = "medium"
        impact_source = "ast_call_references"
        impact_tip = (
            "AST-based: shows every symbol that transitively calls this one via stored "
            "call references. More precise than text heuristic. "
            "call_chain = [target_id, intermediate..., caller_id]."
        )
    else:
        impact_methodology = "text_heuristic"
        impact_confidence = "low"
        impact_source = "text_heuristic"
        impact_tip = (
            "Text-heuristic: shows every symbol that transitively calls this one "
            "via word-token matching. May have false positives for common names. "
            "call_chain = [target_id, intermediate..., caller_id]."
        )

    result: dict = {
        "affected_files": len(by_file),
        "affected_symbol_count": len(affected_symbols),
        "affected_symbols": affected_symbols,
        "affected_by_file": {
            f: [
                {"id": s["id"], "name": s["name"], "kind": s["kind"], "line": s["line"]}
                for s in syms
            ]
            for f, syms in sorted(by_file.items())
        },
        "call_chains": [
            {"symbol_id": s["id"], "chain": s["call_chain"]} for s in affected_symbols
        ],
        "_meta": {
            "methodology": impact_methodology,
            "confidence_level": impact_confidence,
            "source": impact_source,
            "tip": impact_tip,
        },
    }

    # Surface decision context (read-only git archaeology) only on request.
    if include_decisions:
        decision_files = [sym.get("file", "")] + sorted(by_file.keys())
        result["decisions"] = resolve_decision_context(
            getattr(index, "source_root", None),
            decision_files,
        )

    return result


def get_call_hierarchy(
    repo: str,
    symbol_id: str,
    direction: str = "both",
    depth: int = 3,
    storage_path: Optional[str] = None,
    include_impact: bool = False,
    include_decisions: bool = False,
) -> dict:
    """Return incoming callers and outgoing callees for a symbol, N levels deep.

    Uses AST-derived call detection — no LSP required. Callers are found by
    scanning symbols in files that import the target's module; callees are found
    by matching imported-symbol names against the target's source body.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol name or full ID to analyse. Use search_symbols to find IDs.
        direction: 'callers' | 'callees' | 'both'. Default 'both'.
        depth: Maximum hops to traverse (1–5). Default 3.
        storage_path: Custom storage path.
        include_impact: When True, additionally walk the call graph transitively
            from all callers and return an ``impact`` key with the same structure
            as the former get_impact_preview output — affected symbols grouped by
            file with call-chain paths showing how each is reached. Default False.
        include_decisions: When True and include_impact is True, attach a read-only
            ``decisions`` block inside the impact sub-dict — decision-bearing commits
            (revert/perf/refactor/rename/bugfix) mined from the git history of
            the focal symbol's file and the impacted files, plus a volatility read.
            Surface-only; nothing is persisted. Default False (spends a few git-log
            calls).

    Returns:
        Dict with symbol info, callers list, callees list, depth_reached, and _meta.
        When include_impact is True, also includes an ``impact`` key with affected
        symbols grouped by file. Each caller/callee entry includes
        {id, name, kind, file, line, depth}.
    """
    depth = max(1, min(depth, 5))
    if direction not in ("callers", "callees", "both"):
        direction = "both"
    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    if index.imports is None:
        return {
            "error": (
                "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 "
                "to enable call hierarchy analysis."
            )
        }

    matches = _find_symbol(index, symbol_id)
    if not matches:
        return {"error": f"Symbol not found: '{symbol_id}'. Try search_symbols first."}
    if len(matches) > 1:
        ambiguous = [
            {"name": s["name"], "file": s["file"], "id": s["id"]} for s in matches
        ]
        return {
            "error": (
                f"Ambiguous symbol '{symbol_id}': found {len(matches)} definitions. "
                "Use the symbol 'id' field to disambiguate."
            ),
            "candidates": ambiguous,
        }

    sym = matches[0]
    symbols_by_file = build_symbols_by_file(index)
    reverse_adj = build_adjacency(
        index.imports,
        frozenset(index.source_files),
        getattr(index, "alias_map", None),
        getattr(index, "psr4_map", None),
        direction="reverse",
    )

    callers: list[dict] = []
    callees: list[dict] = []
    depth_reached = 0

    if direction in ("callers", "both"):
        callers, dr = bfs_callers(
            index, store, owner, name, sym, reverse_adj, symbols_by_file, depth
        )
        depth_reached = max(depth_reached, dr)

    if direction in ("callees", "both"):
        callees, dr = bfs_callees(
            index, store, owner, name, sym, symbols_by_file, depth
        )
        depth_reached = max(depth_reached, dr)

    elapsed = (time.perf_counter() - start) * 1000

    # Build dispatches section from dispatch edges
    ctx_meta = getattr(index, "context_metadata", None) or {}
    dispatch_edge_data = ctx_meta.get("dispatch_edges", [])
    dispatches: list[dict] = []
    if dispatch_edge_data:
        # Group by (interface_name, method_name)
        grouped: dict[tuple[str, str], list[dict]] = {}
        for de in dispatch_edge_data:
            key = (de.get("interface_name", ""), de.get("method_name", ""))
            grouped.setdefault(key, []).append(de)
        for (iface, method), impls in grouped.items():
            dispatches.append(
                {
                    "interface": iface,
                    "method": method,
                    "implementations": [
                        {
                            "name": imp.get("impl_name", ""),
                            "file": imp.get("impl_file", ""),
                            "line": imp.get("impl_line", 0),
                        }
                        for imp in impls
                    ],
                }
            )

    # Determine methodology based on available data
    get_callers = getattr(index, "get_callers_by_name", None)
    callers_by_name = get_callers() if get_callers else None
    has_call_data = bool(callers_by_name)
    has_lsp_data = bool(ctx_meta.get("lsp_edges"))
    has_dispatch_data = bool(dispatch_edge_data)
    if has_dispatch_data:
        methodology = "lsp_dispatch_enriched"
        confidence = "high"
        source = "lsp_bridge + dispatch_resolution + ast_call_references"
        tip = (
            "LSP dispatch-enriched: compiler-grade resolution via language servers with "
            "interface/trait dispatch resolution — concrete implementations of interface "
            "methods are resolved via textDocument/implementation. Each edge has a "
            "'resolution' field: lsp_dispatch (interface dispatch), lsp_resolved "
            "(compiler-grade), ast_resolved (direct AST), ast_inferred (import graph), "
            "or text_matched (heuristic)."
        )
    elif has_lsp_data:
        methodology = "lsp_enriched"
        confidence = "high"
        source = "lsp_bridge + ast_call_references"
        tip = (
            "LSP-enriched: compiler-grade resolution via language servers (pyright, gopls, "
            "typescript-language-server, rust-analyzer) for highest confidence, with AST "
            "call_references and text heuristic as fallback layers. Each edge has a "
            "'resolution' field: lsp_resolved (compiler-grade), ast_resolved (direct AST), "
            "ast_inferred (import graph), or text_matched (heuristic)."
        )
    elif has_call_data:
        methodology = "ast_call_references"
        confidence = "medium"
        source = "ast_call_references"
        tip = (
            "AST-based: call references extracted from tree-sitter AST during indexing. "
            "More precise than text heuristic, but still approximate for dynamic dispatch. "
            "Each edge has a 'resolution' field: ast_resolved (direct AST match), "
            "ast_inferred (resolved via import graph), or text_matched (heuristic). "
            "Enable LSP enrichment for compiler-grade resolution."
        )
    else:
        methodology = "text_heuristic"
        confidence = "low"
        source = "text_heuristic"
        tip = (
            "Text-heuristic: callers = symbols in importing files that mention this "
            "name as a word token; callees = imported symbols mentioned in this "
            "symbol's body. May have false positives for common names or dynamic "
            "dispatch. Use include_impact=true for a transitive 'what breaks?' view."
        )

    # Summarize resolution tiers across all edges
    resolution_counts: dict[str, int] = {}
    for edge in callers + callees:
        r = edge.get("resolution", "unknown")
        resolution_counts[r] = resolution_counts.get(r, 0) + 1

    response: dict = {
        "repo": f"{owner}/{name}",
        "symbol": {
            "id": sym.get("id", ""),
            "name": sym.get("name", ""),
            "kind": sym.get("kind", ""),
            "file": sym.get("file", ""),
            "line": sym.get("line", 0),
        },
        "direction": direction,
        "depth": depth,
        "depth_reached": depth_reached,
        "caller_count": len(callers),
        "callee_count": len(callees),
        "callers": callers,
        "callees": callees,
        "dispatches": dispatches,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "methodology": methodology,
            "confidence_level": confidence,
            "source": source,
            "resolution_tiers": resolution_counts,
            "tip": tip,
        },
    }

    # Transitive impact walk (formerly get_impact_preview).
    # Additive: absent include_impact=True the response is backward-compatible.
    if include_impact:
        impact = _compute_impact(
            index,
            store,
            owner,
            name,
            sym,
            reverse_adj,
            symbols_by_file,
            include_decisions=include_decisions,
        )
        response["impact"] = impact

    # Phase 2: runtime confidence — zero-cost no-op when no traces ingested.
    from ..runtime.confidence import attach_runtime_confidence as _attach_runtime

    _db_path_str = str(store._sqlite._db_path(owner, name))
    _stamped: list[dict] = [response["symbol"], *callers, *callees]
    _summary = _attach_runtime(_stamped, _db_path_str, id_field="id")
    if _summary:
        response["_meta"]["runtime_freshness"] = _summary
    return response
