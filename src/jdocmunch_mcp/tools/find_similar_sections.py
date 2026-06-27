"""find_similar_sections — multi-signal section dedup detection (v1.60.0).

Inspired by jcodemunch-mcp's ``find_similar_symbols``. Surfaces clusters
of overlapping or duplicate sections so a maintainer can consolidate.

Every wiki of size accumulates "three pages that all say the same
thing." Manual grep finds title duplicates; embedding cosine alone
floods the result with related-but-distinct topics. This tool fuses two
signals — embedding cosine (when available) and title+body lexical
Jaccard — gated by a cheap title-token pre-filter to keep cost bounded
on large wikis.

Output is cluster-shaped: one entry per group of overlapping sections,
each with a ``canonical`` (recommended keeper) and a list of
``variants`` to fold in. Verdict tiers per cluster:

  - ``near_duplicate``    — combined score ≥ near_duplicate_threshold
  - ``overlapping_topic`` — combined score ∈ [min_score, threshold)
  - ``parallel_tutorial`` — overlap detected and *all* cluster members
    live in different doc directories (suggests parallel guides that
    should reference each other rather than be merged)

Read-only.
"""

from __future__ import annotations

import posixpath
import time
from typing import Optional

from ..embeddings import cosine_similarity
from ..retrieval.tokenize import tokenize_unique
from ..storage.doc_store import DocStore
from .get_backlinks import get_backlinks


_DEFAULT_MAX_SECTIONS = 1000
_TITLE_PREFILTER_MIN = 0.1  # title Jaccard floor before paying for cosine


def _byte_overlap_ratio(a: dict, b: dict) -> float:
    """Fraction of the smaller section's byte range that overlaps the
    other. 1.0 means full containment; 0.0 means disjoint.

    Used to filter out parser-artifact pairs: most parsers emit a
    doc-level wrapper section AND the heading-level section for the
    same content, with one containing the other. Those duplicates are
    not interesting to a dedup-detection caller.
    """
    a_s, a_e = int(a.get("byte_start", 0) or 0), int(a.get("byte_end", 0) or 0)
    b_s, b_e = int(b.get("byte_start", 0) or 0), int(b.get("byte_end", 0) or 0)
    if a_e <= a_s or b_e <= b_s:
        return 0.0
    inter = max(0, min(a_e, b_e) - max(a_s, b_s))
    smaller = min(a_e - a_s, b_e - b_s)
    if smaller <= 0:
        return 0.0
    return inter / smaller


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


class _UnionFind:
    """Tiny disjoint-set, just enough for cluster collapse."""
    def __init__(self):
        self._parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self._parent:
            root = self.find(node)
            out.setdefault(root, []).append(node)
        return out


def _section_tokens(sec: dict, body_text: Optional[str]) -> tuple[set[str], set[str]]:
    """Return (title_tokens, body_tokens). ``body_text`` may be None when
    fetching content is too expensive; we then fall back to summary."""
    title_tokens = tokenize_unique(sec.get("title", "") or "")
    if body_text:
        body_tokens = tokenize_unique(body_text)
    else:
        body_tokens = tokenize_unique(sec.get("summary", "") or "")
    return title_tokens, body_tokens


def _combined_score(
    title_jac: float, body_jac: float, cosine: Optional[float]
) -> tuple[float, str]:
    """Fuse signals into one score in [0, 1]; return (score, dominant_signal).

    When embeddings exist, cosine dominates (60%) but title + body keep
    a 40% weight so a high-cosine pair with unrelated titles doesn't
    cluster as "duplicate." Without embeddings, score is 70% body + 30%
    title — title alone is too easy to fool.
    """
    if cosine is not None:
        score = 0.60 * cosine + 0.25 * body_jac + 0.15 * title_jac
        dominant = "embedding" if cosine >= max(body_jac, title_jac) else "lexical"
    else:
        score = 0.70 * body_jac + 0.30 * title_jac
        dominant = "lexical"
    return round(score, 4), dominant


def _differs_by(
    a_tokens: set[str], b_tokens: set[str], a_title: set[str], b_title: set[str]
) -> dict:
    """Per-pair breakdown of which dimensions differ. Cheap and informative.

    body_only: tokens unique to one side's body (5 worst — i.e. most
               distinctive on each side).
    title_diff: tokens unique to one title.
    """
    body_only_a = sorted(a_tokens - b_tokens)[:5]
    body_only_b = sorted(b_tokens - a_tokens)[:5]
    return {
        "title_diff_a": sorted(a_title - b_title)[:5],
        "title_diff_b": sorted(b_title - a_title)[:5],
        "body_unique_a": body_only_a,
        "body_unique_b": body_only_b,
    }


