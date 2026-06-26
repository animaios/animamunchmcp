"""get_call_hierarchy: callers and callees for any indexed symbol, N levels deep.

Optionally includes signal chain discovery (``chains=True``) — the former
get_signal_chains behaviour — which traces how external signals (HTTP routes,
CLI commands, task queues, events) propagate through the codebase via the
call graph.
"""

import logging
import re
import time
from collections import defaultdict, deque
from typing import Optional

from ..storage import IndexStore
from ._call_graph import (
    bfs_callees,
    bfs_callers,
    build_symbols_by_file,
    find_direct_callees,
    find_direct_callers,
)
from ._graph_utils import build_adjacency
from ._utils import index_status_to_tool_error, resolve_repo
from .decision_context import resolve_decision_context
from .flow_edges import resolve_flow_edges
from .get_blast_radius import _find_symbol

logger = logging.getLogger(__name__)

# Max traversal depth for transitive impact walk (bounds compute on pathological graphs)
_IMPACT_MAX_DEPTH = 5


# ═══════════════════════════════════════════════════════════════════════════
# Impact computation (formerly get_impact_preview)
# ═══════════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════════
# Signal chains — former get_signal_chains logic
# ═══════════════════════════════════════════════════════════════════════════

_HTTP_RE = re.compile(
    r"@(?:app|router|blueprint|api|bp|flask_app)\."
    r"(?:route|get|post|put|delete|patch|head|options|websocket)"
    r"|@(?:Get|Post|Put|Delete|Patch|Request)Mapping\b"
    r"|@(?:Get|Post|Put|Patch|Delete|Head|Options|All)\s*\(",
    re.IGNORECASE,
)

_CLI_RE = re.compile(
    r"@(?:cli|app)\.command"
    r"|@click\.(?:command|group)"
    r"|typer\..*command",
    re.IGNORECASE,
)

_EVENT_RE = re.compile(
    r"@on_event\b"
    r"|@event_handler\b"
    r"|@(?:app|router)\.websocket\b"
    r"|@receiver\b"
    r"|@signal\b",
    re.IGNORECASE,
)

_TASK_RE = re.compile(
    r"@(?:celery|huey|dramatiq|rq)\."
    r"|@task\b"
    r"|@shared_task\b"
    r"|@periodic_task\b",
    re.IGNORECASE,
)

_MAIN_GUARD_RE = re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']')

_MAIN_FILENAMES = frozenset(
    {
        "__main__.py",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "app.py",
        "main.py",
        "run.py",
        "cli.py",
    }
)

_TEST_PREFIXES = ("test_",)

_KIND_ORDER = [
    ("http", _HTTP_RE),
    ("cli", _CLI_RE),
    ("event", _EVENT_RE),
    ("task", _TASK_RE),
]


def _chains_classify_gateway(
    sym: dict, file_content: Optional[str] = None
) -> Optional[str]:
    """Return the gateway kind for a symbol, or None if it's not a gateway."""
    decorators = sym.get("decorators") or []
    decorator_text = " ".join(str(d) for d in decorators)

    # Check decorator-based kinds
    for kind, pattern in _KIND_ORDER:
        if decorator_text and pattern.search(decorator_text):
            return kind

    # Main guard: check if file has `if __name__ == "__main__"` and symbol is
    # at module level (function, not method)
    sym_name = sym.get("name", "")
    sym_file = sym.get("file", "")
    filename = sym_file.replace("\\", "/").rsplit("/", 1)[-1]

    if filename in _MAIN_FILENAMES and sym.get("kind") in ("function", "class"):
        return "main"

    # Test gateway (must be opted in)
    if sym_name.startswith("test_") and sym.get("kind") == "function":
        return "test"

    return None


