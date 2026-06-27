#!/usr/bin/env python3
"""Real AI Agent Benchmark — SWE-rebench Task with/without jcodemunch Tools.

Runs TWO agents against the same SWE-rebench task:
  - Agent A: Has full jcodemunch toolset + AGENTS.md instructions
  - Agent B: Basic tools only (read_file, grep, list_dir) — NO jcodemunch

Both agents use the same LLM (model="auto" via http://localhost:3001/v1)
and receive the SAME task prompt. Metrics: tokens, time, success.

Usage:
    PYTHONPATH=src python benchmarks/agent_benchmark/agent_benchmark.py \
        --task-id 0b01001001__spectree-64 \
        --iterations 3 \
        --out benchmarks/agent_benchmark/results.json

Requirements:
    - API endpoint running at http://localhost:3001/v1
    - API key in ~/.animamunch/config.json
    - Model "auto" available at the endpoint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

try:
    import httpx
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except ImportError:
    sys.exit("httpx/tiktoken not found — run: pip install httpx tiktoken")

# ---------------------------------------------------------------------------
# Import jcodemunch tools for Agent A
# ---------------------------------------------------------------------------
try:
    from jcodemunch_mcp.tools.assemble_task_context import (
        assemble_task_context as _assemble_task_context,
    )
    from jcodemunch_mcp.tools.check_safe import check_safe as _check_safe
    from jcodemunch_mcp.tools.get_blast_radius import (
        get_blast_radius as _get_blast_radius,
    )
    from jcodemunch_mcp.tools.get_call_hierarchy import (
        get_call_hierarchy as _get_call_hierarchy,
    )
    from jcodemunch_mcp.tools.get_context_bundle import (
        get_context_bundle as _get_context_bundle,
    )
    from jcodemunch_mcp.tools.get_file_outline import (
        get_file_outline as _get_file_outline,
    )
    from jcodemunch_mcp.tools.get_repo_health import get_repo_health as _get_repo_health
    from jcodemunch_mcp.tools.get_repo_map import get_repo_map as _get_repo_map
    from jcodemunch_mcp.tools.get_symbol import get_symbol_source as _get_symbol_source
    from jcodemunch_mcp.tools.get_tectonic_map import (
        get_tectonic_map as _get_tectonic_map,
    )
    from jcodemunch_mcp.tools.search_symbols import search_symbols as _search_symbols
    from jcodemunch_mcp.tools.search_text import search_text as _search_text

    _HAS_JCODEMUNCH = True
except ImportError as e:
    _HAS_JCODEMUNCH = False
    print(f"Warning: jcodemunch tools not available: {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Config & API client
# ---------------------------------------------------------------------------
CONFIG_PATH = Path.home() / ".animamunch" / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Config not found at {CONFIG_PATH}. Create it with:\n"
            '  {"api_endpoint": "http://localhost:3001/v1", "api_key": "...", "model": "auto"}'
        )
    return json.loads(CONFIG_PATH.read_text())


CONFIG = load_config()
API_ENDPOINT = CONFIG["api_endpoint"].rstrip("/")
API_KEY = CONFIG["api_key"]
MODEL = CONFIG.get("model", "auto")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


class APIClient:
    """Simple async client for the /v1/chat/completions endpoint."""

    def __init__(self, base_url: str, headers: dict):
        self.base_url = base_url
        self.headers = headers
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=300.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def chat_completion(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> dict:
        """Call the chat completions endpoint with retry on 429."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        max_retries = 3
        base_delay = 1.0  # seconds
        for attempt in range(max_retries):
            resp = await self._client.post(url, headers=self.headers, json=payload)
            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)  # exponential backoff
                    print(
                        f"  [API] Rate limited (429), retrying in {delay}s... (attempt {attempt + 1}/{max_retries})",
                        flush=True,
                    )
                    await asyncio.sleep(delay)
                    continue
            resp.raise_for_status()
            return resp.json()
        # If we exhausted retries
        resp.raise_for_status()
        return resp.json()

    async def chat_completion_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ):
        """Stream chat completions (yields chunks)."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        async with self._client.stream(
            "POST", url, headers=self.headers, json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    yield json.loads(data)


# ---------------------------------------------------------------------------
# SWE-rebench task (built-in fallback)
# ---------------------------------------------------------------------------
SWE_TASKS = {
    "0b01001001__spectree-64": {
        "instance_id": "0b01001001__spectree-64",
        "repo": "0b01001001/spectree",
        "base_commit": "a091fab020ac26548250c907bae0855273a98778",
        "problem_statement": (
            "[BUG] description for query paramters can not show in swagger ui\n"
            "Hi, when I add a description for a schema used in query, "
            "it can not show in swagger ui but can show in Redoc\n"
            "```py\n"
            "@HELLO.route('/', methods=['GET'])\n"
            "@api.validate(query=HelloForm)\n"
            "def hello():\n"
            '    """\n'
            "    hello 注释\n"
            "    :return:\n"
            '    """\n'
            "    return '...'\n"
            "```"
        ),
        "version": "0.3",
        "FAIL_TO_PASS": ["tests/test_utils.py::test_parse_params"],
        "PASS_TO_PASS": [
            "tests/test_utils.py::test_comments",
            "tests/test_utils.py::test_parse_code",
            "tests/test_utils.py::test_parse_name",
            "tests/test_utils.py::test_has_model",
            "tests/test_utils.py::test_parse_resp",
            "tests/test_utils.py::test_parse_request",
        ],
    },
}


