"""Record operand types at identity and singleton comparisons in the pinned suite."""
import ast
import builtins
import enum
import importlib.machinery
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ["BR_ROOT"]).resolve()
OUT = Path(os.environ["BR_OUT"])
seen = set()


def owner(cls, name):
    for base in cls.__mro__:
        if name in vars(base):
            return f"{base.__module__}.{base.__qualname__}"


def observe(site, value):
    cls = type(value)
    is_enum = isinstance(value, enum.Enum)
    key = (site, cls, id(value) if is_enum else 0)
    if key not in seen:
        seen.add(key)
        canonical = False
        if is_enum and isinstance(value, str):
            raw = value._value_
            canonical = (type(raw) is str and str.__eq__(value, raw)
                         and cls._value2member_map_.get(raw) is value)
        row = [site, f"{cls.__module__}.{cls.__qualname__}", owner(cls, "__eq__"),
               owner(cls, "__ne__"), canonical]
        # Persist immediately so process pools and pytest workers both contribute.
        with (OUT / f"{os.getpid()}.jsonl").open("a") as stream:
            print(json.dumps(row), file=stream)
    return value


builtins._br_trace_operand = observe
original = importlib.machinery.SourceFileLoader.get_code


def instrument(loader, name):
    if name != "sqlglot" and not name.startswith("sqlglot."):
        return original(loader, name)
    path = Path(loader.path).resolve()
    if not path.is_relative_to(ROOT):
        return original(loader, name)
    tree = ast.parse(path.read_bytes(), str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
            continue
        left, right = node.left, node.comparators[0]
        singleton = any(isinstance(op, ast.Constant) and op.value is None for op in (left, right))
        if not singleton and not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            continue
        for side, operand in enumerate((left, right)):
            site = f"{path.relative_to(ROOT)}:{node.lineno}:{node.col_offset}:{side}"
            call = ast.Call(ast.Name("_br_trace_operand", ast.Load()), [ast.Constant(site), operand], [])
            if side == 0:
                node.left = ast.copy_location(call, operand)
            else:
                node.comparators[0] = ast.copy_location(call, operand)
    return compile(ast.fix_missing_locations(tree), str(path), "exec", dont_inherit=True)


importlib.machinery.SourceFileLoader.get_code = instrument
