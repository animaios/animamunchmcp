#!/usr/bin/env python3
"""Real AI Agent Benchmark — Claude CLI with/without jcodemunch MCP.

Runs Claude Code (non-interactive -p mode) against the same coding task:
  - Agent A: Claude Code WITH jcodemunch MCP server + AGENTS.md instructions
  - Agent B: Claude Code WITHOUT jcodemunch MCP (basic tools only)

Both use the same model via the same API. Metrics: tokens, time, cost, turns.

Usage:
    python benchmarks/agent_benchmark/run_claude_benchmark.py \
        --task dogfood \
        --iterations 5 \
        --out benchmarks/agent_benchmark/results_claude.json

Requirements:
    - Fish shell with claude9 function available
    - jcodemunch-mcp binary at /usr/bin/jcodemunch-mcp
    - Internet access for API calls
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parents[1]
MCP_CONFIG = BENCH_DIR / "mcp_jcodemunch.json"

# Fish shell env for claude1 (extracted from ~/.config/fish/functions/claude1.fish) — FRESH KEY
CLAUDE_ENV = {
    "ANTHROPIC_MODEL": "LongCat-2.0-Preview",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "LongCat-2.0-Preview",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "LongCat-2.0-Preview",
    "CLAUDE_CODE_SUBAGENT_MODEL": "LongCat-2.0-Preview",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "API_TIMEOUT_MS": "3000000",
    "SKIP_CLAUDE_API": "1",
    "ENABLE_TOOL_SEARCH": "true",
    "ANTHROPIC_AUTH_TOKEN": os.environ.get(
        "ANTHROPIC_AUTH_TOKEN",
        # Fresh key from claude1 fish function
        "ak_2dL6e18x82ra11J4Y83Su3Kh4Ry2x",
    ),
    "ANTHROPIC_BASE_URL": "https://api.longcat.chat/anthropic",
}

# ---------------------------------------------------------------------------
# jcodemunch AGENTS.md v2 (behavioral guardrails included)
# ---------------------------------------------------------------------------
JCODEMUNCH_AGENTS_MD = r"""## jcodemunch

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
- `assemble_task_context(repo="{{REPO}}", task="...")` — opening move; auto-classifies intent (explore/debug/refactor/extend/audit/review), surfaces symbols + ranked context
- `resolve_repo(path=".")` — confirm repo is indexed; if not: `index_folder(path=".")`
- `search_symbols(repo="{{REPO}}", query="...")` — find by name, signature, summary
  - `mode="context"` — query-less ranked context assembly
  - `mode="winnow"` — multi-axis constraint filter
  - `detail_level="compact"` — 15 tokens/row, great for broad discovery
  - `fusion=true` — Weighted Reciprocal Rank across lexical/structural/similarity/identity
  - `token_budget` — hard cap on returned tokens
- `get_context_bundle(symbol_ids=[...], budget_strategy="core_first", token_budget=6000)` — multi-symbol context in one call
- `get_symbol_source(repo="{{REPO}}", symbol_id="...")` — full source of one symbol
- `search_text(repo="{{REPO}}", query="...")` — full-text search across file contents
- `search_ast(repo="{{REPO}}", pattern="..." | category="...")` — structural anti-pattern scan

### Impact & safety
- `get_blast_radius(symbol="...", include_source=true, call_depth=2, include_decisions=true)` — check impact before changes
- `find_references` / `get_call_hierarchy` — trace who uses a symbol
- `check_safe(repo="{{REPO}}", symbol="...", mode="edit"|"delete")` — composite preflight
- `plan_refactoring(repo="{{REPO}}", symbol="...", refactor_type="rename"|"move"|"extract"|"signature")` — generate multi-file edit plan

### Repository intelligence
- `get_repo_health(repo="{{REPO}}", detailed=true)` — one-call triage
- `get_repo_map(repo="{{REPO}}", group_by="flat", top_n=30)` — signature-level overview ranked by PageRank
- `get_tectonic_map(repo="{{REPO}}")` — logical module topology
- `get_dead_code_v2(repo="{{REPO}}", min_confidence=0.67)` — multi-signal dead code detection
- `find_similar_symbols(repo="{{REPO}}", threshold=0.85)` — consolidation candidates
- `get_symbol_provenance(repo="{{REPO}}", symbol="...")` — git authorship lineage

