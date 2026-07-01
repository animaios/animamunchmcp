#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-error-pipeline.sh
#
# CI-timeable (<5 s) smoke test for the error-spike-to-issue composite action.
# Exercises the *function-level logic* of
# `.github/actions/error-spike-to-issue/_run.sh` without GitHub-side effects.
#
# Strategy: drive the action with INPUT_* env vars (mirrors the action.yml
# composite env forwarding) against a synthetic log file. `gh` is replaced
# by a fake in PATH that emits canned JSON. Also validates action.yml
# (composite + documented inputs) and workflow YAML
# (cron + dispatch + action ref + SENTRY_DSN read).
#
# Exit code: 0 on success, 1 on any failed step.
# ==============================================================================

set -uo pipefail

PASS=0

ok()  { PASS=$((PASS + 1)); printf '[ok]   %s\n' "$*"; }
bad() { printf '[FAIL] %s\n' "$*"; exit 1; }

cd "$(dirname "$0")/.." || bad "cannot cd to repo root"
REPO_ROOT="$(pwd)"
ACTION="${REPO_ROOT}/.github/actions/error-spike-to-issue/_run.sh"
YML="${REPO_ROOT}/.github/actions/error-spike-to-issue/action.yml"
WF="${REPO_ROOT}/.github/workflows/error-spike.yml"
[ -f "${ACTION}" ] || bad "action runner missing: ${ACTION}"
[ -f "${YML}" ]     || bad "composite action.yml missing: ${YML}"
[ -f "${WF}" ]      || bad "workflow missing: ${WF}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

FAKE_GH_DIR="${TMPDIR}/fakebin"
mkdir -p "${FAKE_GH_DIR}"

# Synthetic-log helper: writes N lines, K with ERROR marker.
make_test_log() {
  local total="$1" error_lines="$2" path="$3"
  local i
  rm -f "${path}"
  for _ in $(seq 1 "${error_lines}"); do
    printf '2026-07-01T00:00:00Z ERROR something failed: foo bar\n' >> "${path}"
  done
  for _ in $(seq 1 $(( total - error_lines ))); do
    printf '2026-07-01T00:00:00Z INFO  nominal line\n' >> "${path}"
  done
}

# Per-step fake-gh builder. The fake emits canned JSON per subcommand and
# logs argv to a calls file. The fake is written in Python to avoid bash
# quoting issues.
build_fake_gh() {
  local path="$1"
  local calls_file="$2"
  rm -f "${calls_file}"
  cat > "${path}" <<FAKESCRIPT
#!/usr/bin/env python3
import sys

calls_file = "${calls_file}"
with open(calls_file, "a") as f:
    for arg in sys.argv[1:]:
        f.write(arg + "\n")

# Dispatch on argv.
args = sys.argv[1:]
if not args:
    sys.exit(0)

if args[:1] == ["auth"]:
    sys.exit(0)

if args[:1] == ["search"]:
    # `gh search issues --repo X --label bug ...`
    print('{"total_count":7}')
    sys.exit(0)

if args[:2] == ["repo", "view"]:
    print("smoke/test-repo")
    sys.exit(0)

if args[:1] == ["api"]:
    # `gh api -X GET search/issues ...` or `gh api repos/X/issues ...`
    for arg in args:
        if arg == "search/issues":
            print('{"total_count":7}')
            sys.exit(0)
    print('[]')
    sys.exit(0)

if args[:1] == ["issue"]:
    if len(args) >= 2 and args[1] == "list":
        # Empty stdout = "no pre-existing spike issue" → proceed to create
        pass
    elif len(args) >= 2 and args[1] == "create":
        print("https://example.test/created/1")
    elif len(args) >= 2 and args[1] == "comment":
        pass
    sys.exit(0)

# gh search issues (without explicit api)
if args[0] == "search" and len(args) > 1 and args[1] == "issues":
    print('{"total_count":7}')
    sys.exit(0)

sys.exit(0)
FAKESCRIPT
  chmod +x "${path}"
}

