"""check_embedding_drift — capture and/or check the embedding drift canary."""

from __future__ import annotations

from typing import Optional

from ..embeddings.embed_drift import capture_canary, check_drift


def check_embedding_drift(
    capture: bool = False,
    force: bool = False,
    threshold: float = 0.05,
    storage_path: Optional[str] = None,
) -> dict:
    """Inspect or seed the embedding-drift canary.

    Args:
        capture: When True, embed CANARY_STRINGS and persist the snapshot.
            Idempotent unless ``force=True``.
        force: With capture=True, overwrite an existing snapshot.
        threshold: Max allowed drift (1 - cosine). Default 0.05 ≈ cos<0.95.
        storage_path: Override the default ~/.doc-index/ root.
    """
    if capture:
        return capture_canary(force=force, base_path=storage_path)
    return check_drift(threshold=threshold, base_path=storage_path)
