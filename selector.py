"""Select tests by the changed paths' static dependencies."""

import json
import re
from math import floor
from pathlib import Path

DATA = Path("/data")
DEPENDENCIES = Path(__file__).with_name("dependencies.json")
MAX_WORKERS = 8
WORKER_COST_MS = 2_000
DIFF_HEADER = re.compile(rb"diff --git a/(\S+) b/(\S+)")


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


def select(patch: bytes, tests: list[dict], test_files_by_path: dict) -> list[dict]:
    affected = set()
    for line in patch.splitlines():
        if not line.startswith(b"diff --git "):
            continue
        match = DIFF_HEADER.fullmatch(line)
        if not match:
            return tests
        for path in match.groups():
            test_files = test_files_by_path.get(path.decode("utf-8", errors="replace"))
            if not test_files:
                return tests
            affected.update(test_files)

    selected = [test for test in tests if test["file"] in affected]
    return selected or tests


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dependencies = json.loads(DEPENDENCIES.read_text())["test_files_by_path"]
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected = select(patch.read_bytes(), tests, dependencies)
        workers = schedule(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
