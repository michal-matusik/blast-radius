"""Merge per-worker traces into per-test records keyed by tests.json numbers."""
import collections
import glob
import json
import os


def load(trace_dir, tests_json):
    tests = json.load(open(tests_json))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    recs = {}
    imp = set()
    for path in sorted(glob.glob(os.path.join(trace_dir, "trace-*.json"))):
        d = json.load(open(path))
        files = d["files"]
        imp.update((files[a], b) for a, b in d.get("import", []))
        for nodeid, v in d["tests"].items():
            if nodeid not in num:
                continue
            r = recs.setdefault(num[nodeid], {k: set() for k in
                ("lines", "setup", "classes", "opens", "setup_classes", "setup_opens")})
            r["lines"].update((files[a], b) for a, b in v["lines"])
            r["setup"].update((files[a], b) for a, b in v["setup"])
            for k in ("classes", "opens", "setup_classes", "setup_opens"):
                r[k].update(v.get(k, []))
    # setUpClass effects apply to every test of the class
    by_class = collections.defaultdict(list)
    for t in tests:
        by_class[t["id"].rsplit("::", 1)[0]].append(t["number"])
    for members in by_class.values():
        shared = {k: set() for k in ("setup", "setup_classes", "setup_opens")}
        for n in members:
            for k in shared:
                shared[k] |= recs[n][k]
        for n in members:
            recs[n]["lines"] |= shared["setup"]
            recs[n]["classes"] |= shared["setup_classes"]
            recs[n]["opens"] |= shared["setup_opens"]
    return tests, recs, imp


def line_index(recs):
    idx = collections.defaultdict(set)
    for n, r in recs.items():
        for fl in r["lines"]:
            idx[fl].add(n)
    return idx
