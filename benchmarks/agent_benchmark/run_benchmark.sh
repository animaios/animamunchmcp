#!/usr/bin/env bash
# ============================================================================
# Real Claude Agent Benchmark — 5× MCP + 5× Bare
# ============================================================================
# Runs Claude Code in non-interactive mode (-p) against the same task:
#   - 5 runs WITH jcodemunch MCP (Agent A)
#   - 5 runs WITHOUT MCP in --bare mode (Agent B)
#
# Usage:
#   bash benchmarks/agent_benchmark/run_benchmark.sh [--iterations N] [--timeout S]
#
# Requirements:
#   - claude CLI on PATH
#   - API keys set in env vars (see below)
# ============================================================================

set -euo pipefail

# ---- Configuration ----
ITERATIONS=${1:-5}
TIMEOUT=${2:-420}
MAX_TURNS=${3:-12}
TASK="Perform a dead code audit of this repository. Identify all functions and methods that appear to be dead code. For each, state the file, function name, and line number. Rank them by confidence. Do NOT make any code changes — just analyze and report."
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
MCP_CONFIG="$(dirname "$0")/mcp_jcodemunch.json"
RESULTS_DIR="$(dirname "$0")"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_FILE="${RESULTS_DIR}/results_${TIMESTAMP}.json"

# API env vars
export ANTHROPIC_MODEL="LongCat-2.0-Preview"
export ANTHROPIC_DEFAULT_SONNET_MODEL="LongCat-2.0-Preview"
export ANTHROPIC_DEFAULT_OPUS_MODEL="LongCat-2.0-Preview"
export CLAUDE_CODE_SUBAGENT_MODEL="LongCat-2.0-Preview"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="200000"
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="70"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS="32000"
export API_TIMEOUT_MS="3000000"
export SKIP_CLAUDE_API="1"
export ENABLE_TOOL_SEARCH="true"
export ANTHROPIC_AUTH_TOKEN="ak_2Kq6Te7Qw4HI8q384p7Ql2vg3y45H"
export ANTHROPIC_BASE_URL="https://api.longcat.chat/anthropic"

echo "========================================================================"
echo "  CLAUDE CODE AGENT BENCHMARK (shell runner)"
echo "========================================================================"
echo "  Iterations:  $ITERATIONS per agent"
echo "  Timeout:     ${TIMEOUT}s per run"
echo "  Max turns:   $MAX_TURNS"
echo "  MCP config:  $MCP_CONFIG"
echo "  Results:     $RESULTS_FILE"
echo "  Repo:        $REPO_DIR"
echo "========================================================================"

# ---- API connectivity test ----
echo ""
echo "  🔌 Testing API connectivity..."
claude -p --bare --output-format json --dangerously-skip-permissions "Reply: OK" 2>/dev/null | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  ✅ API OK: {d[\"duration_ms\"]}ms')" 2>/dev/null || echo "  ⚠️  API test failed"

# ---- MCP warmup ----
echo ""
echo "  🔥 Warming up MCP server..."
timeout 120 claude -p --mcp-config "$MCP_CONFIG" --max-turns 2 --output-format json --add-dir "$REPO_DIR" --dangerously-skip-permissions "How many Python files are in this repo?" 2>/dev/null | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print(f'  ✅ MCP warmup OK: {d[\"duration_ms\"]}ms, {d[\"num_turns\"]} turns')" 2>/dev/null || echo "  ⚠️  MCP warmup timed out (proceeding anyway)"
sleep 5

# ---- Run benchmarks ----
A_RESULTS=()
B_RESULTS=()

for i in $(seq 1 $ITERATIONS); do
    echo ""
    echo "──────────────────────────────────────────────────────────────────────"
    echo "  Iteration $i/$ITERATIONS"
    echo "──────────────────────────────────────────────────────────────────────"

    # Agent A: WITH jcodemunch MCP
    echo ""
    echo "  🟢 Agent A (MCP) — run $i"
    A_OUT="${RESULTS_DIR}/_tmp_a_${i}.json"
    timeout "$TIMEOUT" claude -p \
        --output-format json \
        --add-dir "$REPO_DIR" \
        --mcp-config "$MCP_CONFIG" \
        --max-turns "$MAX_TURNS" \
        --dangerously-skip-permissions \
        "$TASK" > "$A_OUT" 2>/dev/null
    A_EXIT=$?
    if [ $A_EXIT -eq 0 ] && [ -s "$A_OUT" ]; then
        A_METRICS=$(python3 -c "
import json
d = json.load(open('$A_OUT'))
mu = next(iter(d.get('modelUsage',{}).values()), {})
print(f'success={not d.get(\"is_error\",True) or d.get(\"stop_reason\")==\"tool_use\"} | time={d.get(\"duration_ms\",0)/1000:.0f}s | in={mu.get(\"inputTokens\",0):,} | out={mu.get(\"outputTokens\",0):,} | cost=\${mu.get(\"costUSD\",0):.4f} | turns={d.get(\"num_turns\",0)}')
" 2>/dev/null || echo "parse_error")
        echo "    ✅ $A_METRICS"
        A_RESULTS+=("$A_OUT")
    else
        echo "    ❌ FAILED (exit=$A_EXIT, timeout=$TIMEOUT)"
        echo '{}' > "$A_OUT"
    fi
    rm -f "$A_OUT"

    sleep 15  # Cooldown

    # Agent B: WITHOUT MCP (bare mode)
    echo ""
    echo "  🔴 Agent B (bare) — run $i"
    B_OUT="${RESULTS_DIR}/_tmp_b_${i}.json"
    timeout "$TIMEOUT" claude -p \
        --output-format json \
        --add-dir "$REPO_DIR" \
        --bare \
        --max-turns "$MAX_TURNS" \
        --dangerously-skip-permissions \
        "$TASK" > "$B_OUT" 2>/dev/null
    B_EXIT=$?
    if [ $B_EXIT -eq 0 ] && [ -s "$B_OUT" ]; then
        B_METRICS=$(python3 -c "
import json
d = json.load(open('$B_OUT'))
mu = next(iter(d.get('modelUsage',{}).values()), {})
print(f'success={not d.get(\"is_error\",True) or d.get(\"stop_reason\")==\"tool_use\"} | time={d.get(\"duration_ms\",0)/1000:.0f}s | in={mu.get(\"inputTokens\",0):,} | out={mu.get(\"outputTokens\",0):,} | cost=\${mu.get(\"costUSD\",0):.4f} | turns={d.get(\"num_turns\",0)}')
" 2>/dev/null || echo "parse_error")
        echo "    ✅ $B_METRICS"
        B_RESULTS+=("$B_OUT")
    else
        echo "    ❌ FAILED (exit=$B_EXIT, timeout=$TIMEOUT)"
        echo '{}' > "$B_OUT"
    fi
    rm -f "$B_OUT"

    sleep 15  # Cooldown
done

echo ""
echo "========================================================================"
echo "  BENCHMARK COMPLETE"
echo "========================================================================"
echo "  Note: Full results with individual run data are in the log above."
echo "  Aggregate analysis requires the Python runner."
echo "========================================================================"
