"""Build the impact index the selector reads at run time.

usage: build_index.py SQLGLOT_CHECKOUT TESTS_JSON OUT TRACE_DIR [TRACE_DIR ...]

Everything here is derived from the pinned sqlglot commit: per-test line
coverage, instantiated classes, opened files, import-time lines, and lines
whose attribution changes with test order (lazily cached code).
"""
import ast
import collections
import gzip
import json
import os
import sys

from merge_trace import line_index, load


def module_name(path):
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def classes_in(tree, mod):
    keys = []

    def walk(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                q = prefix + node.name
                keys.append(mod + ":" + q)
                walk(node.body, q + ".")

    walk(tree.body, "")
    return keys


def imports_of(tree, path):
    mod = module_name(path)
    pkg = mod if path.endswith("__init__.py") else mod.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join(parts + ([base] if base else []))
            out.add(base)
            for a in node.names:
                out.add(base + "." + a.name)
    return out


def main():
    src, tests_json, out = sys.argv[1:4]
    runs = [load(d, tests_json) for d in sys.argv[4:]]
    tests, recs, imp = runs[0]
    idxs = [line_index(r) for _, r, _ in runs]
    lazy = set()
    for other in idxs[1:]:
        for k in set(idxs[0]) | set(other):
            if idxs[0].get(k, set()) != other.get(k, set()):
                lazy.add(k)
    # union of all runs: a test that executed a line in any run is kept
    lines = collections.defaultdict(set)
    for ix in idxs:
        for k, v in ix.items():
            lines[k] |= v
    classes = collections.defaultdict(set)
    opens = collections.defaultdict(set)
    for _, r, _ in runs:
        for n, rec in r.items():
            for c in rec["classes"]:
                classes[c].add(n)
            for o in rec["opens"]:
                opens[o].add(n)

    sets = {}

    def sid(s):
        key = tuple(sorted(s))
        return sets.setdefault(key, len(sets))

    files = collections.defaultdict(dict)
    file_any = collections.defaultdict(set)
    for (f, l), s in lines.items():
        files[f][l] = sid(s)
        file_any[f] |= s

    py_files = []
    for root, _, names in os.walk(os.path.join(src, "sqlglot")):
        for n in names:
            if n.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, n), src))
    for root, _, names in os.walk(os.path.join(src, "tests")):
        for n in names:
            if n.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, n), src))
    mod_of = {module_name(p): p for p in py_files}
    importers = collections.defaultdict(set)
    mod_classes = {}
    for p in py_files:
        tree = ast.parse(open(os.path.join(src, p), encoding="utf-8").read())
        mod_classes[p] = classes_in(tree, module_name(p))
        for m in imports_of(tree, p):
            if m in mod_of and mod_of[m] != p:
                importers[mod_of[m]].add(p)

    index = {
        "n": len(tests),
        "ids": [t["id"] for t in tests],
        "sets": [list(k) for k in sorted(sets, key=sets.get)],
        "lines": {f: {str(l): s for l, s in sorted(d.items())} for f, d in files.items()},
        "file_any": {f: sid(s) for f, s in file_any.items()},
        "import": {},
        "lazy": {},
        "classes": {c: sid(s) for c, s in classes.items()},
        "opens": {o: sid(s) for o, s in opens.items()},
        "importers": {k: sorted(v) for k, v in importers.items()},
        "mod_classes": mod_classes,
    }
    # sid() above may have added sets after the list was built
    index["sets"] = [list(k) for k in sorted(sets, key=sets.get)]
    for f, l in imp:
        index["import"].setdefault(f, []).append(l)
    for f, l in lazy:
        index["lazy"].setdefault(f, []).append(l)
    for d in (index["import"], index["lazy"]):
        for f in d:
            d[f].sort()
    with gzip.open(out, "wt", compresslevel=9) as fh:
        json.dump(index, fh, separators=(",", ":"))
    print(f"tests={len(tests)} lines={len(lines)} sets={len(sets)} lazy={len(lazy)} "
          f"classes={len(classes)} opens={len(opens)} bytes={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
