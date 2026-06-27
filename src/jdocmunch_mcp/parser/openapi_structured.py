"""Structured OpenAPI parser (v1.18.0).

Promotes OpenAPI / Swagger specs from prose-flattened markdown to first-class
queryable structure. Each operation and each schema becomes its own Section
with typed metadata:

  Section.metadata.openapi_op     = {method, path, operationId, summary,
                                     description, tags, parameters,
                                     request_body, responses, deprecated,
                                     security}
  Section.metadata.openapi_schema = {name, type, properties, required,
                                     used_by_operations: [opId]}

Section IDs are deterministic so cross-references between operations and
schemas survive across re-indexes:

  ops:    {repo}::{doc_path}::operations/{tag-slug}/op-{op-key}#3
  tags:   {repo}::{doc_path}::operations/{tag-slug}#2
  schemas:{repo}::{doc_path}::schemas/schema-{name}#3
  schema-root: {repo}::{doc_path}::schemas#2

op-key prefers operationId (slugified). When absent, falls back to
``{method}-{slugified-path}``.

Returns a list[Section] ready for hierarchy.wire_hierarchy().
"""

from __future__ import annotations

import json
from typing import Optional

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

from .sections import (
    Section,
    compute_content_hash,
    extract_references,
    extract_tags,
    make_section_id,
    slugify,
)

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


def _load_spec(content: str) -> dict:
    """Parse YAML or JSON into a dict. Returns {} on failure."""
    if _YAML_AVAILABLE:
        try:
            spec = yaml.safe_load(content)
            if isinstance(spec, dict):
                return spec
        except Exception:
            pass
    try:
        spec = json.loads(content)
        if isinstance(spec, dict):
            return spec
    except Exception:
        pass
    return {}


def _ref_name(ref: str) -> str:
    """``#/components/schemas/User`` → ``User``."""
    if not ref:
        return ""
    return ref.rstrip("/").rsplit("/", 1)[-1]


def _summarize_schema(schema: dict, depth: int = 0) -> dict:
    """Compact JSON-safe representation of a schema for storage.

    Keeps name + type + items.ref + $ref so queries can still resolve
    nested referenced schemas without inflating the index file.
    """
    if not isinstance(schema, dict) or depth > 4:
        return {}
    out: dict = {}
    if "$ref" in schema:
        out["ref"] = _ref_name(schema["$ref"])
    if "type" in schema:
        out["type"] = schema["type"]
    if "format" in schema:
        out["format"] = schema["format"]
    items = schema.get("items")
    if isinstance(items, dict):
        out["items"] = _summarize_schema(items, depth + 1)
    one_of = schema.get("oneOf") or schema.get("anyOf") or schema.get("allOf")
    if isinstance(one_of, list):
        out["composite"] = [_summarize_schema(s, depth + 1) for s in one_of if isinstance(s, dict)]
    return out


def _refs_in(schema) -> list[str]:
    """Walk a schema and return every ``$ref`` name (deduped)."""
    out: list[str] = []
    seen: set[str] = set()

    def _walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = _ref_name(ref)
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(schema)
    return out


def _op_key(method: str, path: str, op: dict) -> str:
    """Stable identifier for an operation: prefers operationId, falls back
    to ``{method}-{slugified path}``. Pre-replaces ``/`` with whitespace so
    the slugifier preserves path-segment boundaries (``/pets/{id}`` →
    ``pets-id`` rather than ``petsid``)."""
    op_id = op.get("operationId")
    if isinstance(op_id, str) and op_id.strip():
        return slugify(op_id)
    return slugify(f"{method} " + path.replace("/", " "))