def _chains_extract_label(sym: dict, kind: str) -> str:
    """Build a human-readable label for a gateway symbol."""
    sym_name = sym.get("name", "")
    sym_file = sym.get("file", "")
    decorators = sym.get("decorators") or []
    decorator_text = " ".join(str(d) for d in decorators)

    if kind == "http":
        # Try to extract verb + path from decorator
        # Flask/FastAPI: @app.get("/users")
        m = re.search(
            r"@(?:app|router|blueprint|api|bp)\s*\.\s*"
            r"(route|get|post|put|delete|patch|head|options)\s*\(\s*"
            r"[\"']([^\"']+)[\"']",
            decorator_text,
            re.IGNORECASE,
        )
        if m:
            verb = m.group(1).upper()
            if verb == "ROUTE":
                verb = "GET"
            path = m.group(2)
            return f"{verb} {path}"
        # Spring: @GetMapping("/users")
        m = re.search(
            r"@(Get|Post|Put|Delete|Patch|Request)Mapping"
            r"(?:\s*\(\s*(?:value\s*=\s*)?[\"']([^\"']*)[\"'])?",
            decorator_text,
            re.IGNORECASE,
        )
        if m:
            verb = m.group(1).upper()
            path = m.group(2) or "/"
            return f"{verb} {path}"
        # NestJS: @Get("/users")
        m = re.search(
            r"@(Get|Post|Put|Patch|Delete|Head|Options|All)\s*\(\s*"
            r"[\"']([^\"']*)[\"']",
            decorator_text,
            re.IGNORECASE,
        )
        if m:
            verb = m.group(1).upper()
            path = m.group(2) or "/"
            return f"{verb} {path}"
        return f"http:{sym_name}"

    if kind == "cli":
        # @click.command or @app.command — extract command name from decorator
        m = re.search(
            r"@(?:cli|app|click)\.(?:command|group)\s*\(\s*"
            r"(?:name\s*=\s*)?[\"']([^\"']+)[\"']",
            decorator_text,
            re.IGNORECASE,
        )
        if m:
            return f"cli:{m.group(1)}"
        return f"cli:{sym_name}"

    if kind == "task":
        m = re.search(
            r"@(?:celery|huey|dramatiq|rq|shared_task|task)\s*"
            r"(?:\.\s*task\s*)?\(\s*(?:name\s*=\s*)?[\"']([^\"']+)[\"']",
            decorator_text,
            re.IGNORECASE,
        )
        if m:
            return f"task:{m.group(1)}"
        return f"task:{sym_name}"

    if kind == "event":
        return f"event:{sym_name}"

    if kind == "main":
        filename = sym_file.replace("\\", "/").rsplit("/", 1)[-1]
        return f"main:{filename}"

    if kind == "test":
        return f"test:{sym_name}"

    return sym_name


def _chains_bfs(
    index,
    store: IndexStore,
    owner: str,
    repo_name: str,
    gateway_sym: dict,
    symbols_by_file: dict[str, list[dict]],
    max_depth: int,
) -> tuple[list[dict], int]:
    """BFS forward from a gateway through callees.

    Returns (chain_symbols, max_depth_reached) where each chain_symbol is
    {id, name, kind, file, line, depth}.
    """
    sym_id = gateway_sym.get("id", "")
    visited: set[str] = {sym_id}
    queue: deque[tuple[dict, int]] = deque()
    chain: list[dict] = []
    depth_reached = 0
    symbol_index: dict[str, dict] = getattr(index, "_symbol_index", {})

    # Depth-1 callees
    for c in find_direct_callees(
        index, store, owner, repo_name, gateway_sym, symbols_by_file
    ):
        if c["id"] not in visited:
            visited.add(c["id"])
            chain.append({**c, "depth": 1})
            depth_reached = 1
            if max_depth > 1:
                queue.append((c, 1))

    while queue:
        curr_dict, curr_depth = queue.popleft()
        if curr_depth >= max_depth:
            continue
        curr_full = symbol_index.get(curr_dict["id"])
        if not curr_full:
            continue
        for c in find_direct_callees(
            index, store, owner, repo_name, curr_full, symbols_by_file
        ):
            if c["id"] not in visited:
                visited.add(c["id"])
                new_depth = curr_depth + 1
                chain.append({**c, "depth": new_depth})
                depth_reached = max(depth_reached, new_depth)
                if new_depth < max_depth:
                    queue.append((c, new_depth))

    return chain, depth_reached