### Golden Rules
1. **Always start with `assemble_task_context`** — auto-classifies intent and returns ranked context
2. **Batch everything** — use `symbol_ids[]` in `get_context_bundle` instead of serial calls
3. **Prefer `get_context_bundle` over raw file reads** — deduplicates imports, respects token budget
4. **After every edit, call `register_edit`** — keeps index fresh

### Anti-patterns to Avoid
- ❌ Reading full files with `read_file` — use `get_context_bundle` or `get_symbol_source`
- ❌ Using `grep` for symbol lookup — `search_symbols` understands signatures, imports, types
- ❌ Skipping `check_safe` before edits
- ❌ Ignoring `_meta.confidence` < 0.4
- ❌ Re-searching after `verdict: "no_implementation_found"`
"""

# ---------------------------------------------------------------------------
# Blind mode: generic prompt (no tool-specific instructions)
# ---------------------------------------------------------------------------
BLIND_PROMPT = (
    "You are a senior software engineer. "
    "Solve the following task using whichever tools are available to you. "
    "Be thorough and report your findings."
)

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------
DOGFOOD_TASKS = {
    "dead_code_audit": {
        "id": "dead_code_audit",
        "repo": "animaios/animamunchmcp",
        "prompt": (
            "Perform a dead code audit of this repository. "
            "1. Identify all functions and methods that appear to be dead code (not called anywhere). "
            "2. For each, state the file, function name, and line number. "
            "3. Rank them by confidence (definitely dead vs probably dead). "
            "4. Provide a summary count and your methodology. "
            "Do NOT make any code changes — just analyze and report."
        ),
    },
    "refactor_tool_executor": {
        "id": "refactor_tool_executor",
        "repo": "animaios/animamunchmcp",
        "prompt": (
            "Analyze the ToolExecutor class in this codebase. "
            "1. Find the ToolExecutor class and understand its structure. "
            "2. Identify all the tool implementations it contains. "
            "3. Assess its cyclomatic complexity and identify the most complex method. "
            "4. Suggest a refactoring plan to improve maintainability. "
            "5. Check if any of the tool methods are duplicated or could be consolidated. "
            "Do NOT make any code changes — just analyze and report."
        ),
    },
    "find_security_issues": {
        "id": "find_security_issues",
        "repo": "animaios/animamunchmcp",
        "prompt": (
            "Scan this repository for security-related code issues. "
            "1. Search for hardcoded secrets, passwords, API keys, or credentials. "
            "2. Find any use of eval/exec or dangerous function calls. "
            "3. Check for empty catch blocks that silently swallow errors. "
            "4. Identify any deeply nested code that could hide bugs. "
            "5. Report each finding with file, line, and severity. "
            "Do NOT make any code changes — just analyze and report."
        ),
    },
}

SWE_TASKS = {
    "spectree-64": {
        "id": "0b01001001__spectree-64",
        "repo": "0b01001001/spectree",
        "prompt": (
            "[BUG] description for query parameters can not show in swagger ui\n"
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
            "```\n\n"
            "Find the root cause of this bug and explain the fix needed. "
            "The failing test is: tests/test_utils.py::test_parse_params"
        ),
    },
}

ALL_TASKS = {**DOGFOOD_TASKS, **SWE_TASKS}


# ---------------------------------------------------------------------------
# Claude CLI runner
# ---------------------------------------------------------------------------
def run_claude(
    prompt: str,
    workspace: Path,
    with_mcp: bool = True,
    repo: str = "animaios/animamunchmcp",
    max_turns: int = 25,
    timeout_seconds: int = 600,
    blind: bool = False,
) -> dict:
    """Run Claude Code in non-interactive mode and capture metrics.

    Returns:
        dict with keys: success, duration_ms, input_tokens, output_tokens,
        total_tokens, cost_usd, num_turns, result_text, raw_json, error
    """
    env = {**os.environ, **CLAUDE_ENV}

    # Build the command — -p flag must come first, prompt goes LAST as positional arg
    # claude -p [options] --dangerously-skip-permissions "prompt"
    cmd = ["claude", "-p"]

    # Output format for structured metrics
    cmd += ["--output-format", "json"]

    # Add workspace
    cmd += ["--add-dir", str(workspace)]

    # Configure MCP or bare mode
    if with_mcp:
        # Provide jcodemunch MCP server config
        if MCP_CONFIG.exists():
            cmd += ["--mcp-config", str(MCP_CONFIG)]

        # In blind mode: NO AGENTS.md instructions — agent discovers tools on its own
        # In guided mode: append full jcodemunch AGENTS.md v2
        if not blind:
            agents_md = JCODEMUNCH_AGENTS_MD.replace("{{REPO}}", repo)
            agents_md_file = Path(f"/tmp/bench_agents_md_{os.getpid()}.tmp")
            agents_md_file.write_text(agents_md)
            cmd += ["--append-system-prompt-file", str(agents_md_file)]
    else:
        # Bare mode: skip hooks, plugins, LSP, auto-memory, CLAUDE.md discovery
        # This ensures a clean "no MCP" baseline
        cmd += ["--bare"]

    # Skip permissions (we're in a sandbox/benchmark)
    cmd += ["--dangerously-skip-permissions"]

    # Limit max turns
    cmd += ["--max-turns", str(max_turns)]

    # Prompt goes LAST as positional argument
    cmd.append(prompt)

    print(f"    CMD: {' '.join(cmd[:6])}... (prompt: {len(prompt)} chars)", flush=True)

    start = time.perf_counter()
    proc = None
    try:
        # Use Popen + communicate() to avoid pipe buffer deadlocks on long runs
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            env=env,
        )
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        result_code = proc.returncode
        result_stdout = stdout
        result_stderr = stderr
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.communicate()  # drain pipes
        elapsed = (time.perf_counter() - start) * 1000
        # Clean up temp file
        agents_md_file = Path(f"/tmp/bench_agents_md_{os.getpid()}.tmp")
        agents_md_file.unlink(missing_ok=True)
        return {
            "success": False,
            "error": f"Timeout after {timeout_seconds}s",
            "duration_ms": elapsed,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "num_turns": 0,
            "result_text": "",
        }

    elapsed = (time.perf_counter() - start) * 1000

    # Clean up temp file
    agents_md_file = Path(f"/tmp/bench_agents_md_{os.getpid()}.tmp")
    agents_md_file.unlink(missing_ok=True)

    # Parse JSON output
    try:
        data = json.loads(result_stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        return {
            "success": False,
            "error": f"Failed to parse JSON output. stderr: {result_stderr[:500]}",
            "duration_ms": elapsed,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "num_turns": 0,
            "result_text": result_stdout[:1000] if result_stdout else "",
        }

    # Extract metrics from Claude's JSON output
    usage = data.get("usage", {})
    model_usage = data.get("modelUsage", {})
    # Get first model's usage if modelUsage exists
    first_model_usage = next(iter(model_usage.values())) if model_usage else {}

    input_tokens = first_model_usage.get("inputTokens", usage.get("input_tokens", 0))
    output_tokens = first_model_usage.get("outputTokens", usage.get("output_tokens", 0))
    total_tokens = input_tokens + output_tokens
    cost_usd = first_model_usage.get("costUSD", data.get("total_cost_usd", 0.0))
    duration_ms = data.get("duration_ms", elapsed)
    num_turns = data.get("num_turns", 0)
    is_error = data.get("is_error", False)
    stop_reason = data.get("stop_reason", "")
    result_text = data.get("result", "")[:500]  # Truncate for storage

    # Success: task completed normally (not an API error, and either completed or was cut by turns)
    # stop_reason=tool_use means agent wanted more turns but hit the limit — that's OK for benchmarks
    # A true failure is an API-level error (429, auth, etc)
    success = not is_error or stop_reason == "tool_use"

    return {
        "success": success,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "result_text": result_text,
        "error": None
        if not is_error or stop_reason == "tool_use"
        else data.get("api_error_status", "unknown"),
        "stop_reason": stop_reason,
        "session_id": data.get("session_id", ""),
    }


# ---------------------------------------------------------------------------
# Benchmark orchestrator
# ---------------------------------------------------------------------------
def _save_results(results: dict, out_path: str | None):
    """Save results incrementally so we don't lose data on timeout."""
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Compute partial summary
        if results["agent_a_runs"] or results["agent_b_runs"]:
            results["summary"] = compute_summary(results)
        out.write_text(json.dumps(results, indent=2, default=str))


