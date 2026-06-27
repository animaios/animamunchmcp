"""Batch content retrieval for multiple sections."""

import hashlib
import os
import time
from typing import Optional

from ..storage import DocStore
from ..storage.token_tracker import estimate_savings, record_savings, cost_avoided


def get_sections(
    repo: str,
    section_ids: list,
    verify: bool = False,
    strip_boilerplate: bool = False,
    compress_code: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Retrieve full content for multiple sections in one call.

    Args:
        repo: Repository identifier.
        section_ids: List of section IDs to retrieve.
        verify: If True, verify content hashes.
        storage_path: Custom storage path.

    Returns:
        Dict with list of section results.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repo not found: {repo}"}

    results = []
    total_tokens_saved = 0
    total_boilerplate_stripped = 0
    total_code_compressed = 0
    fragments: list = []
    if strip_boilerplate:
        from ..retrieval.boilerplate import load as _load_bp
        fragments = _load_bp(storage_path, owner, name)
    # Cache raw file sizes per doc_path to avoid repeated os.path.getsize calls
    doc_raw_sizes: dict = {}

    for section_id in section_ids:
        sec = index.get_section(section_id)
        if not sec:
            results.append({"error": f"Section not found: {section_id}"})
            continue

        content = store.get_section_content(owner, name, section_id, _index=index)
        if content is None:
            results.append({"error": f"Content not available for section: {section_id}"})
            continue

        # jdoc#70: capture raw indexed bytes before any response-only transform
        # so verification certifies source integrity, not the transformed copy.
        raw_content = content
        transformations = []

        if strip_boilerplate and fragments:
            from ..retrieval.boilerplate import strip as _strip_bp
            content, removed = _strip_bp(content, fragments)
            total_boilerplate_stripped += removed
            if removed > 0:
                transformations.append("strip_boilerplate")

        if compress_code:
            from ..retrieval.code_compress import compress_fenced_code as _compress
            content, saved = _compress(content)
            total_code_compressed += saved
            if saved > 0:
                transformations.append("compress_code")

        # Strip the raw embedding vector — internal index artifact, not API
        # payload (issue #11). Matches get_section_context behavior.
        result_sec = {k: v for k, v in sec.items() if k not in ("content", "embedding")}
        result_sec["content"] = content
        if transformations:
            result_sec["response_transformed"] = True
            result_sec["transformations"] = transformations

        if verify:
            stored_hash = sec.get("content_hash", "")
            # jdoc#70: hash_verified certifies the RAW indexed section, not the
            # transformed response copy, so it is not flipped false by
            # compress_code/strip_boilerplate. source_hash_verified is the
            # explicit alias for the same source-integrity check.
            source_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
            source_verified = (source_hash == stored_hash) if stored_hash else None
            result_sec["hash_verified"] = source_verified
            result_sec["source_hash_verified"] = source_verified
            if transformations:
                response_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                result_sec["response_hash_matches_content_hash"] = (
                    (response_hash == stored_hash) if stored_hash else None
                )

        doc_path = sec.get("doc_path", "")
        if doc_path not in doc_raw_sizes:
            try:
                raw_file = store._safe_content_path(store._content_dir(owner, name), doc_path)
                doc_raw_sizes[doc_path] = os.path.getsize(raw_file) if raw_file else 0
            except OSError:
                doc_raw_sizes[doc_path] = 0
        raw_bytes = doc_raw_sizes[doc_path]

        response_bytes = len(content.encode("utf-8"))
        tokens_saved = estimate_savings(raw_bytes, response_bytes)
        total_tokens_saved += tokens_saved

        results.append({"section": result_sec, "tokens_saved": tokens_saved})

    total = record_savings(total_tokens_saved, storage_path)
    ca = cost_avoided(total_tokens_saved, total)

    latency_ms = int((time.perf_counter() - t0) * 1000)
    meta = {
        "latency_ms": latency_ms,
        "sections_returned": len(results),
        "tokens_saved": total_tokens_saved,
        "total_tokens_saved": total,
        **ca,
    }
    if strip_boilerplate:
        meta["boilerplate_stripped_bytes"] = total_boilerplate_stripped
    if compress_code:
        meta["code_compressed_bytes"] = total_code_compressed
    # v1.32.0: per-section citation block.
    meta["citations"] = []
    for entry in results:
        sec = entry.get("section") if isinstance(entry, dict) else None
        if not isinstance(sec, dict):
            continue
        meta["citations"].append({
            "repo": f"{owner}/{name}",
            "doc_path": sec.get("doc_path", ""),
            "section_id": sec.get("id", ""),
            "byte_start": int(sec.get("byte_start", 0) or 0),
            "byte_end": int(sec.get("byte_end", 0) or 0),
            "content_hash": sec.get("content_hash", ""),
            "indexed_at": index.indexed_at,
        })
    return {
        "sections": results,
        "section_count": len(results),
        "_meta": meta,
    }