def _compute_signal_chains(
    repo: str,
    storage_path: Optional[str],
    kind: Optional[str],
    max_depth: int,
    include_flow_edges: bool = True,
) -> dict:
    """Full signal chain discovery — the former get_signal_chains logic.

    Returns the discovery-mode result (all gateways, all chains, orphan stats).
    """
    t0 = time.perf_counter()
    max_depth = max(1, min(8, max_depth))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return {"error": f"No index found for {repo!r}. Run index_folder first."}

    if not index.imports:
        return {
            "error": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0."
        }

    source_files = frozenset(index.source_files)
    symbols_by_file = build_symbols_by_file(index)

    # ------------------------------------------------------------------
    # Phase 1: detect all gateways
    # ------------------------------------------------------------------
    gateways: list[tuple[dict, str, str]] = []  # (sym, kind, label)

    for sym in index.symbols:
        if sym.get("kind") not in ("function", "method"):
            continue
        gw_kind = _chains_classify_gateway(sym)
        if gw_kind is None:
            continue
        if kind and gw_kind != kind:
            continue
        label = _chains_extract_label(sym, gw_kind)
        gateways.append((sym, gw_kind, label))

    # ------------------------------------------------------------------
    # Phase 1b: framework flow edges the call graph can't see.
    # ------------------------------------------------------------------
    flow_edges: list[dict] = []
    renders_by_symbol: dict[str, list[str]] = {}
    flow_summary = {"route_gateways": 0, "unresolved_routes": 0, "render_views": 0}
    if include_flow_edges:
        try:
            flow_edges = resolve_flow_edges(
                index,
                store,
                owner,
                name,
                symbols_by_file=symbols_by_file,
            )
        except Exception:
            logger.debug("flow-edge resolution failed", exc_info=True)
            flow_edges = []

        symbol_index_fe: dict[str, dict] = getattr(index, "_symbol_index", {})
        existing_gateway_ids = {gw_sym.get("id", "") for gw_sym, _, _ in gateways}
        seen_route_ids: set[str] = set()
        for e in flow_edges:
            etype = e.get("type")
            if etype == "route->handler":
                dst_id = e.get("dst_id")
                if not dst_id:
                    flow_summary["unresolved_routes"] += 1
                    continue
                # Already a decorator gateway, or already added via another route.
                if dst_id in existing_gateway_ids or dst_id in seen_route_ids:
                    continue
                handler_sym = symbol_index_fe.get(dst_id)
                if not handler_sym:
                    continue
                if kind and kind != "http":
                    continue
                seen_route_ids.add(dst_id)
                gateways.append((handler_sym, "http", e.get("label", "")))
                flow_summary["route_gateways"] += 1
            elif etype == "render->view":
                src_id = e.get("src_id")
                if src_id:
                    renders_by_symbol.setdefault(src_id, []).append(
                        e.get("dst_name", "")
                    )
                    flow_summary["render_views"] += 1

    if not gateways:
        elapsed = (time.perf_counter() - t0) * 1000
        warning = (
            "No gateways detected. This tool looks for HTTP route decorators "
            "(Flask/FastAPI/Spring/NestJS/ASP.NET), CLI commands (@click.command, "
            "@app.command), task decorators (@celery.task), event handlers, and "
            "standard entry points (main.py, app.py, __main__.py). "
            "If your framework uses a different pattern, the call graph can "
            "still be explored with get_call_hierarchy."
        )
        no_gw_meta: dict = {"timing_ms": round(elapsed, 1)}
        if include_flow_edges:
            no_gw_meta["flow_edges"] = flow_summary
        return {
            "repo": f"{owner}/{name}",
            "gateway_count": 0,
            "chain_count": 0,
            "chains": [],
            "gateway_warning": warning,
            "_meta": no_gw_meta,
        }

    # ------------------------------------------------------------------
    # Phase 2: trace BFS chains from each gateway
    # ------------------------------------------------------------------
    chains: list[dict] = []
    # Track which symbol IDs appear on any chain (for orphan detection)
    symbol_ids_on_chains: set[str] = set()

    for gw_sym, gw_kind, gw_label in gateways:
        chain_syms, depth_reached = _chains_bfs(
            index,
            store,
            owner,
            name,
            gw_sym,
            symbols_by_file,
            max_depth,
        )

        gw_id = gw_sym.get("id", "")
        symbol_ids_on_chains.add(gw_id)
        for cs in chain_syms:
            symbol_ids_on_chains.add(cs["id"])

        # Collect unique files touched
        files_touched: list[str] = []
        seen_files: set[str] = set()
        gw_file = gw_sym.get("file", "")
        if gw_file:
            seen_files.add(gw_file)
            files_touched.append(gw_file)
        for cs in chain_syms:
            f = cs.get("file", "")
            if f and f not in seen_files:
                seen_files.add(f)
                files_touched.append(f)

        # Collect symbol names for compact display
        sym_names = [gw_sym.get("name", "")]
        for cs in chain_syms:
            sym_names.append(cs.get("name", ""))

        chain_entry: dict = {
            "gateway": gw_id,
            "gateway_name": gw_sym.get("name", ""),
            "kind": gw_kind,
            "label": gw_label,
            "depth": depth_reached,
            "reach": len(chain_syms) + 1,  # +1 for the gateway itself
            "symbols": sym_names,
            "files_touched": files_touched,
            "file_count": len(files_touched),
        }

        # Templates rendered anywhere on this chain (render->view flow edges).
        if renders_by_symbol:
            views: list[str] = []
            seen_views: set[str] = set()
            for sid in [gw_id] + [cs["id"] for cs in chain_syms]:
                for tmpl in renders_by_symbol.get(sid, []):
                    if tmpl and tmpl not in seen_views:
                        seen_views.add(tmpl)
                        views.append(tmpl)
            if views:
                chain_entry["views"] = views

        chains.append(chain_entry)

    # Sort chains: http first, then by reach descending
    _kind_order = {"http": 0, "cli": 1, "task": 2, "event": 3, "main": 4, "test": 5}
    chains.sort(key=lambda c: (_kind_order.get(c["kind"], 9), -c["reach"]))

    # ------------------------------------------------------------------
    # Discovery mode — compute orphan stats
    # ------------------------------------------------------------------
    total_fn_method = sum(
        1 for s in index.symbols if s.get("kind") in ("function", "method")
    )
    orphan_count = total_fn_method - len(symbol_ids_on_chains)
    orphan_pct = (
        round(100 * orphan_count / total_fn_method, 1) if total_fn_method else 0.0
    )

    # Kind summary
    kind_counts: dict[str, int] = defaultdict(int)
    for c in chains:
        kind_counts[c["kind"]] += 1

    elapsed = (time.perf_counter() - t0) * 1000
    result = {
        "repo": f"{owner}/{name}",
        "gateway_count": len(gateways),
        "chain_count": len(chains),
        "chains": chains,
        "kind_summary": dict(kind_counts),
        "orphan_symbols": orphan_count,
        "orphan_symbol_pct": orphan_pct,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "max_depth": max_depth,
            "symbols_on_chains": len(symbol_ids_on_chains),
            "total_functions_methods": total_fn_method,
        },
    }
    if include_flow_edges:
        result["_meta"]["flow_edges"] = flow_summary
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Unified public entry point
# ═══════════════════════════════════════════════════════════════════════════


