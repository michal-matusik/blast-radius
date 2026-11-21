"""Blast Radius selector: run the tests a change can reach, packed onto workers.

For every patch:
  1. Apply it to the pinned sqlglot sources shipped in the image.
  2. Diff base and patched Python files statement by statement on their ASTs
     (docstrings, comments and formatting drop out).
  3. Map every changed unit to the tests that can observe it, using an index
     recorded from the base test suite (see tools/):
       function-level statement   -> tests that executed those base lines
       class-level statement      -> tests that created an instance of the class
                                     (or a subclass) plus tests running its code
       module-level statement     -> everything that uses the module
       fixture / data file        -> tests that opened it
     Lines that only run at import time or lazily once per process widen to the
     whole module, and class or module-level edits are import-checked.
  4. Pack the selected tests with LPT for the k in 1..8 with the lowest
     makespan + 2 s per worker.
"""

from __future__ import annotations

import ast
import difflib
import gzip
import json
import os
import re
import sys
from pathlib import Path

from expression_profiles import observed_readers
from operand_profiles import neutral_comparison
from setting_profiles import readers as setting_readers

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(os.environ.get("BR_APP", "/app"))
SRC = APP / "src"
INDEX = APP / "index.marshal"
INDEX_JSON = APP / "index.json.gz"
WORKER_MS = 2000
MAX_WORKERS = 8

DOC_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg", ".html", ".css")
DOC_PREFIXES = ("posts/", "pdoc/", "benchmarks/", ".github/", "docs/")
DOC_NAMES = {"LICENSE", "CHANGELOG.md", "README.md", "CONTRIBUTING.md", "AGENTS.md",
             "CLAUDE.md", "MANIFEST.in", ".gitignore", ".pre-commit-config.yaml"}


def log(*args):
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- patches

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return [{old, new, hunks: [(old_start, [lines])], binary}] for a unified diff."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            files.append(cur)
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False}
                files.append(cur)
            old, new = line[4:].split("\t")[0], lines[i + 1][4:].split("\t")[0]
            cur["old"] = None if old == "/dev/null" else old[2:] if old.startswith("a/") else old
            cur["new"] = None if new == "/dev/null" else new[2:] if new.startswith("b/") else new
            i += 1
        elif line.startswith("new file mode") and cur is not None:
            cur["old"] = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur["new"] = None
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            if cur is not None:
                cur["binary"] = True
        elif cur is not None and (m := HUNK.match(line)):
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                h = lines[i]
                if h.startswith("\\"):
                    i += 1
                    continue
                tag = h[:1] or " "
                if tag == " ":
                    o += 1
                    n += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append((tag, h[1:]))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((int(m.group(1)), body))
            continue
        i += 1
    return files


def apply_hunks(base_text, hunks):
    """Apply hunks; return (new_text, changed_base_lines, insert_points) or None on mismatch."""
    base = base_text.split("\n")
    out = []
    origin = []  # base line number (1-based) of each output line, or None if added
    pos = 0  # 0-based index into base
    changed, inserts = set(), set()
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if old_start == 0:
            start = 0
        if start < pos:
            return None
        out.extend(base[pos:start])
        origin.extend(range(pos + 1, start + 1))
        pos = start
        for tag, text in body:
            if tag == " ":
                if pos >= len(base) or base[pos] != text:
                    return None
                out.append(text)
                origin.append(pos + 1)
                pos += 1
            elif tag == "-":
                if pos >= len(base) or base[pos] != text:
                    return None
                changed.add(pos + 1)
                pos += 1
            else:
                inserts.add(pos)  # inserted before base line pos+1
                out.append(text)
                origin.append(None)
    out.extend(base[pos:])
    origin.extend(range(pos + 1, len(base) + 1))
    n2b = {i + 1: b for i, b in enumerate(origin) if b is not None}
    return "\n".join(out), changed, inserts, n2b


# --------------------------------------------------------------------------- AST diff

BODY_FIELDS = ("body", "orelse", "finalbody")
DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def executable_body(node):
    """Skip a definition's docstring when its statement block is compared."""
    body = node.body
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return body[1:]
    return body


def dump(node):
    return ast.dump(node)


def header_dump(node):
    """Dump of a compound statement without its nested statement blocks."""
    saved = {}
    for f in BODY_FIELDS + ("handlers", "cases"):
        if hasattr(node, f):
            saved[f] = getattr(node, f)
            setattr(node, f, [])
    try:
        return ast.dump(node)
    finally:
        for f, v in saved.items():
            setattr(node, f, v)


def lambdas(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Lambda)]


def masked_dump(node):
    class Mask(ast.NodeTransformer):
        def visit_Lambda(self, n):
            return ast.Constant(value="<lambda>")

    import copy

    return ast.dump(Mask().visit(copy.deepcopy(node)))


def first_line(node):
    """First source line of a statement, counting decorators."""
    decs = getattr(node, "decorator_list", None)
    return min([node.lineno] + [d.lineno for d in decs]) if decs else node.lineno


def span(node):
    return (first_line(node), node.end_lineno or node.lineno)


def body_span(stmts):
    return (stmts[0].lineno, stmts[-1].end_lineno or stmts[-1].lineno) if stmts else None


def header_span(node):
    """Lines of a compound statement before its first nested block."""
    first = None
    for f in BODY_FIELDS + ("handlers", "cases"):
        v = getattr(node, f, None)
        if v:
            ln = v[0].pattern.lineno if type(v[0]).__name__ == "match_case" else v[0].lineno
            first = ln if first is None else min(first, ln)
    end = (first - 1) if first and first > node.lineno else node.lineno
    return (node.lineno, max(node.lineno, end))


