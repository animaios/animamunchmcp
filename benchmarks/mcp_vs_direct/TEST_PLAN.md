# Test Plan Matrix — MCP vs Direct API Benchmark (SWE-rebench Agent Scenario)

## Objective

Measure the **protocol overhead of MCP** when an agent uses jcodemunch coding server
tools to solve a real software engineering task drawn from the
[nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) dataset.

The question: **When an agent iteratively calls jcodemunch tools to locate and
understand a bug, how much extra time and tokens does the MCP protocol layer
cost compared to direct Python API calls?**

---

## Dataset

**Source:** `nebius/SWE-rebench` (21k+ issue–PR pairs from 3,400+ Python repos)

Each task provides:
- `instance_id` — unique task ID (e.g. `django__django-12345`)
- `repo` — GitHub owner/name (e.g. `django/django`)
- `problem_statement` — the issue title + body (what the agent reads)
- `base_commit` — the commit the repo is at before the fix
- `FAIL_TO_PASS` — tests that must pass after resolution
- `patch` — the gold fix (for validation, not given to the agent)

**Task selection criteria** for this benchmark:

| Criterion            | Value / Rule                                           |
|----------------------|--------------------------------------------------------|
| Language             | Python only (jcodemunch is Python-focused)             |
| Repo size            | Small–medium (≤ 500 source files) for fast indexing    |
| Difficulty score     | 1–3 (avoids pathological multi-hour exploration)        |
| Instance selection   | Configurable via `--instance-id`; default picks first filtered match |

---

## Test Configurations

| Config ID  | Mode          | Transport            | Description                                          |
|------------|---------------|----------------------|------------------------------------------------------|
| MCP-1..5   | MCP Enabled   | Streamable HTTP      | Full MCP JSON-RPC → HTTP → jcodemunch server         |
| DIR-1..5   | MCP Disabled  | Direct Python API    | In-process function calls, zero protocol overhead     |

---

## Agent Scenario (identical for all 10 runs)

The "agent" is a **deterministic scripted workflow** — not a live LLM — that
exercises the same tool-call sequence a coding agent would use to locate the
bug described in the `problem_statement`. This guarantees bit-exact
reproducibility across all runs.

### Step-by-step tool sequence

| Step | jcodemunch Tool         | Arguments (derived from SWE task)                             |
|------|-------------------------|---------------------------------------------------------------|
| 1    | `resolve_repo`          | `{ "path": "<checked-out-repo>" }`                            |
| 2    | `index_folder`          | `{ "path": "<checked-out-repo>" }`                            |
| 3    | `assemble_task_context` | `{ "repo": "<id>", "task": "<problem_statement>", "intent": "debug" }` |
| 4    | `search_symbols`        | `{ "repo": "<id>", "query": "<keyword-from-problem>", "max_results": 5 }` |
| 5    | `get_symbol_source`     | `{ "repo": "<id>", "symbol_id": "<top-hit-id>" }`           |
| 6    | `search_text`           | `{ "repo": "<id>", "query": "<error-msg-or-class-name>" }`  |
| 7    | `get_file_outline`      | `{ "repo": "<id>", "file_path": "<file-from-search>" }`      |
| 8    | `get_blast_radius`      | `{ "repo": "<id>", "symbol": "<suspect-symbol>" }`           |
| 9    | `get_call_hierarchy`    | `{ "repo": "<id>", "symbol_id": "<suspect-symbol>", "depth": 2 }` |
| 10   | `search_symbols`        | `{ "repo": "<id>", "query": "<secondary-keyword>", "max_results": 3 }` |

> The same 10 tool calls execute in every run.
> Only the transport (MCP vs direct) varies.

---

## Run Matrix

| Run # | Config  | Mode   | Independent Variable | Measured Outputs                              |
|-------|---------|--------|----------------------|-----------------------------------------------|
| 1     | MCP-1   | MCP    | Streamable HTTP      | time_ms, input_tok, output_tok, per-tool breakdown |
| 2     | MCP-2   | MCP    | Streamable HTTP      | time_ms, input_tok, output_tok, per-tool breakdown |
| 3     | MCP-3   | MCP    | Streamable HTTP      | time_ms, input_tok, output_tok, per-tool breakdown |
| 4     | MCP-4   | MCP    | Streamable HTTP      | time_ms, input_tok, output_tok, per-tool breakdown |
| 5     | MCP-5   | MCP    | Streamable HTTP      | time_ms, input_tok, output_tok, per-tool breakdown |
| 6     | DIR-1   | Direct | In-process Python    | time_ms, input_tok, output_tok, per-tool breakdown |
| 7     | DIR-2   | Direct | In-process Python    | time_ms, input_tok, output_tok, per-tool breakdown |
| 8     | DIR-3   | Direct | In-process Python    | time_ms, input_tok, output_tok, per-tool breakdown |
| 9     | DIR-4   | Direct | In-process Python    | time_ms, input_tok, output_tok, per-tool breakdown |
| 10    | DIR-5   | Direct | In-process Python    | time_ms, input_tok, output_tok, per-tool breakdown |

**Total: 10 runs × 10 tool calls = 100 instrumented tool invocations.**

---

## Metrics per Run

