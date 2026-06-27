#!/usr/bin/env python3
"""MCP vs Direct API Benchmark (SWE-rebench Agent Scenario).

Measures token consumption and execution time when an agent uses jcodemunch
tools to solve a real SWE task — 5 runs via MCP (Streamable HTTP) and 5 runs
via direct Python function calls.

Dataset: nebius/SWE-rebench  (https://huggingface.co/datasets/nebius/SWE-rebench)
Paper:   SWE-rebench: An Automated Pipeline for Task Collection and
         Decontaminated Evaluation of Software Engineering Agents
         (arXiv:2505.20411)

Usage:
    # Default: auto-select a small Python task from SWE-rebench
    PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py

    # Specific instance
    PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
        --instance-id django__django-15388

    # Custom iterations per mode (default: 5)
    PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
        --iterations 3

    # Write JSON + Markdown results
    PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
        --out benchmarks/mcp_vs_direct/results.json

Methodology
-----------
1. Load a task from SWE-rebench  (problem_statement + repo + base_commit).
2. Clone and index the repo with jcodemunch.
3. Define a deterministic 10-step agent tool-call sequence that simulates
   how a coding agent would explore the codebase to understand the bug.
4. Run the sequence 5× via MCP (streamable-http) and 5× via direct Python API.
5. For each run, measure:
   - Total wall-clock time (ms)
   - Input tokens  (tiktoken cl100k_base on serialized args/request)
   - Output tokens (tiktoken cl100k_base on serialized response)
6. Report raw per-run metrics + group averages + overhead deltas.

Controls
--------
- Exact same tool-call sequence in every run (no LLM non-determinism)
- Result cache cleared between runs
- Fresh server process (MCP) or fresh interpreter state (Direct) per run
- Same indexed repo, same checkout, same tokenizer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import statistics
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
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))

except ImportError:
    sys.exit("tiktoken not found — run: pip install tiktoken")

try:
    import importlib.metadata

    # Monkey-patch torch version detection (Python 3.14 compat fix for datasets)
    _orig_version = importlib.metadata.version

    def _patched_version(dist):
        if dist == "torch":
            return "2.12.0"
        return _orig_version(dist)

    importlib.metadata.version = _patched_version

except Exception:
    pass

try:
    from datasets import load_dataset as _load_dataset

    _HAS_DATASETS = True
except ImportError:
    _load_dataset = None
    _HAS_DATASETS = False

# ---------------------------------------------------------------------------
# Direct API imports (used by the Direct-mode runner)
# ---------------------------------------------------------------------------
from jcodemunch_mcp.storage.sqlite_store import SQLiteIndexStore as IndexStore
from jcodemunch_mcp.storage.token_tracker import result_cache_invalidate
from jcodemunch_mcp.tools.assemble_task_context import (
    assemble_task_context as _assemble_task_context,
)
from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius as _get_blast_radius
from jcodemunch_mcp.tools.get_call_hierarchy import (
    get_call_hierarchy as _get_call_hierarchy,
)
from jcodemunch_mcp.tools.get_file_outline import get_file_outline as _get_file_outline
from jcodemunch_mcp.tools.get_symbol import get_symbol_source as _get_symbol_source
from jcodemunch_mcp.tools.index_folder import index_folder as _index_folder
from jcodemunch_mcp.tools.search_symbols import search_symbols as _search_symbols
from jcodemunch_mcp.tools.search_text import search_text as _search_text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ITERATIONS = 5
MCP_HOST = "127.0.0.1"
MCP_PORT = 8124
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# The deterministic agent tool-call sequence.
# Each tuple: (tool_name, args_template).
# Templates use {repo}, {problem_statement}, {query_keyword}, {file_path},
# {symbol_id} — resolved at runtime after earlier steps produce results.
AGENT_STEPS = [
    # Step 1: resolve repo  (done once outside the loop, not measured)
    # Step 2: index repo    (done once outside the loop, not measured)
    # Step 3: assemble task context from the problem statement
    (
        "assemble_task_context",
        {"repo": "{repo}", "task": "{problem_statement}", "intent": "debug"},
    ),
    # Step 4: broad symbol search derived from the problem
    (
        "search_symbols",
        {"repo": "{repo}", "query": "{query_keyword}", "max_results": 5},
    ),
    # Step 5: fetch source of the top search hit
    ("get_symbol_source", {"repo": "{repo}", "symbol_id": "{top_symbol_id}"}),
    # Step 6: text search for the error/class mention
    ("search_text", {"repo": "{repo}", "query": "{text_query}", "context_lines": 2}),
    # Step 7: outline the file containing the hit
    ("get_file_outline", {"repo": "{repo}", "file_path": "{hit_file_path}"}),
    # Step 8: blast radius on the suspect symbol
    ("get_blast_radius", {"repo": "{repo}", "symbol": "{suspect_symbol}"}),
    # Step 9: call hierarchy on the suspect
    (
        "get_call_hierarchy",
        {"repo": "{repo}", "symbol_id": "{suspect_symbol_id}", "depth": 2},
    ),
    # Step 10: secondary narrower search
    (
        "search_symbols",
        {"repo": "{repo}", "query": "{secondary_keyword}", "max_results": 3},
    ),
]


# ---------------------------------------------------------------------------
# SWE-rebench task loader
# ---------------------------------------------------------------------------
def load_swe_task(instance_id: str | None = None) -> dict:
    """Load a single SWE-rebench task from HuggingFace.

    If instance_id is None, auto-select the first small Python repo task.
    Falls back to a built-in sample task if HF download fails.
    """
    # Built-in fallback task (matches SWE-rebench schema)
    # This is a real task from the dataset: spectree issue about swagger UI
    FALLBACK_TASK = {
        "instance_id": "0b01001001__spectree-64",
        "repo": "0b01001001/spectree",
        "base_commit": "a091fab020ac26548250c907bae0855273a98778",
        "problem_statement": "[BUG]description for query paramters can not show in swagger ui Hi, when I add a description for a schema used in query, it can not show in swagger ui but can show in Redoc",
        "version": "0.3",
        "meta": {
            "commit_name": "head_commit",
            "failed_lite_validators": [
                "has_hyperlinks",
                "has_media",
                "has_many_modified_files",
                "has_many_hunks",
            ],
            "has_test_patch": True,
            "is_lite": False,
            "llm_score": {
                "difficulty_score": 1,
                "issue_text_score": 2,
                "test_score": 0,
            },
            "num_modified_files": 1,
        },
        "install_config": {
            "env_vars": None,
            "env_yml_path": None,
            "install": "pip install -e .[flask,falcon,starlette]",
            "log_parser": "parse_log_pytest",
            "no_use_env": None,
            "packages": "requirements.txt",
            "pip_packages": ["pytest"],
            "pre_install": None,
            "python": "3.9",
            "reqs_path": ["requirements.txt"],
        },
        "requirements": "annotated-types==0.7.0 anyio==4.9.0 blinker==1.9.0 certifi==2025.1.31 charset-normalizer==3.4.1 click==8.1.8 exceptiongroup==1.2.2 falcon==4.0.2 Flask==3.1.0 idna==3.10 importlib_metadata==8.6.1 iniconfig==2.1.0 itsdangerous==2.2.0 Jinja2==3.1.6 MarkupSafe==3.0.2 packaging==24.2 pluggy==1.5.0 pydantic==2.11.1 pydantic_core==2.27.2 pytest==8.3.5 PyYAML==6.0.2 typing_extensions==4.12.2 werkzeug==3.0.6",
        "environment": "name: spectree channels: - defaults - https://repo.anaconda.com/pkgs/main - https://repo.anaconda.com/pkgs/r - conda-forge dependencies: - _libgcc_mutex=0.1=main - _openmp_mutex=5.1=1_gnu - ca-certificates=2025.2.25=h06a4308_0 - ld_impl_linux-64=2.40=h12ee557_0 - libffi=3.4.4=h6a678d5_1 - libgcc-ng=13.0.0=h815a8c3_2 - libstdcxx-ng=13.0.0=h10f8b9a_2 - ncurses=6.5=h36e2e75_1 - openssl=3.3.1=h2a9a68b_0 - pip=24.2=py39h6a678d5_0 - python=3.9.21=h3c1b88f_2 - readline=8.2=h36e2e75_1 - setuptools=75.6.0=py39h6a678d5_0 - sqlite=3.46.1=h6a678d5_0 - tk=8.6.14=h6a678d5_0 - wheel=0.44.0=py39h6a678d5_0 - xz=5.6.2=h6a678d5_0 - zlib=1.3.1=h3e1a82e_1 - pip: - annotated-types==0.7.0 - anyio==4.9.0 - blinker==1.9.0 - certifi==2025.1.31 - charset-normalizer==3.4.1 - click==8.1.8 - exceptiongroup==1.2.2 - falcon==4.0.2 - Flask==3.1.0 - idna==3.10 - importlib_metadata==8.6.1 - iniconfig==2.1.0 - itsdangerous==2.2.0 - Jinja2==3.1.6 - MarkupSafe==3.0.2 - packaging==24.2 - pluggy==1.5.0 - pydantic==2.11.1 - pydantic_core==2.27.2 - pytest==8.3.5 - PyYAML==6.0.2 - typing_extensions==4.12.2 - werkzeug==3.0.6",
        "FAIL_TO_PASS": ["tests/test_utils.py::test_parse_params"],
        "FAIL_TO_FAIL": [],
        "PASS_TO_PASS": [
            "tests/test_utils.py::test_comments",
            "tests/test_utils.py::test_parse_code",
            "tests/test_utils.py::test_parse_name",
            "tests/test_utils.py::test_has_model",
            "tests/test_utils.py::test_parse_resp",
            "tests/test_utils.py::test_parse_request",
        ],
        "PASS_TO_FAIL": [],
        "license_name": "Apache License 2.0",
        "docker_image": "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64",
        "image_name": "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64",
    }

    if not _HAS_DATASETS or _load_dataset is None:
        print("[swe] datasets library not available — using fallback task", flush=True)
        return FALLBACK_TASK

    try:
        print("[swe] loading nebius/SWE-rebench dataset from HF Hub …", flush=True)
        ds = _load_dataset("nebius/SWE-rebench", split="test")

        if instance_id:
            rows = [r for r in ds if r["instance_id"] == instance_id]
            if not rows:
                avail = [r["instance_id"] for r in ds][:20]
                sys.exit(
                    f"instance_id '{instance_id}' not found.\n"
                    f"First 20 available: {avail}"
                )
            return rows[0]

        # Auto-select: Python repo, low difficulty
        for row in ds:
            repo = row.get("repo", "")
            meta = row.get("meta", {})
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            llm_score = meta.get("llm_score", {})
            difficulty = llm_score.get("difficulty_score", 99)
            if difficulty <= 2 and repo:
                return row

        # Fallback: first row
        print("[swe] warning: no ideal match, using first row", flush=True)
        return ds[0]

    except Exception as e:
        print(f"[swe] failed to load from HF Hub: {e}", flush=True)
        print("[swe] using built-in fallback task", flush=True)
        return FALLBACK_TASK


# ---------------------------------------------------------------------------
# Repo clone + index
# ---------------------------------------------------------------------------
def clone_and_index(task: dict, work_dir: Path) -> tuple[str, Path]:
    """Clone the SWE repo at base_commit and index it with jcodemunch.

    Returns (repo_id, checkout_path).
    """
    repo_slug = task["repo"]  # e.g. "django/django"
    base_commit = task["base_commit"]
    checkout = work_dir / repo_slug.replace("/", "__")

    if not checkout.exists():
        print(f"[clone] cloning {repo_slug} @ {base_commit[:8]} …", flush=True)
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo_slug}.git", str(checkout)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", base_commit],
            cwd=str(checkout),
            check=True,
            capture_output=True,
        )
    else:
        print(f"[clone] reusing existing checkout {checkout}", flush=True)

    # Index with jcodemunch
    storage_path = str(work_dir / ".jcm_index")
    print(f"[index] indexing {checkout} → {storage_path} …", flush=True)
    t0 = time.perf_counter()
    result = _index_folder(
        path=str(checkout),
        use_ai_summaries=False,
        storage_path=storage_path,
    )
    elapsed = time.perf_counter() - t0
    repo_id = result.get("repo", repo_slug)
    n_syms = result.get("symbols", result.get("total_symbols", "?"))
    print(
        f"[index] {n_syms} symbols in {elapsed:.1f}s  (repo_id={repo_id})", flush=True
    )

    return repo_id, checkout


# ---------------------------------------------------------------------------
# Token counting helpers
# ---------------------------------------------------------------------------
def _tok_count(obj: Any) -> int:
    """Return tiktoken count of a JSON-serialized object."""
    return count_tokens(json.dumps(obj, separators=(",", ":"), default=str))


def _extract_content_text(result: Any) -> str:
    """Extract text from either a direct-API dict or MCP CallToolResult."""
    if isinstance(result, dict):
        return json.dumps(result, default=str)
    # MCP CallToolResult or list[TextContent]
    if isinstance(result, list):
        parts = []
        for item in result:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif isinstance(item, dict):
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    if hasattr(result, "content"):
        return _extract_content_text(result.content)
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Direct-mode runner
# ---------------------------------------------------------------------------
class DirectRunner:
    """Execute the agent tool-call sequence via in-process Python API calls."""

    def __init__(self, repo_id: str, task: dict):
        self.repo_id = repo_id
        self.task = task
        self._step_data: dict = {}  # accumulates results for template resolution

    def _resolve_args(self, tool_name: str, args_template: dict) -> dict:
        """Fill in {placeholder} values from previous step results."""
        resolved = {}
        for k, v in args_template.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                key = v[1:-1]
                resolved[k] = self._step_data.get(key, "")
            else:
                resolved[k] = v

        # Ensure repo is set
        if "repo" in args_template:
            resolved["repo"] = self.repo_id

        return resolved

    def _extract_agent_keys(self, tool_name: str, args: dict, result: Any):
        """Extract values from results to fill subsequent template placeholders."""
        if tool_name == "assemble_task_context":
            # Extract a query keyword from the task context
            self._step_data["problem_statement"] = self.task.get(
                "problem_statement", ""
            )
            # Pick first meaningful word from the problem statement as query
            words = self.task.get("problem_statement", "").split()
            filtered = [w for w in words[:30] if len(w) > 4 and w.isalpha()]
            self._step_data["query_keyword"] = (
                " ".join(filtered[:3]) if filtered else "error"
            )
            self._step_data["text_query"] = filtered[0] if filtered else "error"
            self._step_data["secondary_keyword"] = (
                " ".join(filtered[:2]) if len(filtered) >= 2 else "fix"
            )

        elif tool_name == "search_symbols":
            results = result.get("results", []) if isinstance(result, dict) else []
            if results:
                first = results[0]
                self._step_data["top_symbol_id"] = first.get(
                    "id", first.get("symbol_id", "")
                )
                self._step_data["suspect_symbol"] = first.get("name", "")
                self._step_data["suspect_symbol_id"] = first.get(
                    "id", first.get("symbol_id", "")
                )
                fp = first.get("file", "")
                self._step_data["hit_file_path"] = fp

        elif tool_name == "search_text":
            if isinstance(result, dict):
                matches = result.get("results", result.get("matches", []))
                if matches and isinstance(matches, list) and matches:
                    first = matches[0]
                    if isinstance(first, dict):
                        self._step_data.setdefault(
                            "hit_file_path",
                            first.get("file", first.get("file_path", "")),
                        )

    async def run_once(self) -> dict:
        """Execute one full agent sequence; return per-run metrics."""
        result_cache_invalidate(repo=None)  # clear all caches
        self._step_data = {
            "repo": self.repo_id,
            "problem_statement": self.task.get("problem_statement", ""),
        }

        total_input_tok = 0
        total_output_tok = 0
        total_time_ms = 0.0
        per_tool: list[dict] = []

        for tool_name, args_template in AGENT_STEPS:
            args = self._resolve_args(tool_name, args_template)
            in_tok = _tok_count({"tool": tool_name, "arguments": args})

            t0 = time.perf_counter()
            try:
                result = await self._call_direct(tool_name, args)
                ok = True
            except Exception as e:
                result = {"error": str(e)}
                ok = False
            elapsed_ms = (time.perf_counter() - t0) * 1000

            out_tok = (
                _tok_count(result)
                if isinstance(result, dict)
                else count_tokens(_extract_content_text(result))
            )

            total_input_tok += in_tok
            total_output_tok += out_tok
            total_time_ms += elapsed_ms
            per_tool.append(
                {
                    "tool": tool_name,
                    "time_ms": round(elapsed_ms, 1),
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "ok": ok,
                }
            )

            if ok:
                self._extract_agent_keys(tool_name, args, result)

        return {
            "mode": "direct",
            "total_time_ms": round(total_time_ms, 1),
            "total_input_tokens": total_input_tok,
            "total_output_tokens": total_output_tok,
            "total_tokens": total_input_tok + total_output_tok,
            "tools": per_tool,
        }

    async def _call_direct(self, tool_name: str, args: dict) -> Any:
        """Dispatch to the appropriate direct Python function."""
        dispatch = {
            "assemble_task_context": _assemble_task_context,
            "search_symbols": _search_symbols,
            "get_symbol_source": _get_symbol_source,
            "search_text": _search_text,
            "get_file_outline": _get_file_outline,
            "get_blast_radius": _get_blast_radius,
            "get_call_hierarchy": _get_call_hierarchy,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return {"error": f"Unknown tool: {tool_name}"}

        result = fn(**args)
        # Allow both sync and async implementations
        if asyncio.iscoroutine(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# MCP-mode runner
# ---------------------------------------------------------------------------
class MCPRunner:
    """Execute the same agent tool-call sequence via MCP Streamable HTTP."""

    def __init__(self, repo_id: str, task: dict, server_url: str = MCP_URL):
        self.repo_id = repo_id
        self.task = task
        self.server_url = server_url
        self._step_data: dict = {}

    def _resolve_args(self, tool_name: str, args_template: dict) -> dict:
        resolved = {}
        for k, v in args_template.items():
            if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
                key = v[1:-1]
                resolved[k] = self._step_data.get(key, "")
            else:
                resolved[k] = v
        if "repo" in args_template:
            resolved["repo"] = self.repo_id
        return resolved

    def _extract_agent_keys(self, tool_name: str, args: dict, result: Any):
        """Mirror DirectRunner key extraction but parse MCP text results."""
        text = _extract_content_text(result)
        if tool_name == "assemble_task_context":
            self._step_data["problem_statement"] = self.task.get(
                "problem_statement", ""
            )
            words = self.task.get("problem_statement", "").split()
            filtered = [w for w in words[:30] if len(w) > 4 and w.isalpha()]
            self._step_data["query_keyword"] = (
                " ".join(filtered[:3]) if filtered else "error"
            )
            self._step_data["text_query"] = filtered[0] if filtered else "error"
            self._step_data["secondary_keyword"] = (
                " ".join(filtered[:2]) if len(filtered) >= 2 else "fix"
            )

        elif tool_name == "search_symbols":
            try:
                data = (
                    json.loads(text)
                    if text.startswith("{") or text.startswith("[")
                    else {}
                )
                results = data.get("results", [])
                if results:
                    first = results[0]
                    self._step_data["top_symbol_id"] = first.get(
                        "id", first.get("symbol_id", "")
                    )
                    self._step_data["suspect_symbol"] = first.get("name", "")
                    self._step_data["suspect_symbol_id"] = first.get(
                        "id", first.get("symbol_id", "")
                    )
                    self._step_data.setdefault("hit_file_path", first.get("file", ""))
            except Exception:
                pass

        elif tool_name == "search_text":
            try:
                data = (
                    json.loads(text)
                    if text.startswith("{") or text.startswith("[")
                    else {}
                )
                matches = data.get("results", data.get("matches", []))
                if matches and isinstance(matches, list) and matches:
                    first = matches[0]
                    if isinstance(first, dict):
                        self._step_data.setdefault(
                            "hit_file_path",
                            first.get("file", first.get("file_path", "")),
                        )
            except Exception:
                pass

    async def run_once(self) -> dict:
        """Execute one full agent sequence via MCP; return per-run metrics."""
        result_cache_invalidate(repo=None)
        self._step_data = {"repo": self.repo_id}

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        total_input_tok = 0
        total_output_tok = 0
        total_time_ms = 0.0
        per_tool: list[dict] = []

        # Connect to MCP server
        async with streamablehttp_client(self.server_url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                for tool_name, args_template in AGENT_STEPS:
                    args = self._resolve_args(tool_name, args_template)
                    in_tok = _tok_count({"tool": tool_name, "arguments": args})

                    t0 = time.perf_counter()
                    try:
                        result = await session.call_tool(tool_name, args)
                        ok = True
                    except Exception as e:
                        result = [
                            type("Err", (), {"text": json.dumps({"error": str(e)})})
                        ]
                        ok = False
                    elapsed_ms = (time.perf_counter() - t0) * 1000

                    out_text = _extract_content_text(result)
                    out_tok = count_tokens(out_text)

                    total_input_tok += in_tok
                    total_output_tok += out_tok
                    total_time_ms += elapsed_ms
                    per_tool.append(
                        {
                            "tool": tool_name,
                            "time_ms": round(elapsed_ms, 1),
                            "input_tokens": in_tok,
                            "output_tokens": out_tok,
                            "ok": ok,
                        }
                    )

                    if ok:
                        self._extract_agent_keys(tool_name, args, result)

        return {
            "mode": "mcp",
            "total_time_ms": round(total_time_ms, 1),
            "total_input_tokens": total_input_tok,
            "total_output_tokens": total_output_tok,
            "total_tokens": total_input_tok + total_output_tok,
            "tools": per_tool,
        }


# ---------------------------------------------------------------------------
# MCP server lifecycle
# ---------------------------------------------------------------------------
class MCPServer:
    """Manage the jcodemunch MCP server as a subprocess (streamable-http)."""

    def __init__(self, port: int = MCP_PORT, storage_path: str | None = None):
        self.port = port
        self.storage_path = storage_path
        self._proc: subprocess.Popen | None = None

    def start(self):
        env = os.environ.copy()
        if self.storage_path:
            env["CODE_INDEX_PATH"] = self.storage_path
        # Suppress telemetry for benchmark runs
        env["JCODEMUNCH_TELEMETRY_ENABLED"] = "0"

        cmd = [
            sys.executable,
            "-m",
            "jcodemunch_mcp",
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(self.port),
        ]
        self._proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait for server to be ready
        import socket

        for attempt in range(60):  # up to 30s
            # First check: is the process still alive?
            if self._proc.poll() is not None:
                self.stop()
                raise RuntimeError(
                    f"MCP server exited with code {self._proc.returncode}"
                )
            # Second check: can we connect to the port?
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex((MCP_HOST, self.port))
                sock.close()
                if result == 0:
                    print(f"[mcp] server ready on port {self.port}", flush=True)
                    # Give the server a moment to finish init
                    time.sleep(1)
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("MCP server failed to start within 30s")

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


# ---------------------------------------------------------------------------
# Benchmark orchestrator
# ---------------------------------------------------------------------------
async def run_benchmark(
    instance_id: str | None = None,
    iterations: int = ITERATIONS,
    out_path: str | None = None,
    dogfood: bool = False,
    repo_override: str | None = None,
) -> dict:
    """Full benchmark: SWE-rebench task → index → 5× MCP + 5× Direct."""

    # 1. Load SWE task
    task = load_swe_task(instance_id)
    print(f"[swe] instance_id = {task['instance_id']}")
    print(f"[swe] repo        = {task['repo']}")
    print(f"[swe] commit      = {task['base_commit'][:8]}")
    ps = task.get("problem_statement", "")
    print(f"[swe] problem     = {ps[:120]}…")

    # 2. Resolve repo_id and storage_path
    if repo_override:
        repo_id = repo_override
        storage_path = os.environ.get("CODE_INDEX_PATH")
        work_dir = None
        print(f"[repo] using override: {repo_id} (storage={storage_path})")
    elif dogfood:
        repo_id = "animaios/animamunchmcp"
        storage_path = os.environ.get("CODE_INDEX_PATH")
        work_dir = None
        print(f"[repo] dogfood mode: using {repo_id} (storage={storage_path})")
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="jcm_bench_"))
        repo_id, checkout = clone_and_index(task, work_dir)
        storage_path = str(work_dir / ".jcm_index")

    # 3. Prepare results container
    all_runs: list[dict] = []

    # ---------------------------------------------------------------
    # Phase A: MCP mode (5 iterations)
    # ---------------------------------------------------------------
    print(f"\n{'=' * 60}", flush=True)
    print(f"Phase A: MCP mode — {iterations} iterations", flush=True)
    print(f"{'=' * 60}", flush=True)

    for i in range(iterations):
        print(f"\n  MCP run {i + 1}/{iterations} …", end=" ", flush=True)
        server = MCPServer(port=MCP_PORT, storage_path=storage_path)
        try:
            server.start()
            runner = MCPRunner(repo_id, task)
            result = await runner.run_once()
            result["run"] = i + 1
            all_runs.append(result)
            print(
                f"done  time={result['total_time_ms']:.0f}ms  "
                f"tokens={result['total_tokens']:,}  "
                f"(in={result['total_input_tokens']:,} out={result['total_output_tokens']:,})",
                flush=True,
            )
        finally:
            server.stop()

    # ---------------------------------------------------------------
    # Phase B: Direct API mode (5 iterations)
    # ---------------------------------------------------------------
    print(f"\n{'=' * 60}", flush=True)
    print(f"Phase B: Direct API mode — {iterations} iterations", flush=True)
    print(f"{'=' * 60}", flush=True)

    for i in range(iterations):
        print(f"\n  Direct run {i + 1}/{iterations} …", end=" ", flush=True)
        runner = DirectRunner(repo_id, task)
        result = await runner.run_once()
        result["run"] = i + 1
        all_runs.append(result)
        print(
            f"done  time={result['total_time_ms']:.0f}ms  "
            f"tokens={result['total_tokens']:,}  "
            f"(in={result['total_input_tokens']:,} out={result['total_output_tokens']:,})",
            flush=True,
        )

    # ---------------------------------------------------------------
    # Compute summaries
    # ---------------------------------------------------------------
    mcp_runs = [r for r in all_runs if r["mode"] == "mcp"]
    dir_runs = [r for r in all_runs if r["mode"] == "direct"]

    def _agg(runs: list[dict]) -> dict:
        n = len(runs)
        return {
            "iterations": n,
            "mean_time_ms": round(statistics.mean(r["total_time_ms"] for r in runs), 1),
            "stdev_time_ms": round(
                statistics.stdev(r["total_time_ms"] for r in runs), 1
            )
            if n > 1
            else 0.0,
            "mean_total_tokens": round(
                statistics.mean(r["total_tokens"] for r in runs), 1
            ),
            "stdev_total_tokens": round(
                statistics.stdev(r["total_tokens"] for r in runs), 1
            )
            if n > 1
            else 0.0,
            "mean_input_tokens": round(
                statistics.mean(r["total_input_tokens"] for r in runs), 1
            ),
            "mean_output_tokens": round(
                statistics.mean(r["total_output_tokens"] for r in runs), 1
            ),
        }

    mcp_summary = _agg(mcp_runs)
    dir_summary = _agg(dir_runs)

    overhead_time_abs = mcp_summary["mean_time_ms"] - dir_summary["mean_time_ms"]
    overhead_time_pct = (
        (overhead_time_abs / dir_summary["mean_time_ms"] * 100)
        if dir_summary["mean_time_ms"]
        else 0
    )
    overhead_tok_abs = (
        mcp_summary["mean_total_tokens"] - dir_summary["mean_total_tokens"]
    )
    overhead_tok_pct = (
        (overhead_tok_abs / dir_summary["mean_total_tokens"] * 100)
        if dir_summary["mean_total_tokens"]
        else 0
    )

    final = {
        "benchmark": "mcp_vs_direct",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "swe_task": {
            "instance_id": task["instance_id"],
            "repo": task["repo"],
            "base_commit": task["base_commit"],
            "problem_statement_preview": ps[:200],
        },
        "agent_steps": len(AGENT_STEPS),
        "mcp_runs": mcp_runs,
        "direct_runs": dir_runs,
        "mcp_summary": mcp_summary,
        "direct_summary": dir_summary,
        "overhead": {
            "time_ms_abs": round(overhead_time_abs, 1),
            "time_pct": round(overhead_time_pct, 1),
            "tokens_abs": round(overhead_tok_abs, 1),
            "tokens_pct": round(overhead_tok_pct, 1),
        },
    }

    # ---------------------------------------------------------------
    # Render output
    # ---------------------------------------------------------------
    print(render_markdown(final))

    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(final, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"\nJSON results written to: {p}")

    # Cleanup
    # shutil.rmtree(work_dir, ignore_errors=True)  # keep for inspection

    return final


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------
def render_markdown(data: dict) -> str:
    lines = []
    lines.append("# MCP vs Direct API Benchmark Results")
    lines.append("")
    lines.append("## SWE-rebench Task")
    lines.append("")
    swe = data["swe_task"]
    lines.append(f"| Field | Value |")
    lines.append(f"|-------|-------|")
    lines.append(f"| Instance ID | `{swe['instance_id']}` |")
    lines.append(f"| Repo | `{swe['repo']}` |")
    lines.append(f"| Base commit | `{swe['base_commit'][:12]}` |")
    lines.append(f"| Problem | {swe['problem_statement_preview'][:100]}… |")
    lines.append(f"| Agent steps | {data['agent_steps']} |")
    lines.append("")

    # Raw results table
    lines.append("## Raw Run Results")
    lines.append("")
    lines.append(
        "| Run | Mode | Time (ms) | Input Tokens | Output Tokens | Total Tokens |"
    )
    lines.append(
        "|-----|------|----------:|-------------:|--------------:|-------------:|"
    )
    for r in data["mcp_runs"] + data["direct_runs"]:
        lines.append(
            f"| {r['run']} | {r['mode'].upper()} | "
            f"{r['total_time_ms']:.0f} | "
            f"{r['total_input_tokens']:,} | "
            f"{r['total_output_tokens']:,} | "
            f"{r['total_tokens']:,} |"
        )
    lines.append("")

    # Aggregate table
    lines.append("## Averages")
    lines.append("")
    lines.append("| Metric | MCP | Direct | Δ (abs) | Δ (%) |")
    lines.append("|--------|------|--------|--------:|------:|")
    m = data["mcp_summary"]
    d = data["direct_summary"]
    o = data["overhead"]
    lines.append(
        f"| Mean time (ms) | {m['mean_time_ms']:.1f} | {d['mean_time_ms']:.1f} | {o['time_ms_abs']:+.1f} | {o['time_pct']:+.1f}% |"
    )
    lines.append(
        f"| Std dev time (ms) | {m['stdev_time_ms']:.1f} | {d['stdev_time_ms']:.1f} | — | — |"
    )
    lines.append(
        f"| Mean input tokens | {m['mean_input_tokens']:.0f} | {d['mean_input_tokens']:.0f} | — | — |"
    )
    lines.append(
        f"| Mean output tokens | {m['mean_output_tokens']:.0f} | {d['mean_output_tokens']:.0f} | — | — |"
    )
    lines.append(
        f"| Mean total tokens | {m['mean_total_tokens']:.0f} | {d['mean_total_tokens']:.0f} | {o['tokens_abs']:+.0f} | {o['tokens_pct']:+.1f}% |"
    )
    lines.append(
        f"| Std dev tokens | {m['stdev_total_tokens']:.1f} | {d['stdev_total_tokens']:.1f} | — | — |"
    )
    lines.append("")

    # Per-tool breakdown (averaged across MCP runs)
    if data["mcp_runs"]:
        lines.append("## Per-Tool Latency (MCP avg)")
        lines.append("")
        lines.append("| Tool | Avg time (ms) | Avg output tokens |")
        lines.append("|------|-------------:|------------------:|")
        n_mcp = len(data["mcp_runs"])
        step_names = [t["tool"] for t in data["mcp_runs"][0].get("tools", [])]
        for idx, name in enumerate(step_names):
            avg_t = sum(r["tools"][idx]["time_ms"] for r in data["mcp_runs"]) / n_mcp
            avg_o = (
                sum(r["tools"][idx]["output_tokens"] for r in data["mcp_runs"]) / n_mcp
            )
            lines.append(f"| `{name}` | {avg_t:.1f} | {avg_o:.0f} |")
        lines.append("")

        lines.append("## Per-Tool Latency (Direct avg)")
        lines.append("")
        lines.append("| Tool | Avg time (ms) | Avg output tokens |")
        lines.append("|------|-------------:|------------------:|")
        n_dir = len(data["direct_runs"])
        step_names = [t["tool"] for t in data["direct_runs"][0].get("tools", [])]
        for idx, name in enumerate(step_names):
            avg_t = sum(r["tools"][idx]["time_ms"] for r in data["direct_runs"]) / n_dir
            avg_o = (
                sum(r["tools"][idx]["output_tokens"] for r in data["direct_runs"])
                / n_dir
            )
            lines.append(f"| `{name}` | {avg_t:.1f} | {avg_o:.0f} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"> Tokenizer: tiktoken cl100k_base  ")
    lines.append(f"> Dataset: nebius/SWE-rebench  ")
    lines.append(f"> MCP transport: streamable-http ({MCP_HOST}:{MCP_PORT})  ")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--instance-id",
        default=None,
        help="SWE-rebench instance_id (default: auto-select)",
    )
    parser.add_argument(
        "--dogfood",
        action="store_true",
        help="Dogfood mode: skip clone, use already-indexed animaios/animamunchmcp repo",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Override repo ID (e.g. 'animaios/animamunchmcp') instead of cloning from SWE task",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=ITERATIONS,
        help=f"Number of iterations per mode (default: {ITERATIONS})",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Write JSON results to FILE",
    )
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(
            instance_id=args.instance_id,
            iterations=args.iterations,
            out_path=args.out,
            dogfood=args.dogfood,
            repo_override=args.repo,
        )
    )


if __name__ == "__main__":
    main()