class Impacts:
    def __init__(self):
        self.lines = []       # (lo, hi) base line ranges: tests that executed them
        self.lam_lines = []   # lambda body ranges (import-time execution expected)
        self.classes = set()  # class qualnames in this module
        self.module = False   # everything that uses the module
        self.import_check = False
        self.full = False
        self.methods = []     # (class qualname, name): plain methods added to a class body
        self.settings = []    # (class qualname, name, span): literal class attributes changed


def plain_method(node):
    """An undecorated def that no name-based dispatch in sqlglot can discover.

    Generators and simplify collect `*_sql` methods with dir(), dunders and
    capitalized names are protocol or settings hooks, so those stay class-level.
    """
    if not isinstance(node, DEF_TYPES):
        return False
    name = node.name
    return (not node.decorator_list and not name.startswith("__")
            and not name.endswith("_sql") and not name[:1].isupper())


def literal_setting(b, n):
    """Name of a class attribute whose literal value is all that changed, else None."""
    if type(b) is not type(n) or not isinstance(b, (ast.Assign, ast.AnnAssign)):
        return None
    if isinstance(b, ast.Assign):
        if len(b.targets) != 1 or len(n.targets) != 1:
            return None
        target, other = b.targets[0], n.targets[0]
    else:
        if b.value is None or n.value is None or dump(b.annotation) != dump(n.annotation):
            return None
        target, other = b.target, n.target
    if not isinstance(target, ast.Name) or dump(target) != dump(other):
        return None
    try:
        ast.literal_eval(b.value)
        ast.literal_eval(n.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return target.id


def def_can_raise(node):
    """Evaluating the def statement itself runs defaults and annotations."""
    args = node.args
    defaults = args.defaults + [d for d in args.kw_defaults if d is not None]
    annotated = node.returns is not None or any(
        a.annotation is not None for a in args.posonlyargs + args.args + args.kwonlyargs
        + [x for x in (args.vararg, args.kwarg) if x is not None])
    return annotated or not all(isinstance(d, ast.Constant) for d in defaults)


class FileDiff:
    def __init__(self, base_tree, new_tree, base_names, edits=None):
        self.imp = Impacts()
        self.base_names = base_names
        self.edits = edits
        self.diff_block(executable_body(base_tree), executable_body(new_tree), ("module",), None)

    # Statements whose source text the patch left alone get a cheap key instead
    # of a full AST dump: same text gives the same AST.
    def key_base(self, s):
        if self.edits is not None:
            changed, inserts, _ = self.edits
            lo, hi = first_line(s), s.end_lineno or s.lineno
            if not any(lo <= c <= hi for c in changed) and not any(lo <= p < hi for p in inserts):
                return ("U", lo, hi)
        if self.edits is not None and isinstance(s, DEF_TYPES + (ast.ClassDef,)):
            return ("changed-base", s.lineno)
        return dump(s)

    def key_new(self, s):
        if self.edits is not None:
            n2b = self.edits[2]
            lo, hi = first_line(s), s.end_lineno or s.lineno
            bl, bh = n2b.get(lo), n2b.get(hi)
            if bl is not None and bh is not None and bh - bl == hi - lo and all(
                    n2b.get(x) == bl + x - lo for x in range(lo, hi + 1)):
                return ("U", bl, bh)
        if self.edits is not None and isinstance(s, DEF_TYPES + (ast.ClassDef,)):
            return ("changed-new", s.lineno)
        return dump(s)

    # contexts: ("module",) | ("class", qualname) | ("func", qualname-or-None)
    def diff_block(self, bstmts, nstmts, ctx, owner_span):
        bd = [self.key_base(s) for s in bstmts]
        nd = [self.key_new(s) for s in nstmts]
        if bd == nd:
            return
        sm = difflib.SequenceMatcher(None, bd, nd, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace" and i2 - i1 == j2 - j1:
                for b, n in zip(bstmts[i1:i2], nstmts[j1:j2]):
                    self.diff_stmt(b, n, ctx)
                continue
            for b in bstmts[i1:i2]:
                self.removed(b, ctx)
            if j2 > j1:
                self.inserted(nstmts[j1:j2], ctx, bstmts, owner_span)

    def class_level(self, ctx):
        """Impact of an edit made directly in a class body (or module body)."""
        if ctx[0] == "class":
            q = ctx[1]
            parts = q.split(".")
            for k in range(1, len(parts) + 1):
                self.imp.classes.add(".".join(parts[:k]))
        else:
            self.imp.module = True
        self.imp.import_check = True

    def diff_stmt(self, b, n, ctx):
        if type(b) is not type(n):
            self.removed(b, ctx)
            self.inserted([n], ctx, None, None)
            return
        if isinstance(b, ast.ClassDef) and b.name == n.name:
            q = b.name if ctx[0] != "class" else ctx[1] + "." + b.name
            if ctx[0] == "func":
                if header_dump(b) != header_dump(n):
                    self.imp.lines.append(span(b))
                body = executable_body(b)
                self.diff_block(body, executable_body(n), ("func", None), body_span(body))
                return
            if header_dump(b) != header_dump(n):
                self.class_level(("class", q))
                self.imp.lines.append(span(b))
            self.diff_block(executable_body(b), executable_body(n), ("class", q), None)
            return
        if isinstance(b, DEF_TYPES) and b.name == n.name:
            if header_dump(b) != header_dump(n):
                bs = body_span(b.body) or span(b)
                self.imp.lines.append(bs)
                if ctx[0] != "func" and plain_method(b) and plain_method(n):
                    # A new signature only matters to calls, and every call that
                    # succeeded on the base ran the body. Defaults run at import.
                    self.imp.import_check |= def_can_raise(n)
                elif ctx[0] != "func":
                    self.class_level(ctx)
                else:
                    self.imp.lines.append(span(b))
            body = executable_body(b)
            self.diff_block(body, executable_body(n), ("func", None), body_span(body))
            return
        if any(hasattr(b, f) for f in BODY_FIELDS) and not isinstance(b, (ast.ClassDef,) + DEF_TYPES):
            if header_dump(b) != header_dump(n):
                self.changed_simple(b, n, ctx, header_span(b))
            for f in BODY_FIELDS:
                bb, nb = getattr(b, f, None), getattr(n, f, None)
                if bb is None:
                    continue
                if bb or nb:
                    owner = header_span(b)
                    self.diff_block(bb, nb, ctx, owner)
            if isinstance(b, (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())):
                if len(b.handlers) != len(n.handlers):
                    self.changed_simple(b, n, ctx, span(b))
                else:
                    for bh, nh in zip(b.handlers, n.handlers):
                        if header_dump(bh) != header_dump(nh):
                            self.changed_simple(bh, nh, ctx, header_span(bh))
                        self.diff_block(bh.body, nh.body, ctx, header_span(bh))
            return
        if type(b).__name__ == "Match":
            if header_dump(b) != header_dump(n) or len(b.cases) != len(n.cases):
                self.changed_simple(b, n, ctx, span(b))
                return
            for bc, nc in zip(b.cases, n.cases):
                if ast.dump(bc.pattern) != ast.dump(nc.pattern) or dump(bc.guard or ast.Pass()) != dump(nc.guard or ast.Pass()):
                    self.changed_simple(b, n, ctx, span(b))
                self.diff_block(bc.body, nc.body, ctx, span(b))
            return
        if dump(b) != dump(n):
            self.changed_simple(b, n, ctx, span(b))

    def changed_simple(self, b, n, ctx, rng):
        if ctx[0] == "func":
            self.imp.lines.append(rng)
            return
        name = literal_setting(b, n) if ctx[0] == "class" else None
        if name:
            # Resolved against the read sites of the name in setting_tests().
            self.imp.settings.append((ctx[1], name, rng))
            return
        # class or module level: only lambda bodies differ?
        bl, nl = lambdas(b), lambdas(n)
        if bl and len(bl) == len(nl) and masked_dump(b) == masked_dump(n):
            for x, y in zip(bl, nl):
                if dump(x) != dump(y):
                    self.imp.lam_lines.append(span(x))
                    if dump(x.args) != dump(y.args):
                        # defaults are evaluated when the lambda is created
                        self.imp.import_check = True
            return
        self.imp.lines.append(rng)
        self.class_level(ctx)

    def removed(self, b, ctx):
        if ctx[0] == "func":
            self.imp.lines.append(span(b))
            return
        if ctx[0] == "class" and plain_method(b):
            # Callers of a removed method, or of the override it shadowed, ran its body.
            self.imp.lines.append(body_span(executable_body(b)) or span(b))
            self.imp.import_check = True  # a class-body reference to it would now fail
            return
        if isinstance(b, DEF_TYPES + (ast.ClassDef,)):
            self.imp.lines.append(span(b))
        self.class_level(ctx)

    def inserted(self, stmts, ctx, block, owner_span):
        if ctx[0] == "func":
            if owner_span:
                self.imp.lines.append(owner_span)
            if block:
                self.imp.lines.append(body_span(block))
            return
        # A brand-new plain function nobody can reference yet only needs to import.
        # Decorators, base classes and metaclasses can register things, so any
        # other inserted definition counts as a module-level change.
        if ctx[0] == "module" and all(
            isinstance(s, DEF_TYPES) and not s.decorator_list and s.name not in self.base_names
            for s in stmts
        ):
            self.imp.import_check = True
            return
        if ctx[0] == "class" and all(plain_method(s) for s in stmts):
            # Resolved against the base class hierarchy in method_tests().
            self.imp.methods.extend((ctx[1], s.name) for s in stmts)
            self.imp.import_check |= any(def_can_raise(s) for s in stmts)
            return
        self.class_level(ctx)


def top_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, DEF_TYPES + (ast.ClassDef,)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
    return names


def class_spans(tree):
    out = {}

    def walk(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                q = prefix + node.name
                out[q] = span(node)
                walk(node.body, q + ".")

    walk(tree.body, "")
    return out


def module_name(path):
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


# --------------------------------------------------------------------------- index


class Index:
    def __init__(self, path, tests):
        if path.is_file():
            import marshal

            d = marshal.loads(path.read_bytes())
        else:
            d = json.loads(gzip.decompress(INDEX_JSON.read_bytes()))
            d["lines"] = {f: {int(k): v for k, v in m.items()} for f, m in d["lines"].items()}
        self.sets = d["sets"]
        self.lines = d["lines"]
        self.file_any = d["file_any"]
        self.imports = {f: set(v) for f, v in d["import"].items()}
        self.lazy = {f: set(v) for f, v in d["lazy"].items()}
        self.classes = d["classes"]
        self.opens = d["opens"]
        self.importers = d["importers"]
        self.mod_classes = d["mod_classes"]
        ids = d.get("ids")
        self._cache = {}
        self.class_refs = d.get("class_refs", {})
        self.collect_opens = d.get("collect_opens", {})
        self.n = len(tests)
        # remap index test numbers onto this run's tests.json by test id
        if ids is not None:
            by_id = {t["id"]: t["number"] for t in tests}
            self.remap = [by_id.get(i) for i in ids]
            known = set(ids)
            self.unknown = [t["number"] for t in tests if t["id"] not in known]
        else:
            self.remap = None
            self.unknown = []

    def tests_of(self, sid):
        cached = self._cache.get(sid)
        if cached is None:
            s = self.sets[sid]
            if self.remap is not None:
                s = [self.remap[x] for x in s if self.remap[x] is not None]
            cached = self._cache[sid] = frozenset(s)
        return cached

    def line_tests(self, f, lo, hi):
        out = set()
        d = self.lines.get(f)
        if not d:
            return out
        # Many adjacent lines share one coverage set.
        for sid in {d[ln] for ln in range(lo, hi + 1) if ln in d}:
            out.update(self.tests_of(sid))
        return out

    def file_tests(self, f):
        sid = self.file_any.get(f)
        return set(self.tests_of(sid)) if sid is not None else set()

    def class_tests(self, key):
        sid = self.classes.get(key)
        return set(self.tests_of(sid)) if sid is not None else set()

    def ref_tests(self, qualname):
        """Tests that ran a line naming the class (static access, no instance needed)."""
        sid = self.class_refs.get(qualname.rsplit(".", 1)[-1])
        return set(self.tests_of(sid)) if sid is not None else set()

    def importers_of(self, f):
        """Modules importing f, directly or through other importers."""
        seen, todo = set(), [f]
        while todo:
            for imp in self.importers.get(todo.pop(), []):
                if imp not in seen and imp != f:
                    seen.add(imp)
                    todo.append(imp)
        return seen

    def module_tests(self, f):
        cached = self._cache.get(("module", f))
        if cached is not None:
            return set(cached)
        out = self.file_tests(f)
        for c in self.mod_classes.get(f, []):
            out |= self.class_tests(c)
        for imp in self.importers_of(f):
            if imp.startswith("tests/"):
                out |= self.file_tests(imp)
                out |= self.tests_in.get(imp, set())
            for c in self.mod_classes.get(imp, []):
                out |= self.class_tests(c)
        self._cache[("module", f)] = frozenset(out)
        return out


# --------------------------------------------------------------------------- import check


IMPORT_DEADLINE = 30.0


def warm_imports():
    """Fork from the initialized pinned core when a patch only touches unloaded modules."""
    import unittest
    if "sqlglot" not in sys.modules:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(SRC))
        import sqlglot


def passing_cases(ids):
    """The pinned dialect suite consists of standard unittest cases."""
    import unittest
    cases = unittest.TestSuite()
    for node in ids:
        path, cls, method = node.split("::")
        suite = unittest.defaultTestLoader.loadTestsFromName(f"{module_name(path)}.{cls}.{method}")
        if suite.countTestCases() != 1:
            return False
        cases.addTest(suite)
    result = unittest.TestResult()
    cases.run(result)
    return (result.wasSuccessful() and result.testsRun == len(ids)
            and not result.skipped and not result.expectedFailures)


def state_fingerprint(ignore=frozenset()):
    """Digest of everything sqlglot computes at import, per module.

    Imports every sqlglot module, then describes module globals and the
    attributes of classes defined there: literals, containers, enums, regexes,
    partials and expression trees by value; functions, classes and modules by
    name (their code is the patch itself and is covered by line impacts).
    """
    import enum
    import functools
    import hashlib
    import importlib
    import types

    addr = re.compile(r" at 0x[0-9a-f]+")
    for p in sorted((SRC / "sqlglot").rglob("*.py")):
        name = module_name(p.relative_to(SRC).as_posix())
        if not name.endswith("__main__"):
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001 - absent modules compare as absent
                pass

    def describe(v, path):
        t = type(v)
        if v is None or t in (bool, int, float, str, bytes, complex):
            return repr(v)
        if isinstance(v, enum.Enum):
            return f"{t.__qualname__}.{v.name}={v.value!r}"
        if isinstance(v, type):
            return f"<class {v.__module__}.{v.__qualname__}>"
        if hasattr(v, "cache_info") and v.cache_info().currsize:
            return f"<filled cache {id(v)}>"  # contents unknown: never compares equal
        if isinstance(v, (types.FunctionType, types.BuiltinFunctionType, types.MethodType,
                          types.ModuleType, classmethod, staticmethod, property)):
            return f"<{t.__name__} {getattr(v, '__qualname__', getattr(v, '__name__', ''))}>"
        if id(v) in path or len(path) > 40:
            return "<cycle>"
        path = path | {id(v)}
        if isinstance(v, functools.partial):
            return f"partial({describe(v.func, path)},{describe(v.args, path)},{describe(v.keywords, path)})"
        if isinstance(v, re.Pattern):
            return f"re({v.pattern!r},{v.flags})"
        if isinstance(v, dict):
            # Sorted: dicts built from sets of classes vary in order between processes.
            return "{" + ",".join(sorted(f"{describe(k, path)}:{describe(x, path)}" for k, x in v.items())) + "}"
        if isinstance(v, (list, tuple)):
            items = [describe(x, path) for x in v]
            if v and all(isinstance(x, type) for x in v):
                # Class tuples are isinstance() targets, often built from sets.
                items.sort()
            return f"{t.__name__}[" + ",".join(items) + "]"
        if isinstance(v, (set, frozenset)):
            return f"{t.__name__}{{" + ",".join(sorted(describe(x, path) for x in v)) + "}"
        if t.__module__.startswith("sqlglot.expressions"):
            # Expression trees compare by structure, not by parent links,
            # cached hashes or source positions.
            return f"<{t.__qualname__} {addr.sub('', repr(v))}>"
        slots = [n for k in t.__mro__ for n in getattr(k, "__slots__", ()) if isinstance(n, str)]
        if slots and not callable(v):
            values = {n: getattr(v, n, "<unset>") for n in slots if n not in ("__dict__", "__weakref__")}
            if hasattr(v, "__dict__"):
                values.update(vars(v))
            return f"<{t.__module__}.{t.__qualname__} {describe(values, path)}>"
        if hasattr(v, "__dict__") and not callable(v):
            return f"<{t.__module__}.{t.__qualname__} {describe(vars(v), path)}>"
        return f"<{t.__module__}.{t.__qualname__} {addr.sub('', repr(v))}>"

    def describe_class(cls, out):
        for k, x in vars(cls).items():
            if k in ("__dict__", "__weakref__", "__doc__", "__module__"):
                continue
            if (cls.__module__, f"{cls.__qualname__}.{k}") in ignore:
                continue
            if isinstance(x, type) and x.__module__ == cls.__module__ and x.__qualname__.startswith(cls.__qualname__ + "."):
                describe_class(x, out)
            else:
                out.append(f"{cls.__qualname__}.{k}={describe(x, frozenset())}")

    digests = {}
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        module = sys.modules[name]
        out = []
        for k, v in list(vars(module).items()):
            if k.startswith("__") and k != "__all__":
                continue
            if isinstance(v, type) and v.__module__ == name:
                describe_class(v, out)
            else:
                out.append(f"{k}={describe(v, frozenset())}")
        digests[name] = hashlib.sha256("\n".join(out).encode()).hexdigest()
    return digests


def check_patch(patched, probe=(), fingerprint=False, ignore=frozenset()):
    """Import sqlglot and every patched module from scratch, with all patched
    sources visible together, in a forked child. False if it raises or hangs.

    With fingerprint, return (status, digests of the resulting import-time state)."""
    mods = {module_name(p): (p, t) for p, t in patched.items()
            if p.startswith("sqlglot/") and p.endswith(".py")}
    if not mods:
        return (0, None) if fingerprint else 0
    warm_imports()
    sys.stdout.flush()
    sys.stderr.flush()
    if fingerprint:
        read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)  # the child must never write answers
            import importlib
            import importlib.abc
            import importlib.util

            sys.dont_write_bytecode = True

            class Loader(importlib.abc.SourceLoader):
                def __init__(self, path, text):
                    self.path, self.text = path, text

                def get_filename(self, fullname):
                    return self.path

                def get_data(self, path):
                    return self.text.encode("utf-8")

            class Finder(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name not in mods:
                        return None
                    rel, text = mods[name]
                    full = str(SRC / rel)
                    pkg = [str(SRC / os.path.dirname(rel))] if rel.endswith("__init__.py") else None
                    return importlib.util.spec_from_file_location(
                        name, full, loader=Loader(full, text), submodule_search_locations=pkg)

            if any(name in sys.modules for name in mods):
                for name in list(sys.modules):
                    if name == "sqlglot" or name.startswith("sqlglot."):
                        del sys.modules[name]
            sys.meta_path.insert(0, Finder())
            sys.path.insert(0, str(SRC))
            import sqlglot  # noqa: F401

            for name in sorted(mods):
                importlib.import_module(name)
            code = 0
            if fingerprint:
                os.close(read_end)
                data = json.dumps(state_fingerprint(ignore)).encode()
                while data:
                    data = data[os.write(write_end, data):]
                os.close(write_end)
            if probe:
                os.chdir(SRC)
                os.environ["SKIP_INTEGRATION"] = "0"
                import signal
                signal.alarm(3)
                if passing_cases(probe):
                    code = 2
        except BaseException as e:  # noqa: BLE001
            try:
                log("import check failed:", type(e).__name__, str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
        os._exit(code)
    import time

    chunks = []
    if fingerprint:
        os.close(write_end)
        os.set_blocking(read_end, False)
    deadline = time.monotonic() + IMPORT_DEADLINE
    result = None
    while result is None:
        if fingerprint:
            try:
                while chunk := os.read(read_end, 1 << 16):
                    chunks.append(chunk)
            except BlockingIOError:
                pass
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            result = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        elif time.monotonic() > deadline:
            log("import check timed out")
            try:
                os.kill(pid, 9)
            finally:
                os.waitpid(pid, 0)
            result = -1
        else:
            time.sleep(0.005)
    if not fingerprint:
        return result
    try:
        while chunk := os.read(read_end, 1 << 16):
            chunks.append(chunk)
    except BlockingIOError:
        pass
    os.close(read_end)
    try:
        digests = json.loads(b"".join(chunks)) if result == 0 else None
    except ValueError:
        digests = None
    return result, digests


def import_state_unchanged(patched, ignore=frozenset()):
    """True if importing all of sqlglot with the patch leaves every module-level
    and class-level value as on the base, loading the same modules the same way."""
    base = {}
    for p in patched:
        if p.startswith("sqlglot/") and p.endswith(".py"):
            if not (SRC / p).is_file():
                return False
            base[p] = (SRC / p).read_text(encoding="utf-8")
    before_status, before = check_patch(base, fingerprint=True, ignore=ignore)
    after_status, after = check_patch(patched, fingerprint=True, ignore=ignore)
    return before_status == 0 and after_status == 0 and before is not None and before == after


# --------------------------------------------------------------------------- added methods

_SOURCE_TEXT = []


def named_in_source(name):
    """True if sqlglot spells the name as a string, e.g. getattr(self, "name")."""
    if not _SOURCE_TEXT:
        _SOURCE_TEXT.append("\n".join(p.read_text(encoding="utf-8") for p in (SRC / "sqlglot").rglob("*.py")))
    text = _SOURCE_TEXT[0]
    return f'"{name}"' in text or f"'{name}'" in text


def method_tests(index, path, qual, name):
    """Tests that can run a plain method added to a base class, or None if unknown.

    A new name is reachable only from changed callers, which are selected on
    their own lines. A name that overrides an inherited method changes what
    runs only for instances of this class that called the inherited method.
    """
    if path.startswith("tests/"):
        # A new test method has no number in the inventory; helpers stay class-level.
        return set() if name.startswith("test") else None
    if not path.startswith("sqlglot/") or named_in_source(name):
        return None
    try:
        import importlib
        import inspect
        import types

        warm_imports()
        obj = importlib.import_module(module_name(path))
        for part in qual.split("."):
            obj = getattr(obj, part)
        if not isinstance(obj, type) or name in obj.__dict__:
            return None
        owner = next((k.__dict__[name] for k in obj.__mro__[1:] if name in k.__dict__), None)
        if owner is None:
            return set()
        if not isinstance(owner, types.FunctionType):
            return None
        source = Path(inspect.getsourcefile(owner)).resolve()
        rel = source.relative_to(SRC.resolve()).as_posix()
    except Exception:  # noqa: BLE001 - unresolvable hierarchy: stay class-level
        return None
    code = owner.__code__
    lines = {ln for *_, ln in code.co_lines() if ln is not None and ln != code.co_firstlineno}
    if not lines or lines & index.lazy.get(rel, set()) or lines & index.imports.get(rel, set()):
        return None
    ran = set()
    for ln in lines:
        ran |= index.line_tests(rel, ln, ln)
    return ran & (index.class_tests(module_name(path) + ":" + qual) | index.ref_tests(qual))


_ATTRS = []


def setting_tests(index, path, qual, name, deferred=None):
    """Tests that read a literal class attribute, or None if a read is not traceable.

    The value is only observable where the name is loaded. `self.NAME` reads it
    from the instance's class hierarchy, so those sites count only for tests
    that used this class; other forms (class objects, other instances) keep
    every test that ran the site. Cached reads or the name spelled as a
    string fall back to class-level impact. Import-time reads are appended to
    `deferred`, to be accepted only if the import-time state is unchanged.
    """
    if not path.startswith("sqlglot/") or named_in_source(name):
        return None
    if not _ATTRS:
        import marshal
        attrs = APP / "attrs.marshal"
        _ATTRS.append(marshal.loads(attrs.read_bytes()) if attrs.is_file() else None)
    if _ATTRS[0] is None:
        return None
    users = index.class_tests(module_name(path) + ":" + qual) | index.ref_tests(qual)
    out = set()
    for f, ln, via_self in _ATTRS[0].get(name, ()):
        if ln in index.lazy.get(f, ()) or (ln in index.imports.get(f, ()) and deferred is None):
            return None
        if ln in index.imports.get(f, ()):
            deferred.append((f, ln))
        ran = index.line_tests(f, ln, ln)
        out |= (ran & users) if via_self else ran
    return out


# --------------------------------------------------------------------------- selection


_BASE_TREES = {}


def is_doc(path):
    name = os.path.basename(path)
    return name in DOC_NAMES or path.startswith(DOC_PREFIXES) or (
        path.endswith(DOC_SUFFIXES) and not path.startswith(("tests/", "sqlglot/")))


def executor_fixture_readers(path, before, after, readers, index):
    """Narrow TestExecutor's shared setUpClass reads on a single fixture edit."""
    suites = {
        "tests/fixtures/optimizer/tpc-h/tpc-h.sql": "tpch",
        "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "tpcds",
    }
    suite = suites.get(path)
    if not suite or index.unknown:
        return readers

    def fields(text):
        # Match the pinned load_sql_fixture_pairs filter and semicolon pairing.
        statements = "\n".join(line for line in text.splitlines()
                               if line and not line.startswith("--")).split(";")
        inputs = [part.strip() for part in statements[:-1:2]]
        expected = [part.strip() for part in statements[1::2]]
        return inputs, expected

    inputs, expected = fields(before)
    new_inputs, new_expected = fields(after)
    # A malformed metadata header can raise while setUpClass loads either suite.
    # Only trust edits with the same headers on every paired input.
    def headers(sql):
        lines = sql.split("\n")
        for i, line in enumerate(lines):
            if not line.startswith("#"):
                return lines[:i]
        return None

    if len(inputs) != len(new_inputs) or any(
        headers(a) != headers(b) or headers(b) is None for a, b in zip(inputs, new_inputs)
    ):
        return readers
    affected = set()
    if inputs != new_inputs:
        affected.add("test_execute_" + suite)
    if suite == "tpch" and (inputs != new_inputs or expected != new_expected):
        affected.add("test_optimized_tpch")
    prefix = "tests/test_executor.py::TestExecutor::"
    return {t for t in readers if not index.test_ids[t].startswith(prefix)
            or index.test_ids[t][len(prefix):] in affected}


def select_patch(patch_text, index, all_tests):
    """Select with the original conservative rules, then, if some impact was
    widened only because it also runs at import time and importing the patch
    leaves all import-time state as on the base, select again trusting that."""
    index.deferred, index.patched, index.edited = False, {}, set()
    chosen, why = select(patch_text, index, all_tests, trust_import_state=False)
    if chosen and index.deferred and index.patched and import_state_unchanged(index.patched, frozenset(index.edited)):
        narrow, why2 = select(patch_text, index, all_tests, trust_import_state=True)
        # The narrowed set can add tests that read a setting through class
        # objects, which class-level impact never covered; keep it if cheaper.
        seconds = {t["number"]: t["seconds"] for t in index.tests}
        if sum(seconds[t] for t in narrow) < sum(seconds[t] for t in chosen):
            return narrow, why2 + ",import-state-same"
    return chosen, why


def select(patch_text, index, all_tests, trust_import_state=False):
    """Return (set of test numbers, reason) for one patch."""
    files = parse_patch(patch_text)
    if not files:
        return set(all_tests), "unparsed"
    chosen = set()
    index.extra = set()  # plausible but unproven: used only to fill idle worker time
    patched = {}
    need_import_check = False
    reasons = []
    for fd in files:
        old, new = fd["old"], fd["new"]
        path = old or new
        if fd["binary"]:
            if path in index.opens:
                chosen |= set(index.tests_of(index.opens[path]))
                reasons.append("binary-fixture")
                continue
            if path.startswith(("tests/", "sqlglot/")):
                return set(all_tests), "binary"
            continue
        if old and new and old != new:
            return set(all_tests), "rename"
        if is_doc(path) and path not in index.opens and path not in index.collect_opens:
            reasons.append("doc")
            continue
        if os.path.basename(path) == "conftest.py":
            return set(all_tests), "conftest"
        base_path = SRC / path
        if old is None:  # new file
            if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
                if path.startswith("sqlglot/"):
                    text = "\n".join(t for h in fd["hunks"] for tag, t in h[1] if tag == "+")
                    patched[path] = text
                    need_import_check = True
                reasons.append("new-py")
                continue
            if path.startswith("tests/fixtures/"):
                d = os.path.dirname(path) + "/"
                for o, sid in index.opens.items():
                    if o.startswith(d):
                        chosen |= set(index.tests_of(sid))
                reasons.append("new-fixture")
                continue
            return set(all_tests), "new-file"
        if not base_path.is_file():
            return set(all_tests), "missing-base"
        base_text = base_path.read_text(encoding="utf-8")
        if new is None:  # deleted
            if path.endswith(".py"):
                if path.startswith("sqlglot/"):
                    return set(all_tests), "deleted-module"
                chosen |= index.file_tests(path)
            if path in index.opens:
                chosen |= set(index.tests_of(index.opens[path]))
            for consumer in index.collect_opens.get(path, []):
                chosen |= index.tests_in.get(consumer, set())
            reasons.append("deleted")
            continue
        applied = apply_hunks(base_text, fd["hunks"])
        if applied is None:
            return set(all_tests), "apply-failed"
        new_text, changed, inserts, n2b = applied
        if not path.endswith(".py"):
            hit = False
            readers = set()
            if path in index.opens:
                readers.update(index.tests_of(index.opens[path]))
                hit = True
            for test_path in index.collect_opens.get(path, []):
                readers.update(index.tests_in.get(test_path, set()))
                hit = True
            if hit:
                if len(files) == 1:
                    readers = executor_fixture_readers(path, base_text, new_text, readers, index)
                chosen.update(readers)
                reasons.append("fixture")
                continue
            if path.startswith("tests/fixtures/"):
                reasons.append("unused-fixture")
                continue
            if path.startswith("sqlglot/") and path.endswith((".pyi", ".typed")):
                continue
            return set(all_tests), "non-python " + path
        if not path.startswith(("sqlglot/", "tests/")):
            if path.startswith("sqlglotc/"):
                chosen |= {t for t in all_tests if index.test_file[t] == "tests/test_sqlglotc.py"}
                reasons.append("sqlglotc")
                continue
            return set(all_tests), "outside " + path
        if len(files) == 1 and not index.unknown and path.startswith("sqlglot/dialects/"):
            ids = setting_readers(path, base_text, new_text, changed, inserts)
            if ids is not None:
                if check_patch({path: new_text}) != 0:
                    return set(all_tests), "import-fails"
                return {test["number"] for test in index.tests if test["id"] in ids}, "setting-read"
        base_tree = _BASE_TREES.get(path)
        if base_tree is None:
            try:
                base_tree = _BASE_TREES[path] = ast.parse(base_text)
            except SyntaxError:
                return set(all_tests), "base-unparsable"
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            if path.startswith("tests/"):
                chosen |= index.tests_in.get(path, set()) | index.module_tests(path)
                reasons.append("test-syntax")
                continue
            return set(all_tests), "syntax-error"
        if len(files) == 1 and not index.unknown and neutral_comparison(
            path, base_tree, new_tree, base_text, new_text, changed, inserts
        ):
            return set(), "equivalent-on-observed-types"
        if len(files) == 1 and not index.unknown:
            ids = observed_readers(path, base_tree, new_tree, base_text, new_text, changed, inserts)
            if ids is not None:
                return {test["number"] for test in index.tests if test["id"] in ids}, "expression-profile"
        fdiff = FileDiff(base_tree, new_tree, top_names(base_tree), (changed, inserts, n2b))
        imp = fdiff.imp
        if path.startswith("sqlglot/"):
            index.extra |= index.module_tests(path)
        if not (imp.lines or imp.lam_lines or imp.classes or imp.module or imp.import_check or imp.methods
                or imp.settings):
            reasons.append("ast-equal")
            continue
        patched[path] = new_text
        mod = module_name(path)
        lazy = index.lazy.get(path, set())
        imports = index.imports.get(path, set())
        widen = import_widen = False
        for lo, hi in imp.lines:
            chosen |= index.line_tests(path, lo, hi)
            rng = set(range(lo, hi + 1))
            if rng & lazy:
                widen = True
            if rng & imports and not (imp.module or imp.classes):
                # function body executed at import time: its effects can be global
                import_widen = True
                need_import_check = True
        for lo, hi in imp.lam_lines:
            chosen |= index.line_tests(path, lo, hi)
            if set(range(lo, hi + 1)) & lazy:
                widen = True
        for q, name, rng in imp.settings:
            deferred = []
            reached = setting_tests(index, path, q, name, deferred)
            index.edited.add((module_name(path), f"{q}.{name}"))
            if deferred and not trust_import_state:
                # Read while importing: keep the class-level impact unless
                # select_patch finds the import-time state identical to the base.
                index.deferred = True
                reached = None
            if reached is None:
                imp.lines.append(rng)
                fdiff.class_level(("class", q))
                reasons.append("setting-fallback")
            else:
                chosen |= reached
                reasons.append("setting")
        for q, name in imp.methods:
            reached = method_tests(index, path, q, name)
            if reached is None:
                fdiff.class_level(("class", q))
                reasons.append("method-fallback")
            else:
                chosen |= reached
                reasons.append("method")
        if imp.classes:
            spans = class_spans(base_tree)
            for q in imp.classes:
                chosen |= index.class_tests(mod + ":" + q)
                if q in spans:
                    chosen |= index.line_tests(path, *spans[q])
        if imp.classes:
            for q in imp.classes:
                chosen |= index.ref_tests(q)
        if path.startswith("tests/") and (imp.classes or imp.module or imp.import_check):
            # a test module or class body that changes can break collection of the
            # whole file and of every test file that imports it
            imp.module = True
        if import_widen and not (imp.module or widen):
            if path.startswith("sqlglot/") and trust_import_state:
                pass  # import-time state was compared equal in select_patch
            else:
                widen = True
                index.deferred |= path.startswith("sqlglot/")
        if imp.module or widen:
            chosen |= index.module_tests(path)
            if path.startswith("tests/"):
                chosen |= index.tests_in.get(path, set())
        if imp.import_check:
            need_import_check = True
        reasons.append("code" + ("+class" if imp.classes else "") + ("+module" if imp.module else "")
                       + ("+widen" if widen else ""))
    index.patched = patched
    if patched:
        probe = []
        if len(files) == 1 and not index.unknown and 1 < len(chosen) <= 8:
            group = [t for t in index.tests if t["number"] in chosen]
            if all(t["file"].startswith("tests/dialects/") for t in group) and sum(t["seconds"] for t in group) <= 0.30:
                probe = [t["id"] for t in group]
        if need_import_check or probe:
            status = check_patch(patched, probe)
            if status == 2:
                return set(), "verified-passing"
            if need_import_check and status == 1:
                return set(all_tests), "import-fails"
            if need_import_check and status == -1 and check_patch(patched) != 0:
                return set(all_tests), "import-fails"
    return chosen, ",".join(reasons)


# --------------------------------------------------------------------------- scheduling


def lpt(items, k):
    """items: [(ms, test)] sorted descending. Returns (makespan, workers)."""
    loads = [0] * k
    workers = [[] for _ in range(k)]
    for ms, t in items:
        i = loads.index(min(loads))
        loads[i] += ms
        workers[i].append(t)
    return max(loads), workers, loads


def improve(workers, loads, dur):
    """Move or swap tests off the busiest worker while it lowers the makespan."""
    for _ in range(200):
        b = loads.index(max(loads))
        best = None
        for i, t in enumerate(workers[b]):
            for o in range(len(workers)):
                if o == b:
                    continue
                if loads[o] + dur[t] < loads[b]:
                    new = max(loads[b] - dur[t], loads[o] + dur[t])
                    if best is None or new < best[0]:
                        best = (new, "move", i, o, None)
                for j, u in enumerate(workers[o]):
                    d = dur[t] - dur[u]
                    if d > 0 and loads[o] + d < loads[b]:
                        new = max(loads[b] - d, loads[o] + d)
                        if best is None or new < best[0]:
                            best = (new, "swap", i, o, j)
        if best is None or best[0] >= loads[b]:
            break
        _, kind, i, o, j = best
        t = workers[b][i]
        if kind == "move":
            workers[b].pop(i)
            workers[o].append(t)
            loads[b] -= dur[t]
            loads[o] += dur[t]
        else:
            u = workers[o][j]
            workers[b][i], workers[o][j] = u, t
            d = dur[t] - dur[u]
            loads[b] -= d
            loads[o] += d
    return workers, loads


def schedule(selected, dur):
    if not selected:
        return []
    items = sorted(((dur[t], t) for t in selected), reverse=True)
    total = sum(ms for ms, _ in items)
    bounds = []
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        lower = max(items[0][0], -(-total // k))
        bounds.append((lower + WORKER_MS * k, k, lower))
    best_key = None
    for bound, k, lower in sorted(bounds):
        if best_key is not None and (bound, k) >= best_key:
            break
        span_, workers, loads = lpt(items, k)
        if span_ > lower and len(items) <= 400:
            workers, loads = improve(workers, loads, dur)
            span_ = max(loads)
        key = (span_ + WORKER_MS * k, k)
        if best_key is None or key < best_key:
            best_key = key
            best = [w for w in workers if w]
    return best


def fill_idle(workers, extra, dur):
    """Add cheap extra tests to workers below the makespan, never raising it."""
    if not workers or not extra:
        return workers
    loads = [sum(dur[t] for t in w) for w in workers]
    top = max(loads)
    for t in sorted(extra, key=lambda t: (dur[t], t)):
        i = loads.index(min(loads))
        if loads[i] + dur[t] > top:
            break
        workers[i].append(t)
        loads[i] += dur[t]
    return workers


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_tests = [t["number"] for t in tests]
    remaining = set(all_tests)
    index = Index(INDEX, tests)
    index.test_ids = {t["number"]: t["id"] for t in tests}
    index.tests = tests
    index.test_file = {t["number"]: t["file"] for t in tests}
    index.tests_in = {}
    for t in tests:
        index.tests_in.setdefault(t["file"], set()).add(t["number"])
    total_cost = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen, why = select_patch(patch.read_text(encoding="utf-8", errors="replace"), index, all_tests)
            chosen |= set(index.unknown) if chosen and index.unknown else set()
        except Exception as e:  # noqa: BLE001 - never crash the batch; run everything
            chosen, why = set(all_tests), f"error {type(e).__name__}: {e}"
        workers = schedule(chosen, dur)
        extra = index.extra - chosen
        workers = fill_idle(workers, extra, dur)
        if workers:
            workers = fill_idle(workers, remaining - chosen - extra, dur)
        cost = (max(sum(dur[t] for t in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total_cost += cost
        log(f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers, {cost / 1000:.3f}s [{why}]")
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    log(f"predicted simulated seconds without misses: {total_cost / 1000:.3f}")


if __name__ == "__main__":
    main()
