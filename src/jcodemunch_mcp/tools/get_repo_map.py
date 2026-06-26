"""Query-less, token-budgeted, signature-level repo overview.

For cold-start orientation when no query exists yet. Reuses the existing
PageRank index scores; emits signatures only (no bodies) and greedy-packs
by file rank under the token budget.

When ``group_by="flat"``, returns a flat ranked list of symbols by PageRank
or in-degree centrality (the former ``get_symbol_importance`` behaviour).

Supports two modes via the ``mode`` parameter:
  - ``"map"`` (default): the original get_repo_map behaviour — symbols
    grouped by file, ranked by PageRank, packed under a token budget.
  - ``"outline"``: the former get_repo_outline behaviour — a lighter
    per-directory summary (file counts, language breakdown, symbol counts).
"""

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Optional

from .. import config as _config
from ..parser.imports import resolve_specifier
from ..storage import IndexStore, cost_avoided, estimate_savings, record_savings
from ..storage.index_store import _get_git_head
from ._utils import index_status_to_tool_error, load_repo_index_or_error, resolve_repo
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


# ═══════════════════════════════════════════════════════════════════════════
# Mode "outline" — former get_repo_outline logic
# ═══════════════════════════════════════════════════════════════════════════


def _mode_outline(
    repo: str,
    storage_path: Optional[str],
) -> dict:
    """Return the simpler per-directory outline (file counts, language breakdown,
    symbol counts) — the former get_repo_outline behaviour."""
    start = time.perf_counter()

    index, error, _status = load_repo_index_or_error(repo, storage_path)
    if error:
        return error
    owner, name = index.owner, index.name
    store = IndexStore(base_path=storage_path)

    # Compute directory-level stats
    # For large repos, use 2-level grouping so agents get useful navigation hints.
    _LARGE_REPO_THRESHOLD = 500  # files
    _MAX_DIR_ENTRIES = 40

    dir_file_counts: Counter[str] = Counter()
    for f in index.source_files:
        parts = f.split("/")
        if len(parts) > 1:
            dir_file_counts[parts[0] + "/"] += 1
        else:
            dir_file_counts["(root)"] += 1

    if len(index.source_files) > _LARGE_REPO_THRESHOLD:
        # Expand large top-level dirs into 2-level groupings
        expanded: Counter[str] = Counter()
        for f in index.source_files:
            parts = f.split("/")
            if len(parts) >= 3:
                key = parts[0] + "/" + parts[1] + "/"
            elif len(parts) == 2:
                key = parts[0] + "/"
            else:
                key = "(root)"
            expanded[key] += 1
        # Only use 2-level if it gives more granularity than 1-level
        if len(expanded) > len(dir_file_counts):
            # Cap at _MAX_DIR_ENTRIES, keeping highest-count dirs
            dir_file_counts = Counter(dict(expanded.most_common(_MAX_DIR_ENTRIES)))

    # Symbol kind breakdown
    kind_counts: Counter[str] = Counter()
    for sym in index.symbols:
        kind_counts[sym.get("kind", "unknown")] += 1

    # Token savings: sum of all raw file sizes (user would need to read all files)
    raw_bytes = 0
    content_dir = store._content_dir(owner, name)
    for f in index.source_files:
        try:
            raw_bytes += os.path.getsize(content_dir / f)
        except OSError:
            pass
    # Most-imported files: count in-degree from import graph (PageRank-lite)
    most_imported: list = []
    if index.imports is not None:
        in_degree: Counter[str] = Counter()
        source_files_set = frozenset(index.source_files)
        for src_file, file_imports in index.imports.items():
            for imp in file_imports:
                target = resolve_specifier(
                    imp["specifier"],
                    src_file,
                    source_files_set,
                    index.alias_map,
                    getattr(index, "psr4_map", None),
                )
                if target and target != src_file:
                    in_degree[target] += 1
        most_imported = [
            {"file": f, "imported_by": c} for f, c in in_degree.most_common(10) if c > 1
        ]

    # Most central symbols: top symbols by PageRank score on the import graph
    most_central: list = []
    if index.imports is not None:
        try:
            pr_scores, _ = compute_pagerank(
                index.imports,
                index.source_files,
                index.alias_map,
                psr4_map=getattr(index, "psr4_map", None),
            )
            # Kind priority for picking the representative symbol per file
            _KIND_PRIO = {
                "class": 0,
                "function": 1,
                "method": 2,
                "type": 3,
                "constant": 4,
            }
            file_to_best: dict = {}
            for sym in index.symbols:
                f = sym.get("file", "")
                if not pr_scores.get(f):
                    continue
                kp = _KIND_PRIO.get(sym.get("kind", ""), 5)
                bl = sym.get("byte_length", 0)
                prev = file_to_best.get(f)
                if prev is None or (kp, -bl) < (prev[0], prev[1]):
                    file_to_best[f] = (kp, -bl, sym)
            top_files = sorted(pr_scores.items(), key=lambda x: x[1], reverse=True)[:10]
            for f, pr_score in top_files:
                entry = file_to_best.get(f)
                if entry and pr_score > 0:
                    most_central.append(
                        {
                            "symbol_id": entry[2]["id"],
                            "score": round(pr_score, 6),
                            "kind": entry[2].get("kind", ""),
                        }
                    )
        except Exception:
            pass

    payload_content = {
        "repo": f"{owner}/{name}",
        "indexed_at": index.indexed_at,
        "file_count": len(index.source_files),
        "symbol_count": len(index.symbols),
        "languages": index.languages,
        "directories": dict(dir_file_counts.most_common()),
        "symbol_kinds": dict(kind_counts.most_common()),
    }
    if most_imported:
        payload_content["most_imported_files"] = most_imported
    if most_central:
        payload_content["most_central_symbols"] = most_central
    response_bytes = len(json.dumps(payload_content).encode("utf-8"))
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="get_repo_outline")

    elapsed = (time.perf_counter() - start) * 1000

    # Staleness check — SHA-based (accurate) with time-based fallback
    staleness_warning = None
    is_stale = None
    try:
        from pathlib import Path

        if index.source_root and index.git_head:
            # Local repo: compare SHAs
            current_sha = _get_git_head(Path(index.source_root))
            if current_sha is not None:
                is_stale = current_sha != index.git_head
                if is_stale:
                    staleness_warning = (
                        f"Index SHA ({index.git_head[:12]}) does not match current HEAD "
                        f"({current_sha[:12]}). Run index_folder to refresh."
                    )
        else:
            # GitHub repo or no git: fall back to time-based check.
            # Project-overridable (#301): per-repo freshness expectations.
            staleness_days = _config.get("staleness_days", 7, repo=repo)
            indexed_dt = datetime.fromisoformat(index.indexed_at)
            if indexed_dt.tzinfo is None:
                indexed_dt = indexed_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - indexed_dt).days
            if age_days >= staleness_days:
                is_stale = True
                staleness_warning = (
                    f"Index is {age_days} days old. Run index_repo to refresh."
                )
    except Exception:
        pass

    result = {
        **payload_content,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            "is_stale": is_stale,
            **cost_avoided(tokens_saved, total_saved),
        },
    }
    if staleness_warning:
        result["staleness_warning"] = staleness_warning
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Mode "map" — original get_repo_map logic
# ═══════════════════════════════════════════════════════════════════════════


