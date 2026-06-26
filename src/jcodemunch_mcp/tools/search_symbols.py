"""Search symbols across repository.

Supports three modes:
  - "search" (default): BM25/centrality/combined text search.
  - "winnow": Multi-axis constraint filtering (absorbed from winnow_symbols).
  - "context": Relevance-ranked context packing within a token budget
    (absorbed from get_ranked_context).
"""

import heapq
import json
import logging
import math
import re
import time
from collections import defaultdict
from fnmatch import fnmatch
from typing import Any, Optional

from ..parser.imports import resolve_specifier
from ..storage import IndexStore, cost_avoided, estimate_savings, record_savings
from ._utils import (
    get_file_churn,
    index_status_to_tool_error,
    resolve_fqn,
    resolve_repo,
    run_git,
)
from .get_context_bundle import _count_tokens

BYTES_PER_TOKEN = 4

# Fuzzy search: BM25 score below this auto-triggers the fuzzy pass
_FUZZY_NEAR_MISS_THRESHOLD = 0.1

# Feature 1: Negative evidence threshold (default; overridden by config)
_NEGATIVE_EVIDENCE_THRESHOLD = 0.5

# BM25 hyperparameters (standard Robertson et al. values)
_BM25_K1 = 1.5
_BM25_B = 0.75

# Per-field repetition weights: name appears 3× in the virtual doc, etc.
_FIELD_REPS = {"name": 3, "keywords": 2, "signature": 2, "summary": 1, "docstring": 1}

# Centrality: log-scaled bonus for symbols in frequently-imported files (tiebreaker only)
_CENTRALITY_WEIGHT = 0.3

# PageRank weight for sort_by="combined" (scales PR scores to be meaningful vs BM25 range)
_PR_COMBINED_WEIGHT = 100.0

# Pre-compiled regexes for _tokenize (called ~9000× on cold BM25 build)
_CAMEL_RE = re.compile(r"([a-z])([A-Z])")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{2,}")

# Search result cache (Feature 5 — session-aware routing)
import threading
from collections import OrderedDict

_RESULT_CACHE_MAX = 128
_result_cache: OrderedDict = OrderedDict()
_result_cache_lock = threading.Lock()


def _result_cache_get(key: tuple) -> Optional[dict]:
    """Return cached result for key, or None on miss. Returns a shallow copy."""
    with _result_cache_lock:
        if key in _result_cache:
            _result_cache.move_to_end(key)  # LRU refresh
            cached = _result_cache[key]
            # Track hit count for session state persistence priority
            cached["_hit_count"] = cached.get("_hit_count", 0) + 1
            # Shallow copy top-level + _meta to prevent caller mutations
            result = dict(cached)
            result.pop("_hit_count", None)  # don't leak internal field
            if "_meta" in result:
                result["_meta"] = dict(result["_meta"])
            return result
    return None


def _get_cache_max() -> int:
    try:
        from .. import config as _cfg

        return _cfg.get("search_result_cache_max", _RESULT_CACHE_MAX)
    except Exception:
        return _RESULT_CACHE_MAX


def _result_cache_put(key: tuple, value: dict) -> None:
    """Store result in LRU cache, evicting oldest if full."""
    with _result_cache_lock:
        if key in _result_cache:
            _result_cache.move_to_end(key)
        _result_cache[key] = value
        _max = _get_cache_max()
        while len(_result_cache) > _max:
            _result_cache.popitem(last=False)  # evict oldest


def result_cache_invalidate_repo(repo_key: str) -> int:
    """Evict all cache entries for a specific repo."""
    evicted = 0
    with _result_cache_lock:
        keys_to_evict = [k for k in _result_cache if k[0] == repo_key]
        for k in keys_to_evict:
            del _result_cache[k]
            evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Abbreviation map: bidirectional code abbreviation <-> full form.
# Built once at import time.
# ---------------------------------------------------------------------------
_ABBREV_MAP: dict[str, list[str]] = {
    "db": ["database"],
    "auth": ["authentication", "authorization"],
    "config": ["configuration"],
    "ctx": ["context"],
    "env": ["environment"],
    "err": ["error"],
    "exec": ["execute", "execution"],
    "fn": ["function"],
    "func": ["function"],
    "impl": ["implementation", "implement"],
    "init": ["initialize", "initialization"],
    "iter": ["iterator", "iterate"],
    "len": ["length"],
    "lib": ["library"],
    "max": ["maximum"],
    "mem": ["memory"],
    "min": ["minimum"],
    "msg": ["message"],
    "num": ["number"],
    "obj": ["object"],
    "param": ["parameter"],
    "params": ["parameters"],
    "pkg": ["package"],
    "prev": ["previous"],
    "proc": ["process", "procedure"],
    "prop": ["property"],
    "props": ["properties"],
    "ref": ["reference"],
    "refs": ["references"],
    "repo": ["repository"],
    "req": ["request"],
    "res": ["response", "result"],
    "ret": ["return"],
    "src": ["source"],
    "str": ["string"],
    "sync": ["synchronize", "synchronous"],
    "sys": ["system"],
    "temp": ["temporary"],
    "tmp": ["temporary"],
    "val": ["value"],
    "var": ["variable"],
    "vars": ["variables"],
    # Reverse mappings
    "database": ["db"],
    "authentication": ["auth"],
    "authorization": ["auth"],
    "configuration": ["config"],
    "context": ["ctx"],
    "environment": ["env"],
    "error": ["err"],
    "execute": ["exec"],
    "function": ["func", "fn"],
    "initialize": ["init"],
    "initialization": ["init"],
    "iterator": ["iter"],
    "message": ["msg"],
    "parameter": ["param"],
    "parameters": ["params"],
    "repository": ["repo"],
    "request": ["req"],
    "response": ["res"],
    "temporary": ["temp", "tmp"],
    "variable": ["var"],
    "variables": ["vars"],
}

# Stemming rules: (suffix, replacement, min_base_length)
# Ordered longest-first; doubled-consonant rules before single.
_STEM_RULES: list[tuple[str, str, int]] = [
    ("ation", "", 3),
    ("izing", "ize", 3),
    ("ating", "ate", 3),
    ("nning", "n", 2),
    ("tting", "t", 2),
    ("pping", "p", 2),
    ("gging", "g", 2),
    ("bbing", "b", 2),
    ("dding", "d", 2),
    ("mming", "m", 2),
    ("lling", "l", 2),
    ("sses", "ss", 2),
    ("ness", "", 3),
    ("ment", "", 3),
    ("tion", "", 3),
    ("ized", "ize", 3),
    ("ling", "le", 3),
    ("ring", "r", 3),
    ("ning", "n", 3),
    ("ting", "t", 3),
    ("ping", "p", 3),
    ("bing", "b", 2),
    ("ding", "d", 3),
    ("ging", "g", 3),
    ("king", "k", 3),
    ("ming", "m", 3),
    ("lled", "ll", 3),
    ("nned", "n", 3),
    ("tted", "t", 3),
    ("pped", "p", 3),
    ("gged", "g", 3),
    ("bbed", "b", 3),
    ("dded", "d", 3),
    ("ing", "", 3),
    ("ies", "y", 3),
    ("ed", "", 3),
    ("er", "", 3),
    ("ly", "", 3),
    ("es", "", 4),
]


def _stem(word: str) -> str:
    """Lightweight Porter-style suffix stripping for code identifiers."""
    w = word.lower()
    if len(w) < 5:
        return w
    for suffix, replacement, min_base in _STEM_RULES:
        if w.endswith(suffix):
            base = w[: -len(suffix)]
            if len(base) >= min_base:
                return base + replacement
    # Strip trailing 's' if result is 4+ chars and doesn't end in 's'
    if w.endswith("s") and len(w) >= 5 and w[-2] != "s":
        return w[:-1]
    return w


def _tokenize(text: str) -> list[str]:
    """Split camelCase / snake_case text into tokens with stemming and
    abbreviation expansion for richer BM25 matching."""
    if not text:
        return []
    text = _CAMEL_RE.sub(r"\1_\2", text)
    raw_tokens = [t.lower() for t in _TOKEN_RE.findall(text)]

    result = []
    seen: set[str] = set()
    for tok in raw_tokens:
        result.append(tok)
        seen.add(tok)
        # Stemmed form
        stemmed = _stem(tok)
        if stemmed != tok and stemmed not in seen:
            result.append(stemmed)
            seen.add(stemmed)
        # Abbreviation expansion (canonical forms, not stemmed)
        for key in (tok, stemmed) if stemmed != tok else (tok,):
            for exp in _ABBREV_MAP.get(key, ()):
                if exp not in seen:
                    result.append(exp)
                    seen.add(exp)
    return result


def _sym_tokens(sym: dict) -> list[str]:
    """Weighted token bag for a symbol (repetition = field weight).
    Cached on the symbol dict to avoid re-tokenizing across calls.
    Also caches _tf (term frequency dict) and _dl (document length)."""
    cached = sym.get("_tokens")
    # Fast path: tokens AND tf/dl all present — nothing to do
    if cached is not None and "_tf" in sym:
        return cached
    # Build tokens if not yet cached (or reuse if carried forward without _tf/_dl)
    if cached is not None:
        tokens = cached
    else:
        tokens = []
        tokens += _tokenize(sym.get("name", "")) * _FIELD_REPS["name"]
        tokens += [kw.lower() for kw in sym.get("keywords", [])] * _FIELD_REPS[
            "keywords"
        ]
        tokens += _tokenize(sym.get("signature", "")) * _FIELD_REPS["signature"]
        tokens += _tokenize(sym.get("summary", "")) * _FIELD_REPS["summary"]
        tokens += _tokenize(sym.get("docstring", "")) * _FIELD_REPS["docstring"]
        sym["_tokens"] = tokens
    # Always (re)compute tf/dl — cheap dict ops, ensures consistency
    # NB: _tokens/_tf/_dl are internal; all API-facing code must use explicit
    # key picks, not raw dict passthrough
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    sym["_tf"] = tf
    # T10: use unique token count for _dl so it matches df (document-frequency)
    # which also counts unique tokens per symbol. Using len(tokens) inflates
    # avgdl by the field-repetition weights, distorting BM25 normalisation.
    sym["_dl"] = len(set(tokens))
    return tokens


def _compute_bm25(
    symbols: list[dict],
) -> tuple[dict[str, float], float, dict[str, list[int]]]:
    """Return (idf_map, avgdl, inverted_index) computed over all symbols.

    The inverted_index maps each term to the list of symbol indices that
    contain it, enabling candidate-set narrowing at query time.
    """
    N = len(symbols)
    if N == 0:
        return {}, 0.0, {}
    df: dict[str, int] = {}
    total_dl = 0
    inverted: dict[str, list[int]] = {}
    for i, sym in enumerate(symbols):
        toks = _sym_tokens(sym)
        # T11: always rewrite _dl with the canonical unique-token count.
        # This makes BM25 rebuilds correct even for retained symbols whose _dl
        # was cached before T10 (i.e., with the old len(tokens) formula).
        unique_toks = set(toks)
        dl = len(unique_toks)
        sym["_dl"] = dl
        total_dl += dl
        for t in unique_toks:
            df[t] = df.get(t, 0) + 1
            inverted.setdefault(t, []).append(i)
    avgdl = total_dl / N
    idf = {t: math.log((N - d + 0.5) / (d + 0.5) + 1.0) for t, d in df.items()}
    return idf, avgdl, inverted


