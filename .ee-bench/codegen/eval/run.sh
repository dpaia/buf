#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${EE_BENCH_PROJECT_ROOT:-/app}"
EVAL_DIR="/ee-bench/eval"
SUBMISSION_DIR="/ee-bench/submission"
export ARTIFACTS_DIR="/tmp/test-results"
mkdir -p "$ARTIFACTS_DIR"

# --- Write expected test lists early (consumed by the run selector below and
# by the emitter at the end). Kept in a file to avoid shell quoting issues. ---
cat > /tmp/_expected.json << 'EXPECTED_EOF'
{"fail_to_pass": {{ instance.expected.fail_to_pass | tojson }}, "pass_to_pass": {{ instance.expected.pass_to_pass | tojson }}, "fail_to_fail": {{ instance.expected.fail_to_fail | default([]) | tojson }}, "fail_to_fail_strict": {{ instance.expected.fail_to_fail_strict | default(true) | tojson }}}
EXPECTED_EOF

# --- Compute the `go test` run set from the expected lists -------------------
# Emits one "<import-path>\t<run-regexp>" line per package into _run_groups.tsv
# so only the listed tests run (scoped by -run + package) instead of ./....
# Inlined (not a separate script) so run.sh is self-contained and portable.
# Test names are "<import-path>.<TestName>"; the final dot is the boundary since
# a Go test identifier never contains a dot. Top-level names for a package are
# batched into one "^(TestA|TestB)$" alternation; a name that already carries a
# subtest path ("TestA/sub") is anchored per slash element.
python3 - << 'EE_SELECT_EOF' > /tmp/_run_groups.tsv
import collections, json, re, sys
try:
    data = json.load(open("/tmp/_expected.json"))
except Exception:
    data = {}
names = []
for key in ("fail_to_pass", "pass_to_pass"):
    names.extend(data.get(key, []) or [])
top = collections.defaultdict(set)
explicit = []
for name in names:
    if not name or name == "*":
        continue
    pkg, sep, test = name.rpartition(".")
    if not sep or not pkg or not test:
        continue
    if "/" in test:
        explicit.append((pkg, test))
    else:
        top[pkg].add(test)
out = []
for pkg in sorted(top):
    alt = "|".join(re.escape(t) for t in sorted(top[pkg]))
    out.append(pkg + "\t^(" + alt + ")$")
for pkg, test in explicit:
    anchored = "/".join("^" + re.escape(e) + "$" for e in test.split("/"))
    out.append(pkg + "\t" + anchored)
sys.stdout.write("".join(line + "\n" for line in out))
EE_SELECT_EOF

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
OVERALL_START=$SECONDS

_elapsed() { echo $(( SECONDS - ${1:-$OVERALL_START} )); }

# --- _run_tests: run tests with isolated ARTIFACTS_DIR ---
# Usage: _run_tests <label>
# Writes: /tmp/<label>_stdout.log, /tmp/<label>_stderr.log, /tmp/<label>_parser.json
_run_tests() {
  local label="$1"
  local orig_artifacts="$ARTIFACTS_DIR"
  local exit_code=0
  export ARTIFACTS_DIR="$orig_artifacts/$label"
  mkdir -p "$ARTIFACTS_DIR"
  : > "/tmp/${label}_stdout.log"
  : > "/tmp/${label}_stderr.log"

  set +e
  # Run only the tests named in fail_to_pass + pass_to_pass, scoped to the
  # package(s) they live in, instead of the whole repo (./...). Each line of
  # _run_groups.tsv (built near the top of this script) is one package; each
  # becomes a separate gotestsum invocation writing its own JUnit XML into
  # ARTIFACTS_DIR. Reading from a file (not a process substitution) keeps this
  # portable across shells.
  local idx=0
  while IFS=$'\t' read -r importpath runregex; do
    [ -z "$importpath" ] && continue
    idx=$((idx + 1))
    gotestsum --junitfile "$ARTIFACTS_DIR/results_${idx}.xml" --format standard-quiet \
      -- -run "$runregex" "$importpath" \
      >> "/tmp/${label}_stdout.log" 2>> "/tmp/${label}_stderr.log"
    local rc=$?
    if [ "$rc" -ne 0 ]; then exit_code=$rc; fi
  done < /tmp/_run_groups.tsv
  # NOTE: do not `set -e` here — errexit is global, not function-scoped, so it
  # would leak into the caller's `set +e` block and abort the script when a run
  # returns a non-zero test exit code (the expected fail_to_pass case). The
  # caller restores errexit after capturing the return value.

  # The parser aggregates every results_*.xml file found in ARTIFACTS_DIR.
  python3 "$EVAL_DIR/scripts/ee_bench_parser_junit.py" "$ARTIFACTS_DIR" > "/tmp/${label}_parser.json" 2>/dev/null || echo '{}' > "/tmp/${label}_parser.json"

  export ARTIFACTS_DIR="$orig_artifacts"
  return "$exit_code"
}

cd "$PROJECT_ROOT"

# --- Reset to base commit (only if EE_BENCH_RESET is set) ---
if [ -n "${EE_BENCH_RESET:-}" ]; then
  git reset --hard "{{ instance.base_commit }}" 2>/dev/null
  git clean -fdx 2>/dev/null
fi

# ============================================================
# Criterion: compilation (clean base, before test_patch)
# ============================================================
COMPILE_START=$SECONDS
COMPILE_STATUS="pass"
go build ./... > /tmp/compile_stdout.log 2> /tmp/compile_stderr.log || {
  COMPILE_STATUS="fail"
}
COMPILE_DURATION=$(_elapsed $COMPILE_START)

# ============================================================
# Apply test patch after clean-base compilation and before baseline.
# This lets fail_to_pass prove the test fails without the solution.
# ============================================================
HAS_TEST_PATCH="false"
if [ -f "$EVAL_DIR/test_patch.diff" ]; then
  git apply -v "$EVAL_DIR/test_patch.diff" 2>/dev/null || true
  HAS_TEST_PATCH="true"
fi

# ============================================================
# Run baseline tests against base+test_patch, tolerating failures.
# This records fail_to_pass tests as failing before the gold patch.
# ============================================================
BASELINE_DURATION=0
BASELINE_TEST_EXIT_CODE=0
if [ "$COMPILE_STATUS" = "pass" ]; then
  BASELINE_START=$SECONDS
  set +e
  _run_tests baseline
  BASELINE_TEST_EXIT_CODE=$?
  set -e
  BASELINE_DURATION=$(_elapsed $BASELINE_START)
fi

# ============================================================
# Criterion: patch_applied (submission patch)
# ============================================================
PATCH_START=$SECONDS
PATCH_STATUS="pass"
PATCH_OUTPUT=""
if [ -f "$SUBMISSION_DIR/patch.diff" ]; then
  PATCH_OUTPUT=$(git apply -v "$SUBMISSION_DIR/patch.diff" 2>&1) || {
    PATCH_STATUS="fail"
    echo "WARN: git apply failed for submission patch" >&2
  }
else
  PATCH_STATUS="skipped"
fi
PATCH_DURATION=$(_elapsed $PATCH_START)

# ============================================================
# Rebuild after submission patch
# ============================================================
REBUILD_STATUS="skipped"
if [ "$PATCH_STATUS" = "pass" ]; then
  go build ./... > /tmp/rebuild_stdout.log 2> /tmp/rebuild_stderr.log || {
    REBUILD_STATUS="fail"
  }
  if [ "$REBUILD_STATUS" != "fail" ]; then
    REBUILD_STATUS="pass"
    COMPILE_STATUS="pass"
  fi
fi

# ============================================================
# Run eval tests (only if rebuild/compilation OK and patch not failed)
# ============================================================
TEST_DURATION=0
EVAL_TEST_EXIT_CODE=0
if [ "$REBUILD_STATUS" = "pass" ] || ([ "$COMPILE_STATUS" = "pass" ] && [ "$PATCH_STATUS" != "fail" ]); then
  TEST_START=$SECONDS
  set +e
  _run_tests eval
  EVAL_TEST_EXIT_CODE=$?
  set -e
  TEST_DURATION=$(_elapsed $TEST_START)
fi

OVERALL_DURATION=$(_elapsed $OVERALL_START)

# --- Write temp files for safe passing to Python emitter ---
echo "$PATCH_OUTPUT" > /tmp/_patch_output.txt
cat /tmp/compile_stdout.log /tmp/compile_stderr.log > /tmp/_compile_output.txt 2>/dev/null || true

# ============================================================
# Emit EE-bench JSON v2.0 (7 criteria)
# ============================================================
export PATCH_STATUS PATCH_DURATION COMPILE_STATUS COMPILE_DURATION
export TEST_DURATION BASELINE_DURATION OVERALL_DURATION TIMESTAMP
export HAS_TEST_PATCH BASELINE_TEST_EXIT_CODE EVAL_TEST_EXIT_CODE

python3 "$EVAL_DIR/scripts/ee_bench_eval.py"