def get_call_hierarchy(
    repo: str,
    symbol_id: str,
    direction: str = "both",
    depth: int = 3,
    storage_path: Optional[str] = None,
    include_impact: bool = False,
    include_decisions: bool = False,
    chains: bool = False,
    kind: Optional[str] = None,
    max_depth: int = 5,
) -> dict:
    """Return incoming callers and outgoing callees for a symbol, N levels deep.

    Uses AST-derived call detection — no LSP required. Callers are found by
    scanning symbols in files that import the target's module; callees are found
    by matching imported-symbol names against the target's source body.

    When ``chains=True``, also returns signal chain discovery data — HTTP routes,
    CLI commands, task queues, and events that flow through this symbol. This
    uses the former get_signal_chains logic.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol name or full ID to analyse. Use search_symbols to find IDs.
        direction: 'callers' | 'callees' | 'both'. Default 'both'.
        depth: Maximum hops to traverse (1–5). Default 3.
        storage_path: Custom storage path.
        include_impact: When True, additionally walk the call graph transitively
            from all callers and return an ``impact`` key. Default False.
        include_decisions: When True and include_impact is True, attach a read-only
            ``decisions`` block inside the impact sub-dict. Default False.
        chains: When True, also discover signal chains (HTTP routes, CLI commands,
            task queues, events) that flow through the codebase and include them
            in a ``signal_chains`` key in the response. Default False.
        kind: Filter signal chain gateways by kind when ``chains=True``:
            http, cli, event, task, main, test. Default None (all kinds).
        max_depth: BFS depth limit per signal chain when ``chains=True``
            (1–8, default 5).

    Returns:
        Dict with symbol info, callers list, callees list, depth_reached, and _meta.
        When ``include_impact=True``, also includes an ``impact`` key.
        When ``chains=True``, also includes a ``signal_chains`` key with the
        full chain discovery data.
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

    # Signal chain discovery (formerly get_signal_chains).
    # Additive: absent chains=True the response is backward-compatible.
    if chains:
        signal_chains = _compute_signal_chains(
            repo=repo,
            storage_path=storage_path,
            kind=kind,
            max_depth=max_depth,
        )
        # If the chain computation returned an error, still attach it so the
        # caller can see why chains are missing while the main result is valid.
        response["signal_chains"] = signal_chains

    # Phase 2: runtime confidence — zero-cost no-op when no traces ingested.
    from ..runtime.confidence import attach_runtime_confidence as _attach_runtime

    _db_path_str = str(store._sqlite._db_path(owner, name))
    _stamped: list[dict] = [response["symbol"], *callers, *callees]
    _summary = _attach_runtime(_stamped, _db_path_str, id_field="id")
    if _summary:
        response["_meta"]["runtime_freshness"] = _summary
    return response