def _compute_centrality(
    symbols: list[dict],
    imports: Optional[dict],
    alias_map: Optional[dict] = None,
    psr4_map: Optional[dict] = None,
) -> dict[str, float]:
    """Return {file: log-scaled centrality bonus} based on importer count."""
    if not imports:
        return {}
    source_files = frozenset(s["file"] for s in symbols)
    counts: dict[str, int] = {}
    for src_file, file_imports in imports.items():
        for imp in file_imports:
            target = resolve_specifier(
                imp["specifier"], src_file, source_files, alias_map, psr4_map
            )
            if target:
                counts[target] = counts.get(target, 0) + 1
    return {f: math.log(1 + c) * _CENTRALITY_WEIGHT for f, c in counts.items()}


def _identity_score(sym: dict, query_joined: str, raw_query: str = "") -> float:
    """Identity channel: exact or prefix match on symbol name/ID.

    Returns a high score for exact matches and a decreasing score for
    prefix matches by specificity.  Replaces the old ``50.0`` exact-name hack.

    Scoring:
      - Exact name match          → 50.0
      - Exact ID match            → 50.0
      - Name starts with query    → 30.0
      - ID contains query segment → 20.0
      - No match                  →  0.0
    """
    raw_lower = raw_query.lower() if raw_query else ""
    if not raw_lower and not query_joined:
        return 0.0
    name_lower = sym.get("name", "").lower()
    sym_id_lower = sym.get("id", "").lower()

    # Raw query preserves snake_case/camelCase for exact matches.
    if raw_lower and (raw_lower == name_lower or raw_lower == sym_id_lower):
        return 50.0

    # Tokenized fallback preserves previous semantics for callers that only have terms.
    if query_joined == name_lower or query_joined == sym_id_lower:
        return 50.0

    # Prefix match on name (e.g. query "get_sym" matches "get_symbol_source")
    if query_joined and name_lower.startswith(query_joined):
        return 30.0
    if raw_lower and name_lower.startswith(raw_lower):
        return 30.0

    # Qualified ID segment match (e.g. query "storage.indexstore" matches
    # "src/storage/index_store.py::IndexStore")
    if query_joined and query_joined in sym_id_lower:
        return 20.0
    if raw_lower and raw_lower in sym_id_lower:
        return 20.0

    return 0.0


def _bm25_score(
    sym: dict,
    query_terms: list[str],
    idf: dict[str, float],
    avgdl: float,
    centrality: Optional[dict] = None,
    raw_query: str = "",
) -> float:
    """BM25 score for a single symbol.

    Uses pre-cached _tf and _dl from _sym_tokens() to avoid rebuilding
    the term frequency dict on every call.
    """
    _sym_tokens(sym)  # ensure _tf/_dl are populated
    tf_raw = sym["_tf"]
    dl = sym["_dl"]

    # Identity channel: exact/prefix match on symbol name or ID
    query_joined = " ".join(query_terms)
    score: float = _identity_score(sym, query_joined, raw_query)

    K = _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / max(avgdl, 1.0))
    for term in set(query_terms):
        idf_val = idf.get(term, 0.0)
        if idf_val == 0.0:
            continue
        tf = tf_raw.get(term, 0)
        if tf == 0:
            continue
        score += idf_val * (tf * (_BM25_K1 + 1)) / (tf + K)

    if centrality and score > 0:
        score += centrality.get(sym.get("file", ""), 0.0)

    return score


def _bm25_breakdown(
    sym: dict,
    query_terms: list[str],
    idf: dict[str, float],
    avgdl: float,
    raw_query: str = "",
) -> dict:
    """Per-field BM25 contribution breakdown (for debug mode).

    Uses cached _dl from _sym_tokens() for K computation but re-tokenizes
    per field to attribute score contributions individually.
    """
    _sym_tokens(sym)  # ensure _dl is populated
    dl = sym["_dl"]
    K = _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / max(avgdl, 1.0))

    query_set = set(query_terms)
    # Per-field tokenization is unavoidable here — we need per-field attribution
    fields = {
        "name": _tokenize(sym.get("name", "")) * _FIELD_REPS["name"],
        "keywords": [kw.lower() for kw in sym.get("keywords", [])]
        * _FIELD_REPS["keywords"],
        "signature": _tokenize(sym.get("signature", "")) * _FIELD_REPS["signature"],
        "summary": _tokenize(sym.get("summary", "")) * _FIELD_REPS["summary"],
        "docstring": _tokenize(sym.get("docstring", "")) * _FIELD_REPS["docstring"],
    }
    out: dict[str, float] = {}
    for fname, ftoks in fields.items():
        tf_raw: dict[str, int] = {}
        for t in ftoks:
            tf_raw[t] = tf_raw.get(t, 0) + 1
        field_score = 0.0
        for term in query_set:
            tf = tf_raw.get(term, 0)
            if tf > 0 and idf.get(term, 0.0) > 0:
                field_score += idf[term] * (tf * (_BM25_K1 + 1)) / (tf + K)
        out[fname] = round(field_score, 3)
    query_joined = " ".join(query_terms)
    identity = _identity_score(sym, query_joined, raw_query)
    out["identity"] = identity
    if identity >= 50.0:
        out["identity_type"] = "exact"
    elif identity >= 30.0:
        out["identity_type"] = "prefix"
    elif identity >= 20.0:
        out["identity_type"] = "segment"
    else:
        out["identity_type"] = "none"
    return out


