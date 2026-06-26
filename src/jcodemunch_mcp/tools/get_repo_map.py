"""Query-less, token-budgeted, signature-level repo overview.

For cold-start orientation when no query exists yet. Reuses the existing
PageRank index scores; emits signatures only (no bodies) and greedy-packs
by file rank under the token budget.

When ``group_by="flat"``, returns a flat ranked list of symbols by PageRank
or in-degree centrality (the former ``get_symbol_importance`` behaviour).
"""

import time
from fnmatch import fnmatch
from typing import Optional

from ..storage import IndexStore, cost_avoided, estimate_savings, record_savings
from ._utils import index_status_to_tool_error, resolve_repo
from .get_context_bundle import _count_tokens
from .pagerank import compute_in_out_degrees, compute_pagerank

# Kind priority for picking the representative symbol per file.
_KIND_PRIORITY = {"class": 0, "function": 1, "method": 2, "type": 3, "constant": 4}

_MAX_PER_FILE_CAP = 50
_DEFAULT_BUDGET = 2048


def _signature_or_name(sym: dict) -> str:
    """Return signature when present, otherwise fall back to name + kind."""
    sig = (sym.get("signature") or "").strip()
    if sig:
        return sig
    name = sym.get("name", "")
    kind = sym.get("kind", "")
    return f"{kind} {name}".strip() if name else ""