| Metric                    | Unit   | MCP Mode Source                   | Direct Mode Source                |
|---------------------------|--------|-----------------------------------|-----------------------------------|
| Total execution time      | ms     | `time.perf_counter` around run    | `time.perf_counter` around run    |
| Total input tokens        | tokens | tiktoken on serialized requests   | tiktoken on serialized args dicts |
| Total output tokens       | tokens | tiktoken on serialized responses  | tiktoken on serialized return dicts |
| Total tokens              | tokens | input + output                    | input + output                    |
| Per-tool latency          | ms     | per-call `perf_counter`           | per-call `perf_counter`           |
| Per-tool input tokens     | tokens | per-call tiktoken                 | per-call tiktoken                  |
| Per-tool output tokens    | tokens | per-call tiktoken                 | per-call tiktoken                  |
| MCP protocol overhead     | ms/tok | computed: MCP_total − Direct_total | —                                 |

---

## Controls & Consistency

| Factor                    | Control                                                                |
|---------------------------|------------------------------------------------------------------------|
| SWE Task                  | Same `instance_id` for all 10 runs                                     |
| Problem statement         | Identical text fed to `assemble_task_context` in every run             |
| Tool call sequence        | Hard-coded; no LLM non-determinism                                     |
| Repository + commit       | Same checkout; pre-indexed once before benchmark starts                 |
| Server process            | MCP: fresh server per run (kill + restart); Direct: fresh Python proc  |
| Result cache              | Cleared between runs (`result_cache_invalidate()`)                      |
| Network                   | Localhost only (`127.0.0.1`)                                           |
| Tokenizer                 | `tiktoken` cl100k_base (consistent with existing benchmark harness)     |
| Python interpreter        | Same version, same venv across all runs                                 |
| Concurrency               | Sequential; no parallel runs                                            |

---

## Summary Calculation

After all 10 runs:

| Aggregation       | MCP Group              | Direct Group           |
|-------------------|------------------------|------------------------|
| Mean time_ms      | avg(MCP-1..5)          | avg(DIR-1..5)          |
| Mean input_tok    | avg(MCP-1..5)          | avg(DIR-1..5)          |
| Mean output_tok   | avg(MCP-1..5)          | avg(DIR-1..5)          |
| Mean total_tok    | avg(MCP-1..5)          | avg(DIR-1..5)          |
| Std dev time_ms   | stdev(MCP-1..5)        | stdev(DIR-1..5)        |
| Std dev total_tok | stdev(MCP-1..5)        | stdev(DIR-1..5)        |
| **Time overhead (abs)** | mean_MCP_time − mean_DIR_time | — |
| **Time overhead (%)**   | (overhead / mean_DIR_time) × 100 | — |
| **Token overhead (abs)** | mean_MCP_tok − mean_DIR_tok | — |
| **Token overhead (%)**  | (token_overhead / mean_DIR_tok) × 100 | — |

---

## Acceptance Criteria Checklist

- [x] Target: jcodemunch coding server
- [x] Task source: SWE-rebench dataset (`nebius/SWE-rebench`)
- [x] Same coding task for all 10 iterations (single `instance_id`)
- [x] 5 runs with MCP (Streamable HTTP protocol)
- [x] 5 runs without MCP (Direct Python API)
- [x] Each run tracks total execution time (ms)
- [x] Each run tracks total token consumption (input + output)
- [x] Raw metrics displayed for every run
- [x] Averages calculated per configuration
- [x] Overhead delta (time & tokens) computed

---

## Reproduce

```bash
# Install dependencies
pip install tiktoken datasets

# Option 1: Dogfood mode (uses already-indexed animaios/animamunchmcp repo)
PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py --dogfood

# Option 2: Override repo (use any pre-indexed repo)
PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py --repo animaios/animamunchmcp

# Option 3: Full SWE-rebench flow (clone + index from HF dataset)
#          Requires HF Hub access and datasets library
PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
    --instance-id django__django-15388

# Custom number of iterations per mode (default: 5)
PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
    --dogfood --iterations 3

# Write JSON + Markdown results to a file
PYTHONPATH=src python benchmarks/mcp_vs_direct/benchmark_mcp_vs_direct.py \
    --dogfood --out benchmarks/mcp_vs_direct/results.json
```

---

## Sample Results (5× MCP vs 5× Direct, dogfood: animaios/animamunchmcp)

| Run | Mode | Time (ms) | Input Tokens | Output Tokens | Total Tokens |
|-----|------|----------:|-------------:|--------------:|-------------:|
| 1 | MCP | 540 | 235 | 2,911 | 3,146 |
| 2 | MCP | 506 | 235 | 2,911 | 3,146 |
| 3 | MCP | 512 | 235 | 2,911 | 3,146 |
| 4 | MCP | 502 | 235 | 2,911 | 3,146 |
| 5 | MCP | 495 | 235 | 2,911 | 3,146 |
| 1 | DIRECT | 600 | 320 | 12,243 | 12,563 |
| 2 | DIRECT | 183 | 320 | 12,253 | 12,573 |
| 3 | DIRECT | 176 | 320 | 12,253 | 12,573 |
| 4 | DIRECT | 178 | 320 | 12,253 | 12,573 |
| 5 | DIRECT | 175 | 320 | 12,253 | 12,573 |

| Metric | MCP (avg) | Direct (avg) | Δ (abs) | Δ (%) |
|--------|------|--------|--------:|------:|
| Mean time (ms) | 510.8 | 262.4 | +248.4 | +94.7% |
| Mean total tokens | 3,146 | 12,571 | −9,425 | −75.0% |

**Key finding:** MCP's MUNCH compact encoding reduces output token consumption by **75%**
while adding ~250ms of protocol overhead (≈95% time increase for 8 tool calls,
or ≈31ms per call). The token savings far outweigh the latency cost for
multi-turn agent workflows.
