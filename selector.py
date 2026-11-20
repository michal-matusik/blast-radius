"""Select tests using static file and function dependencies."""

import json
import re
from math import floor
from pathlib import Path

from generator_selection import generator_groups
from probabilistic import plan

DATA = Path("/data")
DEPENDENCIES = Path(__file__).with_name("dependencies.json")
GENERATOR_COVERAGE = Path(__file__).with_name("generator_coverage.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2_000
DIFF_HEADER = re.compile(rb"diff --git a/(\S+) b/(\S+)")
HUNK_HEADER = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: .*)?")


def split(tests: list[tuple[int, int]], worker_count: int) -> tuple[int, list[list[int]]]:
    """Assign longest tests first and return the schedule cost in milliseconds."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for index, (duration, number) in enumerate(tests):
        lightest = index if index < worker_count else loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += duration
    return max(loads) + WORKER_COST_MS * worker_count, workers


def schedule(tests: list[dict]) -> list[list[int]]:
    if not tests:
        return []
    # Match the official scorer's integer milliseconds and rounding.
    ranked = sorted(
        ((floor(test["seconds"] * 1000 + 0.5), test["number"]) for test in tests),
        key=lambda item: item[0],
        reverse=True,
    )
    limit = min(MAX_WORKERS, len(ranked))
    candidates = (split(ranked, count) for count in range(1, limit + 1))
    # Candidates arrive in worker-count order, so ties use fewer workers.
    return min(candidates, key=lambda candidate: candidate[0])[1]


def symbol_consumers(lines, spans):
    """Return None unless every edit lies inside indexed function or method bodies."""
    affected = set()
    old = old_left = new_left = 0
    saw_edit = False
    in_hunk = False
    for line in lines:
        if line.startswith(b"@@"):
            if old_left or new_left:
                return None
            match = HUNK_HEADER.fullmatch(line)
            if not match:
                return None
            old, old_count, new_count = match.groups()
            old, old_left, new_left = int(old), int(old_count or 1), int(new_count or 1)
            in_hunk = True
            continue
        if not in_hunk:
            if not line.startswith((b"index ", b"--- a/", b"+++ b/")):
                return None
            continue
        if line == rb"\ No newline at end of file":
            continue
        marker = line[:1]
        if marker not in (b" ", b"+", b"-"):
            return None
        if marker != b" ":
            saw_edit = True
            # Insertions must be between body lines, not at a symbol boundary.
            first = old - (marker == b"+")
            for start, end, consumers, *minimum_indent in spans:
                if start <= first <= old <= end:
                    content = line[1:]
                    if marker == b"+" and content.strip():
                        indent = len(content) - len(content.lstrip(b" \t"))
                        if indent < (minimum_indent[0] if minimum_indent else 1):
                            return None
                    affected.update(consumers)
                    break
            else:
                return None
        if marker != b"+":
            old += 1
            old_left -= 1
        if marker != b"-":
            new_left -= 1
        if old_left < 0 or new_left < 0:
            return None
    return affected if saw_edit and old_left == new_left == 0 else None


def selection_evidence(patch: bytes, tests: list[dict], dependencies: dict, coverage=None):
    by_path = dependencies["test_files_by_path"]
    by_symbol = dependencies.get("symbols_by_path", {})
    by_method = dependencies.get("methods_by_path", {})
    lines = patch.splitlines()
    headers = [i for i, line in enumerate(lines) if line.startswith(b"diff --git ")]
    affected = set()
    unmeasured_files = set()
    covered_tests = set()
    observed_groups = []
    catalog_ids = None
    for start, end in zip(headers, headers[1:] + [len(lines)]):
        match = DIFF_HEADER.fullmatch(lines[start])
        if not match:
            return tests, {test['id'] for test in tests}, []
        old, new = (path.decode("utf-8", errors="replace") for path in match.groups())
        if old not in by_path or new not in by_path:
            return tests, {test['id'] for test in tests}, []
        if old == new and coverage and old in coverage["files"]:
            groups = generator_groups(lines[start + 1:end], coverage["files"][old])
            if groups is not None:
                if catalog_ids is None:
                    catalog_ids = {test["id"] for test in tests}
                consumers = [catalog_ids.intersection(coverage["groups"][group]) for group in groups]
                if all(consumers):
                    covered_tests.update(*consumers)
                    observed_groups.extend(consumers)
                    unmeasured_files.update(by_path[old])
                    continue
        spans = [*by_symbol.get(old, ()), *by_method.get(old, ())]
        if old == new and spans:
            consumers = symbol_consumers(lines[start + 1:end], spans)
            if consumers is not None:
                affected.update(consumers)
                continue
        affected.update(by_path[old])
        affected.update(by_path[new])

    measured = coverage["measured"] if coverage else ()
    selected = [
        test for test in tests
        if test["file"] in affected
        or (covered_tests and test["id"] in covered_tests)
        or (test["file"] in unmeasured_files and test["id"] not in measured)
    ]
    if not selected:
        return tests, {test["id"] for test in tests}, []
    required = {test["id"] for test in selected if test["file"] in affected}
    return selected, required, observed_groups


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dependencies = json.loads(DEPENDENCIES.read_text())
    coverage = json.loads(GENERATOR_COVERAGE.read_text())
    if coverage["revision"] != dependencies["revision"]:
        raise ValueError("Static index and generator coverage use different revisions")
    coverage["measured"] = set(coverage["measured"])
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected, required, observed = selection_evidence(patch.read_bytes(), tests, dependencies, coverage)
        workers = plan(selected, required, observed, schedule)
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
