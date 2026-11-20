"""Select tests by the changed paths' static dependencies."""

import json
import re
from pathlib import Path

DATA = Path("/data")
DEPENDENCIES = Path(__file__).with_name("dependencies.json")
WORKERS = 3
DIFF_HEADER = re.compile(rb"diff --git a/(\S+) b/(\S+)")


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0.0] * worker_count
    for test in sorted(tests, key=lambda test: test["seconds"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += test["seconds"]
    return workers


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
        workers = split(selected, WORKERS)
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
