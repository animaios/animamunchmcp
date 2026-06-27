# Test Plan — Real AI Agent Benchmark (SWE-rebench)

## Objective

Run **real LLM agents** against the same SWE-rebench task to measure the impact of jcodemunch toolset on:
- Task success rate
- Token consumption (input + output)
- Execution time
- Tool call efficiency

Two agent configurations:
- **Agent A**: Full jcodemunch toolset + v2 AGENTS.md instructions (behavioral guardrails)
- **Agent B**: Basic tools only (read_file, grep, list_directory, run_command) — NO jcodemunch

Both use the **same LLM** (model="auto" via http://localhost:3001/v1) and receive the **same task prompt**.

---

## Dataset

**Source**: nebius/SWE-rebench (built-in fallback task: `0b01001001__spectree-64`)

**Task**: Fix Swagger UI bug — query parameter descriptions not showing in Swagger but visible in Redoc

**Repo**: `0b01001001/spectree` @ `a091fab0`

**Failing test**: `tests/test_utils.py::test_parse_params`

**Problem statement excerpt**:
> "[BUG] description for query paramters can not show in swagger ui Hi, when I add a description for a schema used in query, it can not show in swagger ui but can show in Redoc"

---

## Test Configurations

| Config | Agent | Tools | Instructions |
|--------|-------|-------|--------------|
| JCM-1..N | Agent A | 12 jcodemunch tools + 4 basic tools | Full AGENTS.md v2 (with behavioral guardrails) |
| BAS-1..N | Agent B | 4 basic tools only | No jcodemunch, manual exploration only |

**Default iterations**: 3 per agent (configurable via `--iterations`)

---

## Agent Prompt (identical for both)

> You are a senior software engineer tasked with fixing a bug in a codebase.
>
> ## Repository
> - Repo: `{repo}`
> - Commit: `{commit}`
> - Language: Python
>
> ## Problem Statement
> {problem_statement}
>
> ## Failing Test(s)
> {fail_to_pass}
>
> ## Your Goal
> 1. Explore the codebase to understand the bug
> 2. Locate the root cause
> 3. Identify the exact file(s) and function(s) that need to be fixed
> 4. Provide a clear explanation of the fix needed
>
> ## Available Tools
> {tools_description}

---

## Run Matrix

| Run # | Config | Agent | Tools | Measured Outputs |
|-------|--------|-------|-------|------------------|
| 1 | JCM-1 | A | Full jcodemunch | success, time_ms, input_tok, output_tok, tool_calls, turns |
| 2 | JCM-2 | A | Full jcodemunch | success, time_ms, input_tok, output_tok, tool_calls, turns |
| 3 | JCM-3 | A | Full jcodemunch | success, time_ms, input_tok, output_tok, tool_calls, turns |
| 4 | BAS-1 | B | Basic only | success, time_ms, input_tok, output_tok, tool_calls, turns |
| 5 | BAS-2 | B | | success, time_ms, input_tok, output_tok, tool_calls, turns |
| 6 | BAS-3 | B | | success, time_ms, input_tok, output_tok, tool_calls, turns |

---

## Metrics per Run

| Metric | Source |
|--------|--------|
| Success (boolean) | Heuristic: final response contains "root cause", "fix", "issue is", "problem is", "need to change", "should be" |
| Total time (ms) | `time.perf_counter` around API calls |
| Input tokens | API `usage.prompt_tokens` |
| Output tokens | API `usage.completion_tokens` |
| Tool calls | Count of tool invocations |
| Turns | Count of LLM response cycles |

---

## Controls & Consistency

| Factor | Control |
|--------|---------|
| Task | Same SWE-rebench instance_id for all runs |
| LLM | Same model ("auto" via same API endpoint) |
| Prompt | Identical task prompt (only tools_description differs) |
| Temperature | 0.1 (deterministic) |
| Max turns | 20 |
| Tool timeout | 30s grep, 60s run_command |
| Workspace | Same directory for both agents |
| Concurrency | Sequential (one agent at a time) |

---

## Summary Calculation

After all runs:

| Aggregation | Agent A (jcodemunch) | Agent B (basic) |
|-------------|---------------------|-----------------|
| Success rate | avg success | avg success |
| Mean time (ms) | avg | avg |
| Mean total tokens | avg (in + out) | avg (in + out) |
| Mean tool calls | avg | avg |
| Mean turns | avg | avg |

| Delta (A − B) | Value |
|---------------|-------|
| Time overhead (ms) | mean_A_time − mean_B_time |
| Token overhead | mean_A_tokens − mean_B_tokens |
| Success rate delta | mean_A_success − mean_B_success |

---

## Acceptance Criteria

- [x] Same SWE-rebench task for all runs
- [x] Same LLM model ("auto") for both agents
- [x] Same task prompt (only tools differ)
- [x] N iterations per agent (configurable)
- [x] Each run tracks: success, time, tokens, tool calls
- [x] Averages calculated per agent
- [x] Delta computed (A vs B)

---

## Reproduce

```bash
# Prerequisites
# 1. API endpoint running at http://localhost:3001/v1
# 2. Config in ~/.animamunch/config.json with api_key
# 3. Model "auto" available

# Quick dogfood test (uses already-indexed animaios/animamunchmcp)
PYTHONPATH=src python benchmarks/agent_benchmark/agent_benchmark.py \
    --dogfood --iterations 2

# Specific SWE-rebench task (full clone + index)
PYTHONPATH=src python benchmarks/agent_benchmark/agent_benchmark.py \
    --task-id 0b01001001__spectree-64 \
    --iterations 3 \
    --out benchmarks/agent_benchmark/results.json

# Override repo (any pre-indexed repo)
PYTHONPATH=src python benchmarks/agent_benchmark/agent_benchmark.py \
    --repo animaios/animamunchmcp \
    --iterations 3
```

---

## Expected Outcomes (Hypothesis)

| Metric | Expected Direction | Rationale |
|--------|-------------------|-----------|
| **Success rate** | Agent A > Agent B | jcodemunch provides structured exploration vs. manual grep |
| **Total tokens** | Agent A < Agent B | Compact encoding + targeted retrieval vs. reading full files |
| **Time** | Agent A < Agent B (after 1st turn) | Fewer turns needed with smart tools |
| **Tool calls** | Agent A > Agent B | More tool calls but each more informative |
| **Turns** | Agent A < Agent B | Faster convergence to root cause |

---

## AGENTS.md v2 Behavioral Guardrails (Agent A only)

Critical policies baked into Agent A's instructions:

1. **Code Exploration Policy** — "Never fall back to Read, Grep, Glob, or Bash for code exploration"
2. **Session-Aware Routing** — Confidence tiers (high/medium/low) + negative evidence handling
3. **Response Envelope Reading** — Interpret `_meta.confidence`, `_meta.freshness`, `repo_is_stale`
4. **Model-Driven Tool Tiering** — `model="auto"` on `assemble_task_context`
5. **resolve_repo First** — Confirm indexed before exploring
6. **register_edit After Edits** — Keep index fresh

---

## Sample Output Format

```
============================================================
Iteration 1/3
============================================================

  Agent A (jcodemunch) running...
    done: success=True, time=12450ms, tokens=45,230, tool_calls=8

  Agent B (basic) running...
    done: success=False, time=31200ms, tokens=89,102, tool_calls=15

============================================================
BENCHMARK SUMMARY
============================================================
Task: 0b01001001__spectree-64
Iterations: 3

        Metric          Agent A (jcodemunch)       Agent B (basic)          Delta
        ------------    -------------------       -------------------       ------
         Avg Time (ms)                 12,345                  28,450        -16,105
       Avg Input Tokens                 8,234                  15,432         -7,198
      Avg Output Tokens                 7,123                  14,890         -7,767
      Avg Total Tokens                 15,357                  30,322        -14,965
      Avg Tool Calls                      8                      12           -4
         Avg Turns                        5                       9           -4
      Success Rate (%)                 100.0                    33.3        +66.7
```

---

## Prerequisites

1. **API Server**: Running at `http://localhost:3001/v1` with model "auto"
2. **Config File**: `~/.animamunch/config.json` with:
   ```json
   {
     "api_endpoint": "http://localhost:3001/v1",
     "api_key": "your-key-here",
     "model": "auto"
   }
   ```
3. **Dependencies**: `pip install httpx tiktoken`
4. **jcodemunch indexed repo** (for dogfood mode): `animaios/animamunchmcp` already indexed

---

## Configuration Options

| Flag | Description | Default |
|------|-------------|---------|
| `--task-id` | SWE-rebench instance ID | `0b01001001__spectree-64` |
| `--iterations` | Runs per agent | `3` |
| `--out` | Output JSON file | none |
| `--dogfood` | Use indexed animaios/animamunchmcp | false |
| `--repo` | Override repo ID | task's repo |