def run_benchmark(
    task_id: str,
    iterations: int = 5,
    out_path: str | None = None,
    timeout_seconds: int = 600,
    max_turns: int = 25,
    blind: bool = False,
) -> dict:
    task = ALL_TASKS.get(task_id)
    if not task:
        print(f"ERROR: Unknown task '{task_id}'. Available: {list(ALL_TASKS.keys())}")
        sys.exit(1)

    repo = task["repo"]
    workspace = REPO_ROOT

    print(f"{'=' * 70}")
    print(f"  CLAUDE CODE AGENT BENCHMARK")
    print(f"{'=' * 70}")
    print(f"  Task:    {task_id}")
    print(f"  Repo:    {repo}")
    print(f"  Iters:   {iterations}")
    print(f"  Timeout: {timeout_seconds}s per run")
    print(f"  MCP cfg: {MCP_CONFIG}")
    print(f"{'=' * 70}")

    results = {
        "benchmark": {
            "task_id": task_id,
            "repo": repo,
            "iterations": iterations,
            "prompt": task["prompt"][:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "agent_a_runs": [],  # With jcodemunch MCP
        "agent_b_runs": [],  # Without MCP (bare)
    }

    # API connectivity test
    print("\n  🔌 Testing API connectivity...", flush=True)
    try:
        test_env = {**os.environ, **CLAUDE_ENV}
        test_cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--bare",
            "--dangerously-skip-permissions",
            "Reply with just: OK",
        ]
        test_proc = subprocess.Popen(
            test_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workspace),
            env=test_env,
        )
        test_stdout, _ = test_proc.communicate(timeout=60)
        test_data = json.loads(test_stdout.strip())
        print(
            f"  ✅ API connected: {test_data.get('duration_ms', 0)}ms, {test_data.get('usage', {}).get('input_tokens', 0)} input tokens",
            flush=True,
        )
    except Exception as e:
        print(f"  ⚠️  API test failed: {e}", flush=True)
    time.sleep(3)

    # Warmup: run a simple MCP call first to prime the server
    warmup_done = False
    if MCP_CONFIG.exists():
        print("\n  🔥 Warming up jcodemunch MCP server...", flush=True)
        warmup_prompt = (
            "List the Python files in this repository. Just give me a count."
        )
        warmup_env = {**os.environ, **CLAUDE_ENV}
        warmup_cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--add-dir",
            str(workspace),
            "--mcp-config",
            str(MCP_CONFIG),
            "--max-turns",
            "3",
            "--dangerously-skip-permissions",
            warmup_prompt,
        ]
        try:
            warmup_proc = subprocess.Popen(
                warmup_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(workspace),
                env=warmup_env,
            )
            warmup_proc.communicate(timeout=180)
            warmup_done = True
            print("  ✅ MCP server warmed up", flush=True)
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"  ⚠️  MCP warmup failed: {e}, proceeding anyway", flush=True)
        time.sleep(5)  # Let server settle

    for i in range(iterations):
        print(f"\n{'─' * 70}")
        print(f"  Iteration {i + 1}/{iterations}")
        print(f"{'─' * 70}")

        # --- Agent A: WITH jcodemunch MCP ---
        agent_a_label = "A (jcodemunch MCP" + (" blind)" if blind else "") + ")"
        print(f"\n  🟢 {agent_a_label} — run {i + 1}", flush=True)
        prompt_a = BLIND_PROMPT + "\n\n" + task["prompt"] if blind else task["prompt"]
        result_a = run_claude(
            prompt=prompt_a,
            workspace=workspace,
            with_mcp=True,
            repo=repo,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            blind=blind,
        )
        result_a["iteration"] = i + 1
        result_a["agent"] = "A (jcodemunch MCP)"
        results["agent_a_runs"].append(result_a)

        # Save incrementally after each run
        _save_results(results, out_path)

        print(
            f"    ✅ success={result_a['success']} | "
            f"time={result_a['duration_ms']:.0f}ms | "
            f"tokens={result_a['total_tokens']:,} | "
            f"cost=${result_a['cost_usd']:.4f} | "
            f"turns={result_a['num_turns']}"
        )

        # Cooldown between runs to avoid rate limiting
        if i < iterations - 1:
            print("    ⏳ Cooldown 30s...", flush=True)
            time.sleep(30)

        # --- Agent B: WITHOUT MCP (bare mode) ---
        agent_b_label = "B (no MCP / bare" + (" blind)" if blind else "") + ")"
        print(f"\n  🔴 {agent_b_label} — run {i + 1}", flush=True)
        prompt_b = BLIND_PROMPT + "\n\n" + task["prompt"] if blind else task["prompt"]
        result_b = run_claude(
            prompt=prompt_b,
            workspace=workspace,
            with_mcp=False,
            repo=repo,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            blind=blind,  # doesn't matter for bare, but pass for consistency
        )
        result_b["iteration"] = i + 1
        result_b["agent"] = "B (no MCP / bare)"
        results["agent_b_runs"].append(result_b)

        # Save incrementally after each run
        _save_results(results, out_path)

        print(
            f"    ✅ success={result_b['success']} | "
            f"time={result_b['duration_ms']:.0f}ms | "
            f"tokens={result_b['total_tokens']:,} | "
            f"cost=${result_b['cost_usd']:.4f} | "
            f"turns={result_b['num_turns']}"
        )

        # Cooldown between iterations
        if i < iterations - 1:
            print("    ⏳ Cooldown 30s...", flush=True)
            time.sleep(30)

    # Compute summary
    summary = compute_summary(results)
    results["summary"] = summary
    print_summary(summary, task_id)

    # Write results
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, default=str))
        print(f"\n📁 Results saved to: {out_path}")

    return results


