"""resolve_related_code_repos — map a jdocmunch docs repo to jCodeMunch code handles (jdoc#68).

The two suites keep independent repo-identity models on purpose, so an agent
can't reliably derive the jCodeMunch ``code_repo`` for a docs corpus by string
munging the docs handle. This bridge helper does it by ``source_root``: given a
jdocmunch docs repo, it returns candidate jCodeMunch code repo handles whose
indexed ``source_root`` matches (exact), contains, or is contained by the docs
``source_root``, each with a confidence and reason, plus an ``ambiguous`` flag
when more than one strong candidate exists.

Read-only and best-effort: when jCodeMunch is not importable in this
environment it returns ``bridge_available: false`` with an explanatory hint
rather than an empty candidate list with no explanation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..storage import DocStore
from ._bridge import import_code_index_store


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _is_within(inner: Path, outer: Path) -> bool:
    """True when ``inner`` is the same as or nested under ``outer``."""
    try:
        return inner == outer or inner.is_relative_to(outer)
    except (OSError, ValueError, AttributeError):
        return False


def resolve_related_code_repos(repo: str, storage_path: Optional[str] = None) -> dict:
    """Return candidate jCodeMunch code handles for a jdocmunch docs repo.

    Args:
        repo: jdocmunch docs repo identifier (owner/repo or bare name).
        storage_path: Override DOC_INDEX_PATH for testing. (jCodeMunch's own
            index store is always read from its own default location, never
            this docs path.)
    """
    t0 = time.perf_counter()

    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    docs_source_root = getattr(index, "source_root", "") or ""

    IndexStore = import_code_index_store()
    bridge_available = IndexStore is not None

    if not bridge_available:
        return {
            "repo": f"{owner}/{name}",
            "repo_kind": "doc_index",
            "source_root": docs_source_root,
            "candidates": [],
            "ambiguous": False,
            "_meta": {
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "bridge_available": False,
                "candidate_count": 0,
                "hint": "Install jcodemunch-mcp in this environment to resolve related code repos.",
            },
        }

    candidates: list[dict] = []
    if docs_source_root:
        try:
            # jCodeMunch reads its own index root (CODE_INDEX_PATH); never the
            # docs storage_path.
            code_repos = IndexStore().list_repos()
        except Exception:
            code_repos = []

        try:
            docs_p = Path(docs_source_root).resolve()
        except (OSError, ValueError):
            docs_p = None

        if docs_p is not None:
            for entry in code_repos:
                sr = entry.get("source_root") or ""
                if not sr:
                    continue
                try:
                    code_p = Path(sr).resolve()
                except (OSError, ValueError):
                    continue
                if docs_p == code_p:
                    confidence, reason = "high", "source_root_exact_match"
                elif _is_within(docs_p, code_p):
                    confidence, reason = "medium", "source_root_contains_docs_root"
                elif _is_within(code_p, docs_p):
                    confidence, reason = "low", "docs_root_contains_source_root"
                else:
                    continue
                candidates.append(
                    {
                        "repo": entry.get("repo", ""),
                        "confidence": confidence,
                        "reason": reason,
                        "source_root": sr,
                    }
                )

    candidates.sort(key=lambda c: (_CONFIDENCE_ORDER.get(c["confidence"], 9), c["repo"]))

    high = [c for c in candidates if c["confidence"] == "high"]
    # Ambiguous when no single handle is the obvious choice: multiple exact
    # matches, or no exact match but several weaker candidates.
    ambiguous = len(high) > 1 or (not high and len(candidates) > 1)

    if not candidates:
        if not docs_source_root:
            hint = (
                "This docs repo has no source_root (e.g. a GitHub-indexed docs "
                "corpus), so it can't be matched to a code repo by path. Pass "
                "the jCodeMunch code_repo explicitly."
            )
        else:
            hint = (
                "No jCodeMunch index shares this docs source_root. Index the "
                "code with jcodemunch, or pass the code_repo explicitly."
            )
    elif ambiguous:
        hint = "Multiple candidate code repos matched — disambiguate before using one as code_repo."
    else:
        hint = None

    return {
        "repo": f"{owner}/{name}",
        "repo_kind": "doc_index",
        "source_root": docs_source_root,
        "candidates": candidates,
        "ambiguous": ambiguous,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "bridge_available": True,
            "candidate_count": len(candidates),
            "hint": hint,
        },
    }
