"""Record per-test generator calls on the pinned, unpatched sqlglot checkout."""

import argparse
import ast
import json
import os
from pathlib import Path
import sys

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    import pytest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    prefix = str(root / "sqlglot/generators") + os.sep
    calls = {}
    outside_tests = set()
    current = None
    passed = set()

    def record(code, offset):
        filename = code.co_filename
        if filename.startswith(prefix):
            key = (filename[len(prefix):], code.co_qualname)
            (outside_tests if current is None else current).add(key)

    class TraceTests:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_protocol(self, item):
            nonlocal current
            current = set()
            try:
                return (yield)
            finally:
                for key in current:
                    calls.setdefault(key, set()).add(item.nodeid)
                current = None

        def pytest_runtest_logreport(self, report):
            if report.when == "call" and report.passed:
                passed.add(report.nodeid)
            elif report.failed:
                passed.discard(report.nodeid)

    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    monitor = sys.monitoring
    tool = monitor.PROFILER_ID
    monitor.use_tool_id(tool, "generator method coverage")
    monitor.register_callback(tool, monitor.events.PY_START, record)
    monitor.set_events(tool, monitor.events.PY_START)
    try:
        status = pytest.main(["-q", "-p", "no:cacheprovider", "tests/dialects"], plugins=[TraceTests()])
    finally:
        monitor.set_events(tool, 0)
        monitor.free_tool_id(tool)
    if status not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        raise SystemExit(int(status))
    if not passed:
        raise SystemExit("No tests passed, so the trace cannot be used")
    sources = {}
    groups = []
    group_ids = {}
    for file in sorted((root / "sqlglot/generators").glob("*.py")):
        source = file.read_text()
        spans = []
        for container in ast.parse(source).body:
            if isinstance(container, ast.ClassDef) and not (container.decorator_list or container.keywords):
                candidates = [(f"{container.name}.{node.name}", node) for node in container.body
                              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            elif isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef)):
                candidates = [(container.name, container)]
            else:
                continue
            for name, node in candidates:
                if node.decorator_list or node.name.startswith("__") or node.body[0].lineno <= node.lineno:
                    continue
                key = (file.name, name)
                observed = calls.get(key, set())
                if key in outside_tests or not observed & passed:
                    continue
                consumers = tuple(sorted(observed))
                if consumers not in group_ids:
                    group_ids[consumers] = len(groups)
                    groups.append(consumers)
                spans.append([node.body[0].lineno, node.end_lineno, node.col_offset + 1,
                              group_ids[consumers]])
        if spans:
            sources[file.relative_to(root).as_posix()] = {"source": source, "spans": spans}

    # Tests without generator activity can include untraced subprocess work.
    measured = passed.intersection(set().union(*calls.values()))
    trace = {"revision": REVISION, "measured": sorted(measured), "groups": groups, "files": sources}
    output.write_text(json.dumps(trace, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
