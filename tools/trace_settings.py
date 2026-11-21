"""Pytest plugin for reads of numeric settings and tuple-valued configuration maps."""
import ast
import builtins
import importlib.machinery
import json
import os
from pathlib import Path

import pytest

from br_trace import _inline_pools

ROOT = Path(os.environ["BR_ROOT"])
sites = {}
observations = {}
current = "*"


def record(site, key):
    observations.setdefault((site, key), set()).add(current)


class SettingsDict(dict):
    def __init__(self, site, values):
        self.site = site
        super().__init__(values)
        observations.setdefault((site, ""), set())

    def __getitem__(self, key):
        if key in self:
            record(self.site, key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self[key] if key in self else default

    def __iter__(self):
        return super().__iter__()


def observe_all(method):
    def observed(self, *args):
        record(self.site, "*")
        return method(self, *args)
    return observed


for method in ("items", "values", "copy", "pop", "popitem", "setdefault", "__eq__",
               "__ne__", "__repr__", "__str__", "__or__", "__ror__", "__reduce__"):
    setattr(SettingsDict, method, observe_all(getattr(dict, method)))


def setting(obj, name):
    value = getattr(obj, name)
    cls = obj if isinstance(obj, type) else type(obj)
    for owner in cls.__mro__:
        if name in vars(owner):
            site = sites.get((owner.__module__, owner.__qualname__, name))
            if site:
                record(site, "")
            break
    return value


def settings(root):
    for path in (root / "sqlglot/dialects").glob("*.py"):
        rel = path.relative_to(root)
        mod = ".".join(rel.with_suffix("").parts)
        tree = ast.parse(path.read_bytes())
        for outer in tree.body:
            if not isinstance(outer, ast.ClassDef):
                continue
            classes = [(outer, outer.name)] + [(c, outer.name + "." + c.name)
                       for c in outer.body if isinstance(c, ast.ClassDef)]
            for cls, name in classes:
                for stmt in cls.body:
                    if isinstance(stmt, ast.AnnAssign):
                        target = stmt.target
                    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                        target = stmt.targets[0]
                    else:
                        continue
                    if not isinstance(target, ast.Name) or not target.id.isupper():
                        continue
                    try:
                        value = ast.literal_eval(stmt.value)
                    except (TypeError, ValueError):
                        continue
                    site = f"{rel}:{stmt.lineno}"
                    if type(value) is int:
                        yield mod, name, target.id, site, False
                    if isinstance(value, dict) and value and all(
                        isinstance(k, str) and k not in ("", "*") and isinstance(v, tuple)
                        for k, v in value.items()
                    ):
                        yield mod, name, target.id, site, True


def pytest_configure(config):
    _inline_pools()
    maps = {}
    names = set()
    for mod, cls, name, site, mapping in settings(ROOT):
        if mapping:
            maps[site] = True
        else:
            sites[mod, cls, name] = site
            names.add(name)
    builtins._br_config_map = SettingsDict
    builtins._br_config_read = setting
    original = importlib.machinery.SourceFileLoader.get_code

    def load(loader, name):
        path = Path(loader.path).resolve()
        if not path.is_relative_to(ROOT) or not name.startswith(("sqlglot", "tests")):
            return original(loader, name)
        rel = path.relative_to(ROOT)

        class Rewrite(ast.NodeTransformer):
            def visit_Attribute(self, node):
                self.generic_visit(node)
                if node.attr in names and isinstance(node.ctx, ast.Load):
                    call = ast.Call(ast.Name("_br_config_read", ast.Load()), [node.value, ast.Constant(node.attr)], [])
                    return ast.copy_location(call, node)
                return node

            def visit_Assign(self, node):
                self.generic_visit(node)
                site = f"{rel}:{node.lineno}"
                if site in maps:
                    node.value = ast.copy_location(ast.Call(ast.Name("_br_config_map", ast.Load()),
                                                  [ast.Constant(site), node.value], []), node.value)
                return node

            visit_AnnAssign = visit_Assign

        tree = Rewrite().visit(ast.parse(path.read_bytes(), str(path)))
        return compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)
    importlib.machinery.SourceFileLoader.get_code = load


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global current
    current = item.nodeid
    yield
    current = "*"


def pytest_sessionfinish(session):
    rows = [[site, key, sorted(ids)] for (site, key), ids in observations.items()]
    dest = Path(os.environ["BR_OUT"]) / f"settings-{os.getpid()}.json"
    dest.write_text(json.dumps(rows))
