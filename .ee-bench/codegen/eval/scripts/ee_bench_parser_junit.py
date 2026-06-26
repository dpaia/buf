#!/usr/bin/env python3
"""Parse JUnit XML test results into EE-bench JSON.

For Java (Maven/Gradle) and Python (pytest --junitxml) projects.

Usage: python3 ee_bench_parser_junit.py <artifacts_dir>
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

MAX_STACKTRACE = 4096


def _truncate(text, limit=MAX_STACKTRACE):
    if text and len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


def parse_junit_xml(root):
    """Parse JUnit XML format (<testsuites>/<testsuite>/<testcase>)."""
    methods = []

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        suites = root.findall(".//testsuite")

    for suite in suites:
        for tc in suite.findall("testcase"):
            name = tc.get("name", "unknown")
            classname = tc.get("classname", "")
            if classname and not name.startswith(classname):
                full_name = f"{classname}.{name}"
            else:
                full_name = name

            duration = 0.0
            try:
                duration = float(tc.get("time", "0"))
            except (ValueError, TypeError):
                pass

            entry = {
                "name": full_name,
                "canonical_name": full_name,
                "match_keys": [full_name],
                "duration_seconds": duration,
            }

            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            outcome = _testcase_outcome(tc)

            if failure is not None:
                entry["status"] = "failed"
                entry["type"] = "assertion"
                entry["message"] = failure.get("message", "")
                entry["stacktrace"] = _truncate(failure.text or "")
            elif error is not None:
                entry["status"] = "failed"
                entry["type"] = "error"
                entry["message"] = error.get("message", "")
                entry["stacktrace"] = _truncate(error.text or "")
            elif skipped is not None or outcome in {"skipped", "ignored", "notexecuted", "inconclusive"}:
                entry["status"] = "skipped"
                msg = ""
                if skipped is not None:
                    msg = skipped.get("message", "") or (skipped.text or "")
                if msg:
                    entry["message"] = msg
            elif outcome in {"failed", "failure"}:
                entry["status"] = "failed"
                entry["type"] = "assertion"
            elif outcome == "error":
                entry["status"] = "failed"
                entry["type"] = "error"
            else:
                entry["status"] = "passed"

            methods.append(entry)
    return methods


def _testcase_outcome(testcase):
    """Return lower-cased testcase outcome from common JUnit logger attrs."""
    for attr in ("result", "status", "outcome"):
        value = testcase.get(attr)
        if value:
            return value.strip().lower()
    return ""


def detect_and_parse(artifacts_dir):
    """Scan artifacts dir for JUnit XML files and parse them."""
    methods = []
    for fname in sorted(os.listdir(artifacts_dir)):
        fpath = os.path.join(artifacts_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            tree = ET.parse(fpath)
            root = tree.getroot()
        except ET.ParseError:
            continue

        if root.tag in ("testsuites", "testsuite"):
            methods.extend(parse_junit_xml(root))
        elif root.findall(".//testcase"):
            methods.extend(parse_junit_xml(root))

    return methods


def aggregate(methods):
    """Build summary and test lists from parsed method results."""
    passed_entries = []
    failed_entries = []
    skipped_entries = []
    total_duration = 0.0
    n_errors = 0

    for m in methods:
        total_duration += m.get("duration_seconds", 0.0)
        status = m["status"]
        if status == "passed":
            passed_entries.append(m)
        elif status == "failed":
            failed_entries.append(m)
            if m.get("type") == "error":
                n_errors += 1
        elif status == "skipped":
            skipped_entries.append(m)

    def unique_entries(entries):
        by_name = {}
        for entry in entries:
            by_name.setdefault(entry["name"], entry)
        return [by_name[name] for name in sorted(by_name)]

    return {
        "summary": {
            "total": len(methods),
            "passed": len(passed_entries),
            "failed": len(failed_entries) - n_errors,
            "errors": n_errors,
            "skipped": len(skipped_entries),
            "duration_seconds": round(total_duration, 3),
        },
        "passed_tests": unique_entries(passed_entries),
        "failed_tests": unique_entries(failed_entries),
        "skipped_tests": unique_entries(skipped_entries),
        "methods": methods,
    }


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <artifacts_dir>", file=sys.stderr)
        sys.exit(1)

    artifacts_dir = sys.argv[1]
    methods = detect_and_parse(artifacts_dir)
    result = aggregate(methods)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