def get_repo_map(
    repo: str,
    token_budget: int = _DEFAULT_BUDGET,
    scope: Optional[str] = None,
    max_per_file: int = 5,
    include_kinds: Optional[list] = None,
    storage_path: Optional[str] = None,
    group_by: str = "file",
    algorithm: str = "pagerank",
    top_n: int = 20,
) -> dict:
    """Build a query-less, signature-level map of a repository within a token budget.

    Groups symbols by file, ranks files by PageRank on the import graph, and
    greedy-packs signatures (not bodies) under ``token_budget``. Designed for
    cold-start orientation: "I just cloned this repo — what matters here?"

    When ``group_by="flat"``, returns a flat ranked list of symbols sorted by
    PageRank or in-degree centrality (the former ``get_symbol_importance``
    behaviour). In flat mode, ``token_budget``, ``max_per_file`` are ignored;
    ``algorithm`` and ``top_n`` apply.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        token_budget: Hard cap on returned tokens (default 2048). Ignored when
            ``group_by="flat"``.
        scope: Optional subdirectory glob to limit results (e.g. ``src/core/*``).
        max_per_file: Max signatures to include per file (default 5, capped at 50).
            Ignored when ``group_by="flat"``.
        include_kinds: Optional list of symbol kinds to restrict results to
            (e.g. ``['class', 'function']``). Defaults to all kinds.
        storage_path: Custom storage path.
        group_by: ``"file"`` (default, backward-compatible) groups symbols by
            file. ``"flat"`` returns a flat ranked list of symbols sorted by
            importance.
        algorithm: ``"pagerank"`` (default) or ``"degree"`` (simple in-degree
            count). Only used when ``group_by="flat"``.
        top_n: Number of top symbols to return in flat mode (default 20, max 200).
            Only used when ``group_by="flat"``.

    Returns:
        When ``group_by="file"``: dict with ``files`` list (each entry has
        path, rank, score, in_degree, symbols[]) plus summary fields and
        ``_meta``.

        When ``group_by="flat"``: dict with ``ranked_symbols`` list, each entry
        has symbol_id, rank, score, in_degree, out_degree, kind; plus
        ``algorithm`` and ``iterations_to_converge``.
    """
    start = time.perf_counter()

    if group_by not in ("file", "flat"):
        return {"error": "group_by must be 'file' (default) or 'flat'."}

    if algorithm not in ("pagerank", "degree"):
        return {
            "error": f"Invalid algorithm '{algorithm}'. Must be 'pagerank' or 'degree'."
        }

    top_n = max(1, min(top_n, 200))

    if token_budget < 1:
        return {"error": "token_budget must be >= 1"}

    max_per_file = max(1, min(int(max_per_file), _MAX_PER_FILE_CAP))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    # Apply scope filter to the file list used for graph computation
    source_files = index.source_files
    if scope:
        scope_prefix = scope.rstrip("/") + "/"
        source_files = [
            f
            for f in source_files
            if fnmatch(f, scope)
            or f.startswith(scope_prefix)
            or fnmatch(f, scope + "/**")
        ]

    if not source_files:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "files": [],
            "total_tokens": 0,
            "budget_tokens": token_budget,
            "files_included": 0,
            "files_considered": 0,
            "note": "No files match the requested scope."
            if scope
            else "Repository has no indexed files.",
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "tokens_saved": 0,
                "total_tokens_saved": 0,
            },
        }

    _psr4 = getattr(index, "psr4_map", None)

    in_deg, out_deg = compute_in_out_degrees(
        index.imports or {}, source_files, index.alias_map, _psr4
    )

    # --- Flat mode (former get_symbol_importance) --- #
    if group_by == "flat":
        if not index.imports:
            elapsed = (time.perf_counter() - start) * 1000
            return {
                "ranked_symbols": [],
                "algorithm": algorithm,
                "iterations_to_converge": 0,
                "note": "No import graph available. Re-index to build import graph.",
                "_meta": {
                    "timing_ms": round(elapsed, 1),
                    "tokens_saved": 0,
                    "total_tokens_saved": 0,
                },
            }

        iterations = 0
        if algorithm == "pagerank":
            scores, iterations = compute_pagerank(
                index.imports, source_files, index.alias_map, psr4_map=_psr4
            )
        else:
            # degree: score is normalized in-degree (proportion of all imports)
            total_in = sum(in_deg.values()) or 1
            scores = {f: in_deg.get(f, 0) / total_in for f in source_files}

        # Build symbol list: for each file, pick the best representative symbol
        scope_set = set(source_files) if scope else None
        kinds_filter = set(include_kinds) if include_kinds else None

        file_to_best: dict = {}
        for sym in index.symbols:
            f = sym.get("file", "")
            if f not in scores or scores[f] == 0.0:
                continue
            if scope_set is not None and f not in scope_set:
                continue
            if kinds_filter is not None and sym.get("kind") not in kinds_filter:
                continue
            kind_rank = _KIND_PRIORITY.get(sym.get("kind", ""), 5)
            byte_len = sym.get("byte_length", 0)
            prev = file_to_best.get(f)
            if prev is None:
                file_to_best[f] = (kind_rank, -byte_len, sym)
            else:
                if (kind_rank, -byte_len) < (prev[0], prev[1]):
                    file_to_best[f] = (kind_rank, -byte_len, sym)

        ranked_files = sorted(
            [(scores[f], f) for f in file_to_best],
            key=lambda x: x[0],
            reverse=True,
        )

        ranked_symbols = []
        for rank_idx, (score, f) in enumerate(ranked_files[:top_n], start=1):
            _, _, sym = file_to_best[f]
            ranked_symbols.append(
                {
                    "symbol_id": sym["id"],
                    "rank": rank_idx,
                    "score": round(score, 6),
                    "in_degree": in_deg.get(f, 0),
                    "out_degree": out_deg.get(f, 0),
                    "kind": sym.get("kind", ""),
                }
            )

        raw_bytes = sum(index.file_sizes.get(f, 0) for f in source_files)
        response_bytes = sum(len(str(s)) for s in ranked_symbols)
        tokens_saved = estimate_savings(raw_bytes, response_bytes)
        total_saved = record_savings(tokens_saved, tool_name="get_repo_map")
        elapsed = (time.perf_counter() - start) * 1000

        return {
            "ranked_symbols": ranked_symbols,
            "algorithm": algorithm,
            "iterations_to_converge": iterations,
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "tokens_saved": tokens_saved,
                "total_tokens_saved": total_saved,
                **cost_avoided(tokens_saved, total_saved),
            },
        }

    # --- File mode (original get_repo_map behaviour) --- #
    # Reuse cached PageRank when available; compute and cache when not.
    cache = getattr(index, "_bm25_cache", None)
    if cache is not None and "pagerank" in cache and scope is None:
        scores = cache["pagerank"]
    else:
        scores, _iterations = compute_pagerank(
            index.imports or {}, source_files, index.alias_map, psr4_map=_psr4
        )
        if cache is not None and scope is None:
            cache["pagerank"] = scores

    scope_set = set(source_files) if scope else None
    kinds_filter = set(include_kinds) if include_kinds else None

    # Group symbols by file, keep top-K per file by kind priority + size.
    per_file: dict[str, list[dict]] = {}
    for sym in index.symbols:
        f = sym.get("file", "")
        if not f:
            continue
        if scope_set is not None and f not in scope_set:
            continue
        if kinds_filter is not None and sym.get("kind") not in kinds_filter:
            continue
        per_file.setdefault(f, []).append(sym)

    files_considered = 0
    ranked: list[tuple[float, str, list[dict]]] = []
    for f, syms in per_file.items():
        score = scores.get(f, 0.0)
        if score <= 0.0:
            continue
        files_considered += 1
        syms.sort(
            key=lambda s: (
                _KIND_PRIORITY.get(s.get("kind", ""), 9),
                -int(s.get("byte_length", 0)),
                s.get("line", 0),
            )
        )
        ranked.append((score, f, syms[:max_per_file]))

    ranked.sort(key=lambda x: x[0], reverse=True)

    # Greedy pack under token budget; signatures only.
    files_out: list[dict] = []
    total_tokens = 0
    rank_idx = 0
    for score, f, syms in ranked:
        chosen: list[dict] = []
        file_tokens = 0
        for sym in syms:
            sig = _signature_or_name(sym)
            if not sig:
                continue
            sig_tokens = _count_tokens(sig) or 1
            if total_tokens + file_tokens + sig_tokens > token_budget:
                break
            chosen.append(
                {
                    "id": sym["id"],
                    "name": sym.get("name", ""),
                    "kind": sym.get("kind", ""),
                    "line": sym.get("line", 0),
                    "signature": sig,
                    "tokens": sig_tokens,
                }
            )
            file_tokens += sig_tokens
        if not chosen:
            # If a single signature is too large for the remaining budget, stop —
            # subsequent (lower-ranked) files won't fit either at the same cost.
            if file_tokens == 0 and total_tokens >= token_budget:
                break
            continue
        rank_idx += 1
        files_out.append(
            {
                "path": f,
                "rank": rank_idx,
                "score": round(score, 6),
                "in_degree": in_deg.get(f, 0),
                "tokens": file_tokens,
                "symbols": chosen,
            }
        )
        total_tokens += file_tokens
        if total_tokens >= token_budget:
            break

    # Token-savings ledger entry — compare against full-repo source bytes.
    raw_bytes = sum(index.file_sizes.get(f, 0) for f in source_files)
    response_bytes = total_tokens * 4  # signatures only; coarse byte estimate
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="get_repo_map")

    elapsed = (time.perf_counter() - start) * 1000

    result = {
        "files": files_out,
        "total_tokens": total_tokens,
        "budget_tokens": token_budget,
        "files_included": len(files_out),
        "files_considered": files_considered,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }

    # Helpful note when the import graph is empty (e.g. single-file repos).
    if not index.imports:
        result["note"] = (
            "No import graph available — files ranked uniformly. "
            "Re-index after the repo has cross-file imports for meaningful ranking."
        )

    return result