def _render_op_markdown(method: str, path: str, op: dict) -> str:
    """Compact, human-readable summary for an operation Section's content."""
    lines = [f"### {method} {path}"]
    summary = (op.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    desc = (op.get("description") or "").strip()
    if desc and desc != summary:
        lines.append(desc)
    tags = op.get("tags") or []
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if op.get("deprecated"):
        lines.append("DEPRECATED.")
    return "\n\n".join(lines) + "\n"


def _render_schema_markdown(name: str, schema: dict) -> str:
    lines = [f"### {name}"]
    desc = (schema.get("description") or "").strip()
    if desc:
        lines.append(desc)
    props = schema.get("properties") or {}
    if isinstance(props, dict) and props:
        prop_lines = ["Properties:"]
        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            t = prop_schema.get("type") or _ref_name(prop_schema.get("$ref", ""))
            prop_lines.append(f"- {prop_name}: {t}")
        lines.append("\n".join(prop_lines))
    return "\n\n".join(lines) + "\n"


def parse_openapi_structured(content: str, doc_path: str, repo: str) -> list:
    """Parse an OpenAPI spec into structured Section objects.

    Returns ``[]`` when the spec is unparseable or missing the
    ``openapi`` / ``swagger`` marker so the caller can fall back to the
    markdown converter.
    """
    spec = _load_spec(content)
    if not spec or ("openapi" not in spec and "swagger" not in spec):
        return []

    sections: list[Section] = []

    # ----- Root -----
    info = spec.get("info") or {}
    title = (info.get("title") or "API Reference").strip()
    version = str(info.get("version") or "").strip()
    root_slug = slugify(title) or "api"
    root_id = make_section_id(repo, doc_path, root_slug, 1)
    root_content = title + (f"\n\nVersion: {version}" if version else "")
    desc = (info.get("description") or "").strip()
    if desc:
        root_content += "\n\n" + desc
    sections.append(
        Section(
            id=root_id,
            repo=repo,
            doc_path=doc_path,
            title=title,
            content=root_content,
            level=1,
            parent_id="",
            children=[],
            byte_start=0,
            byte_end=0,
            summary=title,
            content_hash=compute_content_hash(root_content),
            references=extract_references(root_content),
            tags=extract_tags(root_content),
            metadata={"openapi_root": {"title": title, "version": version, "description": desc}},
        )
    )

    # ----- Operations grouped by tag -----
    paths = spec.get("paths") or {}
    schema_to_ops: dict[str, list[str]] = {}
    tagged_ops: dict[str, list[Section]] = {}
    untagged_ops: list[Section] = []
    seen_tags: list[str] = []

    for path, path_item in (paths.items() if isinstance(paths, dict) else []):
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            op_tags = [t for t in (op.get("tags") or []) if isinstance(t, str)] or [None]
            tag = op_tags[0]

            params = []
            for p in (op.get("parameters") or []):
                if not isinstance(p, dict):
                    continue
                params.append(
                    {
                        "name": p.get("name", ""),
                        "in": p.get("in", ""),
                        "required": bool(p.get("required")),
                        "schema": _summarize_schema(p.get("schema") or {}),
                        "description": (p.get("description") or "").strip(),
                    }
                )

            req_body_schema = None
            req_body = op.get("requestBody")
            if isinstance(req_body, dict):
                content_obj = req_body.get("content") or {}
                if isinstance(content_obj, dict):
                    for media_type, media_obj in content_obj.items():
                        if isinstance(media_obj, dict) and isinstance(media_obj.get("schema"), dict):
                            req_body_schema = {
                                "media_type": media_type,
                                "schema": _summarize_schema(media_obj["schema"]),
                                "refs": _refs_in(media_obj["schema"]),
                            }
                            break

            response_refs: dict[str, dict] = {}
            for code, resp in (op.get("responses") or {}).items():
                if not isinstance(resp, dict):
                    continue
                refs: list[str] = []
                content_obj = resp.get("content") or {}
                if isinstance(content_obj, dict):
                    for media_obj in content_obj.values():
                        if isinstance(media_obj, dict):
                            refs.extend(_refs_in(media_obj.get("schema") or {}))
                response_refs[str(code)] = {
                    "description": (resp.get("description") or "").strip(),
                    "refs": list(dict.fromkeys(refs)),
                }

            tag_slug = slugify(tag) if tag else "untagged"
            op_key = _op_key(method.upper(), path, op)
            slug = f"operations/{tag_slug}/op-{op_key}"
            section_id = make_section_id(repo, doc_path, slug, 3)

            op_content = _render_op_markdown(method.upper(), path, op)

            sec = Section(
                id=section_id,
                repo=repo,
                doc_path=doc_path,
                title=f"{method.upper()} {path}",
                content=op_content,
                level=3,
                parent_id="",  # wired below
                children=[],
                byte_start=0,
                byte_end=0,
                summary=(op.get("summary") or f"{method.upper()} {path}")[:200],
                content_hash=compute_content_hash(op_content),
                references=extract_references(op_content),
                tags=extract_tags(op_content),
                metadata={
                    "openapi_op": {
                        "method": method.upper(),
                        "path": path,
                        "operationId": (op.get("operationId") or "").strip() or None,
                        "summary": (op.get("summary") or "").strip(),
                        "description": (op.get("description") or "").strip(),
                        "tags": [t for t in (op.get("tags") or []) if isinstance(t, str)],
                        "parameters": params,
                        "request_body": req_body_schema,
                        "responses": response_refs,
                        "deprecated": bool(op.get("deprecated")),
                        "security": op.get("security") or [],
                    }
                },
            )

            # Track schema → op_keys for the schema-side metadata.
            referenced: set[str] = set()
            if req_body_schema:
                for r in req_body_schema.get("refs", []) or []:
                    referenced.add(r)
            for r in response_refs.values():
                for ref in r.get("refs", []) or []:
                    referenced.add(ref)
            for sname in referenced:
                schema_to_ops.setdefault(sname, []).append(op_key)

            if tag:
                if tag not in tagged_ops:
                    tagged_ops[tag] = []
                    seen_tags.append(tag)
                tagged_ops[tag].append(sec)
            else:
                untagged_ops.append(sec)

    # ----- Operations parent + tag groups -----
    ops_parent_id: Optional[str] = None
    if tagged_ops or untagged_ops:
        ops_parent_id = make_section_id(repo, doc_path, "operations", 2)
        sections.append(
            Section(
                id=ops_parent_id,
                repo=repo,
                doc_path=doc_path,
                title="Operations",
                content="HTTP operations exposed by the API.\n",
                level=2,
                parent_id=root_id,
                children=[],
                byte_start=0,
                byte_end=0,
                summary="Operations",
                content_hash=compute_content_hash("operations"),
            )
        )
        for tag in seen_tags:
            tag_slug = slugify(tag) or "untagged"
            tag_id = make_section_id(repo, doc_path, f"operations/{tag_slug}", 2)
            sections.append(
                Section(
                    id=tag_id,
                    repo=repo,
                    doc_path=doc_path,
                    title=tag,
                    content=f"{tag} operations.\n",
                    level=2,
                    parent_id=ops_parent_id,
                    children=[],
                    byte_start=0,
                    byte_end=0,
                    summary=tag,
                    content_hash=compute_content_hash(tag),
                    metadata={"openapi_tag": tag},
                )
            )
            for op_sec in tagged_ops[tag]:
                op_sec.parent_id = tag_id
                sections.append(op_sec)
        if untagged_ops:
            untagged_id = make_section_id(repo, doc_path, "operations/untagged", 2)
            sections.append(
                Section(
                    id=untagged_id,
                    repo=repo,
                    doc_path=doc_path,
                    title="Untagged",
                    content="Operations without an explicit tag.\n",
                    level=2,
                    parent_id=ops_parent_id,
                    children=[],
                    byte_start=0,
                    byte_end=0,
                    summary="Untagged",
                    content_hash=compute_content_hash("untagged"),
                )
            )
            for op_sec in untagged_ops:
                op_sec.parent_id = untagged_id
                sections.append(op_sec)

    # ----- Schemas -----
    schemas = (spec.get("components") or {}).get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        schemas = spec.get("definitions") or {}  # OpenAPI 2.x

    if isinstance(schemas, dict) and schemas:
        schemas_parent_id = make_section_id(repo, doc_path, "schemas", 2)
        sections.append(
            Section(
                id=schemas_parent_id,
                repo=repo,
                doc_path=doc_path,
                title="Schemas",
                content="Component schemas.\n",
                level=2,
                parent_id=root_id,
                children=[],
                byte_start=0,
                byte_end=0,
                summary="Schemas",
                content_hash=compute_content_hash("schemas"),
            )
        )
        for sname, schema in schemas.items():
            if not isinstance(schema, dict):
                continue
            slug = f"schemas/schema-{slugify(sname)}"
            sid = make_section_id(repo, doc_path, slug, 3)
            content_str = _render_schema_markdown(sname, schema)
            sections.append(
                Section(
                    id=sid,
                    repo=repo,
                    doc_path=doc_path,
                    title=sname,
                    content=content_str,
                    level=3,
                    parent_id=schemas_parent_id,
                    children=[],
                    byte_start=0,
                    byte_end=0,
                    summary=(schema.get("description") or sname)[:200],
                    content_hash=compute_content_hash(content_str),
                    references=extract_references(content_str),
                    tags=extract_tags(content_str),
                    metadata={
                        "openapi_schema": {
                            "name": sname,
                            "type": schema.get("type"),
                            "format": schema.get("format"),
                            "properties": list((schema.get("properties") or {}).keys()),
                            "required": list(schema.get("required") or []),
                            "refs": _refs_in(schema),
                            "used_by_operations": list(dict.fromkeys(schema_to_ops.get(sname, []))),
                        }
                    },
                )
            )

    return sections