# Helper to run the action in a sandbox with a fresh env.
run_action_with_fake_gh() {
  local fake_calls="$1"
  shift
  # Extra KEY=VALUE pairs for env -i follow
  env -i \
    PATH="${FAKE_GH_DIR}:/usr/bin:/bin:/usr/local/bin" \
    TMPDIR="${TMPDIR}" \
    HOME="${TMPDIR}" \
    "$@" \
    python3 -c "
import subprocess, sys, os
env = os.environ.copy()
# Strip any shell-function pollution
for k in list(env.keys()):
    if k.startswith('BASH_FUNC_'):
        del env[k]
result = subprocess.run(['bash', '-u', '${ACTION}'], env=env, capture_output=True, text=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
"
}

# ---------------------------------------------------------------------------
# Step 1 — SENTRY_DSN unset → short-circuit, exits 0.
# ---------------------------------------------------------------------------
calls_1="${TMPDIR}/calls_1.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_1}"
out="$(run_action_with_fake_gh "${calls_1}")"
rc=$?
if [ "${rc}" -eq 0 ] && printf '%s' "${out}" | grep -q "SENTRY_DSN is unset"; then
  ok "step 1: action short-circuits with SENTRY_DSN unset, exits 0"
else
  bad "step 1: expected clean short-circuit (rc=0 + SENTRY_DSN log); got rc=${rc}: ${out}"
fi

# ---------------------------------------------------------------------------
# Step 2 — Sentry configured + log probe below threshold → no issue.
# ---------------------------------------------------------------------------
make_test_log 20 5 "${TMPDIR}/under.log"
calls_2="${TMPDIR}/calls_2.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_2}"
out="$(run_action_with_fake_gh "${calls_2}" \
    GH_TOKEN="stub-token-for-test" \
    SENTRY_DSN="*************************************" \
    INPUT_LOG_PATH="${TMPDIR}/under.log" \
    INPUT_WINDOW_MINUTES="60" \
    INPUT_THRESHOLD="50" \
    INPUT_REPO="smoke/test-repo")"
rc=$?
if [ "${rc}" -eq 0 ] && printf '%s' "${out}" | grep -q "Under threshold"; then
  ok "step 2: log probe stays under threshold (rc=0)"
else
  bad "step 2: expected clean under-threshold path; rc=${rc}: ${out}"
fi

# ---------------------------------------------------------------------------
# Step 3 — Over threshold → issue-create path fires.
# ---------------------------------------------------------------------------
make_test_log 500 75 "${TMPDIR}/over.log"
calls_3="${TMPDIR}/calls_3.txt"
build_fake_gh "${FAKE_GH_DIR}/gh" "${calls_3}"
out="$(run_action_with_fake_gh "${calls_3}" \
    GH_TOKEN="stub-token-for-test" \
    SENTRY_DSN="*************************************" \
    INPUT_LOG_PATH="${TMPDIR}/over.log" \
    INPUT_WINDOW_MINUTES="60" \
    INPUT_THRESHOLD="50" \
    INPUT_REPO="smoke/test-repo")"
rc=$?
if [ "${rc}" -eq 0 ] \
   && printf '%s' "${out}" | grep -q "Over threshold" \
   && [ -f "${calls_3}" ] \
   && grep -q "issue" "${calls_3}" \
   && grep -q "create" "${calls_3}"; then
  ok "step 3: over-threshold path opens `gh issue create` (rc=0)"
else
  bad "step 3: expected issue-create path; rc=${rc}; out=${out}; gh_calls=$(cat "${calls_3}" 2>/dev/null || echo '<none>')"
fi

# ---------------------------------------------------------------------------
# Step 4 — Composite action.yml parses as valid YAML with documented inputs.
# ---------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PYERR="$(python3 -c "
import yaml,sys
d=yaml.safe_load(open(sys.argv[1]))
assert isinstance(d.get('inputs'), dict), 'inputs not a dict'
assert isinstance(d.get('runs'), dict), 'runs not a dict'
assert d['runs'].get('using') == 'composite', 'not a composite action'
for k in ['window-minutes','threshold','labels','repo','token','sentry-dsn','log-path']:
    assert k in d['inputs'], f'missing input {k}'
print('OK')
" "${YML}" 2>&1)"
  if [ "${PYERR}" = "OK" ]; then
    ok "step 4: composite action.yml parses as valid YAML with documented inputs"
  else
    bad "step 4: action.yml is not valid or missing inputs (${PYERR})"
  fi
else
  if grep -qE '^\s*window-minutes:' "${YML}" \
     && grep -qE '^\s*threshold:' "${YML}" \
     && grep -qE '^\s*labels:' "${YML}" \
     && grep -qE '^\s*repo:' "${YML}" \
     && grep -qE '^\s*token:' "${YML}" \
     && grep -qE '^\s*sentry-dsn:' "${YML}" \
     && grep -qE '^\s*log-path:' "${YML}" \
     && grep -q 'using: composite' "${YML}"; then
    ok "step 4: action.yml structurally valid (PyYAML unavailable; pattern-checked)"
  else
    bad "step 4: action.yml missing required inputs or not a composite action"
  fi
fi

# ---------------------------------------------------------------------------
# Step 5 — Workflow file parses + wires the action with cron + dispatch.
# ---------------------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  WFERR="$(python3 -c "
import yaml,sys
d=yaml.safe_load(open(sys.argv[1]))
assert 'on' in d, 'missing on:'
assert 'schedule' in d['on'], 'missing schedule'
cron=d['on']['schedule']
assert isinstance(cron, list) and any('*/30 * * * *' in str(s) for s in cron), 'missing */30 cron'
assert 'workflow_dispatch' in d['on'], 'missing workflow_dispatch'
txt=open(sys.argv[1]).read()
assert 'error-spike-to-issue' in txt, 'does not reference action'
assert 'SENTRY_DSN' in txt, 'does not read SENTRY_DSN'
print('OK')
" "${WF}" 2>&1)"
  if [ "${WFERR}" = "OK" ]; then
    ok "step 5: error-spike.yml is valid YAML with cron, workflow_dispatch, and action ref"
  else
    bad "step 5: error-spike.yml is invalid or missing required keys (${WFERR})"
  fi
else
  if grep -q 'cron:' "${WF}" \
     && grep -q 'workflow_dispatch:' "${WF}" \
     && grep -q 'error-spike-to-issue' "${WF}" \
     && grep -q 'SENTRY_DSN' "${WF}"; then
    ok "step 5: error-spike.yml structurally valid (PyYAML unavailable; pattern-checked)"
  else
    bad "step 5: error-spike.yml missing cron/workflow_dispatch/action ref"
  fi
fi

# ---------------------------------------------------------------------------
# Step 6 — OBSERVABILITY docs mention the pipeline.
# ---------------------------------------------------------------------------
if grep -q 'error-spike.yml' /home/vi/animamunchmcp/docs/OBSERVABILITY.md; then
  ok "step 6: docs/OBSERVABILITY.md references error-spike.yml"
else
  bad "step 6: OBSERVABILITY.md does not reference error-spike.yml"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\n[smoke-error-pipeline] %d steps passed\n' "${PASS}"
exit 0
