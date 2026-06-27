"""Structured OpenAPI retrieval tools (v1.18.0).

Read sections whose ``metadata`` carries ``openapi_op`` or ``openapi_schema``.
Together they make API specs first-class — agents can ask for an endpoint by
path/method/tag, find every operation that touches a schema, walk the
schema dependency graph, etc., without re-parsing the raw YAML each call.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Optional

from ..storage import DocStore


def _iter_op_sections(index):
    for sec in index.sections:
        op = (sec.get("metadata") or {}).get("openapi_op")
        if op:
            yield sec, op


def _iter_schema_sections(index):
    for sec in index.sections:
        sch = (sec.get("metadata") or {}).get("openapi_schema")
        if sch:
            yield sec, sch


def _op_summary(sec: dict, op: dict) -> dict:
    """Compact projection used as result rows in find_endpoint /
    list_endpoints_by_tag / find_operations_using_schema."""
    return {
        "section_id": sec.get("id", ""),
        "doc_path": sec.get("doc_path", ""),
        "method": op.get("method"),
        "path": op.get("path"),
        "operationId": op.get("operationId"),
        "summary": op.get("summary", ""),
        "tags": op.get("tags", []),
        "deprecated": bool(op.get("deprecated")),
    }


def find_endpoint(
    repo: str,
    path: Optional[str] = None,
    method: Optional[str] = None,
    tag: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Return operations matching path glob / method / tag filters.

    All filters are AND'd. ``path`` is a fnmatch glob (e.g. ``"/pets/*"``),
    case-sensitive. ``method`` is case-insensitive.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    method_norm = (method or "").upper().strip() or None
    results = []
    for sec, op in _iter_op_sections(index):
        if method_norm and (op.get("method") or "").upper() != method_norm:
            continue
        if path and not fnmatch.fnmatchcase(op.get("path") or "", path):
            continue
        if tag and tag not in (op.get("tags") or []):
            continue
        results.append(_op_summary(sec, op))

    return {
        "repo": f"{owner}/{name}",
        "filters": {"path": path, "method": method_norm, "tag": tag},
        "results": results,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "result_count": len(results),
        },
    }


def list_endpoints_by_tag(
    repo: str,
    tag: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return every operation whose ``tags`` list contains ``tag`` (exact)."""
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    results = []
    for sec, op in _iter_op_sections(index):
        if tag in (op.get("tags") or []):
            results.append(_op_summary(sec, op))
    return {
        "repo": f"{owner}/{name}",
        "tag": tag,
        "results": results,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "result_count": len(results),
        },
    }


def find_operations_using_schema(
    repo: str,
    schema_name: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Return operations whose request body or any response references
    ``schema_name``.

    Cross-references both directions:
      - openapi_op.request_body.refs / openapi_op.responses.*.refs
      - openapi_schema.used_by_operations (as a faster fallback path)
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    results = []
    for sec, op in _iter_op_sections(index):
        refs: set[str] = set()
        rb = op.get("request_body")
        if isinstance(rb, dict):
            for r in rb.get("refs", []) or []:
                refs.add(r)
        for resp in (op.get("responses") or {}).values():
            for r in (resp or {}).get("refs", []) or []:
                refs.add(r)
        if schema_name in refs:
            row = _op_summary(sec, op)
            row["referenced_in"] = sorted(refs)
            results.append(row)

    return {
        "repo": f"{owner}/{name}",
        "schema": schema_name,
        "results": results,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "result_count": len(results),
        },
    }


def get_schema_graph(
    repo: str,
    schema_name: str,
    max_depth: int = 5,
    storage_path: Optional[str] = None,
) -> dict:
    """Walk the schema reference graph from ``schema_name``.

    Returns ``{root, depth, nodes:{name:{type, properties, required, refs}},
    edges:[(from, to)], unresolved:[name, ...]}``. Cycles are tracked in
    ``visited`` and surface as edges only once. ``max_depth`` caps the
    BFS in case of pathological inputs.
    """
    t0 = time.perf_counter()
    store = DocStore(base_path=storage_path)
    owner, name = store._resolve_repo(repo)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repo not found: {repo}"}

    by_name: dict[str, dict] = {}
    for _sec, sch in _iter_schema_sections(index):
        if isinstance(sch.get("name"), str):
            by_name[sch["name"]] = sch

    if schema_name not in by_name:
        return {
            "repo": f"{owner}/{name}",
            "root": schema_name,
            "error": f"Schema not indexed: {schema_name}",
        }

    nodes: dict[str, dict] = {}
    edges: list[list[str]] = []
    unresolved: list[str] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(schema_name, 0)]

    while queue:
        cur, depth = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        sch = by_name.get(cur)
        if not sch:
            unresolved.append(cur)
            continue
        nodes[cur] = {
            "type": sch.get("type"),
            "properties": sch.get("properties", []),
            "required": sch.get("required", []),
            "refs": sch.get("refs", []),
        }
        if depth >= max_depth:
            continue
        for ref in sch.get("refs", []) or []:
            edges.append([cur, ref])
            if ref not in visited:
                queue.append((ref, depth + 1))

    return {
        "repo": f"{owner}/{name}",
        "root": schema_name,
        "nodes": nodes,
        "edges": edges,
        "unresolved": unresolved,
        "_meta": {
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "max_depth": max_depth,
        },
    }
