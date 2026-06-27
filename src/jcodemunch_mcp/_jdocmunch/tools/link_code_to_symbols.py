"""link_code_to_symbols — best-effort jdocmunch ↔ jcodemunch bridge (v1.17.0).

Given a doc index that contains code blocks (v1.17.0+) and a jcodemunch
repo identifier, returns mappings:

  by_block:  {doc_block_id → [code_symbol_id, ...]}
  by_symbol: {code_symbol_id → [doc_block_id, ...]}

Implementation strategy:

1. **Identifier extraction.** Tokenize each code block via the existing
   markdown-aware tokenizer; the BM25 tokenizer already splits CamelCase
   and snake_case, so identifiers like ``Client.authenticate`` arrive as
   ``client``, ``authenticate``. We also pick up ``module.function``
   patterns by splitting on ``.`` first.

2. **jcodemunch lookup.** Best-effort import from a sibling installation:
   ``from jcodemunch_mcp.tools.search_symbols import search_symbols``. When
   that import fails — package not installed, different env, etc. — we
   set ``_meta.bridge_available=false`` and return empty mappings. This
   keeps jdocmunch standalone-friendly and turns the bridge into an
   opt-in advantage when both packages are present.

3. **Aggregation.** Per block, lookup top-K symbols for each identifier
   (via search_symbols's ``query=identifier``, ``max_results=3``); merge.

The return is dense (block_id keys) but capped at ``max_examples`` block
inputs to bound provider calls — large doc sets should call this on a
filtered slice (e.g. results of find_code_examples).
"""

from __future__ import annotations

import time
from typing import Optional

from ..storage import DocStore
from ..retrieval.tokenize import tokenize
from ._bridge import (
    import_search_symbols,
    probe_code_repo,
    code_repo_not_found_result,
)


def _extract_identifiers(code: str) -> list[str]:
    """Return a deduplicated list of identifier tokens from a code block.

    Pre-tokenize on ``.`` to preserve qualified-name pieces like
    ``Client.authenticate`` (becomes ``Client`` and ``authenticate``),
    then route through the markdown tokenizer which handles CamelCase /
    snake_case splitting and stop-word removal.
    """
    if not code:
        return []
    pieces = []
    for chunk in code.replace(".", " ").split():
        pieces.append(chunk)
    out: list[str] = []
    seen: set[str] = set()
    for token in tokenize(" ".join(pieces)):
        if len(token) < 3:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out[:30]  # cap per-block to keep lookup bounded


def link_code_to_symbols(
    repo: str,
    code_repo: str,
    max_examples: int = 200,
    max_symbols_per_block: int = 5,
    storage_path: Optional[str] = None,
) -> dict:
    """Bridge doc code blocks to jcodemunch code symbols.

    Args:
        repo: jdocmunch repo identifier (owner/repo or bare name).
        code_repo: jcodemunch repo identifier — passed verbatim to
            ``search_symbols(repo=...)``.
        max_examples: Cap on input blocks to bound jcodemunch lookups.
        max_symbols_per_block: Cap on resolved symbols per block.
        storage_path: Override DOC_INDEX_PATH for testing.
    """
    t0 = time.perf_counter()

    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    search_symbols = import_search_symbols()
    bridge_available = search_symbols is not None

    # jdoc#68: validate code_repo once up front. A jdocmunch docs handle reused
    # as code_repo is syntactically valid but does not resolve to a jCodeMunch
    # index — previously the per-identifier search loop swallowed that error and
    # returned empty mappings indistinguishable from a genuine zero-match. Only
    # fires on an unresolvable handle; a resolved-but-no-links repo still
    # returns empty mappings as before.
    if bridge_available:
        code_repo_error = probe_code_repo(search_symbols, code_repo)
        if code_repo_error is not None:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return code_repo_not_found_result(
                f"{owner}/{name}", code_repo, latency_ms, code_repo_error
            )

    by_block: dict[str, list[str]] = {}
    by_symbol: dict[str, list[str]] = {}
    blocks_examined = 0

    if bridge_available:
        for sec in index.sections:
            # Fenced blocks plus a synthetic entry for the section's inline code
            # spans (#59) — inline backtick mentions are how prose names symbols.
            inputs: list[tuple[str, list[str]]] = [
                (blk.get("block_id", ""), _extract_identifiers(blk.get("content", "")))
                for blk in (sec.get("code_blocks", []) or [])
            ]
            inline = sec.get("inline_code", []) or []
            if inline:
                inputs.append((f"{sec.get('id', '')}::inline", list(inline)))
            for block_id, idents in inputs:
                if blocks_examined >= max_examples:
                    break
                blocks_examined += 1
                if not idents:
                    continue
                resolved: list[str] = []
                seen_sids: set[str] = set()
                for ident in idents[:8]:  # cap identifiers per block
                    try:
                        out = search_symbols(repo=code_repo, query=ident, max_results=3)
                    except Exception:
                        continue
                    if not isinstance(out, dict):
                        continue
                    for r in out.get("results", []) or []:
                        sid = r.get("id") or r.get("symbol_id") or ""
                        if not sid or sid in seen_sids:
                            continue
                        seen_sids.add(sid)
                        resolved.append(sid)
                        if len(resolved) >= max_symbols_per_block:
                            break
                    if len(resolved) >= max_symbols_per_block:
                        break
                if resolved:
                    by_block[block_id] = resolved
                    for sid in resolved:
                        by_symbol.setdefault(sid, []).append(block_id)
            if blocks_examined >= max_examples:
                break

    return {
        "repo": f"{owner}/{name}",
        "code_repo": code_repo,
        "by_block": by_block,
        "by_symbol": by_symbol,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "bridge_available": bridge_available,
            "blocks_examined": blocks_examined,
            "blocks_linked": len(by_block),
            "symbols_resolved": len(by_symbol),
            "hint": (
                None
                if bridge_available
                else "Install jcodemunch-mcp in this environment to enable code-to-symbol linking."
            ),
        },
    }