def load_task(task_id: str | None = None) -> dict:
    if task_id and task_id in SWE_TASKS:
        return SWE_TASKS[task_id]
    # Default to first task
    return next(iter(SWE_TASKS.values()))


# ---------------------------------------------------------------------------
# jcodemunch tool definitions (for Agent A)
# ---------------------------------------------------------------------------
JCODEMUNCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "assemble_task_context",
            "description": "Opening move for any task. Auto-classifies intent (explore/debug/refactor/extend/audit/review), surfaces ranked symbols + context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Repository identifier (owner/repo)",
                    },
                    "task": {
                        "type": "string",
                        "description": "Natural-language task description",
                    },
                    "intent": {
                        "type": "string",
                        "enum": [
                            "explore",
                            "debug",
                            "refactor",
                            "extend",
                            "audit",
                            "review",
                        ],
                        "default": "debug",
                    },
                    "token_budget": {"type": "integer", "default": 8000},
                },
                "required": ["repo", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbols",
            "description": "Find symbols by name, signature, summary. Supports mode=context (ranked context), mode=winnow (filters), semantic=true (embeddings).",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                    "mode": {
                        "type": "string",
                        "enum": ["search", "winnow", "context"],
                        "default": "search",
                    },
                    "detail_level": {
                        "type": "string",
                        "enum": ["compact", "standard", "full"],
                        "default": "standard",
                    },
                    "semantic": {"type": "boolean", "default": False},
                    "token_budget": {"type": "integer"},
                },
                "required": ["repo", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_symbol_source",
            "description": "Get full source code of a symbol by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol_id": {"type": "string"},
                    "context_lines": {"type": "integer", "default": 0},
                },
                "required": ["repo", "symbol_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Full-text search across file contents (string literals, comments, configs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "query": {"type": "string"},
                    "context_lines": {"type": "integer", "default": 3},
                    "is_regex": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 20},
                },
                "required": ["repo", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": "Get all symbols (functions, classes, methods) in a file with signatures and summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "file_path": {"type": "string"},
                },
                "required": ["repo", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_blast_radius",
            "description": "Find all files affected by changing a symbol. Returns confirmed + potential files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol": {"type": "string"},
                    "depth": {"type": "integer", "default": 1},
                    "include_source": {"type": "boolean", "default": True},
                },
                "required": ["repo", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_call_hierarchy",
            "description": "Return incoming callers and outgoing callees for a symbol, N levels deep.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol_id": {"type": "string"},
                    "depth": {"type": "integer", "default": 3},
                    "direction": {
                        "type": "string",
                        "enum": ["callers", "callees", "both"],
                        "default": "both",
                    },
                    "include_impact": {"type": "boolean", "default": False},
                },
                "required": ["repo", "symbol_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_context_bundle",
            "description": "Get full source + imports for one or more symbols in one call. Deduplicates shared imports.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol_ids": {"type": "array", "items": {"type": "string"}},
                    "budget_strategy": {
                        "type": "string",
                        "enum": ["most_relevant", "core_first", "compact"],
                        "default": "core_first",
                    },
                    "token_budget": {"type": "integer"},
                    "include_callers": {"type": "boolean", "default": False},
                },
                "required": ["repo", "symbol_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repo_map",
            "description": "Signature-level overview of a repository, grouped by file, ranked by PageRank.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "group_by": {
                        "type": "string",
                        "enum": ["file", "flat"],
                        "default": "file",
                    },
                    "token_budget": {"type": "integer", "default": 2048},
                    "top_n": {"type": "integer", "default": 20},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repo_health",
            "description": "One-call triage: dead code %, complexity, hotspots, dependency cycles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "detailed": {"type": "boolean", "default": True},
                    "top_n": {"type": "integer", "default": 20},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tectonic_map",
            "description": "Logical module topology: plates, drifters, nexus plates (coupled >=4).",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "days": {"type": "integer", "default": 90},
                    "min_plate_size": {"type": "integer", "default": 2},
                },
                "required": ["repo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_safe",
            "description": "Preflight: can this symbol be safely edited/deleted?",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "symbol": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["edit", "delete"],
                        "default": "edit",
                    },
                    "include_runtime": {"type": "boolean", "default": True},
                },
                "required": ["repo", "symbol"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Basic tools (for Agent B - no jcodemunch)
# ---------------------------------------------------------------------------
BASIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with regex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "include_pattern": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["command", "cwd"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# AGENTS.md Instructions for Agent A
# ---------------------------------------------------------------------------
JCODEMUNCH_AGENTS_MD = """## jcodemunch

Repo: `{{REPO}}` (indexed). Symbol ID: `{file_path}::{qualified_name}#{kind}`

### Code Exploration Policy (CRITICAL)
**Always use jCodemunch-MCP tools for code navigation.** Never fall back to Read, Grep, Glob, or Bash for code exploration. **Exception:** Use `Read` only when you need to edit a file.

### Session-Aware Routing (confidence + negative evidence)
- `confidence: high` → act directly, max 2 supplementary reads
- `confidence: medium` → explore recommended files, max 5 reads
- `confidence: low` → report the gap, don't keep searching
- `verdict: "no_implementation_found"` → STOP, don't re-search with different terms
- `_meta.confidence < 0.4` → low confidence means widen search or report gap, don't proceed as-is
- `_meta.freshness.repo_is_stale` → suggest re-indexing before trusting results

### Core lookup
- `assemble_task_context(repo="{{REPO}}", task="...", model="auto")` — opening move; auto-classifies intent (explore/debug/refactor/extend/audit/review), surfaces symbols + ranked context. **Model-driven tiering**: passes your model id so server tailors sub-tool selection.
- `resolve_repo(path=".")` — confirm repo is indexed; if not: `index_folder(path=".")`
- `search_symbols(repo="{{REPO}}", query="...")` — find by name, signature, summary
  - `mode="context"` — query-less ranked context assembly
  - `mode="winnow"` — multi-axis constraint filter (kind, language, complexity, churn, etc.)
  - `mode="search"` — standard symbol search (default)
  - `detail_level="compact"` — 15 tokens/row, great for broad discovery
  - `detail_level="standard"` — signatures + summaries (default)
  - `detail_level="full"` — full source/docstring
  - `semantic=true` — embedding-based search (requires `embed_repo` first)
  - `fusion=true` — Weighted Reciprocal Rank across lexical/structural/similarity/identity
  - `token_budget` — hard cap on returned tokens
- `get_context_bundle(symbol_ids=[...], budget_strategy="core_first", token_budget=6000)` — multi-symbol context in one call; deduplicates imports
- `get_symbol_source(repo="{{REPO}}", symbol_id="...")` — full source of one symbol
- `search_text(repo="{{REPO}}", query="...")` — full-text search across file contents (string literals, comments, configs)
- `search_ast(repo="{{REPO}}", pattern="..." | category="...")` — structural anti-pattern scan (empty_catch, god_function, hardcoded_secret, etc.)

### Impact & safety
- `get_blast_radius(symbol="...", include_source=true, call_depth=2, include_decisions=true, source_budget=8000)` — check impact before changes; `include_decisions` surfaces git commit intent (revert/perf/refactor/bugfix)
- `find_references` / `get_call_hierarchy` — trace who uses a symbol
  - `mode="refs"` (default) / `"importers"` / `"related"`
  - `quick=true` — dead-code shortcut (returns bool only)
  - `chains=true` — signal chains (HTTP routes, CLI commands, etc.)
  - `kind` — filter gateways: http, cli, event, task, main, test
  - `max_depth` — BFS depth limit (1-8, default 5)
  - `include_impact=true` — transitive "what breaks if I delete this?"
- `check_safe(repo="{{REPO}}", symbol="...", mode="edit"|"delete", include_runtime=true)` — composite preflight: can this symbol be safely edited/deleted? Returns verdict + top-5 blockers
- `plan_refactoring(repo="{{REPO}}", symbol="...", refactor_type="rename"|"move"|"extract"|"signature", new_name=...)` — generate multi-file edit plan before refactoring
- `get_changed_symbols(repo="{{REPO}}", include_blast_radius=true, max_blast_depth=3)` — map git diff to affected symbols + downstream impact
- `get_pr_risk_profile(repo="{{REPO}}", base_ref="main", head_ref="HEAD")` — unified risk assessment for a PR/branch

### Repository intelligence
- `get_repo_health(repo="{{REPO}}", detailed=true, top_n=30)` — one-call triage (dead code %, complexity, hotspots, cycle count)
- `get_repo_map(repo="{{REPO}}", group_by="file", token_budget=2048, top_n=20)` — signature-level overview ranked by PageRank
  - `mode="outline"` — lightweight directory/language/symbol count overview
- `get_tectonic_map(repo="{{REPO}}", days=90, min_plate_size=2)` — logical module topology (hidden boundaries, misplaced files, drifters, nexus plates coupled ≥4)
- `find_hot_paths(repo="{{REPO}}", top_n=20)` — top-N symbols by runtime hit count (requires ingested traces)
- `get_dead_code_v2(repo="{{REPO}}", min_confidence=0.67, file_pattern="src/**")` — multi-signal dead code detection
- `find_similar_symbols(repo="{{REPO}}", threshold=0.85, semantic_weight=0.6, include_tests=false)` — cluster similar functions/methods (consolidation candidates)
- `get_symbol_provenance(repo="{{REPO}}", symbol="...", max_commits=30)` — git authorship lineage & evolution narrative
- `get_symbol_complexity(repo="{{REPO}}", symbol_id="...")` — cyclomatic complexity, nesting, params
- `get_class_hierarchy(repo="{{REPO}}", class_name="...")` — inheritance ancestors + descendants
- `find_implementations(repo="{{REPO}}", symbol="...", include_subclasses=true)` — find concrete impls of an interface/abstract
- `get_project_intel(repo="{{REPO}}")` — auto-discover Dockerfiles, CI configs, deps, APIs
- `list_workspaces(repo="{{REPO}}")` — enumerate monorepo workspace members
- `search_columns(repo="{{REPO}}", query="...")` — search column metadata across indexed models

### Runtime & indexing
- `import_runtime_signal(repo="{{REPO}}", path="...", source="otel"|"sql_log"|"stack_log")` — ingest runtime traces
- `embed_repo(repo="{{REPO}}", force=true, batch_size=50)` — precompute symbol embeddings for semantic search
- `summarize_repo(repo="{{REPO}}", force=true)` — re-run AI summarization pipeline
- `index_file(path="...")` — surgical single-file reindex after edits
- `index_folder(path="...")` / `index_repo(url="...")` — full index/reindex
- `register_edit(repo="{{REPO}}", file_paths=[...], reindex=true)` — invalidate caches after file edits (call after every edit)

### Golden Rules
1. **Always start with `assemble_task_context`** — it auto-classifies intent and returns ranked symbols + context in one call. Never manually hunt for entry points.
2. **Batch everything** — use `symbol_ids[]` in `get_context_bundle`, `get_symbol_source`, `search_symbols` instead of serial calls. Token budget is your friend.
3. **Verify with `verify=true` / `verify_against="git_sha"`** — catches index drift vs. working tree.
4. **Use `mode` switches on `search_symbols`: `context` for query-less ranked context, `winnow` for multi-axis filters, `semantic=true` for embedding search.
5. **Prefer `get_context_bundle` over raw file reads` — deduplicates imports, respects token budget, returns ready-to-use context.
6. **After every edit, call `register_edit`** — keeps index fresh for subsequent tool calls in same session.

### Common Workflows

#### 1. Cold-start orientation (new repo / unfamiliar area)
```
get_repo_map(repo="{{REPO}}", group_by="flat", top_n=30)     # Top symbols by PageRank
get_tectonic_map(repo="{{REPO}}")                               # Logical module boundaries
get_repo_health(repo="{{REPO}}", detailed=true)                 # Dead code %, complexity, cycles
```

#### 2. Feature exploration — "How does X work?"
```
assemble_task_context(repo="{{REPO}}", task="How does X work?")
# → returns ranked symbols + context
get_context_bundle(symbol_ids=[...], budget_strategy="core_first")
```

#### 3. Refactoring safety (rename/move/extract)
```
check_safe(repo="{{REPO}}", symbol="SymbolName", mode="edit")
plan_refactoring(repo="{{REPO}}", symbol="SymbolName", refactor_type="rename", new_name="newName")
get_blast_radius(symbol="SymbolName", depth=2, include_source=true, include_decisions=true)
```

#### 4. Dead code cleanup
```
get_dead_code_v2(repo="{{REPO}}", min_confidence=0.67, file_pattern="src/**")
find_similar_symbols(repo="{{REPO}}", threshold=0.85, include_kinds=["function", "method"])
```

#### 5. Performance hotspot triage
```
find_hot_paths(repo="{{REPO}}", top_n=20)
get_repo_health(repo="{{REPO}}", detailed=true, top_n=30)
get_symbol_complexity(repo="{{REPO}}", symbol_id="...")
```

#### 6. PR / change risk assessment
```
get_changed_symbols(repo="{{REPO}}", include_blast_radius=true, max_blast_depth=3)
get_pr_risk_profile(repo="{{REPO}}", base_ref="main", head_ref="HEAD")
```

#### 7. Understanding unfamiliar code before modifying
```
get_symbol_provenance(repo="{{REPO}}", symbol="SymbolName", max_commits=30)
get_call_hierarchy(symbol_id="...", direction="both", depth=3, include_impact=true)
find_implementations(repo="{{REPO}}", symbol="InterfaceName", include_subclasses=true)
```

#### 8. Finding config / string literals / comments (not symbols)
```
search_text(repo="{{REPO}}", query="MAX_RETRIES", context_lines=3)
search_ast(repo="{{REPO}}", category="security")              # hardcoded_secret, eval_exec
search_ast(repo="{{REPO}}", pattern="string:/password/i")      # custom pattern
```

### Parameter Cheatsheet

| Tool | Key params | When to use |
|---|---|---|
| `assemble_task_context` | `task`, `token_budget` (8k), `model` | **First call for any task** — returns intent, symbols, context |
| `search_symbols` | `mode`, `semantic`, `fusion`, `detail_level`, `token_budget` | Symbol discovery; `mode=context` = ranked context w/o query |
| `get_context_bundle` | `symbol_ids[]`, `budget_strategy`, `token_budget` | Multi-symbol context in one call; `core_first` keeps primary symbol |
| `get_blast_radius` | `depth`, `include_source`, `include_depth_scores`, `include_decisions`, `source_budget`, `call_depth` | Pre-edit impact; `include_decisions` = per-hop git intent |
| `check_safe` | `mode` (edit/delete), `include_runtime` | Preflight — returns verdict + top-5 blockers |
| `plan_refactoring` | `refactor_type`, `new_name`/`new_file`/`new_signature` | Returns `{old_text, new_text}` blocks ready for Edit tool |
| `get_repo_health` | `detailed`, `rules` (layer defs) | One-call triage; `detailed=true` adds cycles, coupling, hotspots |
| `get_tectonic_map` | `days`, `min_plate_size` | Module topology; finds drifters, nexus plates (coupled ≥4) |
| `find_similar_symbols` | `threshold`, `semantic_weight`, `include_tests` | Consolidation candidates; `semantic_weight=0.6` default |
| `get_symbol_provenance` | `max_commits` | Authorship lineage + evolution narrative |
| `search_ast` | `category`, `pattern`, `language` | Anti-pattern sweep; `category=all` runs everything |
| `get_changed_symbols` | `since_sha`, `until_sha`, `include_blast_radius` | Maps git diff → symbols + downstream impact |
| `get_pr_risk_profile` | `base_ref`, `head_ref`, `days` | Composite risk score (blast + complexity + churn + tests + volume) |
| `find_references` | `mode`, `quick`, `include_call_chain` | `quick=true` = dead-code shortcut; `chains=true` = signal chains |
| `get_call_hierarchy` | `chains`, `kind`, `max_depth` | `chains=true` merges get_signal_chains functionality |
| `get_repo_map` | `mode="outline"` | Lightweight directory/language/symbol count overview |
| `embed_repo` | `force`, `batch_size` | Precompute embeddings for semantic search |

### Anti-patterns to Avoid
- ❌ Reading full files with `read_file` — use `get_context_bundle` or `get_symbol_source`
- ❌ Calling `search_symbols` repeatedly — batch with `symbol_ids[]` in `get_context_bundle`
- ❌ Skipping `check_safe` before edits/deletes — 5s call prevents hours of revert
- ❌ Not verifying with `verify=true` — index can drift from working tree
- ❌ Using `grep` for symbol lookup — `search_symbols` understands signatures, imports, types
- ❌ Manual blast radius tracing — `get_blast_radius(depth=2, include_source=true)` is instant
- ❌ Ignoring `_meta.confidence` < 0.4 — low confidence means widen search or report gap
- ❌ Re-searching after `verdict: "no_implementation_found"` — report the gap, don't hallucinate
- ❌ Forgetting `register_edit` after file edits — index drifts, subsequent calls see stale data

### Pro Tips
- **`fusion=true` on `search_symbols`** — uses Weighted Reciprocal Rank across lexical/structural/similarity/identity channels; best for vague queries
- **`budget_strategy="compact"` on `get_context_bundle`** — returns signatures only (min tokens), great for call-chain mapping
- **`include_decisions=true` on `get_blast_radius` / `get_call_hierarchy(include_impact=true)`** — surfaces git commit intent (revert/perf/refactor/bugfix) from history
- **`embed_repo(repo="{{REPO}}")` once** — then `semantic=true` on `search_symbols` works instantly for semantic queries
- **`index_file` after every edit** — keeps index fresh for subsequent tool calls in same session
- **`cross_repo=true` on `get_blast_radius` / `find_references`** — finds consumers in other indexed repos

### Token Budget Discipline
- `assemble_task_context(token_budget=4000)` for focused tasks
- `get_context_bundle(token_budget=6000, budget_strategy="core_first")` for multi-symbol context
- `search_symbols(token_budget=3000)` with `detail_level="compact"` for broad discovery (15 tokens/row)
- Always check `_meta.tokens_used` / `_meta.tokens_remaining` in responses
"""


# ---------------------------------------------------------------------------
# Tool Executor — executes jcodemunch and basic tools
# ---------------------------------------------------------------------------
class ToolExecutor:
    """Executes tool calls for both agents."""

    def __init__(self, repo: str, workspace: Path | None = None):
        self.repo = repo
        self.workspace = workspace or Path.cwd()

    async def execute(self, name: str, args: dict) -> dict:
        """Execute a tool by name with given arguments."""
        # jcodemunch tools
        if name == "assemble_task_context":
            return await self._call(_assemble_task_context, args)
        if name == "search_symbols":
            return await self._call(_search_symbols, args)
        if name == "get_symbol_source":
            return await self._call(_get_symbol_source, args)
        if name == "search_text":
            return await self._call(_search_text, args)
        if name == "get_file_outline":
            return await self._call(_get_file_outline, args)
        if name == "get_blast_radius":
            return await self._call(_get_blast_radius, args)
        if name == "get_call_hierarchy":
            return await self._call(_get_call_hierarchy, args)
        if name == "get_context_bundle":
            return await self._call(_get_context_bundle, args)
        if name == "get_repo_map":
            return await self._call(_get_repo_map, args)
        if name == "get_repo_health":
            return await self._call(_get_repo_health, args)
        if name == "get_tectonic_map":
            return await self._call(_get_tectonic_map, args)
        if name == "check_safe":
            return await self._call(_check_safe, args)
        # Basic tools
        if name == "read_file":
            return self._read_file(args)
        if name == "grep":
            return self._grep(args)
        if name == "list_directory":
            return self._list_directory(args)
        if name == "run_command":
            return self._run_command(args)
        return {"error": f"Unknown tool: {name}"}

    async def _call(self, fn, args: dict) -> dict:
        """Call a function (sync or async) and return result as dict."""
        try:
            result = fn(**args)
            if asyncio.iscoroutine(result):
                result = await result
            return self._serialize(result)
        except Exception as e:
            return {"error": str(e), "tool": fn.__name__}

    def _serialize(self, obj: Any) -> dict:
        """Convert tool result to JSON-serializable dict."""
        if obj is None:
            return {}
        if isinstance(obj, (dict, list, str, int, float, bool)):
            return obj
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)

    # --- Basic tool implementations ---
    def _read_file(self, args: dict) -> dict:
        path = args.get("path", "")
        start = args.get("start_line")
        end = args.get("end_line")
        abs_path = self.workspace / path
        try:
            if not abs_path.exists():
                return {"error": f"File not found: {path}"}
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            if start is not None or end is not None:
                s = (start - 1) if start else 0
                e = end if end else len(lines)
                lines = lines[s:e]
                content = "\n".join(lines)
            return {"path": path, "content": content, "lines": len(lines)}
        except Exception as e:
            return {"error": str(e)}

    def _grep(self, args: dict) -> dict:
        pattern = args.get("pattern", "")
        include = args.get("include_pattern", "**/*.py")
        try:
            result = subprocess.run(
                ["grep", "-r", "-n", pattern, "--include", include],
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
            matches = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        matches.append(
                            {"file": parts[0], "line": int(parts[1]), "text": parts[2]}
                        )
            return {"matches": matches, "count": len(matches)}
        except Exception as e:
            return {"error": str(e)}

    def _list_directory(self, args: dict) -> dict:
        path = args.get("path", ".")
        abs_path = self.workspace / path
        try:
            if not abs_path.exists():
                return {"error": f"Path not found: {path}"}
            entries = []
            for entry in abs_path.iterdir():
                entries.append(
                    {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
                )
            return {"path": path, "entries": entries}
        except Exception as e:
            return {"error": str(e)}

    def _run_command(self, args: dict) -> dict:
        cmd = args.get("command", "")
        cwd = args.get("cwd", str(self.workspace))
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Task prompt for both agents
# ---------------------------------------------------------------------------
TASK_PROMPT_TEMPLATE = """You are a senior software engineer tasked with fixing a bug in a codebase.

## Repository
- Repo: `{repo}`
- Commit: `{commit}`
- Language: Python

## Problem Statement
{problem_statement}

## Failing Test(s)
{fail_to_pass}

## Your Goal
1. Explore the codebase to understand the bug
2. Locate the root cause
3. Identify the exact file(s) and function(s) that need to be fixed
4. Provide a clear explanation of the fix needed

## Available Tools
{tools_description}

## Instructions
- Use the tools available to you to investigate the codebase
- Be systematic: start broad, then narrow down
- Think step by step and explain your reasoning
- When you have identified the root cause, provide a final answer with:
  - The file(s) and function(s) to fix
  - The specific change needed
  - Why this fixes the issue

Begin by exploring the repository structure and understanding the problem.
"""


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------
class AgentRunner:
    def __init__(
        self,
        name: str,
        tools: list[dict],
        system_prompt: str,
        api_client: APIClient,
        tool_executor: ToolExecutor,
    ):
        self.name = name
        self.tools = tools
        self.system_prompt = system_prompt
        self.api_client = api_client
        self.tool_executor = tool_executor
        self.metrics = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_time_ms": 0.0,
            "tool_calls": 0,
            "turns": 0,
        }

    def _count_tokens(self, text: str) -> int:
        # Rough approximation: 1 token ≈ 4 chars for cl100k_base
        return max(1, len(text) // 4)

    async def run(self, task_prompt: str, max_turns: int = 20) -> dict:
        """Run the agent on the task."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task_prompt},
        ]

        start_time = time.perf_counter()

        for turn in range(max_turns):
            self.metrics["turns"] += 1

            # Call LLM
            t0 = time.perf_counter()
            try:
                resp = await self.api_client.chat_completion(
                    messages=messages,
                    tools=self.tools,
                    tool_choice="auto",
                    temperature=0.1,
                )
            except Exception as e:
                return {
                    "success": False,
                    "error": f"API error: {e}",
                    "metrics": self.metrics,
                    "messages": messages,
                }

            elapsed = (time.perf_counter() - t0) * 1000
            self.metrics["total_time_ms"] += elapsed

            # Extract usage
            usage = resp.get("usage", {})
            in_tok = usage.get("prompt_tokens", 0)
            out_tok = usage.get("completion_tokens", 0)
            self.metrics["total_input_tokens"] += in_tok
            self.metrics["total_output_tokens"] += out_tok

            choice = resp["choices"][0]
            message = choice["message"]
            messages.append(message)

            # Check for tool calls
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # No tool calls - agent is done
                content = message.get("content", "")
                success = self._evaluate_success(content)
                return {
                    "success": success,
                    "final_response": content,
                    "metrics": self.metrics,
                    "messages": messages,
                }

            # Execute tool calls
            for tc in tool_calls:
                self.metrics["tool_calls"] += 1
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])

                result = await self.tool_executor.execute(fn_name, fn_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fn_name,
                        "content": json.dumps(result),
                    }
                )

        return {
            "success": False,
            "error": "Max turns reached",
            "metrics": self.metrics,
            "messages": messages,
        }

    def _evaluate_success(self, content: str) -> bool:
        """Check if the agent's final response indicates success."""
        # Simple heuristic: look for key indicators
        content_lower = content.lower()
        success_indicators = [
            "root cause",
            "fix",
            "the issue is",
            "the problem is",
            "need to change",
            "should be",
        ]
        return any(ind in content_lower for ind in success_indicators)


# ---------------------------------------------------------------------------
# Benchmark orchestrator
# ---------------------------------------------------------------------------
async def run_agent_benchmark(
    task_id: str,
    iterations: int = 3,
    out_path: str | None = None,
    dogfood: bool = False,
    repo_override: str | None = None,
) -> dict:
    task = load_task(task_id)
    original_repo = task["repo"]

    # Handle repo selection
    if repo_override:
        repo = repo_override
        workspace = Path.cwd()  # Use current dir as workspace for basic tools
        print(f"[repo] using override: {repo}")
    elif dogfood:
        repo = "animaios/animamunchmcp"
        workspace = Path.cwd()
        print(f"[repo] dogfood mode: using {repo}")
    else:
        repo = original_repo
        # For real SWE tasks, we'd need to clone and index the repo
        # For now, use current dir as workspace
        workspace = Path.cwd()
        print(f"[repo] using task repo: {repo} (workspace: {workspace})")

    # Prepare system prompts
    agent_a_system = (
        "You are a senior software engineer with access to jcodemunch, "
        "a powerful code intelligence toolset. Follow the instructions in AGENTS.md.\n\n"
        + JCODEMUNCH_AGENTS_MD.replace("{{REPO}}", repo)
    )

    agent_b_system = (
        "You are a senior software engineer. You have basic file operations "
        "(read_file, grep, list_directory, run_command) but NO jcodemunch tools. "
        "Explore the codebase manually using grep/read_file."
    )

    # Prepare task prompts
    fail_to_pass = "\n".join(f"- {t}" for t in task.get("FAIL_TO_PASS", []))
    tools_a = "Full jcodemunch toolset (assemble_task_context, search_symbols, get_symbol_source, search_text, get_file_outline, get_blast_radius, get_call_hierarchy, get_context_bundle, get_repo_map, get_repo_health, get_tectonic_map, check_safe, etc.)"
    tools_b = "Basic tools only: read_file, grep, list_directory, run_command"

    task_prompt_a = TASK_PROMPT_TEMPLATE.format(
        repo=repo,
        commit=task["base_commit"][:8],
        problem_statement=task["problem_statement"],
        fail_to_pass=fail_to_pass,
        tools_description=tools_a,
    )

    task_prompt_b = TASK_PROMPT_TEMPLATE.format(
        repo=repo,
        commit=task["base_commit"][:8],
        problem_statement=task["problem_statement"],
        fail_to_pass=fail_to_pass,
        tools_description=tools_b,
    )

    results = {"task": task, "runs": []}

    async with APIClient(API_ENDPOINT, HEADERS) as client:
        for i in range(iterations):
            print(f"\n{'=' * 60}")
            print(f"Iteration {i + 1}/{iterations}")
            print(f"{'=' * 60}")

            # Create tool executors for this iteration
            executor_a = ToolExecutor(repo, workspace)
            executor_b = ToolExecutor(repo, workspace)

            # Agent A (with jcodemunch)
            print(f"\n  Agent A (jcodemunch) running...", flush=True)
            agent_a = AgentRunner(
                "Agent-A", JCODEMUNCH_TOOLS, agent_a_system, client, executor_a
            )
            result_a = await agent_a.run(task_prompt_a)
            result_a["agent"] = "A (jcodemunch)"
            result_a["iteration"] = i + 1
            results["runs"].append(result_a)
            print(
                f"    done: success={result_a.get('success')}, "
                f"time={result_a['metrics']['total_time_ms']:.0f}ms, "
                f"tokens={result_a['metrics']['total_input_tokens'] + result_a['metrics']['total_output_tokens']:,}, "
                f"tool_calls={result_a['metrics']['tool_calls']}"
            )

            # Agent B (basic)
            print(f"\n  Agent B (basic) running...", flush=True)
            agent_b = AgentRunner(
                "Agent-B", BASIC_TOOLS, agent_b_system, client, executor_b
            )
            result_b = await agent_b.run(task_prompt_b)
            result_b["agent"] = "B (basic)"
            result_b["iteration"] = i + 1
            results["runs"].append(result_b)
            print(
                f"    done: success={result_b.get('success')}, "
                f"time={result_b['metrics']['total_time_ms']:.0f}ms, "
                f"tokens={result_b['metrics']['total_input_tokens'] + result_b['metrics']['total_output_tokens']:,}, "
                f"tool_calls={result_b['metrics']['tool_calls']}"
            )

    # Compute summaries
    agent_a_runs = [r for r in results["runs"] if r["agent"].startswith("A")]
    agent_b_runs = [r for r in results["runs"] if r["agent"].startswith("B")]

    def _avg(runs, key):
        if not runs:
            return 0
        return sum(r["metrics"].get(key, 0) for r in runs) / len(runs)

    def _success_rate(runs):
        if not runs:
            return 0
        return sum(1 for r in runs if r.get("success")) / len(runs) * 100

    summary = {
        "agent_a": {
            "avg_time_ms": _avg(agent_a_runs, "total_time_ms"),
            "avg_input_tokens": _avg(agent_a_runs, "total_input_tokens"),
            "avg_output_tokens": _avg(agent_a_runs, "total_output_tokens"),
            "avg_tool_calls": _avg(agent_a_runs, "tool_calls"),
            "avg_turns": _avg(agent_a_runs, "turns"),
            "success_rate": _success_rate(agent_a_runs),
        },
        "agent_b": {
            "avg_time_ms": _avg(agent_b_runs, "total_time_ms"),
            "avg_input_tokens": _avg(agent_b_runs, "total_input_tokens"),
            "avg_output_tokens": _avg(agent_b_runs, "total_output_tokens"),
            "avg_tool_calls": _avg(agent_b_runs, "tool_calls"),
            "avg_turns": _avg(agent_b_runs, "turns"),
            "success_rate": _success_rate(agent_b_runs),
        },
        "overhead": {
            "time_ms": _avg(agent_a_runs, "total_time_ms")
            - _avg(agent_b_runs, "total_time_ms"),
            "tokens": (
                _avg(agent_a_runs, "total_input_tokens")
                + _avg(agent_a_runs, "total_output_tokens")
            )
            - (
                _avg(agent_b_runs, "total_input_tokens")
                + _avg(agent_b_runs, "total_output_tokens")
            ),
            "success_rate": _success_rate(agent_a_runs) - _success_rate(agent_b_runs),
        },
    }

    results["summary"] = summary

    # Print summary
    print(f"\n{'=' * 60}")
    print("BENCHMARK SUMMARY")
    print(f"{'=' * 60}")
    print(f"Task: {task['instance_id']}")
    print(f"Iterations: {iterations}")
    print()
    print(
        f"{'Metric':<25} {'Agent A (jcodemunch)':>20} {'Agent B (basic)':>20} {'Delta':>15}"
    )
    print("-" * 80)
    for key, label in [
        ("avg_time_ms", "Avg Time (ms)"),
        ("avg_input_tokens", "Avg Input Tokens"),
        ("avg_output_tokens", "Avg Output Tokens"),
        ("avg_total_tokens", "Avg Total Tokens"),
        ("avg_tool_calls", "Avg Tool Calls"),
        ("avg_turns", "Avg Turns"),
        ("success_rate", "Success Rate (%)"),
    ]:
        a_val = summary["agent_a"].get(key, 0)
        b_val = summary["agent_b"].get(key, 0)
        delta = a_val - b_val
        if "rate" in key:
            print(f"{label:<25} {a_val:>20.1f} {b_val:>20.1f} {delta:>+15.1f}")
        else:
            print(f"{label:<25} {a_val:>20,.0f} {b_val:>20,.0f} {delta:>+15,.0f}")

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(results, indent=2, default=str))
        print(f"\nResults written to: {out_path}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--task-id", default="0b01001001__spectree-64", help="SWE-rebench task ID"
    )
    parser.add_argument(
        "--iterations", type=int, default=3, help="Iterations per agent"
    )
    parser.add_argument(
        "--dogfood",
        action="store_true",
        help="Dogfood mode: use already-indexed animaios/animamunchmcp repo",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Override repo ID (e.g. 'animaios/animamunchmcp') instead of task's repo",
    )
    parser.add_argument(
        "--out",
        help="Output JSON file",
    )
    args = parser.parse_args()

    print(f"Agent Benchmark: {args.task_id}")
    print(f"API: {API_ENDPOINT} | Model: {MODEL}")
    print(f"Iterations: {args.iterations} per agent")

    asyncio.run(
        run_agent_benchmark(
            task_id=args.task_id,
            iterations=args.iterations,
            out_path=args.out,
            dogfood=args.dogfood,
            repo_override=args.repo,
        )
    )


if __name__ == "__main__":
    main()
