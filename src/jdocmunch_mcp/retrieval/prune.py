"""Stage-A prune: token → posting list, built lazily on a DocIndex.

The v1.12 BM25 engine scores every section in the index for every query.
At ~10k sections that's ~10k×k1 BM25 calls per query — manageable. At 100k
it's painful. Stage A reduces the candidate set to sections that contain at
least one query token, capped at MAX_CANDIDATES.

Two-stage retrieval flow:

    1. tokenize(query) → query_terms
    2. candidates = ∪ posting_list[term] for term in query_terms (capped)
    3. for sec_id in candidates: bm25.score_section(sec, query, ...)

The posting list is built once per DocIndex instance, lazily on first
search. No persistence — rebuild cost on a 5k-section index is ~50ms.

The cap (default 200) is the union size; we collect across all postings
until we hit the cap, then stop. With multi-token queries we sample early
posting lists more deeply than later ones — fine for retrieval quality,
since BM25 will re-rank anyway.

Falls back to full-corpus scan when the query has zero in-vocabulary terms
(rare; usually means a typo).
"""

from __future__ import annotations

from typing import Iterable, Optional

from .tokenize import tokenize

MAX_CANDIDATES = 200


class PostingIndex:
    """In-memory posting list for one DocIndex.

    Keys are query-time tokens (post-tokenize). Values are sets of
    section IDs that contain the token in any of {title, summary, content}.

    Built once per DocIndex via ``PostingIndex.build(sections, content_loader)``;
    cached on the index. Idempotent rebuild — pass ``force=True`` to discard.
    """

    __slots__ = ("postings", "all_section_ids")

    def __init__(self) -> None:
        self.postings: dict[str, set[str]] = {}
        # Fallback: every section ID, used when query has no in-vocab terms.
        self.all_section_ids: list[str] = []

    @classmethod
    def build(cls, sections: list, content_loader=None) -> "PostingIndex":
        """Construct from a list of section dicts.

        ``content_loader(doc_path, byte_start, byte_end) -> str`` is invoked
        for sections whose ``content`` field is empty (the common case post-
        load, since Section.to_dict drops content). When the loader is
        absent, content is skipped silently — title and summary still index.
        """
        idx = cls()
        for sec in sections:
            sec_id = sec.get("id", "")
            if not sec_id:
                continue
            idx.all_section_ids.append(sec_id)

            title = sec.get("title", "") or ""
            summary = sec.get("summary", "") or ""
            content = sec.get("content", "") or ""
            if not content and content_loader is not None:
                try:
                    content = content_loader(
                        sec.get("doc_path", ""),
                        int(sec.get("byte_start", 0)),
                        int(sec.get("byte_end", 0)),
                    ) or ""
                except Exception:
                    content = ""

            terms: set[str] = set()
            terms.update(tokenize(title))
            terms.update(tokenize(summary))
            terms.update(tokenize(content))
            for t in terms:
                bucket = idx.postings.get(t)
                if bucket is None:
                    bucket = set()
                    idx.postings[t] = bucket
                bucket.add(sec_id)

        return idx

    def candidates(self, query: str, max_candidates: int = MAX_CANDIDATES) -> Optional[set[str]]:
        """Return up to ``max_candidates`` section IDs whose tokens overlap ``query``.

        Returns ``None`` when the query has zero in-vocab terms — the caller
        should fall back to full-corpus scoring.
        """
        terms = tokenize(query)
        if not terms:
            return None

        out: set[str] = set()
        any_hit = False
        for term in terms:
            postings = self.postings.get(term)
            if postings is None:
                continue
            any_hit = True
            for sid in postings:
                out.add(sid)
                if len(out) >= max_candidates:
                    return out
        if not any_hit:
            return None
        return out


def get_or_build(index, content_loader=None) -> PostingIndex:
    """Lazy accessor — builds and caches a PostingIndex on the DocIndex.

    Keyed by ``id(index.sections)``: if the underlying section list is
    replaced (e.g. during a re-index), the cache is rebuilt automatically.
    """
    cache_key = id(index.sections)
    cached = getattr(index, "_posting_index", None)
    cached_key = getattr(index, "_posting_index_key", None)
    if cached is not None and cached_key == cache_key:
        return cached
    posting = PostingIndex.build(index.sections, content_loader=content_loader)
    index._posting_index = posting
    index._posting_index_key = cache_key
    return posting


def reciprocal_rank_fusion(
    rankings: Iterable[Iterable[str]],
    k: int = 60,
    weights: Optional[Iterable[float]] = None,
) -> list[tuple[str, float]]:
    """Combine multiple rankings via Reciprocal Rank Fusion.

    Each ranking is an iterable of section IDs in best-first order. RRF
    score for an item is ``Σ_i w_i / (k + rank_i)`` where ``rank_i`` is the
    1-based position in ranking ``i`` (or 0 contribution if absent).

    Default ``k=60`` is the value from the original RRF paper (Cormack 2009).
    Higher ``k`` flattens the score curve, lower ``k`` rewards top positions
    more aggressively. Weights default to 1.0 per ranking.

    Returns ``[(section_id, fused_score), ...]`` sorted by fused score
    descending. Stable on ties (preserves first-ranking-seen order).
    """
    rankings_list = [list(r) for r in rankings]
    if not rankings_list:
        return []
    if weights is None:
        weights_list = [1.0] * len(rankings_list)
    else:
        weights_list = list(weights)
        if len(weights_list) != len(rankings_list):
            raise ValueError("weights length must match number of rankings")

    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    counter = 0
    for w, ranking in zip(weights_list, rankings_list):
        for rank, sid in enumerate(ranking, start=1):
            scores[sid] = scores.get(sid, 0.0) + w / (k + rank)
            if sid not in first_seen:
                first_seen[sid] = counter
                counter += 1

    # Sort by score desc, then by first-seen order for stable ties.
    return sorted(
        scores.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )
