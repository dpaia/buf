#!/usr/bin/env python3
"""Emit EE-bench JSON v2.0 evaluation output (7 criteria).

Language-independent emitter. Reads criteria status from environment
variables (set by run.sh) and parser JSON files from /tmp.
Prints the result JSON to stdout.

Environment variables consumed:
    COMPILE_STATUS, COMPILE_DURATION, PATCH_STATUS, PATCH_DURATION,
    TEST_DURATION, BASELINE_DURATION, OVERALL_DURATION, TIMESTAMP,
    HAS_TEST_PATCH, BASELINE_TEST_EXIT_CODE, EVAL_TEST_EXIT_CODE

Temp files consumed:
    /tmp/_compile_output.txt, /tmp/_patch_output.txt, /tmp/_expected.json,
    /tmp/baseline_parser.json, /tmp/eval_parser.json,
    /tmp/eval_stdout.log, /tmp/eval_stderr.log
"""
import json
import os
import re

MAX_OUTPUT = 8192


def read_file(path, limit=MAX_OUTPUT):
    try:
        with open(path) as f:
            return f.read(limit)
    except FileNotFoundError:
        return ""


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def int_env(name, default=0):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _legacy_prefix(name):
    """Strip parameterized suffix: 'Foo.Bar(x: 1)' -> 'Foo.Bar'."""
    return re.sub(r"\(.*\)$", "", name)


def _legacy_has_parameters(name):
    return _legacy_prefix(name) != name


def _decode_json_unicode_escapes(name):
    """Decode literal JSON-style unicode escapes in test names.

    Some result formats contain the text ``\\ud83c\\udf0d`` instead of the
    actual emoji character. Decode only ``\\uXXXX`` escapes so other
    backslash-heavy parameter values, such as ``\\x1b`` or ``\\n``, stay
    unchanged.
    """
    if "\\u" not in name:
        return name

    def replace_escape(match):
        return chr(int(match.group(1), 16))

    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", replace_escape, name)
    return decoded.encode("utf-16", "surrogatepass").decode("utf-16")


def _normalize_legacy_name(name):
    """Normalize raw legacy test names for compatibility matching."""
    name = _decode_json_unicode_escapes(name)
    colon_index = name.find(":")
    first_dot_index = name.find(".")
    if colon_index >= 0 and (first_dot_index < 0 or colon_index < first_dot_index):
        # Gradle module paths are colon-separated (for example
        # "microservices:review-service:FQN"); JUnit reports only the FQN.
        name = name.rsplit(":", 1)[-1]
    name = name.replace("\\", "/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.endswith(".java"):
        name = name[:-5]
    return name.replace("#", ".").replace("+", ".")


def _legacy_class_key(name):
    normalized = _legacy_prefix(_normalize_legacy_name(name))
    parts = normalized.split(".")
    for index, part in enumerate(parts):
        if re.search(r"(Test|Tests|IT)$", part):
            return ".".join(parts[: index + 1])
    return normalized


def _legacy_simple_class_key(name):
    return _legacy_class_key(name).split(".")[-1]


def legacy_matches(expected_name, actual_names):
    """Return true if a legacy expected name matches any raw actual name."""
    name = _normalize_legacy_name(expected_name)
    normalized_set = {_normalize_legacy_name(n) for n in actual_names}
    if name in actual_names:
        return True
    if name in normalized_set:
        return True

    pname = _legacy_prefix(name)
    if not _legacy_has_parameters(name) and pname in {
        _legacy_prefix(n) for n in normalized_set
    }:
        return True
    if _legacy_has_parameters(name) and pname in {
        n for n in normalized_set if not _legacy_has_parameters(n)
    }:
        return True

    if _legacy_has_parameters(name):
        return False

    # Method-level legacy names may omit the package:
    # 'FooTest.testA' should match 'pkg.FooTest.testA'.
    if any(n.endswith("." + name) for n in normalized_set):
        return True

    class_key = _legacy_class_key(name)
    is_class_level_expected = _legacy_prefix(name) == class_key
    if not is_class_level_expected:
        return False

    class_prefix = class_key + "."
    if any(n.startswith(class_prefix) for n in normalized_set):
        return True

    return any(
        _legacy_simple_class_key(n) == _legacy_simple_class_key(name)
        for n in normalized_set
    )


def _as_string(value):
    return "" if value is None else str(value)


def _entry_name(entry):
    if isinstance(entry, dict):
        if entry.get("name") or entry.get("canonical_name"):
            return _as_string(entry.get("name") or entry.get("canonical_name"))
        match_keys = entry.get("match_keys", [])
        if isinstance(match_keys, list) and match_keys:
            return _as_string(match_keys[0])
        if isinstance(match_keys, str):
            return match_keys
    return _as_string(entry)


def _entry_id(entry):
    if isinstance(entry, dict):
        if entry.get("canonical_name") or entry.get("name"):
            return _as_string(entry.get("canonical_name") or entry.get("name"))
        match_keys = entry.get("match_keys", [])
        if isinstance(match_keys, list) and match_keys:
            return _as_string(match_keys[0])
        if isinstance(match_keys, str):
            return match_keys
    return _as_string(entry)


def _entry_keys(entry):
    keys = []
    if isinstance(entry, dict):
        match_keys = entry.get("match_keys", [])
        if isinstance(match_keys, str):
            keys.append(match_keys)
        elif isinstance(match_keys, list):
            keys.extend(match_keys)
        keys.extend([entry.get("canonical_name"), entry.get("name")])
    else:
        keys.append(entry)
    return {_as_string(key) for key in keys if _as_string(key)}


def _entry_key_set(entries):
    keys = set()
    for entry in entries:
        keys.update(_entry_keys(entry))
    return keys


def _raw_names(entries):
    return {_entry_name(entry) for entry in entries if _entry_name(entry)}


def _matches_entry(expected, actual_entries):
    """Match expected test to actual parser entries.

    Primary matching is language-independent: exact intersection of
    ``canonical_name``, ``match_keys``, or raw ``name``. If older datapoints do
    not provide canonical keys, fall back to legacy raw-name compatibility.
    """
    if _entry_keys(expected) & _entry_key_set(actual_entries):
        return True

    expected_name = _entry_name(expected)
    if not expected_name:
        return False
    return legacy_matches(expected_name, _raw_names(actual_entries))


def _evaluate_criterion(expected, eval_passed, baseline_passed, baseline_failed,
                        has_test_patch, should_fail_baseline, empty_status):
    """Evaluate a fail_to_pass or pass_to_pass criterion.

    Args:
        expected: list of expected test names
        eval_passed: set of test names that passed in eval run
        baseline_passed: set of test names that passed in baseline run
        baseline_failed: set of test names that failed in baseline run
        has_test_patch: whether a test patch was applied
        should_fail_baseline: True for fail_to_pass (tests must fail in baseline)
        empty_status: "fail" or "skipped" — status when expected list is empty
    Returns:
        (status, detail_string) tuple
    """
    if not expected:
        label = "no expected tests defined" if empty_status == "fail" else "no expected tests"
        return empty_status, label

    # Check eval: all expected tests must pass after submission
    eval_ok = all(_matches_entry(t, eval_passed) for t in expected)

    # Check baseline consistency (only if test patch exists)
    baseline_ok = True
    baseline_bad = []
    if has_test_patch:
        for t in expected:
            # Skip tests not present in baseline (likely added by test_patch)
            baseline_all = baseline_passed + baseline_failed
            if not _matches_entry(t, baseline_all):
                continue
            if should_fail_baseline:
                # fail_to_pass: test should fail in baseline
                if _matches_entry(t, baseline_passed):
                    baseline_bad.append(t)
            else:
                # pass_to_pass: test should pass in baseline
                if not _matches_entry(t, baseline_passed):
                    baseline_bad.append(t)
        baseline_ok = not baseline_bad

    status = "pass" if (eval_ok and baseline_ok) else "fail"

    detail_parts = []
    if not eval_ok:
        missing = [_entry_name(t) for t in expected if not _matches_entry(t, eval_passed)]
        detail_parts.append("eval missing: " + ", ".join(missing[:10]))
    if not baseline_ok:
        label = "baseline unexpected pass" if should_fail_baseline else "baseline missing"
        detail_parts.append(label + ": " + ", ".join(_entry_name(t) for t in baseline_bad[:10]))

    if should_fail_baseline:
        success_msg = "all fail_to_pass tests fixed"
    else:
        success_msg = "all pass_to_pass tests still passing"

    return status, "; ".join(detail_parts) if detail_parts else success_msg


def _matches_any_expected(actual_entry, expected_names):
    """True if actual test entry matches any expected test entry.

    Reuses the same matching semantics used for fail_to_pass/pass_to_pass.
    """
    return any(_matches_entry(exp, [actual_entry]) for exp in expected_names)


def _evaluate_fail_to_fail(expected, eval_passed, baseline_passed,
                           empty_status="skipped"):
    """Evaluate fail_to_fail criterion.

    Each listed test must NOT appear in baseline_passed NOR in eval_passed
    (i.e. it should have failed or been absent in both runs).

    Returns:
        (status, detail_string) tuple
    """
    if not expected:
        return empty_status, "no expected fail_to_fail tests"

    eval_unexpected = [t for t in expected if _matches_entry(t, eval_passed)]
    baseline_unexpected = [t for t in expected if _matches_entry(t, baseline_passed)]

    detail_parts = []
    if eval_unexpected:
        detail_parts.append(
            "eval unexpected pass: " + ", ".join(_entry_name(t) for t in eval_unexpected[:10])
        )
    if baseline_unexpected:
        detail_parts.append(
            "baseline unexpected pass: "
            + ", ".join(_entry_name(t) for t in baseline_unexpected[:10])
        )

    if detail_parts:
        return "fail", "; ".join(detail_parts)
    return "pass", "all fail_to_fail tests still failing"


def _evaluate_tests_status(can_run, eval_summary_failed, eval_test_exit_code,
                           expected_f2f, fail_to_fail_strict):
    if not can_run:
        return "skipped", False

    # Some loggers can produce incomplete/empty XML while the test runner exits
    # non-zero. Treat that as a test failure unless fail_to_fail is explicitly
    # allowed to keep failing.
    allow_eval_exit_failure = bool(expected_f2f) and not fail_to_fail_strict
    eval_exit_failed = eval_test_exit_code != 0 and not allow_eval_exit_failure
    tests_status = "fail" if eval_summary_failed > 0 or eval_exit_failed else "pass"
    return tests_status, eval_exit_failed


def main():
    compile_status = os.environ.get("COMPILE_STATUS", "fail")
    compile_duration = int(os.environ.get("COMPILE_DURATION", "0"))
    patch_status = os.environ.get("PATCH_STATUS", "skipped")
    patch_duration = int(os.environ.get("PATCH_DURATION", "0"))
    test_duration = int(os.environ.get("TEST_DURATION", "0"))
    baseline_duration = int(os.environ.get("BASELINE_DURATION", "0"))
    overall_duration = int(os.environ.get("OVERALL_DURATION", "0"))
    timestamp = os.environ.get("TIMESTAMP", "")
    has_test_patch = os.environ.get("HAS_TEST_PATCH", "false") == "true"
    baseline_test_exit_code = int_env("BASELINE_TEST_EXIT_CODE")
    eval_test_exit_code = int_env("EVAL_TEST_EXIT_CODE")

    compile_output = read_file("/tmp/_compile_output.txt")
    patch_output = read_file("/tmp/_patch_output.txt")

    baseline_data = load_json("/tmp/baseline_parser.json")
    eval_data = load_json("/tmp/eval_parser.json")

    baseline_passed = [
        t for t in baseline_data.get("passed_tests", []) if isinstance(t, (dict, str))
    ]
    baseline_failed = [
        t for t in baseline_data.get("failed_tests", []) if isinstance(t, (dict, str))
    ]
    eval_passed = [
        t for t in eval_data.get("passed_tests", []) if isinstance(t, (dict, str))
    ]
    eval_failed = [
        t for t in eval_data.get("failed_tests", []) if isinstance(t, (dict, str))
    ]

    expected = load_json("/tmp/_expected.json")
    expected_f2p = expected.get("fail_to_pass", [])
    expected_p2p = expected.get("pass_to_pass", [])
    expected_f2f = expected.get("fail_to_fail", [])
    fail_to_fail_strict = expected.get("fail_to_fail_strict", True)

    if has_test_patch and baseline_test_exit_code != 0 and expected_f2p:
        baseline_all = baseline_passed + baseline_failed
        baseline_failed.extend(
            t for t in expected_f2p if not _matches_entry(t, baseline_all)
        )

    # Expand wildcards: ["*"] means "all discovered tests"
    all_eval_tests = sorted(
        {_entry_id(t) for t in eval_passed}
        | {_entry_id(t) for t in eval_failed}
    )
    if expected_f2p == ["*"]:
        expected_f2p = all_eval_tests
    if expected_p2p == ["*"]:
        # Exclude fail_to_fail names from the wildcard — "expected to still fail"
        # and "expected to still pass" are contradictory on the same test.
        if expected_f2f:
            expected_p2p = [
                n for n in all_eval_tests
                if not _matches_any_expected(n, expected_f2f)
            ]
        else:
            expected_p2p = all_eval_tests

    can_run = compile_status == "pass" and patch_status in ("pass", "skipped")

    eval_summary = eval_data.get("summary", {
        "total": 0, "passed": 0, "failed": 0,
        "errors": 0, "skipped": 0, "duration_seconds": 0.0,
    })

    eval_summary_failed = eval_summary.get("failed", 0) + eval_summary.get("errors", 0)
    if not fail_to_fail_strict and expected_f2f:
        excluded = [n for n in eval_failed if _matches_any_expected(n, expected_f2f)]
        eval_summary_failed = max(0, eval_summary_failed - len(excluded))

    # --- Criterion: baseline_tests ---
    baseline_status = "pass" if compile_status == "pass" else "skipped"

    # --- Criterion: tests (eval run) ---
    tests_status, eval_exit_failed = _evaluate_tests_status(
        can_run, eval_summary_failed, eval_test_exit_code,
        expected_f2f, fail_to_fail_strict,
    )

    # --- Criterion: fail_to_pass ---
    if not expected_f2p:
        f2p_status, f2p_detail = "skipped", "no expected fail_to_pass tests"
    elif not can_run:
        f2p_status, f2p_detail = "skipped", "skipped due to compilation or patch failure"
    else:
        f2p_status, f2p_detail = _evaluate_criterion(
            expected_f2p, eval_passed, baseline_passed, baseline_failed,
            has_test_patch, should_fail_baseline=True, empty_status="fail",
        )

    # --- Criterion: pass_to_pass ---
    if not expected_p2p:
        p2p_status, p2p_detail = "skipped", "no expected pass_to_pass tests"
    elif not can_run:
        p2p_status, p2p_detail = "skipped", "skipped due to compilation or patch failure"
    else:
        p2p_status, p2p_detail = _evaluate_criterion(
            expected_p2p, eval_passed, baseline_passed, baseline_failed,
            has_test_patch, should_fail_baseline=False, empty_status="skipped",
        )

    # --- Criterion: fail_to_fail ---
    if not expected_f2f:
        f2f_status, f2f_detail = "skipped", "no expected fail_to_fail tests"
    elif not can_run:
        f2f_status, f2f_detail = "skipped", "skipped due to compilation or patch failure"
    else:
        f2f_status, f2f_detail = _evaluate_fail_to_fail(
            expected_f2f, eval_passed, baseline_passed, empty_status="skipped",
        )

    # --- Overall status ---
    has_failure = any(
        s == "fail"
        for s in [compile_status, patch_status, tests_status, f2p_status, p2p_status, f2f_status]
    )
    overall_status = "failure" if has_failure else "success"

    eval_test_output = read_file("/tmp/eval_stdout.log") + read_file("/tmp/eval_stderr.log")

    result = {
        "schema_version": "2.0",
        "status": overall_status,
        "timestamp": timestamp,
        "duration_seconds": overall_duration,
        "criteria": [
            {
                "criterion": "compilation",
                "status": compile_status,
                "duration_seconds": compile_duration,
                "output": compile_output,
            },
            {
                "criterion": "baseline_tests",
                "status": baseline_status,
                "duration_seconds": baseline_duration,
                "test_exit_code": baseline_test_exit_code,
                "passed_tests": sorted(_entry_name(t) for t in baseline_passed),
                "failed_tests": baseline_failed,
            },
            {
                "criterion": "patch_applied",
                "status": patch_status,
                "duration_seconds": patch_duration,
                "output": patch_output,
            },
            {
                "criterion": "tests",
                "status": tests_status,
                "duration_seconds": test_duration,
                "test_exit_code": eval_test_exit_code,
                "detail": "test runner exited non-zero" if eval_exit_failed else "",
                "output": eval_test_output,
                "summary": eval_summary,
                "passed_tests": eval_data.get("passed_tests", []),
                "failed_tests": eval_data.get("failed_tests", []),
                "skipped_tests": eval_data.get("skipped_tests", []),
                "methods": eval_data.get("methods", []),
            },
            {
                "criterion": "fail_to_pass",
                "status": f2p_status,
                "expected": expected_f2p,
                "detail": f2p_detail,
            },
            {
                "criterion": "pass_to_pass",
                "status": p2p_status,
                "expected": expected_p2p,
                "detail": p2p_detail,
            },
            {
                "criterion": "fail_to_fail",
                "status": f2f_status,
                "expected": expected_f2f,
                "detail": f2f_detail,
                "strict": fail_to_fail_strict,
            },
        ],
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