def _mode_map(
    repo: str,
    token_budget: int,
    scope: Optional[str],
    max_per_file: int,
    include_kinds: Optional[list],
    storage_path: Optional[str],
    group_by: str,
    algorithm: str,
    top_n: int,
) -> dict:
    """Original get_repo_map behaviour — symbols grouped by file, ranked by PageRank."""
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


# ═══════════════════════════════════════════════════════════════════════════
# Unified public entry point
# ═══════════════════════════════════════════════════════════════════════════


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
    mode: str = "map",
) -> dict:
    """Build a query-less, signature-level map or outline of a repository.

    Two modes via the ``mode`` parameter:

    * ``"map"`` (default): the original get_repo_map behaviour. Groups symbols
      by file, ranks files by PageRank on the import graph, and greedy-packs
      signatures (not bodies) under ``token_budget``. Designed for cold-start
      orientation: "I just cloned this repo — what matters here?"

      When ``group_by="flat"``, returns a flat ranked list of symbols sorted by
      PageRank or in-degree centrality (the former ``get_symbol_importance``
      behaviour). In flat mode, ``token_budget``, ``max_per_file`` are ignored;
      ``algorithm`` and ``top_n`` apply.

    * ``"outline"``: the former get_repo_outline behaviour. A lighter weight
      overview returning top-level directories, file counts, language breakdown,
      and symbol counts. The ``token_budget``, ``scope``, ``max_per_file``,
      ``include_kinds``, ``group_by``, ``algorithm``, and ``top_n`` params are
      ignored in outline mode.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        mode: ``"map"`` (default) or ``"outline"``.

    Map-mode params:
        token_budget: Hard cap on returned tokens (default 2048). Ignored when
            ``group_by="flat"``.
        scope: Optional subdirectory glob to limit results (e.g. ``src/core/*``).
        max_per_file: Max signatures to include per file (default 5, capped at 50).
            Ignored when ``group_by="flat"``.
        include_kinds: Optional list of symbol kinds to restrict results to
            (e.g. ``['class', 'function']``). Defaults to all kinds.
        storage_path: Custom storage path.
        group_by: ``"file"`` (default) or ``"flat"``.
        algorithm: ``"pagerank"`` (default) or ``"degree"``. Only used when ``group_by="flat"``.
        top_n: Number of top symbols to return in flat mode (default 20, max 200).

    Returns:
        Map mode: dict with ``files`` list or ``ranked_symbols`` list, plus
        ``_meta``.

        Outline mode: dict with repo info, directories, languages, symbol_kinds,
        and ``_meta``.
    """
    if mode == "outline":
        return _mode_outline(repo=repo, storage_path=storage_path)

    if mode != "map":
        return {"error": f"Invalid mode '{mode}'. Must be 'map' or 'outline'."}

    return _mode_map(
        repo=repo,
        token_budget=token_budget,
        scope=scope,
        max_per_file=max_per_file,
        include_kinds=include_kinds,
        storage_path=storage_path,
        group_by=group_by,
        algorithm=algorithm,
        top_n=top_n,
    )
