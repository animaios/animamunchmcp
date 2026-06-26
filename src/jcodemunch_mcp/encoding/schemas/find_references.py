"""Compact encoder for find_references."""

from .. import schema_driven as sd

TOOLS = ("find_references",)
ENCODING_ID = "fr2"

# ─── Refs mode (default) ──────────────────────────────────────────────
_ROWS_KEY = "__rows__"
_EMPTY_GROUPS_KEY = "__empty_groups__"

_REFS_TABLES = [
    sd.TableSpec(
        key=_ROWS_KEY,
        tag="r",
        cols=["file", "specifier", "match_type"],
        intern=["file", "specifier"],
    ),
]
_REFS_SCALARS = ("repo", "identifier", "reference_count", "note")
_REFS_META = ("timing_ms", "truncated", "tokens_saved", "total_tokens_saved")
_REFS_JSON = ("results", _EMPTY_GROUPS_KEY)

# ─── Importers mode ──────────────────────────────────────────────────
_IMPORTERS_KEY = "__importers__"

_IMPORTERS_TABLES = [
    sd.TableSpec(
        key=_IMPORTERS_KEY,
        tag="i",
        cols=["file", "specifier", "has_importers"],
        intern=["file", "specifier"],
        types={"has_importers": "bool"},
    ),
]
_IMPORTERS_SCALARS = ("repo", "file_path", "importer_count", "note")
_IMPORTERS_META = ("timing_ms", "truncated", "tokens_saved", "total_tokens_saved")
_IMPORTERS_JSON = ("results",)

# ─── Related mode ──────────────────────────────────────────────────────
_RELATED_KEY = "__related__"

_RELATED_TABLES = [
    sd.TableSpec(
        key=_RELATED_KEY,
        tag="l",
        cols=["id", "name", "kind", "file", "line", "signature", "relatedness_score"],
        intern=["file", "id"],
        types={"line": "int", "relatedness_score": "float"},
    ),
]
_RELATED_SCALARS = ("repo", "related_count")
_RELATED_META = ("timing_ms",)
_RELATED_NESTED = {"symbol": ["id", "name", "kind", "file", "line"]}


# ─── Back-compat: old encoding_id fi2 for importers payloads ─────────
LEGACY_ENCODING_IDS = ("fi2",)


def _flatten(response: dict) -> dict:
    """Replace nested references[].matches[] with flat rows."""
    out = {k: v for k, v in response.items() if k != "references"}
    rows = []
    empty_groups: list[str] = []
    for group in response.get("references") or []:
        if not isinstance(group, dict):
            continue
        file_path = group.get("file")
        matches = group.get("matches") or []
        if not matches:
            if isinstance(file_path, str):
                empty_groups.append(file_path)
            continue
        for m in matches:
            if not isinstance(m, dict):
                continue
            rows.append(
                {
                    "file": file_path,
                    "specifier": m.get("specifier", ""),
                    "match_type": m.get("match_type", ""),
                }
            )
    out[_ROWS_KEY] = rows
    if empty_groups:
        out[_EMPTY_GROUPS_KEY] = empty_groups
    return out


def _regroup(decoded: dict) -> dict:
    """Inverse of _flatten: rebuild references list."""
    rows = decoded.pop(_ROWS_KEY, None) or []
    empty_groups = decoded.pop(_EMPTY_GROUPS_KEY, None) or []
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for file_path in empty_groups:
        if not isinstance(file_path, str):
            continue
        if file_path not in groups:
            groups[file_path] = []
            order.append(file_path)
    for row in rows:
        file_path = row.get("file")
        if not isinstance(file_path, str):
            continue
        match = {
            "specifier": row.get("specifier", ""),
            "match_type": row.get("match_type", ""),
        }
        if file_path not in groups:
            groups[file_path] = []
            order.append(file_path)
        groups[file_path].append(match)
    decoded["references"] = [{"file": f, "matches": groups[f]} for f in order]
    return decoded


def _flatten_importers(response: dict) -> dict:
    """Replace nested importers[] with flat rows (former find_importers shape)."""
    out = {k: v for k, v in response.items() if k != "importers"}
    rows = []
    for imp in response.get("importers") or []:
        if not isinstance(imp, dict):
            continue
        rows.append(
            {
                "file": imp.get("file", ""),
                "specifier": imp.get("specifier", ""),
                "has_importers": imp.get("has_importers", False),
            }
        )
    out[_IMPORTERS_KEY] = rows
    return out


