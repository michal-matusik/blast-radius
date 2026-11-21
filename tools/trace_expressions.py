"""Observe ordered-comparison operands and registered callback invocations."""
import ast
import builtins
import functools
import importlib.machinery
import inspect
import json
import os
import sys
from pathlib import Path

from trace_settings import ROOT, _inline_pools
import trace_settings
from trace_settings import pytest_runtest_protocol

values = {}
callbacks = {}
calls = {}


def observe(site, value):
    domain = values.setdefault(site, set())
    if None not in domain:
        if type(value) is int and value.bit_length() <= 64:
            domain.add(value)
        elif type(value) is tuple and len(value) <= 8 and all(type(n) is int and n.bit_length() <= 64 for n in value):
            domain.add(value)
        else:
            domain.add(None)
        if len(domain) > 32:
            values[site] = {None}
    return value


@functools.lru_cache(None)
def pure_factory(fn):
    if not inspect.isfunction(fn):
        return False
    body = ast.parse(inspect.getsource(fn)).body[0].body
    if len(body) != 2:
        return False
    inner, ret = body
    return (isinstance(inner, ast.FunctionDef) and not inner.decorator_list
            and not inner.args.defaults and not any(inner.args.kw_defaults)
            and isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name)
            and ret.value.id == inner.name)


def make(site, factory, *args, **kwargs):
    fn = factory(*args, **kwargs)
    if inspect.isfunction(fn) and inspect.isfunction(factory):
        try:
            supported = pure_factory(factory)
        except (SyntaxError, OSError, ValueError):
            supported = False
        if supported:
            callbacks[id(fn)] = (site, fn)
            calls.setdefault(site, set())
    return fn


def on_call(code, offset, fn, arg):
    entry = callbacks.get(id(fn))
    if entry:
        calls[entry[0]].add(trace_settings.current)


def pytest_configure(config):
    _inline_pools()
    builtins._br_comparison_operand = observe
    builtins._br_callback_factory = make
    mon = sys.monitoring
    mon.use_tool_id(3, "callback_trace")
    mon.register_callback(3, mon.events.CALL, on_call)
    original = importlib.machinery.SourceFileLoader.get_code

    def load(loader, name):
        path = Path(loader.path).resolve()
        if not path.is_relative_to(ROOT) or not name.startswith("sqlglot"):
            return original(loader, name)
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_bytes(), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(
                node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
            ):
                try:
                    literal = ast.literal_eval(node.comparators[0])
                except (ValueError, TypeError):
                    continue
                if type(literal) not in (int, tuple):
                    continue
                site = f"{rel}:{node.lineno}:{node.col_offset}"
                node.left = ast.copy_location(ast.Call(ast.Name("_br_comparison_operand", ast.Load()),
                                                      [ast.Constant(site), node.left], []), node.left)
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                        continue
                    for val in stmt.value.values:
                        if not isinstance(val, ast.Call) or not isinstance(val.func, ast.Name):
                            continue
                        site = f"{rel}:{val.lineno}:{val.col_offset}"
                        val.args = [ast.Constant(site), val.func, *val.args]
                        val.func = ast.Name("_br_callback_factory", ast.Load())
        compiled = compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)
        # Local CALL monitoring only in sqlglot code. External call sites are
        # also tracked below by enabling the event globally.
        return compiled

    importlib.machinery.SourceFileLoader.get_code = load
    mon.set_events(3, mon.events.CALL)


def pytest_sessionfinish(session):
    sys.monitoring.set_events(3, 0)
    dest = Path(os.environ["BR_OUT"]) / f"expr-{os.getpid()}.json"
    dest.write_text(json.dumps([{k: list(v) for k, v in values.items()},
                                {k: sorted(v) for k, v in calls.items()}]))
