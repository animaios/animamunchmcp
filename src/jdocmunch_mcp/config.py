"""Environment variable handling and defaults for jdocmunch-mcp."""

import os
from typing import Optional


def get_meta_fields() -> Optional[list[str]]:
    """Return meta_fields config: None = all fields, [] = strip _meta, list = keep only those."""
    raw = os.environ.get("JDOCMUNCH_META_FIELDS")
    if raw is None:
        return []  # default: no _meta (token-efficient)
    raw = raw.strip()
    if raw.lower() in ("null", "all", "*"):
        return None  # all fields
    if raw == "" or raw == "[]":
        return []
    return [f.strip() for f in raw.split(",") if f.strip()]