def _unflatten_importers(decoded: dict) -> dict:
    """Inverse of _flatten_importers: rebuild importers list."""
    rows = decoded.pop(_IMPORTERS_KEY, None) or []
    decoded["importers"] = [
        {
            "file": r.get("file", ""),
            "specifier": r.get("specifier", ""),
            "has_importers": r.get("has_importers", False),
        }
        for r in rows
    ]
    return decoded


def _flatten_related(response: dict) -> dict:
    """Replace nested related[] with flat rows."""
    out = {k: v for k, v in response.items() if k != "related"}
    rows = []
    for r in response.get("related") or []:
        if not isinstance(r, dict):
            continue
        rows.append(
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "kind": r.get("kind", ""),
                "file": r.get("file", ""),
                "line": r.get("line", 0),
                "signature": r.get("signature", ""),
                "relatedness_score": r.get("relatedness_score", 0.0),
            }
        )
    out[_RELATED_KEY] = rows
    return out


def _unflatten_related(decoded: dict) -> dict:
    """Inverse of _flatten_related: rebuild related list."""
    rows = decoded.pop(_RELATED_KEY, None) or []
    decoded["related"] = [
        {
            "id": r.get("id", ""),
            "name": r.get("name", ""),
            "kind": r.get("kind", ""),
            "file": r.get("file", ""),
            "line": r.get("line", 0),
            "signature": r.get("signature", ""),
            "relatedness_score": r.get("relatedness_score", 0.0),
        }
        for r in rows
    ]
    return decoded


def encode(tool: str, response: dict) -> tuple[str, str]:
    # Related mode (former get_related_symbols)
    if (
        "related" in response
        and "references" not in response
        and "importers" not in response
    ):
        return sd.encode(
            tool,
            _flatten_related(response),
            ENCODING_ID,
            _RELATED_TABLES,
            _RELATED_SCALARS,
            meta_keys=_RELATED_META,
            nested_dicts=_RELATED_NESTED,
        )
    # Importers mode (former find_importers)
    if "importers" in response and "references" not in response:
        return sd.encode(
            tool,
            _flatten_importers(response),
            ENCODING_ID,
            _IMPORTERS_TABLES,
            _IMPORTERS_SCALARS,
            meta_keys=_IMPORTERS_META,
            json_blobs=_IMPORTERS_JSON,
        )
    # Refs mode (default)
    if "references" in response:
        return sd.encode(
            tool,
            _flatten(response),
            ENCODING_ID,
            _REFS_TABLES,
            _REFS_SCALARS,
            meta_keys=_REFS_META,
            json_blobs=_REFS_JSON,
        )
    # Quick mode / other
    return sd.encode(
        tool,
        response,
        ENCODING_ID,
        _REFS_TABLES,
        _REFS_SCALARS,
        meta_keys=_REFS_META,
        json_blobs=_REFS_JSON,
    )


def decode(payload: str) -> dict:
    # Decode with combined tables so both refs and importers rows are recovered
    combined_tables = _REFS_TABLES + _IMPORTERS_TABLES + _RELATED_TABLES
    combined_scalars = tuple(
        dict.fromkeys(_REFS_SCALARS + _IMPORTERS_SCALARS + _RELATED_SCALARS)
    )
    combined_meta = tuple(dict.fromkeys(_REFS_META + _IMPORTERS_META + _RELATED_META))
    combined_json = tuple(dict.fromkeys(_REFS_JSON + _IMPORTERS_JSON))
    combined_nested = {**_RELATED_NESTED}

    decoded = sd.decode(
        payload,
        combined_tables,
        combined_scalars,
        meta_keys=combined_meta,
        json_blobs=combined_json,
        nested_dicts=combined_nested or None,
    )
    if _IMPORTERS_KEY in decoded and decoded[_IMPORTERS_KEY]:
        return _unflatten_importers(decoded)
    elif _RELATED_KEY in decoded and decoded[_RELATED_KEY]:
        return _unflatten_related(decoded)
    elif _ROWS_KEY in decoded:
        return _regroup(decoded)
    return decoded