def compute_summary(results: dict) -> dict:
    def _avg(runs, key):
        vals = [r.get(key, 0) for r in runs if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0

    def _success_rate(runs):
        if not runs:
            return 0
        return sum(1 for r in runs if r.get("success")) / len(runs) * 100

    def _total_cost(runs):
        return sum(r.get("cost_usd", 0) for r in runs)

    a = results["agent_a_runs"]
    b = results["agent_b_runs"]

    summary = {
        "agent_a": {
            "label": "A (jcodemunch MCP)",
            "n_runs": len(a),
            "avg_duration_ms": _avg(a, "duration_ms"),
            "avg_input_tokens": _avg(a, "input_tokens"),
            "avg_output_tokens": _avg(a, "output_tokens"),
            "avg_total_tokens": _avg(a, "total_tokens"),
            "avg_turns": _avg(a, "num_turns"),
            "avg_cost_usd": _avg(a, "cost_usd"),
            "total_cost_usd": _total_cost(a),
            "success_rate": _success_rate(a),
        },
        "agent_b": {
            "label": "B (no MCP / bare)",
            "n_runs": len(b),
            "avg_duration_ms": _avg(b, "duration_ms"),
            "avg_input_tokens": _avg(b, "input_tokens"),
            "avg_output_tokens": _avg(b, "output_tokens"),
            "avg_total_tokens": _avg(b, "total_tokens"),
            "avg_turns": _avg(b, "num_turns"),
            "avg_cost_usd": _avg(b, "cost_usd"),
            "total_cost_usd": _total_cost(b),
            "success_rate": _success_rate(b),
        },
    }

    # Delta
    a_s, b_s = summary["agent_a"], summary["agent_b"]
    summary["delta"] = {
        "avg_duration_ms": a_s["avg_duration_ms"] - b_s["avg_duration_ms"],
        "avg_total_tokens": a_s["avg_total_tokens"] - b_s["avg_total_tokens"],
        "avg_cost_usd": a_s["avg_cost_usd"] - b_s["avg_cost_usd"],
        "success_rate": a_s["success_rate"] - b_s["success_rate"],
        "token_savings_pct": (
            (
                (b_s["avg_total_tokens"] - a_s["avg_total_tokens"])
                / b_s["avg_total_tokens"]
                * 100
            )
            if b_s["avg_total_tokens"] > 0
            else 0
        ),
        "time_overhead_pct": (
            (
                (a_s["avg_duration_ms"] - b_s["avg_duration_ms"])
                / b_s["avg_duration_ms"]
                * 100
            )
            if b_s["avg_duration_ms"] > 0
            else 0
        ),
    }

    return summary


def print_summary(summary: dict, task_id: str):
    a = summary["agent_a"]
    b = summary["agent_b"]
    d = summary["delta"]

    print(f"\n{'=' * 80}")
    print(f"  BENCHMARK RESULTS — {task_id}")
    print(f"{'=' * 80}")
    print()
    print(
        f"  {'Metric':<25} {'Agent A (MCP)':>18} {'Agent B (bare)':>18} {'Delta':>15}"
    )
    print(f"  {'─' * 25} {'─' * 18} {'─' * 18} {'─' * 15}")

    for key, label, fmt in [
        ("avg_duration_ms", "Avg Time (ms)", ",.0f"),
        ("avg_input_tokens", "Avg In Tokens", ",.0f"),
        ("avg_output_tokens", "Avg Out Tokens", ",.0f"),
        ("avg_total_tokens", "Avg Total Tokens", ",.0f"),
        ("avg_turns", "Avg Turns", ".1f"),
        ("avg_cost_usd", "Avg Cost ($)", ".4f"),
        ("success_rate", "Success Rate (%)", ".1f"),
    ]:
        a_val = a.get(key, 0)
        b_val = b.get(key, 0)
        delta = a_val - b_val if "rate" not in key else a_val - b_val
        if "rate" in key:
            print(f"  {label:<25} {a_val:>18.1f} {b_val:>18.1f} {delta:>+15.1f}")
        else:
            print(f"  {label:<25} {a_val:>18{fmt}} {b_val:>18{fmt}} {delta:>+15,.0f}")

    print()
    print(
        f"  {'Token Savings':<25} {'':>18} {'':>18} {d.get('token_savings_pct', 0):>+14.1f}%"
    )
    print(
        f"  {'Time Overhead':<25} {'':>18} {'':>18} {d.get('time_overhead_pct', 0):>+14.1f}%"
    )
    print(f"{'=' * 80}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Real Claude CLI benchmark: jcodemunch MCP vs bare",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--task",
        default="dead_code_audit",
        choices=list(ALL_TASKS.keys()),
        help="Task to run (default: dead_code_audit)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Iterations per agent (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout per run in seconds (default: 600)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=25,
        help="Max turns per run (default: 25)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output JSON file path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running",
    )
    parser.add_argument(
        "--blind",
        action="store_true",
        help="Blind mode: both agents get identical generic prompt (no AGENTS.md for MCP, no special instructions for bare)",
    )

    args = parser.parse_args()

    if args.dry_run:
        task = ALL_TASKS[args.task]
        print("DRY RUN — would execute:")
        print(f"  Task: {args.task}")
        print(f"  Repo: {task['repo']}")
        print(f"  Prompt: {task['prompt'][:100]}...")
        print(f"  Iterations: {args.iterations}")
        print(
            f"  Agent A: claude -p ... --mcp-config {MCP_CONFIG} --append-system-prompt '...'"
        )
        print(f"  Agent B: claude -p ... --bare")
        return

    run_benchmark(
        task_id=args.task,
        iterations=args.iterations,
        out_path=args.out,
        timeout_seconds=args.timeout,
        max_turns=args.max_turns,
        blind=args.blind,
    )


if __name__ == "__main__":
    main()
