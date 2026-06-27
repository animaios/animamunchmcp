"""Related-section graph (v2.0.0).

Two complementary edge types per section:

- **structural** — siblings (sections sharing parent_id) and cousins
  (sections sharing grandparent_id). Cheap, no embeddings required,
  always available.
- **semantic** — top-N cosine neighbors over stored embeddings. Filtered
  by ``min_score`` threshold so only meaningfully-similar pairs land in
  the graph.

The graph is computed on demand from the loaded ``DocIndex``; not
persisted to disk. For large indices this stays fast because we only
traverse the requested section's neighborhood — no full O(N²) build
unless the caller asks for all-pairs (we don't expose that).

Returned shape:

    {
        "section_id": "...",
        "structural": [{"id", "title", "level", "kind"}, ...],
        "semantic":   [{"id", "title", "level", "score"}, ...],
    }

``kind`` ∈ {sibling, child, parent, ancestor, cousin}.
"""

from __future__ import annotations

from typing import Optional

from ..embeddings import cosine_similarity


# ---------------------------------------------------------------------------
# Structural edges
# ---------------------------------------------------------------------------

def _by_id(sections: list) -> dict:
    return {s.get("id"): s for s in sections if isinstance(s, dict) and s.get("id")}


def _children_by_parent(sections: list) -> dict:
    """Build a parent_id → [section, ...] map in one pass.

    Lets callers replace per-call O(N) linear scans of ``sections`` with
    O(1) dict lookups. Hot-path callers (e.g. ``related_persist.build``)
    should pass the result via the ``_children_cache`` kwarg below.
    """
    out: dict = {}
    for s in sections:
        if not isinstance(s, dict):
            continue
        pid = s.get("parent_id") or ""
        if pid:
            out.setdefault(pid, []).append(s)
    return out


def _children_of(parent_id: str, sections: list, cache: Optional[dict] = None) -> list:
    """Return every section whose parent_id matches the given id."""
    if not parent_id:
        return []
    if cache is not None:
        return cache.get(parent_id, [])
    return [s for s in sections if s.get("parent_id") == parent_id]


def structural_neighbors(
    sections: list,
    section_id: str,
    *,
    include_parent: bool = True,
    include_children: bool = True,
    include_siblings: bool = True,
    include_cousins: bool = False,
    max_per_kind: int = 10,
    _by_id_cache: Optional[dict] = None,
    _children_cache: Optional[dict] = None,
) -> list[dict]:
    """Return structurally-related sections for ``section_id``.

    Order: parent → children → siblings → cousins. Each entry has a
    ``kind`` discriminator so callers can filter or weight by relation
    type.

    The ``_by_id_cache`` and ``_children_cache`` kwargs let hot-path
    callers reuse precomputed lookups so the call is O(1) in section count
    instead of O(N). External callers can ignore them; on-demand callers
    pay the O(N) build per call as before.
    """
    by_id = _by_id_cache if _by_id_cache is not None else _by_id(sections)
    target = by_id.get(section_id)
    if not target:
        return []
    out: list[dict] = []
    seen: set[str] = {section_id}

    def _add(sec: dict, kind: str) -> None:
        sid = sec.get("id", "")
        if not sid or sid in seen:
            return
        seen.add(sid)
        out.append(
            {
                "id": sid,
                "title": sec.get("title", ""),
                "level": sec.get("level", 0),
                "kind": kind,
            }
        )

    parent_id = target.get("parent_id") or ""
    if include_parent and parent_id:
        parent = by_id.get(parent_id)
        if parent:
            _add(parent, "parent")

    if include_children:
        for child in _children_of(section_id, sections, _children_cache)[:max_per_kind]:
            _add(child, "child")

    if include_siblings and parent_id:
        siblings = [
            s for s in _children_of(parent_id, sections, _children_cache)
            if s.get("id") != section_id
        ]
        for sib in siblings[:max_per_kind]:
            _add(sib, "sibling")

    if include_cousins and parent_id:
        grandparent_id = (by_id.get(parent_id) or {}).get("parent_id") or ""
        if grandparent_id:
            for uncle in _children_of(grandparent_id, sections, _children_cache):
                if uncle.get("id") == parent_id:
                    continue
                for cousin in _children_of(uncle.get("id"), sections, _children_cache)[:max_per_kind]:
                    _add(cousin, "cousin")

    return out


# ---------------------------------------------------------------------------
# Semantic edges
# ---------------------------------------------------------------------------

def semantic_neighbors(
    sections: list,
    section_id: str,
    *,
    top_n: int = 5,
    min_score: float = 0.6,
    _by_id_cache: Optional[dict] = None,
) -> list[dict]:
    """Return up to ``top_n`` cosine-nearest sections to ``section_id``.

    Requires the index to have been built with embeddings; sections
    without embeddings are skipped silently. Same-section is excluded.
    """
    by_id = _by_id_cache if _by_id_cache is not None else _by_id(sections)
    target = by_id.get(section_id)
    if not target:
        return []
    target_emb = target.get("embedding")
    if not target_emb:
        return []

    scored: list[tuple[float, dict]] = []
    for sec in sections:
        sid = sec.get("id")
        if not sid or sid == section_id:
            continue
        emb = sec.get("embedding")
        if not emb:
            continue
        score = cosine_similarity(target_emb, emb)
        if score < min_score:
            continue
        scored.append((score, sec))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "id": s.get("id"),
            "title": s.get("title", ""),
            "level": s.get("level", 0),
            "score": round(float(score), 4),
        }
        for score, s in scored[:top_n]
    ]


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def get_related(
    sections: list,
    section_id: str,
    *,
    mode: str = "both",
    top_n: int = 5,
    min_score: float = 0.6,
    max_per_kind: int = 10,
) -> dict:
    """Return ``{section_id, structural, semantic}`` according to ``mode``.

    ``mode`` ∈ {"structural", "semantic", "both"}.
    """
    out: dict = {"section_id": section_id, "structural": [], "semantic": []}
    if mode in ("structural", "both"):
        out["structural"] = structural_neighbors(
            sections, section_id, max_per_kind=max_per_kind
        )
    if mode in ("semantic", "both"):
        out["semantic"] = semantic_neighbors(
            sections, section_id, top_n=top_n, min_score=min_score
        )
    return out