def _trigrams(text: str) -> frozenset:
    """Return trigram frozenset for a lowercased string."""
    s = text.lower()
    if len(s) < 3:
        return frozenset({s}) if s else frozenset()
    return frozenset(s[i : i + 3] for i in range(len(s) - 2))


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein edit distance (Wagner-Fischer, O(min(m,n)) space)."""
    if len(a) > len(b):
        a, b = b, a
    la, lb = len(a), len(b)
    row = list(range(la + 1))
    for j in range(1, lb + 1):
        prev, row[0] = row[0], j
        for i in range(1, la + 1):
            temp = row[i]
            row[i] = min(
                row[i] + 1, row[i - 1] + 1, prev + (0 if a[i - 1] == b[j - 1] else 1)
            )
            prev = temp
    return row[la]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity in pure Python (no numpy).

    Returns 0.0 if either vector is zero-length or the lists differ in size.
    Uses ``math.sqrt`` and ``sum()`` — no external deps.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _materialize_full_entry(entry: dict, index, store, owner: str, name: str) -> None:
    """Inline source/docstring/end_line and update byte_length to reflect full content.

    Mutates entry in place. Called before token-budget packing so the packer sees
    real byte sizes rather than the pre-materialization signature-only skeleton.
    Without this, full + token_budget can overshoot the budget by 5-20x (§1.2).
    """
    sym = index._get_symbol_raw(entry["id"])
    if not sym:
        return
    source = store.get_symbol_content(owner, name, entry["id"], _index=index) or ""
    docstring = sym.get("docstring", "") or ""
    entry["end_line"] = sym.get("end_line", entry["line"])
    entry["docstring"] = docstring
    entry["source"] = source
    entry["byte_length"] = (
        (sym.get("byte_length", 0) or 0)
        + len(docstring.encode("utf-8"))
        + len(source.encode("utf-8"))
    )


def _packing_cost_bytes(entry: dict, detail_level: str) -> int:
    """Bytes an entry charges against token_budget (jcm#328).

    full: the materialized byte_length set by _materialize_full_entry before
    packing (§1.2) — source + docstring dominate the payload there.
    compact/standard: the encoded row itself. In these shapes the symbol's
    source-body byte_length is metadata, not payload; charging it admitted
    "as many rows as fit budget_bytes of source code" (84 rows observed for
    max_results=18) and made tokens_used describe code nobody received.
    """
    if detail_level == "full":
        return entry.get("byte_length", 0)
    return len(json.dumps(entry, default=str))


def _row_summary(sym: dict) -> str:
    """Summary for a result row; empty when it merely echoes the signature.

    Indexes built without an AI summarizer persist signature_fallback output
    (the signature truncated to 120 chars) as the summary, so emitting both
    columns duplicated the full signature in every such row (jcm#328).
    Class/constant/type fallbacks ("Class Foo") carry real signal and pass.
    """
    summary = sym.get("summary", "") or ""
    sig = sym.get("signature", "") or ""
    if summary and sig and (summary == sig or summary == sig[:120]):
        return ""
    return summary


def search_symbols(
    repo: str,
    query: str = "",
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    language: Optional[str] = None,
    decorator: Optional[str] = None,
    max_results: int = 10,
    token_budget: Optional[int] = None,
    detail_level: str = "auto",
    debug: bool = False,
    fuzzy: bool = False,
    fuzzy_threshold: float = 0.4,
    max_edit_distance: int = 2,
    sort_by: str = "relevance",
    semantic: bool = False,
    semantic_weight: float = 0.5,
    semantic_only: bool = False,
    fusion: bool = False,
    storage_path: Optional[str] = None,
    fqn: Optional[str] = None,
    mode: str = "search",
    criteria: Optional[list] = None,
    order: str = "desc",
    strategy: str = "combined",
    include_kinds: Optional[list] = None,
    scope: Optional[str] = None,
    rank_by: str = "importance",
) -> dict:
    """Search for symbols matching a query.

    Supports three modes controlled by the ``mode`` parameter:

    **mode="search"" (default):
        Current BM25 / centrality / combined text search.

    **mode="winnow"**:
        Multi-axis constraint filtering (absorbed from winnow_symbols).
        Accepts ``criteria`` (list of {axis, op, value} dicts), ``rank_by``,
        and ``order`` params.

    **mode="context"**:
        Relevance-ranked context packing within a token budget (absorbed
        from get_ranked_context). Accepts ``strategy``, ``include_kinds``,
        ``scope``, and ``token_budget`` params.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        query: Search query (required for "search" and "context" modes;
            empty string for "winnow" mode).
        kind: Optional filter by symbol kind.
        file_pattern: Optional glob pattern to filter files.
        language: Optional filter by language (e.g., "python", "javascript").
        decorator: Optional filter by decorator (substring match, e.g. 'route', 'property').
        max_results: Maximum results to return (ignored when token_budget is set).
        token_budget: Maximum tokens to consume. Results are greedily packed by
            score until the budget is exhausted. Overrides max_results. The
            budget charges each result's actual payload contribution (jcm#328):
            encoded row size in compact/standard, materialized source bytes in
            full. Compact rows are cheap (~15 tokens), so a budget can admit
            many rows; pass max_results without token_budget when row count is
            the constraint that matters.
        detail_level: Controls result verbosity.
            "auto" (default) picks "compact" for broad discovery (no token_budget,
            no debug, max_results >= 5) and "standard" otherwise. Explicitly-passed
            values are always honored.
            "compact" returns id/name/kind/file/line only (~15 tokens each, ideal
            for discovery).
            "standard" returns signatures and summaries.
            "full" inlines source code, docstring, and end_line.
        debug: When True, include per-field score breakdown in each result.
        fuzzy: Enable fuzzy matching. When True (or when BM25 confidence is low),
            uses trigram overlap + edit distance as fallback. Fuzzy results carry
            match_type="fuzzy", fuzzy_similarity, and edit_distance fields.
        fuzzy_threshold: Minimum Jaccard trigram similarity (0.0–1.0) for fuzzy
            candidates. Default 0.4.
        max_edit_distance: Maximum Levenshtein distance for direct name matching
            (catches typos even when trigrams don't match). Default 2.
        sort_by: Ranking strategy. "relevance" (default) = BM25 + centrality tiebreaker.
            "centrality" = filter by query match, rank by PageRank score.
            "combined" = BM25 + PageRank weighted combination.
        semantic: Enable semantic (embedding-based) search. Requires an embedding
            provider to be configured (JCODEMUNCH_EMBED_MODEL, GOOGLE_API_KEY +
            GOOGLE_EMBED_MODEL, or OPENAI_API_KEY + OPENAI_EMBED_MODEL).
            When False (default) there is zero performance impact and no new imports.
        semantic_weight: Weight for semantic score in hybrid ranking (0.0–1.0).
            BM25 receives ``1 - semantic_weight``. Default 0.5.
            Set to 0.0 for pure BM25 behaviour; set to 1.0 for pure semantic.
        semantic_only: Skip BM25 entirely; rank solely by embedding similarity.
            Implies semantic=True.
        fusion: Enable multi-signal fusion (Weighted Reciprocal Rank) across
            lexical, structural, similarity, and identity channels. Produces
            higher-quality ranking than linear score addition. When True,
            ``sort_by`` is ignored (fusion handles its own ranking).
        storage_path: Custom storage path.
        fqn: PHP fully-qualified class name (e.g. 'App\\Models\\User'). Resolves
            via PSR-4 and uses the class name as query. Alternative to query.
        mode: Operation mode — "search" (default), "winnow", or "context".
        criteria: (mode="winnow") Ordered list of {axis, op, value} filters (AND).
        order: (mode="winnow") Sort direction — "desc" (default) or "asc".
        strategy: (mode="context") Ranking strategy — "combined" (default),
            "bm25", or "centrality".
        include_kinds: (mode="context") Optional symbol kind whitelist.
        scope: (mode="context") Optional subdirectory glob to limit search.
        rank_by: (mode="winnow") Ranking axis — "importance" (default),
            "complexity", "churn", or "name".

    Returns:
        Dict with results and _meta envelope. Format depends on mode:
        "search" → {result_count, results, _meta}
        "winnow" → {repo, criteria, rank_by, matched, total_scanned, results, _meta}
        "context" → {context_items, total_tokens, budget_tokens, items_included, _meta}
    """
    # ── Mode dispatch ──────────────────────────────────────────────────────
    if mode == "winnow":
        return _run_winnow_mode(
            repo=repo,
            criteria=criteria or [],
            rank_by=rank_by,
            order=order,
            max_results=max_results,
            storage_path=storage_path,
        )
    if mode == "context":
        return _run_context_mode(
            repo=repo,
            query=query,
            token_budget=token_budget if token_budget is not None else 4000,
            strategy=strategy,
            include_kinds=include_kinds,
            scope=scope,
            fusion=fusion,
            storage_path=storage_path,
        )
    # Default: mode="search" — existing behaviour below
    if mode not in ("search", "winnow", "context"):
        return {
            "error": f"Invalid mode '{mode}'. Must be 'search', 'winnow', or 'context'."
        }

    if detail_level not in ("auto", "compact", "standard", "full"):
        return {
            "error": f"Invalid detail_level '{detail_level}'. Must be 'auto', 'compact', 'standard', or 'full'."
        }

    if sort_by not in ("relevance", "centrality", "combined"):
        return {
            "error": f"Invalid sort_by '{sort_by}'. Must be 'relevance', 'centrality', or 'combined'."
        }

    # FQN shortcut: resolve PHP FQN and use class name as query
    if fqn:
        _resolved, _ = resolve_fqn(repo, fqn, storage_path)
        if _resolved:
            query = fqn.rsplit("\\", 1)[-1].split("::")[0]

    _MAX_QUERY_LEN = 500
    if len(query) > _MAX_QUERY_LEN:
        return {"error": f"Query too long ({len(query)} chars, max {_MAX_QUERY_LEN})"}

    start = time.perf_counter()
    max_results = max(1, min(max_results, 100))

    # §1.1: Resolve "auto" to a concrete level BEFORE cache_key build so cache
    # keys reflect what we'll actually materialize. Explicit values pass through.
    if detail_level == "auto":
        if token_budget is None and not debug and max_results >= 5:
            detail_level = "compact"
        else:
            detail_level = "standard"

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    # v1.79.0 — apply learned per-repo semantic_weight override when the
    # caller used the default. Treats 0.5 (the function default) as
    # "unspecified"; explicit non-default values always win.
    if (semantic or fusion) and semantic_weight == 0.5:
        from ..retrieval.tuning import get_semantic_weight as _get_tuned_sw

        semantic_weight = _get_tuned_sw(
            f"{owner}/{name}", explicit=None, base_path=storage_path
        )

    # Load index
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    # Feature 5: Search result cache
    # Skip cache for debug/semantic modes (these need fresh data)
    _cacheable = (
        not debug and not semantic and not semantic_only and _get_cache_max() > 0
    )
    _indexed_at = getattr(index, "indexed_at", "")
    _cache_key: Optional[tuple] = None
    if _cacheable:
        # Include indexed_at in key so cache auto-invalidates on reindex
        _cache_key = (
            f"{owner}/{name}",
            _indexed_at,
            query,
            detail_level,
            kind,
            file_pattern,
            language,
            decorator,
            max_results,
            fuzzy,
            fuzzy_threshold,
            max_edit_distance,
            sort_by,
            semantic_weight,
            token_budget,
            fusion,
        )
        _cached = _result_cache_get(_cache_key)
        if _cached is not None:
            # Cache hit — return immediately with fresh timing.
            # Synthesize _meta if the cached result lacks it (#331): a cached
            # entry without _meta must not raise KeyError here, because the
            # dispatcher renders any KeyError as a bogus "missing argument".
            _hit_meta = _cached.setdefault("_meta", {})
            _hit_meta["timing_ms"] = round((time.perf_counter() - start) * 1000, 1)
            _hit_meta["cache_hit"] = True
            return _cached

    # Semantic: validate provider before doing any expensive work
    _semantic_provider: Optional[tuple[str, str]] = None
    if semantic or semantic_only:
        semantic = True  # semantic_only implies semantic
        from .embed_repo import _detect_provider

        _semantic_provider = _detect_provider()
        if _semantic_provider is None:
            return {
                "error": "no_embedding_provider",
                "message": (
                    "No embedding provider is configured. Set one of: "
                    "JCODEMUNCH_EMBED_MODEL (sentence-transformers, free/local), "
                    "GOOGLE_API_KEY + GOOGLE_EMBED_MODEL (Gemini), or "
                    "OPENAI_API_KEY + OPENAI_EMBED_MODEL (OpenAI)."
                ),
            }

    # BM25 corpus stats — cached on CodeIndex, computed once per index load
    query_terms = _tokenize(query) or [query.lower()]
    # Guard: empty string in query_terms causes "" to match every filename
    query_terms = [t for t in query_terms if t]
    cache = index._bm25_cache
    if "idf" not in cache:
        cache["idf"], cache["avgdl"], cache["inverted"] = _compute_bm25(index.symbols)
        cache["centrality"] = _compute_centrality(
            index.symbols,
            index.imports,
            index.alias_map,
            getattr(index, "psr4_map", None),
        )
    idf = cache["idf"]
    avgdl = cache["avgdl"]
    centrality = cache["centrality"]
    inverted = cache["inverted"]

    # PageRank scores — computed and cached when sort_by requires it
    pagerank: dict = {}
    if sort_by in ("centrality", "combined"):
        if "pagerank" not in cache:
            from .pagerank import compute_pagerank

            pr_scores, _ = compute_pagerank(
                index.imports or {},
                index.source_files,
                index.alias_map,
                psr4_map=getattr(index, "psr4_map", None),
            )
            cache["pagerank"] = pr_scores
        pagerank = cache["pagerank"]

    has_filters = bool(kind or file_pattern or language or decorator)

    # Bound the heap size in both modes.
    # token_budget mode: estimate ceiling as budget_bytes / min_symbol_size so the
    # heap stays O(N log K) instead of O(N log N) on large indexes.
    # A 20-byte floor is conservative — real symbols are rarely smaller.
    _MIN_BYTES_PER_SYMBOL = 20
    if token_budget is not None:
        budget_bytes = token_budget * BYTES_PER_TOKEN
        effective_limit = max(max_results, budget_bytes // _MIN_BYTES_PER_SYMBOL)
    else:
        budget_bytes = 0
        effective_limit = max_results

    # ── Semantic / hybrid search path ──────────────────────────────────────
    # Diverges here when semantic=True; pure BM25 path continues below.
    if semantic and _semantic_provider is not None:
        return _search_symbols_semantic(
            index=index,
            store=store,
            owner=owner,
            name=name,
            query=query,
            query_terms=query_terms,
            idf=idf,
            avgdl=avgdl,
            centrality=centrality,
            has_filters=has_filters,
            kind=kind,
            file_pattern=file_pattern,
            language=language,
            decorator=decorator,
            max_results=max_results,
            effective_limit=effective_limit,
            token_budget=token_budget,
            budget_bytes=budget_bytes,
            detail_level=detail_level,
            debug=debug,
            semantic_weight=semantic_weight,
            semantic_only=semantic_only,
            provider=_semantic_provider[0],
            model=_semantic_provider[1],
            start=start,
        )

    # ── Fusion search path ──────────────────────────────────────────────
    # Multi-signal ranking via Weighted Reciprocal Rank (WRR).
    if fusion:
        return _search_symbols_fusion(
            index=index,
            store=store,
            owner=owner,
            name=name,
            query=query,
            query_terms=query_terms,
            idf=idf,
            avgdl=avgdl,
            centrality=centrality,
            pagerank=pagerank,
            has_filters=has_filters,
            kind=kind,
            file_pattern=file_pattern,
            language=language,
            decorator=decorator,
            max_results=max_results,
            effective_limit=effective_limit,
            token_budget=token_budget,
            budget_bytes=budget_bytes,
            detail_level=detail_level,
            debug=debug,
            start=start,
            cache_key=_cache_key,
            cacheable=_cacheable,
        )

    # Narrow candidates using inverted index: only score symbols that
    # contain at least one query term (union of posting lists).
    # Filters (kind/file_pattern/language) are applied AFTER narrowing.
    # Falls back to full scan when no posting lists match (e.g. query
    # terms not in any symbol) to preserve centrality-only results.
    candidate_indices: set[int] = set()
    for term in query_terms:
        posting = inverted.get(term)
        if posting:
            candidate_indices.update(posting)
    if candidate_indices:
        candidates = [index.symbols[i] for i in sorted(candidate_indices)]
    else:
        candidates = index.symbols
    heap: list[tuple[float, int, dict]] = []  # (score, candidates_scored, entry)
    candidates_scored = 0
    max_bm25_score = 0.0

    for sym in candidates:
        if has_filters:
            if kind and sym.get("kind") != kind:
                continue
            if file_pattern and not fnmatch(sym.get("file", ""), file_pattern):
                continue
            if language and sym.get("language") != language:
                continue
            if decorator and not any(
                decorator.lower() in d.lower() for d in (sym.get("decorators") or [])
            ):
                continue

        score = _bm25_score(sym, query_terms, idf, avgdl, centrality, raw_query=query)
        if score <= 0:
            continue

        if score > max_bm25_score:
            max_bm25_score = score
        candidates_scored += 1

        # Compute sort key based on sort_by strategy
        if sort_by == "centrality":
            heap_score = pagerank.get(sym.get("file", ""), 0.0)
        elif sort_by == "combined":
            heap_score = (
                score + pagerank.get(sym.get("file", ""), 0.0) * _PR_COMBINED_WEIGHT
            )
        else:
            heap_score = score

        if detail_level == "compact":
            entry = {
                "id": sym["id"],
                "name": sym["name"],
                "kind": sym["kind"],
                "file": sym["file"],
                "line": sym["line"],
                "byte_length": sym.get("byte_length", 0),
            }
        else:
            entry = {
                "id": sym["id"],
                "kind": sym["kind"],
                "name": sym["name"],
                "file": sym["file"],
                "line": sym["line"],
                "signature": sym["signature"],
                "summary": _row_summary(sym),
                "byte_length": sym.get("byte_length", 0),
            }
        decs = sym.get("decorators") or []
        if decs:
            entry["decorators"] = decs
        if debug:
            entry["score"] = round(score, 3)
            entry["score_breakdown"] = _bm25_breakdown(
                sym, query_terms, idf, avgdl, raw_query=query
            )

        # Bounded heap: O(N log K) instead of O(N log N)
        if len(heap) < effective_limit:
            heapq.heappush(heap, (heap_score, candidates_scored, entry))
        elif heap_score > heap[0][0]:
            heapq.heapreplace(heap, (heap_score, candidates_scored, entry))

    # Extract results sorted by score descending
    scored_results = [
        entry for _, _, entry in sorted(heap, key=lambda x: x[0], reverse=True)
    ]
    heap_count = len(scored_results)  # save before budget packing

    # §1.2: Materialize full-detail payload BEFORE packing so byte_length reflects
    # what will actually be returned. Prior to this fix, the packer saw the pre-full
    # skeleton size and the token_budget could overshoot by 5-20x.
    if detail_level == "full":
        for entry in scored_results:
            _materialize_full_entry(entry, index, store, owner, name)

    budget_truncated = False
    if token_budget is not None:
        # jcm#328: charge what the row actually adds to the response.
        packed, used_bytes = [], 0
        for entry in scored_results:
            b = _packing_cost_bytes(entry, detail_level)
            if used_bytes + b <= budget_bytes:
                packed.append(entry)
                used_bytes += b
        budget_truncated = len(packed) < heap_count
        scored_results = packed

    # Fuzzy pass: runs when explicitly requested OR when BM25 found nothing useful
    run_fuzzy = fuzzy or (max_bm25_score < _FUZZY_NEAR_MISS_THRESHOLD)
    if run_fuzzy:
        for entry in scored_results:
            entry["match_type"] = "exact"

        query_lower = query.lower()
        query_tris = _trigrams(query_lower)
        existing_ids = {e["id"] for e in scored_results}
        fuzzy_hits: list[tuple[dict, float, int]] = []

        # Cap fuzzy candidates to avoid O(N) scan on very large repos.
        # Collect up to 5× max_results candidates, then stop scanning.
        fuzzy_cap = max_results * 5
        for sym in index.symbols:
            if sym["id"] in existing_ids:
                continue
            if has_filters:
                if kind and sym.get("kind") != kind:
                    continue
                if file_pattern and not fnmatch(sym.get("file", ""), file_pattern):
                    continue
                if language and sym.get("language") != language:
                    continue
                if decorator and not any(
                    decorator.lower() in d.lower()
                    for d in (sym.get("decorators") or [])
                ):
                    continue
            name_lower = sym.get("name", "").lower()
            name_tris = _trigrams(name_lower)
            union_size = len(query_tris | name_tris)
            jac = len(query_tris & name_tris) / union_size if union_size else 0.0
            ed = _edit_distance(query_lower, name_lower)
            if jac < fuzzy_threshold and ed > max_edit_distance:
                continue
            fuzzy_hits.append((sym, jac, ed))
            if len(fuzzy_hits) >= fuzzy_cap:
                break

        # Rank: lowest edit distance first, then highest Jaccard as tiebreaker
        fuzzy_hits.sort(key=lambda x: (x[2], -x[1]))

        for sym, jac, ed in fuzzy_hits[:max_results]:
            if detail_level == "compact":
                entry = {
                    "id": sym["id"],
                    "name": sym["name"],
                    "kind": sym["kind"],
                    "file": sym["file"],
                    "line": sym["line"],
                    "byte_length": sym.get("byte_length", 0),
                }
            else:
                entry = {
                    "id": sym["id"],
                    "kind": sym["kind"],
                    "name": sym["name"],
                    "file": sym["file"],
                    "line": sym["line"],
                    "signature": sym["signature"],
                    "summary": sym.get("summary", ""),
                    "byte_length": sym.get("byte_length", 0),
                }
            decs = sym.get("decorators") or []
            if decs:
                entry["decorators"] = decs
            entry["match_type"] = "fuzzy"
            entry["fuzzy_similarity"] = round(jac, 3)
            entry["edit_distance"] = ed
            if debug:
                entry["score"] = 0.0
            if detail_level == "full":
                _materialize_full_entry(entry, index, store, owner, name)
            scored_results.append(entry)

    # Token savings: files containing matches vs symbol byte_lengths of results
    raw_bytes = 0
    seen_files: set = set()
    response_bytes = 0
    for entry in scored_results:
        f = entry["file"]
        if f not in seen_files:
            seen_files.add(f)
            raw_bytes += index.file_sizes.get(f, 0)
        response_bytes += entry["byte_length"]
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="search_symbols")

    elapsed = (time.perf_counter() - start) * 1000

    # Feature 1: Negative evidence — tell the AI when nothing was found
    negative_evidence: Optional[dict] = None
    _ne_threshold = _NEGATIVE_EVIDENCE_THRESHOLD
    try:
        from .. import config as _cfg

        _ne_threshold = _cfg.get(
            "negative_evidence_threshold", _NEGATIVE_EVIDENCE_THRESHOLD
        )
    except Exception:
        pass
    if not scored_results or max_bm25_score < _ne_threshold:
        # Find files whose names partially match query terms
        query_lower = query.lower()
        related_existing: list[str] = []
        for f in index.source_files:
            fname = f.lower().split("/")[-1].split("\\")[-1]
            for term in query_terms:
                if term in fname:
                    related_existing.append(f)
                    break
        related_existing = related_existing[:5]  # cap at 5

        verdict = (
            "no_implementation_found"
            if not scored_results
            else "low_confidence_matches"
        )
        negative_evidence = {
            "verdict": verdict,
            "scanned_symbols": candidates_scored
            if candidates_scored > 0
            else len(index.symbols),
            "scanned_files": len(seen_files) if seen_files else len(index.source_files),
            "best_match_score": round(max_bm25_score, 3) if max_bm25_score > 0 else 0.0,
        }
        if related_existing:
            negative_evidence["related_existing"] = related_existing

    meta = {
        "timing_ms": round(elapsed, 1),
        "total_symbols": len(index.symbols),
        "truncated": candidates_scored > heap_count or budget_truncated,
        "tokens_saved": tokens_saved,
        "total_tokens_saved": total_saved,
        **cost_avoided(tokens_saved, total_saved),
    }
    if token_budget is not None:
        # jcm#328: report payload cost, not source-body bytes.
        used = sum(_packing_cost_bytes(e, detail_level) for e in scored_results)
        meta["token_budget"] = token_budget
        meta["tokens_used"] = used // BYTES_PER_TOKEN
        meta["tokens_remaining"] = max(0, token_budget - used // BYTES_PER_TOKEN)
    if debug:
        meta["candidates_scored"] = candidates_scored
    if scored_results:
        meta["hint"] = (
            "Use get_context_bundle(symbol_id) to retrieve source + imports in one call"
        )

    result = {
        "result_count": len(scored_results),
        "results": scored_results,
        "_meta": meta,
    }
    from ..retrieval.confidence import attach_confidence as _attach_confidence
    from ..retrieval.confidence import extract_ledger_features as _ledger_feats
    from ..retrieval.freshness import FreshnessProbe as _FreshnessProbe
    from ..storage.token_tracker import record_ranking_event as _record_ranking_event

    _probe = _FreshnessProbe(
        source_root=getattr(index, "source_root", "") or None,
        indexed_at=getattr(index, "indexed_at", ""),
        index_sha=getattr(index, "git_head", None),
        file_mtimes=getattr(index, "file_mtimes", None),
    )
    _probe.annotate(scored_results)
    meta["freshness"] = _probe.summary(scored_results)
    # Phase 2: runtime confidence — zero-cost no-op when no traces ingested.
    from ..runtime.confidence import attach_runtime_confidence as _attach_runtime

    _runtime_summary = _attach_runtime(
        scored_results,
        str(store._sqlite._db_path(owner, name)),
        id_field="id",
    )
    if _runtime_summary:
        meta["runtime_freshness"] = _runtime_summary
    _attach_confidence(result, scored_results, is_stale=_probe.repo_is_stale)
    _feat = _ledger_feats(scored_results)
    _record_ranking_event(
        tool="search_symbols",
        repo=f"{owner}/{name}",
        query=query,
        returned_ids=[r.get("id", "") for r in scored_results],
        confidence=result["_meta"].get("confidence"),
        semantic_used=False,
        repo_is_stale=_probe.repo_is_stale,
        **_feat,
    )

    # Feature 1: Add negative_evidence if present
    if negative_evidence is not None:
        result["negative_evidence"] = negative_evidence
        query_display = query[:80]
        if negative_evidence["verdict"] == "no_implementation_found":
            result["\u26a0 warning"] = (
                f"No implementation found for '{query_display}'. "
                f"Do not claim this feature exists."
            )
        else:
            result["\u26a0 warning"] = (
                f"Low-confidence matches for '{query_display}' "
                f"(best score: {negative_evidence['best_match_score']}). "
                f"Verify before claiming this feature exists."
            )

    # Feature 5: Cache the result if cacheable
    if _cacheable and _cache_key is not None:
        _result_cache_put(_cache_key, result)

    return result


def _search_symbols_semantic(
    *,
    index,
    store,
    owner: str,
    name: str,
    query: str,
    query_terms: list[str],
    idf: dict,
    avgdl: float,
    centrality: dict,
    has_filters: bool,
    kind: Optional[str],
    file_pattern: Optional[str],
    language: Optional[str],
    decorator: Optional[str],
    max_results: int,
    effective_limit: int,
    token_budget: Optional[int],
    budget_bytes: int,
    detail_level: str,
    debug: bool,
    semantic_weight: float,
    semantic_only: bool,
    provider: str,
    model: str,
    start: float,
) -> dict:
    """Semantic / hybrid scoring path for search_symbols.

    Two-pass algorithm:
    1. Compute BM25 scores for all filtered symbols (for normalisation).
    2. Compute cosine similarity against the query embedding for all symbols.
    3. Combine: ``combined = (1-w)*bm25_norm + w*cosine``.

    When ``semantic_only=True`` the BM25 component is skipped entirely (w=1).
    When ``semantic_weight=0.0`` the result is identical to pure BM25.
    """
    import logging as _logging

    from ..storage.embedding_store import EmbeddingStore
    from .embed_repo import EMBED_BATCH_SIZE, _gemini_task_aware, _sym_text, embed_texts

    _logger = _logging.getLogger(__name__)

    # Config-driven negative evidence threshold
    _ne_threshold = _NEGATIVE_EVIDENCE_THRESHOLD
    try:
        from .. import config as _cfg

        _ne_threshold = _cfg.get(
            "negative_evidence_threshold", _NEGATIVE_EVIDENCE_THRESHOLD
        )
    except Exception:
        pass

    # Determine task types (Gemini only; no-op for other providers).
    query_task_type: Optional[str] = None
    doc_task_type: Optional[str] = None
    if provider == "gemini" and _gemini_task_aware():
        query_task_type = "CODE_RETRIEVAL_QUERY"
        doc_task_type = "RETRIEVAL_DOCUMENT"

    # ── Get query embedding ────────────────────────────────────────────────
    try:
        query_vec = embed_texts([query], provider, model, task_type=query_task_type)[0]
    except Exception as exc:
        return {"error": f"Failed to embed query: {exc}"}

    # ── Load / lazily compute symbol embeddings ────────────────────────────
    db_path = store._sqlite._db_path(owner, name)
    emb_store = EmbeddingStore(db_path)
    all_emb: dict[str, list[float]] = emb_store.get_all()

    missing = [s for s in index.symbols if s["id"] not in all_emb]
    if missing:
        new_emb: dict[str, list[float]] = {}
        for bi in range(0, len(missing), EMBED_BATCH_SIZE):
            batch = missing[bi : bi + EMBED_BATCH_SIZE]
            try:
                vecs = embed_texts(
                    [_sym_text(s) for s in batch],
                    provider,
                    model,
                    task_type=doc_task_type,
                )
                for j, sym in enumerate(batch):
                    new_emb[sym["id"]] = vecs[j]
            except Exception as exc:
                _logger.warning(
                    "semantic: embedding batch %d failed: %s",
                    bi // EMBED_BATCH_SIZE,
                    exc,
                )
        if new_emb:
            if emb_store.get_dimension() is None:
                dim = len(next(iter(new_emb.values())))
                emb_store.set_dimension(dim, model)
                emb_store.set_task_type(doc_task_type or "")
            emb_store.set_many(new_emb)
            all_emb.update(new_emb)

    # ── Two-pass scoring ───────────────────────────────────────────────────
    # Pass 1: collect BM25 + cosine for every filtered symbol
    raw: list[tuple[dict, float, float]] = []  # (sym, bm25, cosine)
    max_bm25 = 0.0
    max_cos = 0.0

    for sym in index.symbols:
        if has_filters:
            if kind and sym.get("kind") != kind:
                continue
            if file_pattern and not fnmatch(sym.get("file", ""), file_pattern):
                continue
            if language and sym.get("language") != language:
                continue
            if decorator and not any(
                decorator.lower() in d.lower() for d in (sym.get("decorators") or [])
            ):
                continue

        bm25 = (
            0.0
            if semantic_only
            else _bm25_score(sym, query_terms, idf, avgdl, centrality, raw_query=query)
        )
        if bm25 > max_bm25:
            max_bm25 = bm25

        sym_vec = all_emb.get(sym["id"])
        cos = _cosine_similarity(query_vec, sym_vec) if sym_vec else 0.0
        if cos > max_cos:
            max_cos = cos

        raw.append((sym, bm25, cos))

    # Pass 2: normalise BM25 and compute combined score
    scored: list[tuple[float, dict]] = []
    for sym, bm25, cos in raw:
        bm25_norm = (bm25 / max_bm25) if max_bm25 > 0.0 else 0.0
        score = (
            cos
            if semantic_only
            else (1.0 - semantic_weight) * bm25_norm + semantic_weight * cos
        )
        if score <= 0.0:
            continue
        scored.append((score, sym))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:effective_limit]

    # ── Build result entries ───────────────────────────────────────────────
    scored_results: list[dict] = []
    for score, sym in top:
        if detail_level == "compact":
            entry: dict = {
                "id": sym["id"],
                "name": sym["name"],
                "kind": sym["kind"],
                "file": sym["file"],
                "line": sym["line"],
                "byte_length": sym.get("byte_length", 0),
            }
        else:
            entry = {
                "id": sym["id"],
                "kind": sym["kind"],
                "name": sym["name"],
                "file": sym["file"],
                "line": sym["line"],
                "signature": sym["signature"],
                "summary": _row_summary(sym),
                "byte_length": sym.get("byte_length", 0),
            }
        decs = sym.get("decorators") or []
        if decs:
            entry["decorators"] = decs
        if debug:
            entry["score"] = round(score, 4)
        scored_results.append(entry)

    # ── Full detail: materialize BEFORE packing (§1.2) ─────────────────────
    # Semantic path had the same packer/materialization ordering bug.
    if detail_level == "full":
        for entry in scored_results:
            _materialize_full_entry(entry, index, store, owner, name)

    # ── Token budget packing ───────────────────────────────────────────────
    if token_budget is not None:
        # jcm#328: charge what the row actually adds to the response.
        packed: list[dict] = []
        used = 0
        for entry in scored_results:
            b = _packing_cost_bytes(entry, detail_level)
            if used + b <= budget_bytes:
                packed.append(entry)
                used += b
        scored_results = packed

    # ── Meta ───────────────────────────────────────────────────────────────
    raw_bytes = 0
    seen_files: set = set()
    response_bytes = 0
    for entry in scored_results:
        f = entry["file"]
        if f not in seen_files:
            seen_files.add(f)
            raw_bytes += index.file_sizes.get(f, 0)
        response_bytes += entry["byte_length"]
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="search_symbols")
    elapsed = (time.perf_counter() - start) * 1000

    meta: dict = {
        "timing_ms": round(elapsed, 1),
        "total_symbols": len(index.symbols),
        "truncated": len(scored) > len(scored_results),
        "tokens_saved": tokens_saved,
        "total_tokens_saved": total_saved,
        "search_mode": "semantic_only" if semantic_only else "hybrid",
        **cost_avoided(tokens_saved, total_saved),
    }
    if token_budget is not None:
        # jcm#328: report payload cost, not source-body bytes.
        used_bytes = sum(_packing_cost_bytes(e, detail_level) for e in scored_results)
        meta["token_budget"] = token_budget
        meta["tokens_used"] = used_bytes // BYTES_PER_TOKEN
        meta["tokens_remaining"] = max(0, token_budget - used_bytes // BYTES_PER_TOKEN)
    if scored_results:
        meta["hint"] = (
            "Use get_context_bundle(symbol_id) to retrieve source + imports in one call"
        )

    # Feature 1: Negative evidence for semantic search
    result = {
        "result_count": len(scored_results),
        "results": scored_results,
        "_meta": meta,
    }
    from ..retrieval.confidence import attach_confidence as _attach_confidence
    from ..retrieval.confidence import extract_ledger_features as _ledger_feats
    from ..retrieval.freshness import FreshnessProbe as _FreshnessProbe
    from ..storage.token_tracker import record_ranking_event as _record_ranking_event

    _probe = _FreshnessProbe(
        source_root=getattr(index, "source_root", "") or None,
        indexed_at=getattr(index, "indexed_at", ""),
        index_sha=getattr(index, "git_head", None),
        file_mtimes=getattr(index, "file_mtimes", None),
    )
    _probe.annotate(scored_results)
    meta["freshness"] = _probe.summary(scored_results)
    # Phase 2: runtime confidence — zero-cost no-op when no traces ingested.
    from ..runtime.confidence import attach_runtime_confidence as _attach_runtime

    _runtime_summary = _attach_runtime(
        scored_results,
        str(store._sqlite._db_path(owner, name)),
        id_field="id",
    )
    if _runtime_summary:
        meta["runtime_freshness"] = _runtime_summary
    _attach_confidence(result, scored_results, is_stale=_probe.repo_is_stale)
    _feat = _ledger_feats(scored_results)
    _record_ranking_event(
        tool="search_symbols",
        repo=f"{owner}/{name}",
        query=query,
        returned_ids=[r.get("id", "") for r in scored_results],
        confidence=result["_meta"].get("confidence"),
        semantic_used=True,
        repo_is_stale=_probe.repo_is_stale,
        **_feat,
    )
    best_score = max_cos if semantic_only else max_bm25
    if not scored_results or best_score < _ne_threshold:
        # Find files whose names partially match query terms
        query_lower = query.lower()
        related_existing: list[str] = []
        for f in index.source_files:
            fname = f.lower().split("/")[-1].split("\\")[-1]
            for term in query_terms:
                if term in fname:
                    related_existing.append(f)
                    break
        related_existing = related_existing[:5]  # cap at 5

        verdict = (
            "no_implementation_found"
            if not scored_results
            else "low_confidence_matches"
        )
        result["negative_evidence"] = {
            "verdict": verdict,
            "scanned_symbols": len(raw),
            "scanned_files": len(seen_files) if seen_files else len(index.source_files),
            "best_match_score": round(best_score, 3) if best_score > 0 else 0.0,
            **({"related_existing": related_existing} if related_existing else {}),
        }
        # Add warning string alongside negative_evidence
        query_display = query[:80]
        if verdict == "no_implementation_found":
            result["\u26a0 warning"] = (
                f"No implementation found for '{query_display}'. "
                f"Do not claim this feature exists."
            )
        else:
            _best = result["negative_evidence"]["best_match_score"]
            result["\u26a0 warning"] = (
                f"Low-confidence matches for '{query_display}' "
                f"(best score: {_best}). "
                f"Verify before claiming this feature exists."
            )

    return result


def _search_symbols_fusion(
    *,
    index,
    store,
    owner: str,
    name: str,
    query: str,
    query_terms: list[str],
    idf: dict,
    avgdl: float,
    centrality: dict,
    pagerank: dict,
    has_filters: bool,
    kind,
    file_pattern,
    language,
    decorator,
    max_results: int,
    effective_limit: int,
    token_budget,
    budget_bytes: int,
    detail_level: str,
    debug: bool,
    start: float,
    cache_key,
    cacheable: bool,
) -> dict:
    """Fusion search path: multi-signal WRR ranking."""
    from ..retrieval.signal_fusion import (
        build_identity_channel,
        build_lexical_channel,
        build_structural_channel,
        fuse,
        load_fusion_weights,
    )

    # Apply filters to get candidate symbols
    if has_filters:
        from fnmatch import fnmatch as _fnmatch

        candidates = [
            sym
            for sym in index.symbols
            if (not kind or sym.get("kind") == kind)
            and (not file_pattern or _fnmatch(sym.get("file", ""), file_pattern))
            and (not language or sym.get("language") == language)
            and (
                not decorator
                or any(
                    decorator.lower() in d.lower()
                    for d in (sym.get("decorators") or [])
                )
            )
        ]
    else:
        candidates = index.symbols

    if not candidates:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "result_count": 0,
            "results": [],
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "total_symbols": len(index.symbols),
            },
        }

    # Load config weights
    weights, smoothing = load_fusion_weights()

    # Build channels
    channels = []

    # Lexical (BM25 without identity — identity is a separate channel)
    lex_ch = build_lexical_channel(candidates, query_terms, idf, avgdl, centrality)
    channels.append(lex_ch)

    # Identity
    id_ch = build_identity_channel(candidates, query)
    channels.append(id_ch)

    # Structural (PageRank) — only if we have PageRank data
    if not pagerank:
        cache = index._bm25_cache
        if "pagerank" not in cache:
            from .pagerank import compute_pagerank

            pr_scores, _ = compute_pagerank(
                index.imports or {},
                index.source_files,
                index.alias_map,
                psr4_map=getattr(index, "psr4_map", None),
            )
            cache["pagerank"] = pr_scores
        pagerank = cache["pagerank"]

    if pagerank:
        candidate_ids = set(lex_ch.ranked_ids) | set(id_ch.ranked_ids)
        struct_ch = build_structural_channel(candidates, pagerank, candidate_ids)
        channels.append(struct_ch)

    # Similarity channel: only if embeddings exist for this repo
    try:
        from ..storage.embedding_store import EmbeddingStore

        emb_store = EmbeddingStore(
            base_path=store._base_path if hasattr(store, "_base_path") else None
        )
        all_embeddings = emb_store.get_all(owner, name)
        if all_embeddings:
            from .embed_repo import _detect_provider, _embed_texts

            provider = _detect_provider()
            if provider:
                q_emb = _embed_texts([query], provider[0], provider[1])
                if q_emb and q_emb[0]:
                    from ..retrieval.signal_fusion import build_similarity_channel

                    sim_ch = build_similarity_channel(q_emb[0], all_embeddings)
                    channels.append(sim_ch)
    except Exception:
        pass  # Similarity is optional

    # Fuse
    fused = fuse(channels, smoothing=smoothing, weights=weights)

    # Build result list
    sym_by_id = {sym["id"]: sym for sym in candidates}
    scored_results = []

    for fr in fused[:effective_limit]:
        sym = sym_by_id.get(fr.symbol_id)
        if not sym:
            continue

        if detail_level == "compact":
            entry = {
                "id": sym["id"],
                "name": sym["name"],
                "kind": sym["kind"],
                "file": sym["file"],
                "line": sym["line"],
                "byte_length": sym.get("byte_length", 0),
            }
        else:
            entry = {
                "id": sym["id"],
                "kind": sym["kind"],
                "name": sym["name"],
                "file": sym["file"],
                "line": sym["line"],
                "signature": sym["signature"],
                "summary": _row_summary(sym),
                "byte_length": sym.get("byte_length", 0),
            }
        decs = sym.get("decorators") or []
        if decs:
            entry["decorators"] = decs
        if debug:
            entry["fusion_score"] = round(fr.score, 6)
            entry["channel_contributions"] = {
                k: round(v, 6) for k, v in fr.channel_contributions.items()
            }
            entry["channel_ranks"] = fr.channel_ranks
        scored_results.append(entry)

    # Full detail: materialize BEFORE packing so byte_length reflects payload (§1.2).
    if detail_level == "full":
        for entry in scored_results:
            _materialize_full_entry(entry, index, store, owner, name)

    # Budget packing
    budget_truncated = False
    if token_budget is not None:
        # jcm#328: charge what the row actually adds to the response.
        packed, used_bytes = [], 0
        for entry in scored_results:
            b = _packing_cost_bytes(entry, detail_level)
            if used_bytes + b <= budget_bytes:
                packed.append(entry)
                used_bytes += b
        budget_truncated = len(packed) < len(scored_results)
        scored_results = packed

    # Token savings
    raw_bytes = 0
    seen_files: set = set()
    response_bytes = 0
    for entry in scored_results:
        f = entry["file"]
        if f not in seen_files:
            seen_files.add(f)
            raw_bytes += index.file_sizes.get(f, 0)
        response_bytes += entry["byte_length"]
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="search_symbols")

    elapsed = (time.perf_counter() - start) * 1000

    meta = {
        "timing_ms": round(elapsed, 1),
        "total_symbols": len(index.symbols),
        "truncated": len(fused) > len(scored_results) or budget_truncated,
        "tokens_saved": tokens_saved,
        "total_tokens_saved": total_saved,
        **cost_avoided(tokens_saved, total_saved),
        "fusion": True,
        "channels": [ch.name for ch in channels],
    }
    if token_budget is not None:
        # jcm#328: report payload cost, not source-body bytes.
        used = sum(_packing_cost_bytes(e, detail_level) for e in scored_results)
        meta["token_budget"] = token_budget
        meta["tokens_used"] = used // BYTES_PER_TOKEN
        meta["tokens_remaining"] = max(0, token_budget - used // BYTES_PER_TOKEN)
    if debug:
        meta["fusion_weights"] = weights
        meta["fusion_smoothing"] = smoothing
    if scored_results:
        meta["hint"] = (
            "Use get_context_bundle(symbol_id) to retrieve source + imports in one call"
        )

    result = {
        "result_count": len(scored_results),
        "results": scored_results,
        "_meta": meta,
    }
    from ..retrieval.confidence import attach_confidence as _attach_confidence
    from ..retrieval.confidence import extract_ledger_features as _ledger_feats
    from ..retrieval.freshness import FreshnessProbe as _FreshnessProbe
    from ..storage.token_tracker import record_ranking_event as _record_ranking_event

    _probe = _FreshnessProbe(
        source_root=getattr(index, "source_root", "") or None,
        indexed_at=getattr(index, "indexed_at", ""),
        index_sha=getattr(index, "git_head", None),
        file_mtimes=getattr(index, "file_mtimes", None),
    )
    _probe.annotate(scored_results)
    meta["freshness"] = _probe.summary(scored_results)
    # Phase 2: runtime confidence — zero-cost no-op when no traces ingested.
    from ..runtime.confidence import attach_runtime_confidence as _attach_runtime

    _runtime_summary = _attach_runtime(
        scored_results,
        str(store._sqlite._db_path(owner, name)),
        id_field="id",
    )
    if _runtime_summary:
        meta["runtime_freshness"] = _runtime_summary
    _attach_confidence(result, scored_results, is_stale=_probe.repo_is_stale)
    _feat = _ledger_feats(scored_results)
    _record_ranking_event(
        tool="search_symbols_fusion",
        repo=f"{owner}/{name}",
        query=query,
        returned_ids=[r.get("id", "") for r in scored_results],
        confidence=result["_meta"].get("confidence"),
        semantic_used=True,
        repo_is_stale=_probe.repo_is_stale,
        **_feat,
    )

    if cacheable and cache_key is not None:
        _result_cache_put(cache_key, result)

    return result


# =========================================================================
# Winnow-mode helpers (absorbed from winnow_symbols.py)
# =========================================================================

logger = logging.getLogger(__name__)

_SUPPORTED_AXES = frozenset(
    {
        "kind",
        "language",
        "name",
        "file",
        "complexity",
        "decorator",
        "calls",
        "summary",
        "churn",
    }
)
_NUMERIC_OPS = {">", "<", ">=", "<=", "=="}
_SET_OPS = {"in", "eq"}
_REGEX_OPS = {"matches"}
_CONTAINS_OPS = {"contains"}

_RANK_AXES = frozenset({"importance", "complexity", "churn", "name"})


def _normalize_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    return [str(val)]


def _apply_numeric(op: str, sym_val: Any, threshold: Any) -> bool:
    try:
        sv = float(sym_val or 0)
        tv = float(threshold)
    except (TypeError, ValueError):
        return False
    if op == ">":
        return sv > tv
    if op == "<":
        return sv < tv
    if op == ">=":
        return sv >= tv
    if op == "<=":
        return sv <= tv
    if op == "==":
        return sv == tv
    return False


def _match_criterion(
    sym: dict,
    criterion: dict,
    file_churn: dict[str, int],
) -> bool:
    axis = criterion.get("axis")
    op = criterion.get("op")
    value = criterion.get("value")

    if axis == "kind":
        if op not in _SET_OPS:
            return False
        allowed = _normalize_list(value)
        return sym.get("kind", "") in allowed

    if axis == "language":
        if op not in _SET_OPS:
            return False
        allowed = _normalize_list(value)
        return sym.get("language", "") in allowed

    if axis == "name":
        name = sym.get("name", "") or ""
        if op == "eq":
            return name == str(value)
        if op == "matches":
            try:
                return re.search(str(value), name) is not None
            except re.error:
                return False
        return False

    if axis == "file":
        if op != "matches":
            return False
        import fnmatch as _fnmatch

        file_path = (sym.get("file", "") or "").replace("\\", "/")
        pattern = str(value).replace("\\", "/")
        return _fnmatch.fnmatch(file_path, pattern)

    if axis == "complexity":
        return _apply_numeric(op, sym.get("cyclomatic"), value)

    if axis == "decorator":
        if op != "contains":
            return False
        needles = [n.lower() for n in _normalize_list(value)]
        haystack = [d.lower() for d in (sym.get("decorators") or [])]
        return any(any(n in d for d in haystack) for n in needles)

    if axis == "calls":
        if op != "contains":
            return False
        needles = [n.lower() for n in _normalize_list(value)]
        haystack = [c.lower() for c in (sym.get("call_references") or [])]
        return any(any(n in c for c in haystack) for n in needles)

    if axis == "summary":
        if op != "contains":
            return False
        needle = str(value).lower()
        hay = f"{sym.get('summary', '')} {sym.get('docstring', '')}".lower()
        return needle in hay

    if axis == "churn":
        file_norm = (sym.get("file", "") or "").replace("\\", "/")
        count = file_churn.get(file_norm, 0)
        return _apply_numeric(op, count, value)

    return False


def _validate_criteria(criteria: list) -> Optional[str]:
    if not isinstance(criteria, list):
        return "criteria must be a list"
    for i, c in enumerate(criteria):
        if not isinstance(c, dict):
            return f"criteria[{i}] must be an object"
        axis = c.get("axis")
        op = c.get("op")
        if axis not in _SUPPORTED_AXES:
            return (
                f"criteria[{i}].axis '{axis}' not supported. "
                f"Supported: {sorted(_SUPPORTED_AXES)}"
            )
        if op not in (_NUMERIC_OPS | _SET_OPS | _REGEX_OPS | _CONTAINS_OPS):
            return f"criteria[{i}].op '{op}' not recognized"
        if "value" not in c:
            return f"criteria[{i}] missing 'value'"
        # Validate regex patterns upfront to avoid silent per-symbol failures
        if op == "matches":
            try:
                re.compile(str(c["value"]))
            except re.error as e:
                return f"criteria[{i}].value: invalid regex pattern: {e}"
    return None


def _run_winnow_mode(
    repo: str,
    criteria: list,
    rank_by: str = "importance",
    order: str = "desc",
    max_results: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Run a constraint-chain query against an indexed repo (mode=\"winnow\").

    Args:
        repo:          Repo id (owner/repo or bare name).
        criteria:      Ordered list of ``{axis, op, value}`` filters (AND).
        rank_by:       ``importance`` (default) | ``complexity`` | ``churn`` | ``name``.
        order:         ``desc`` (default) | ``asc``.
        max_results:   Hard cap on returned symbols.
        storage_path:  Optional storage override.

    Returns:
        Dict with ``repo``, ``criteria``, ``rank_by``, ``matched``, ``total_scanned``,
        ``results``, ``_meta``.
    """
    t0 = time.perf_counter()

    if rank_by not in _RANK_AXES:
        return {
            "error": f"rank_by '{rank_by}' invalid. Must be one of {sorted(_RANK_AXES)}"
        }
    if order not in ("asc", "desc"):
        return {"error": f"order '{order}' invalid. Must be 'asc' or 'desc'"}

    err = _validate_criteria(criteria or [])
    if err:
        return {"error": err}

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return {"error": f"No index found for {repo!r}. Run index_folder first."}

    # Churn window: largest window requested across churn criteria, default 90.
    churn_window = 90
    for c in criteria:
        if c.get("axis") == "churn":
            w = c.get("window_days")
            if isinstance(w, int) and w > 0:
                churn_window = max(churn_window, w)

    file_churn: dict[str, int] = {}
    git_available = False
    needs_churn = rank_by == "churn" or any(c.get("axis") == "churn" for c in criteria)
    if needs_churn and index.source_root:
        rc, _, _ = run_git(["rev-parse", "--git-dir"], cwd=index.source_root)
        if rc == 0:
            git_available = True
            raw = get_file_churn(index.source_root, churn_window)
            file_churn = {k.replace("\\", "/"): v for k, v in raw.items()}

    # File-level PageRank → per-symbol importance (file score inherited).
    importance_by_file: dict[str, float] = {}
    if rank_by == "importance" and index.imports:
        try:
            from .pagerank import compute_pagerank

            source_files = [s.get("file", "") for s in index.symbols if s.get("file")]
            source_files = sorted(set(source_files))
            scores, _iters = compute_pagerank(
                index.imports,
                source_files,
                alias_map=getattr(index, "alias_map", None),
                psr4_map=getattr(index, "psr4_map", None),
            )
            importance_by_file = scores
        except Exception as exc:
            logger.debug(
                "pagerank failed for %s/%s: %s", owner, name, exc, exc_info=True
            )

    survivors: list[dict] = []
    total = 0
    for sym in index.symbols:
        total += 1
        ok = True
        for c in criteria:
            if not _match_criterion(sym, c, file_churn):
                ok = False
                break
        if not ok:
            continue

        file_norm = (sym.get("file", "") or "").replace("\\", "/")
        churn = file_churn.get(file_norm, 0)
        importance = round(importance_by_file.get(sym.get("file", ""), 0.0), 6)

        survivors.append(
            {
                "symbol_id": sym.get("id", ""),
                "name": sym.get("name", ""),
                "kind": sym.get("kind", ""),
                "language": sym.get("language", ""),
                "file": sym.get("file", ""),
                "line": sym.get("line") or 0,
                "signature": sym.get("signature", ""),
                "summary": sym.get("summary", ""),
                "cyclomatic": sym.get("cyclomatic") or 0,
                "churn": churn,
                "importance": importance,
            }
        )

    reverse = order == "desc"
    if rank_by == "importance":
        survivors.sort(key=lambda x: x["importance"], reverse=reverse)
    elif rank_by == "complexity":
        survivors.sort(key=lambda x: x["cyclomatic"], reverse=reverse)
    elif rank_by == "churn":
        survivors.sort(key=lambda x: x["churn"], reverse=reverse)
    elif rank_by == "name":
        survivors.sort(key=lambda x: x["name"].lower(), reverse=reverse)

    capped = max(1, int(max_results))
    results = survivors[:capped]

    # Telemetry: estimate tokens saved vs agent doing the work by hand.
    try:
        raw_bytes = sum((s.get("byte_length") or 0) for s in index.symbols)
        response_bytes = sum(
            len(r.get("signature", "")) + len(r.get("summary", "")) + 200
            for r in results
        )
        tokens_saved = estimate_savings(raw_bytes, response_bytes)
        total_saved = record_savings(
            tokens_saved, base_path=storage_path, tool_name="winnow_symbols"
        )
        cost = cost_avoided(tokens_saved, total_saved)
    except Exception as exc:
        logger.debug("telemetry failed: %s", exc, exc_info=True)
        tokens_saved, total_saved, cost = 0, 0, {}

    return {
        "repo": f"{owner}/{name}",
        "criteria": criteria,
        "rank_by": rank_by,
        "order": order,
        "matched": len(survivors),
        "total_scanned": total,
        "truncated": len(survivors) > capped,
        "git_available": git_available,
        "results": results,
        "_meta": {
            "timing_ms": round((time.perf_counter() - t0) * 1000, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            "cost_avoided": cost,
            "supported_axes": sorted(_SUPPORTED_AXES),
        },
    }


# =========================================================================
# Context-mode helpers (absorbed from get_ranked_context.py)
# =========================================================================

# Weight for PageRank when strategy="combined"
_PR_WEIGHT = 100.0

# Diversity packing parameters
_DIVERSITY_DECAY = 0.5  # penalty growth per same-file symbol
_FILE_GROUP_CAP = 3  # max symbols from a single file

# Max chars for the compact-row summary so packed rows stay cheap (#354).
_COMPACT_SUMMARY_MAX = 160


def _compact_fields(sym: dict, score: float, token_cost: int) -> dict:
    """Display fields the rc1 compact encoder declares (#354)."""
    summary = sym.get("summary") or sym.get("signature") or ""
    if len(summary) > _COMPACT_SUMMARY_MAX:
        summary = summary[:_COMPACT_SUMMARY_MAX].rstrip() + "…"
    return {
        "id": sym.get("id"),
        "name": sym.get("name"),
        "kind": sym.get("kind"),
        "file": sym.get("file"),
        "line": sym.get("line"),
        "score": round(score, 4),
        "token_cost": token_cost,
        "summary": summary,
    }


def _pack_budget(
    scored_items: list[tuple[float, dict]],
    token_budget: int,
    get_tokens: callable,
    *,
    diversity: bool = True,
) -> tuple[list[tuple[float, dict, str, int]], int]:
    """Diversity-aware greedy budget packing."""
    packed: list[tuple[float, dict, str, int]] = []
    total_tokens = 0
    file_counts: dict[str, int] = {}

    for score, sym in scored_items:
        sym_file = sym.get("file", "")

        # Diversity: enforce per-file cap
        if diversity and file_counts.get(sym_file, 0) >= _FILE_GROUP_CAP:
            continue

        source, item_tokens = get_tokens(sym)
        if item_tokens == 0:
            continue
        if total_tokens + item_tokens > token_budget:
            continue

        # Diversity: decay score for repeated files
        if diversity:
            n = file_counts.get(sym_file, 0)
            adjusted = score * (_DIVERSITY_DECAY**n)
        else:
            adjusted = score

        packed.append((adjusted, sym, source, item_tokens))
        total_tokens += item_tokens
        file_counts[sym_file] = file_counts.get(sym_file, 0) + 1

    return packed, total_tokens


def _run_context_mode(
    repo: str,
    query: str,
    token_budget: int = 4000,
    strategy: str = "combined",
    include_kinds: Optional[list] = None,
    scope: Optional[str] = None,
    fusion: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Relevance-ranked context packing within a token budget (mode=\"context\").

    Ranks all symbols by relevance (BM25) and/or centrality (PageRank),
    loads source for the top candidates, and packs greedily until
    ``token_budget`` is exhausted.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        query: Natural language or identifier describing the task.
        token_budget: Hard cap on returned tokens (default 4000).
        strategy: Ranking strategy.
            'combined' (default) = BM25 + PageRank weighted sum.
            'bm25' = pure BM25 text relevance.
            'centrality' = PageRank only (filtered to symbols with BM25 > 0).
        include_kinds: Optional list of symbol kinds to restrict results to
            (e.g. ['class', 'function']).
        scope: Optional subdirectory glob to limit search (e.g. 'src/core/*').
        fusion: Enable multi-signal fusion (Weighted Reciprocal Rank) for
            ranking. When True, ``strategy`` maps to channel weight presets.
        storage_path: Custom storage path.

    Returns:
        Dict with ``context_items`` list and summary fields.
    """
    _MAX_QUERY_LEN = 500
    if len(query) > _MAX_QUERY_LEN:
        return {"error": f"Query too long ({len(query)} chars, max {_MAX_QUERY_LEN})"}

    if strategy not in ("combined", "bm25", "centrality"):
        return {
            "error": f"Invalid strategy '{strategy}'. Must be 'combined', 'bm25', or 'centrality'."
        }

    if token_budget < 1:
        return {"error": "token_budget must be >= 1"}

    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return index_status_to_tool_error(store.inspect_index(owner, name))

    # BM25 corpus — cached on CodeIndex
    query_terms = _tokenize(query) or [query.lower()]
    # Guard: empty string in query_terms causes "" to match every filename
    query_terms = [t for t in query_terms if t]
    cache = index._bm25_cache
    if "idf" not in cache:
        cache["idf"], cache["avgdl"], cache["inverted"] = _compute_bm25(index.symbols)
        cache["centrality"] = _compute_centrality(
            index.symbols,
            index.imports,
            index.alias_map,
            getattr(index, "psr4_map", None),
        )
    idf = cache["idf"]
    avgdl = cache["avgdl"]
    inverted = cache["inverted"]

    # PageRank — computed when strategy requires it
    pagerank: dict = {}
    if strategy in ("centrality", "combined"):
        if "pagerank" not in cache:
            from .pagerank import compute_pagerank

            pr_scores, _ = compute_pagerank(
                index.imports or {},
                index.source_files,
                index.alias_map,
                psr4_map=getattr(index, "psr4_map", None),
            )
            cache["pagerank"] = pr_scores
        pagerank = cache["pagerank"]

    # ── Fusion path ─────────────────────────────────────────────────────
    if fusion:
        return _run_context_mode_fusion(
            index=index,
            store=store,
            owner=owner,
            name=name,
            query=query,
            query_terms=query_terms,
            idf=idf,
            avgdl=avgdl,
            pagerank=pagerank,
            token_budget=token_budget,
            include_kinds=include_kinds,
            scope=scope,
            start=start,
        )

    # Normalize PageRank to [0,1] for score combination
    max_pr = max(pagerank.values()) if pagerank else 1.0

    # Candidate narrowing via inverted index
    candidate_indices: set[int] = set()
    for term in query_terms:
        posting = inverted.get(term)
        if posting:
            candidate_indices.update(posting)
    candidates = (
        [index.symbols[i] for i in sorted(candidate_indices)]
        if candidate_indices
        else index.symbols
    )

    # Score and filter candidates
    scored: list[
        tuple[float, float, float, dict]
    ] = []  # (combined, bm25_norm, pr_norm, sym)
    max_bm25 = 0.0
    raw_scores: list[tuple[float, float, dict]] = []  # (bm25, pr_raw, sym)

    for sym in candidates:
        if include_kinds and sym.get("kind") not in include_kinds:
            continue
        if scope and not fnmatch(sym.get("file", ""), scope):
            continue

        bm25 = _bm25_score(sym, query_terms, idf, avgdl, raw_query=query)
        if bm25 <= 0 and strategy != "centrality":
            continue
        pr_raw = pagerank.get(sym.get("file", ""), 0.0)
        if bm25 > max_bm25:
            max_bm25 = bm25
        raw_scores.append((bm25, pr_raw, sym))

    if not raw_scores:
        elapsed = (time.perf_counter() - start) * 1000
        # Negative evidence: signal that nothing matched
        related_existing = [
            f
            for f in index.source_files
            if any(
                t in f.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                for t in query_terms
            )
        ][:5]
        ne = {
            "verdict": "no_implementation_found",
            "scanned_symbols": len(candidates),
            "scanned_files": len(index.source_files),
            "best_match_score": 0.0,
        }
        if related_existing:
            ne["related_existing"] = related_existing
        result = {
            "context_items": [],
            "total_tokens": 0,
            "budget_tokens": token_budget,
            "items_included": 0,
            "items_considered": 0,
            "negative_evidence": ne,
            "\u26a0 warning": (
                f"No implementation found for '{query[:80]}'. "
                f"Do not claim this feature exists."
            ),
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "tokens_saved": 0,
                "total_tokens_saved": 0,
            },
        }
        return result

    # Normalize and compute combined score
    norm_bm25_denom = max_bm25 if max_bm25 > 0 else 1.0
    norm_pr_denom = max_pr if max_pr > 0 else 1.0

    for bm25, pr_raw, sym in raw_scores:
        bm25_norm = bm25 / norm_bm25_denom
        pr_norm = pr_raw / norm_pr_denom
        if strategy == "bm25":
            combined = bm25_norm
        elif strategy == "centrality":
            combined = pr_norm
        else:  # combined
            combined = 0.5 * bm25_norm + 0.5 * pr_norm
        scored.append((combined, bm25_norm, pr_norm, sym))

    scored.sort(key=lambda x: x[0], reverse=True)

    items_considered = len(scored)

    # Build score lookup for BM25/PR per symbol
    _score_lookup: dict[str, tuple[float, float]] = {}
    for combined_score, bm25_norm, pr_norm, sym in scored:
        _score_lookup[sym["id"]] = (bm25_norm, pr_norm)

    def _get_tokens(sym):
        source = store.get_symbol_content(owner, name, sym["id"], _index=index) or ""
        tokens = (
            _count_tokens(source)
            if source
            else max(1, sym.get("byte_length", 0) // BYTES_PER_TOKEN)
        )
        return source, tokens

    packed, total_tokens = _pack_budget(
        [(combined_score, sym) for combined_score, _, _, sym in scored],
        token_budget,
        _get_tokens,
    )

    context_items: list[dict] = []
    for adj_score, sym, source, item_tokens in packed:
        bm25_norm, pr_norm = _score_lookup.get(sym["id"], (0.0, 0.0))
        context_items.append(
            {
                "symbol_id": sym["id"],
                "relevance_score": round(bm25_norm, 4),
                "centrality_score": round(pr_norm, 4),
                "combined_score": round(adj_score, 4),
                "tokens": item_tokens,
                "source": source,
                # Compact-encoder display fields (#354):
                **_compact_fields(sym, adj_score, item_tokens),
            }
        )

    # Negative evidence for low-confidence or empty results
    _ne_threshold = _NEGATIVE_EVIDENCE_THRESHOLD
    try:
        from .. import config as _cfg

        _ne_threshold = _cfg.get(
            "negative_evidence_threshold", _NEGATIVE_EVIDENCE_THRESHOLD
        )
    except Exception:
        pass

    negative_evidence = None
    if not context_items or max_bm25 < _ne_threshold:
        verdict = (
            "no_implementation_found" if not context_items else "low_confidence_matches"
        )
        related_existing = [
            f
            for f in index.source_files
            if any(
                t in f.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                for t in query_terms
            )
        ][:5]
        negative_evidence = {
            "verdict": verdict,
            "scanned_symbols": items_considered,
            "scanned_files": len(set(s.get("file", "") for _, _, _, s in scored)),
            "best_match_score": round(max_bm25, 3),
        }
        if related_existing:
            negative_evidence["related_existing"] = related_existing

    # Token savings estimate
    raw_bytes = sum(
        index.file_sizes.get(sym.get("file", ""), 0)
        for _, _, _, sym in scored[:items_considered]
    )
    response_bytes = total_tokens * BYTES_PER_TOKEN
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="get_ranked_context")

    elapsed = (time.perf_counter() - start) * 1000

    result = {
        "context_items": context_items,
        "total_tokens": total_tokens,
        "budget_tokens": token_budget,
        "items_included": len(context_items),
        "items_considered": items_considered,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }
    from ..retrieval.confidence import attach_confidence as _attach_confidence
    from ..retrieval.freshness import FreshnessProbe as _FreshnessProbe
    from ..storage.token_tracker import record_ranking_event as _record_ranking_event

    _probe = _FreshnessProbe(
        source_root=getattr(index, "source_root", "") or None,
        indexed_at=getattr(index, "indexed_at", ""),
        index_sha=getattr(index, "git_head", None),
        file_mtimes=getattr(index, "file_mtimes", None),
    )
    _probe.annotate(context_items)
    result["_meta"]["freshness"] = _probe.summary(context_items)
    _attach_confidence(result, context_items, is_stale=_probe.repo_is_stale)
    _record_ranking_event(
        tool="get_ranked_context",
        repo=f"{owner}/{name}",
        query=query,
        returned_ids=[c.get("symbol_id", "") for c in context_items],
        confidence=result["_meta"].get("confidence"),
        semantic_used=False,
        repo_is_stale=_probe.repo_is_stale,
    )
    if negative_evidence is not None:
        result["negative_evidence"] = negative_evidence
        if negative_evidence["verdict"] == "no_implementation_found":
            result["\u26a0 warning"] = (
                f"No implementation found for '{query[:80]}'. "
                f"Do not claim this feature exists."
            )
        else:
            result["\u26a0 warning"] = (
                f"Low-confidence matches for '{query[:80]}’ "
                f"(best score: {negative_evidence['best_match_score']}). "
                f"Verify before claiming this feature exists."
            )
    return result


def _run_context_mode_fusion(
    *,
    index,
    store,
    owner: str,
    name: str,
    query: str,
    query_terms: list[str],
    idf: dict,
    avgdl: float,
    pagerank: dict,
    token_budget: int,
    include_kinds,
    scope,
    start: float,
) -> dict:
    """Fusion-based ranked context: WRR across channels, greedy budget packing."""
    from ..retrieval.signal_fusion import (
        build_identity_channel,
        build_lexical_channel,
        build_structural_channel,
        fuse,
        load_fusion_weights,
    )

    # Filter candidates
    candidates = index.symbols
    if include_kinds or scope:
        candidates = [
            sym
            for sym in candidates
            if (not include_kinds or sym.get("kind") in include_kinds)
            and (not scope or fnmatch(sym.get("file", ""), scope))
        ]

    if not candidates:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "context_items": [],
            "total_tokens": 0,
            "budget_tokens": token_budget,
            "items_included": 0,
            "items_considered": 0,
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "tokens_saved": 0,
                "total_tokens_saved": 0,
            },
        }

    # Centrality for BM25 tiebreaker
    cache = index._bm25_cache
    if "centrality" not in cache:
        cache["centrality"] = _compute_centrality(
            index.symbols,
            index.imports,
            index.alias_map,
            getattr(index, "psr4_map", None),
        )
    centrality = cache["centrality"]

    weights, smoothing = load_fusion_weights()

    channels = []
    lex_ch = build_lexical_channel(candidates, query_terms, idf, avgdl, centrality)
    channels.append(lex_ch)

    id_ch = build_identity_channel(candidates, query)
    channels.append(id_ch)

    if pagerank:
        candidate_ids = set(lex_ch.ranked_ids) | set(id_ch.ranked_ids)
        struct_ch = build_structural_channel(candidates, pagerank, candidate_ids)
        channels.append(struct_ch)

    fused = fuse(channels, smoothing=smoothing, weights=weights)

    # Diversity-aware budget packing
    sym_by_id = {sym["id"]: sym for sym in candidates}

    # Build fused score lookup for channel contributions
    _fused_lookup: dict[str, object] = {fr.symbol_id: fr for fr in fused}

    # Filter to valid symbols and pass to packer
    fused_scored = []
    for fr in fused:
        sym = sym_by_id.get(fr.symbol_id)
        if sym:
            fused_scored.append((fr.score, sym))

    def _get_tokens_fusion(sym):
        source = store.get_symbol_content(owner, name, sym["id"], _index=index) or ""
        tokens = (
            _count_tokens(source)
            if source
            else max(1, sym.get("byte_length", 0) // BYTES_PER_TOKEN)
        )
        return source, tokens

    packed, total_tokens = _pack_budget(
        fused_scored,
        token_budget,
        _get_tokens_fusion,
    )

    context_items = []
    for adj_score, sym, source, item_tokens in packed:
        fr = _fused_lookup.get(sym["id"])
        context_items.append(
            {
                "symbol_id": sym["id"],
                "fusion_score": round(adj_score, 6),
                "channels": {
                    k: round(v, 6) for k, v in fr.channel_contributions.items()
                }
                if fr
                else {},
                "tokens": item_tokens,
                "source": source,
                # Compact-encoder display fields (#354) — see non-fusion path.
                **_compact_fields(sym, adj_score, item_tokens),
            }
        )

    raw_bytes = sum(
        index.file_sizes.get(sym.get("file", ""), 0) for _, sym, _, _ in packed
    )
    response_bytes = total_tokens * BYTES_PER_TOKEN
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="get_ranked_context")
    elapsed = (time.perf_counter() - start) * 1000

    fusion_result = {
        "context_items": context_items,
        "total_tokens": total_tokens,
        "budget_tokens": token_budget,
        "items_included": len(context_items),
        "items_considered": len(fused),
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
            "fusion": True,
            "channels": [ch.name for ch in channels],
        },
    }
    from ..retrieval.confidence import attach_confidence as _attach_confidence
    from ..retrieval.freshness import FreshnessProbe as _FreshnessProbe
    from ..storage.token_tracker import record_ranking_event as _record_ranking_event

    _probe = _FreshnessProbe(
        source_root=getattr(index, "source_root", "") or None,
        indexed_at=getattr(index, "indexed_at", ""),
        index_sha=getattr(index, "git_head", None),
        file_mtimes=getattr(index, "file_mtimes", None),
    )
    # Fusion context_items expose only ``symbol_id`` (e.g. ``path/to/file.py::Name#kind``)
    # — derive the file path from each id rather than the raw id string.
    for item in context_items:
        sid = item.get("symbol_id", "")
        file_rel = sid.split("::", 1)[0] if "::" in sid else ""
        item["_freshness"] = _probe.classify(file_rel)
    fusion_result["_meta"]["freshness"] = _probe.summary(context_items)
    _attach_confidence(
        fusion_result,
        [{"score": item.get("fusion_score")} for item in context_items],
        is_stale=_probe.repo_is_stale,
    )
    _record_ranking_event(
        tool="get_ranked_context_fusion",
        repo=f"{owner}/{name}",
        query=query,
        returned_ids=[c.get("symbol_id", "") for c in context_items],
        confidence=fusion_result["_meta"].get("confidence"),
        semantic_used=True,
        repo_is_stale=_probe.repo_is_stale,
    )
    return fusion_result
