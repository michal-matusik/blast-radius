"""Pytest plugin: record what every test touches in the base sqlglot checkout.

Per test it records
  lines   - (file, line) executed in sqlglot/ and tests/ (sys.monitoring LINE events)
  classes - sqlglot classes in the MRO of every object created through a sqlglot __init__
  opens   - repository files opened (fixtures, data files)
Lines executed in a test's setup phase (setUpClass) are recorded separately: a
failure there errors every test of the class.
"""
import importlib
import json
import os
import pkgutil
import sys

ROOT = os.path.realpath(os.environ.get("BR_ROOT", os.getcwd()))
OUT = os.environ["BR_OUT"]
PREFIXES = (os.path.join(ROOT, "sqlglot") + os.sep, os.path.join(ROOT, "tests") + os.sep)
TOOL = 3
MON = sys.monitoring

_lines = set()
_types = set()
_opens = set()
_active = False
_records = {}


def _line(code, line):
    if _active and code.co_filename.startswith(PREFIXES):
        _lines.add((code.co_filename, line))
    return MON.DISABLE


def _audit(event, args):
    if _active and event == "open" and args and isinstance(args[0], str):
        path = args[0]
        if not path.endswith((".pyc", ".py")) or "/fixtures/" in path:
            real = os.path.realpath(path) if not path.startswith(ROOT) else path
            if real.startswith(ROOT):
                _opens.add(real)


def _wrap_inits():
    import functools

    seen = set()

    def visit(cls):
        if cls in seen:
            return
        seen.add(cls)
        init = cls.__dict__.get("__init__")
        if init is not None and type(init).__name__ == "function":

            def make(orig):
                @functools.wraps(orig)
                def __init__(self, *a, **k):
                    if _active:
                        _types.add(type(self))
                    return orig(self, *a, **k)

                return __init__

            try:
                cls.__init__ = make(init)
            except (TypeError, AttributeError):
                pass
        for v in list(cls.__dict__.values()):
            if isinstance(v, type) and getattr(v, "__module__", "").startswith("sqlglot"):
                visit(v)

    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for v in list(vars(mod).values()):
            if isinstance(v, type) and getattr(v, "__module__", None) == name:
                visit(v)


_import_lines = set()


def _roundtrip(obj):
    """Pickle and unpickle, as crossing a process boundary would."""
    import pickle

    return pickle.loads(pickle.dumps(obj))


class InlineExecutor:
    """Stand-in for ProcessPoolExecutor: run each task now, in this process,
    so its lines are attributed to the test that submitted it."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            args, kwargs = _roundtrip((args, kwargs))
            fut.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [_roundtrip(fn(*_roundtrip(a))) for a in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _inline_pools():
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = InlineExecutor


def pytest_configure(config):
    global _active
    _inline_pools()
    MON.use_tool_id(TOOL, "br_trace")
    MON.register_callback(TOOL, MON.events.LINE, _line)
    MON.set_events(TOOL, MON.events.LINE)
    _active = True
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    _active = False
    _import_lines.update(_lines)
    _lines.clear()
    _wrap_inits()
    sys.addaudithook(_audit)


def _class_keys(types):
    keys = set()
    for t in types:
        for c in t.__mro__:
            m = getattr(c, "__module__", "")
            if m.startswith("sqlglot"):
                keys.add(m + ":" + c.__qualname__)
    return keys


def _new():
    return {"lines": set(), "setup": set(), "classes": set(), "opens": set(),
            "setup_classes": set(), "setup_opens": set()}


def _rel(p):
    return os.path.relpath(p, ROOT)


def _flush(nodeid, phase):
    global _lines, _types, _opens
    rec = _records.setdefault(nodeid, _new())
    pre = "setup_" if phase == "setup" else ""
    rec["setup" if pre else "lines"].update(_lines)
    rec[pre + "classes"].update(_class_keys(_types))
    rec[pre + "opens"].update(_opens)
    _lines, _types, _opens = set(), set(), set()


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    global _active
    MON.restart_events()
    _lines.clear(); _types.clear(); _opens.clear()
    _active = True
    yield
    _active = False
    _flush(item.nodeid, "setup")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global _active
    _active = True
    yield
    _active = False
    _flush(item.nodeid, "call")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    global _active
    _active = True
    yield
    _active = False
    _flush(item.nodeid, "teardown")


def pytest_runtest_logreport(report):
    rec = _records.setdefault(report.nodeid, _new())
    if report.failed:
        rec.setdefault("failed", []).append(report.when)


def pytest_sessionfinish(session):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    files = {}

    def fid(p):
        return files.setdefault(_rel(p), len(files))

    out = {}
    for nodeid, rec in _records.items():
        out[nodeid] = {
            "lines": sorted((fid(f), l) for f, l in rec["lines"]),
            "setup": sorted((fid(f), l) for f, l in rec["setup"]),
            "classes": sorted(rec["classes"]),
            "opens": sorted(_rel(p) for p in rec["opens"]),
            "setup_classes": sorted(rec["setup_classes"]),
            "setup_opens": sorted(_rel(p) for p in rec["setup_opens"]),
            "failed": rec.get("failed", []),
        }
    with open(os.path.join(OUT, f"trace-{wid}.json"), "w") as f:
        imp = sorted((fid(f), l) for f, l in _import_lines)
        json.dump({"files": sorted(files, key=files.get), "tests": out, "import": imp}, f)