def _canonical_score(sec: dict, backlink_count: int) -> float:
    """Higher wins. Backlinks dominate (a section others reference is
    the de-facto canonical), with byte-length as tiebreaker (longer
    sections usually have more substance)."""
    bytes_len = max(0, int(sec.get("byte_end", 0) or 0) - int(sec.get("byte_start", 0) or 0))
    return backlink_count * 100.0 + bytes_len * 0.001


def _verdict(
    max_score: float,
    near_duplicate_threshold: float,
    doc_dirs: set[str],
) -> str:
    if max_score >= near_duplicate_threshold:
        return "near_duplicate"
    # All members in different directories → parallel tutorials.
    if len(doc_dirs) > 1 and len(doc_dirs) == len({d for d in doc_dirs}):
        # Only mark as parallel_tutorial when the cluster's members
        # actually live in N distinct directories (rare in same-doc
        # clusters, common in cross-tutorial overlap).
        return "parallel_tutorial"
    return "overlapping_topic"


def find_similar_sections(
    repo: str,
    min_score: float = 0.7,
    near_duplicate_threshold: float = 0.92,
    max_clusters: int = 50,
    exclude_same_doc: bool = False,
    max_sections: int = _DEFAULT_MAX_SECTIONS,
    storage_path: Optional[str] = None,
) -> dict:
    """Surface clusters of overlapping or duplicate sections.

    Args:
        repo: Repository identifier (owner/name).
        min_score: Pairwise score floor for clustering. Default 0.7.
        near_duplicate_threshold: Score at/above which a cluster is
            flagged ``near_duplicate``. Default 0.92.
        max_clusters: Cap on number of clusters returned. Default 50.
        exclude_same_doc: When True, pairs in the same doc don't count
            toward clustering. Useful when the wiki has long pages with
            repeated section structures. Default False.
        max_sections: Hard cap on sections examined. Default 1000.
        storage_path: Custom storage path.

    Returns:
        ``{result: {clusters: [...], cluster_count, ...}, _meta}``.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    all_sections = list(index.sections)
    examined = all_sections[:max_sections]
    has_embeddings = bool(index._has_embeddings()) if hasattr(index, "_has_embeddings") else False

    # Precompute token sets + embeddings + identity tuples.
    cache: list[dict] = []
    skipped_no_content = 0
    for sec in examined:
        sid = sec.get("id")
        if not sid:
            continue
        # Skip parser-artifact ghost sections: doc-level wrappers with
        # zero byte range. They share content with the real heading
        # section under the same doc and would otherwise cluster as
        # near-duplicates of themselves.
        b_s = int(sec.get("byte_start", 0) or 0)
        b_e = int(sec.get("byte_end", 0) or 0)
        if b_e <= b_s:
            continue
        # Cheap content proxy: summary + title. Avoid the slow path of
        # reading every section's bytes — for dedup we only need overlap
        # signal, not full text.
        body_text = sec.get("summary") or ""
        title_tokens, body_tokens = _section_tokens(sec, body_text)
        emb = sec.get("embedding") if has_embeddings else None
        cache.append({
            "id": sid,
            "doc_path": sec.get("doc_path", ""),
            "title": sec.get("title", ""),
            "byte_start": sec.get("byte_start", 0),
            "byte_end": sec.get("byte_end", 0),
            "title_tokens": title_tokens,
            "body_tokens": body_tokens,
            "embedding": emb,
            "sec": sec,
        })
        if not body_tokens and not title_tokens:
            skipped_no_content += 1

    # Pairwise scan with title-Jaccard pre-filter. Quadratic-but-bounded.
    uf = _UnionFind()
    pair_scores: dict[tuple[str, str], dict] = {}
    n = len(cache)
    for i in range(n):
        a = cache[i]
        if not a["title_tokens"]:
            continue
        for j in range(i + 1, n):
            b = cache[j]
            if exclude_same_doc and a["doc_path"] == b["doc_path"]:
                continue
            if not b["title_tokens"]:
                continue
            # Parser-artifact filter: most doc parsers emit a doc-level
            # wrapper PLUS the heading-level section for the same bytes.
            # Skip when one section's range substantially contains the
            # other's (>0.5 of the smaller range).
            if a["doc_path"] == b["doc_path"] and _byte_overlap_ratio(a["sec"], b["sec"]) > 0.5:
                continue
            title_jac = _jaccard(a["title_tokens"], b["title_tokens"])
            body_jac = _jaccard(a["body_tokens"], b["body_tokens"])
            # Pre-filter: at least one signal must clear the floor before
            # we spend a cosine call.
            if title_jac < _TITLE_PREFILTER_MIN and body_jac < _TITLE_PREFILTER_MIN:
                continue
            cosine: Optional[float] = None
            if a["embedding"] and b["embedding"]:
                try:
                    cosine = float(cosine_similarity(a["embedding"], b["embedding"]))
                except Exception:
                    cosine = None
            score, dominant = _combined_score(title_jac, body_jac, cosine)
            if score < min_score:
                continue
            pair_scores[(a["id"], b["id"])] = {
                "score": score,
                "title_jac": round(title_jac, 4),
                "body_jac": round(body_jac, 4),
                "cosine": None if cosine is None else round(cosine, 4),
                "dominant": dominant,
            }
            uf.union(a["id"], b["id"])

    # Group into clusters. Singletons are filtered out.
    groups = uf.groups()
    clusters: list[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_caches = [c for c in cache if c["id"] in set(members)]

        # Backlink counts (one call per doc, memoised within this run).
        doc_backlinks: dict[str, int] = {}
        for mc in member_caches:
            dp = mc["doc_path"]
            if dp in doc_backlinks:
                continue
            try:
                bl = get_backlinks(repo=repo, doc_path=dp, storage_path=storage_path)
                doc_backlinks[dp] = (bl.get("result") or {}).get("backlink_count", 0)
            except Exception:
                doc_backlinks[dp] = 0

        # Pick canonical.
        ranked = sorted(
            member_caches,
            key=lambda c: _canonical_score(c["sec"], doc_backlinks.get(c["doc_path"], 0)),
            reverse=True,
        )
        canonical = ranked[0]

        # Score stats for verdict.
        member_set = {c["id"] for c in member_caches}
        scores_in_cluster = [
            v["score"]
            for (a_id, b_id), v in pair_scores.items()
            if a_id in member_set and b_id in member_set
        ]
        max_s = max(scores_in_cluster) if scores_in_cluster else 0.0
        avg_s = sum(scores_in_cluster) / len(scores_in_cluster) if scores_in_cluster else 0.0

        doc_dirs = {posixpath.dirname(c["doc_path"].replace("\\", "/")) for c in member_caches}
        verdict = _verdict(max_s, near_duplicate_threshold, doc_dirs)

        variants = []
        for c in ranked[1:]:
            # Pair score against canonical.
            key = (canonical["id"], c["id"]) if (canonical["id"], c["id"]) in pair_scores else (c["id"], canonical["id"])
            pair = pair_scores.get(key)
            if not pair:
                continue
            differs = _differs_by(
                canonical["body_tokens"], c["body_tokens"],
                canonical["title_tokens"], c["title_tokens"],
            )
            variants.append({
                "section_id": c["id"],
                "doc_path": c["doc_path"],
                "title": c["title"],
                "score": pair["score"],
                "dominant_signal": pair["dominant"],
                "differs_by": differs,
            })

        clusters.append({
            "verdict": verdict,
            "canonical": {
                "section_id": canonical["id"],
                "doc_path": canonical["doc_path"],
                "title": canonical["title"],
                "backlink_count": doc_backlinks.get(canonical["doc_path"], 0),
                "rationale": f"highest backlink_count ({doc_backlinks.get(canonical['doc_path'], 0)}) + size",
            },
            "variants": variants,
            "size": len(member_caches),
            "max_score": round(max_s, 4),
            "avg_score": round(avg_s, 4),
        })

    # Sort clusters: near_duplicate first, then by max_score desc.
    verdict_rank = {"near_duplicate": 0, "overlapping_topic": 1, "parallel_tutorial": 2}
    clusters.sort(key=lambda c: (verdict_rank.get(c["verdict"], 9), -c["max_score"]))
    clusters = clusters[:max_clusters]

    return {
        "result": {
            "repo": f"{owner}/{name}",
            "cluster_count": len(clusters),
            "section_count_examined": n,
            "section_count_total": len(all_sections),
            "had_embeddings": has_embeddings,
            "clusters": clusters,
        },
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "min_score": min_score,
            "near_duplicate_threshold": near_duplicate_threshold,
            "skipped_no_content": skipped_no_content,
            "truncated": len(all_sections) > max_sections,
        },
    }
