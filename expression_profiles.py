"""Check isolated source edits against operand and call-site observations."""
import ast
import gzip
import json
import operator
from functools import lru_cache
from pathlib import Path

ORDER = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


@lru_cache(maxsize=1)
def profiles():
    path = Path(__file__).parent / "data" / "expressions.json.gz"
    domains, callbacks = json.loads(gzip.decompress(path.read_bytes()))
    grouped = {}
    for kind, sites in (("order", domains), ("callback", callbacks)):
        for location, observations in sites.items():
            site, col = location.rsplit(":", 1)
            grouped.setdefault(site, []).append((int(col), kind, observations))
    return grouped


def ordered_equivalent(old, new, values):
    if not isinstance(new, ast.Compare) or len(new.ops) != 1 or type(new.ops[0]) not in ORDER:
        return False
    if ast.dump(old.left) != ast.dump(new.left):
        return False
    before = ast.literal_eval(old.comparators[0])
    after = ast.literal_eval(new.comparators[0])
    for v in values:
        value = tuple(v) if isinstance(v, list) else v
        if ORDER[type(old.ops[0])](value, before) != ORDER[type(new.ops[0])](value, after):
            return False
    return bool(values)


def callback_change(old, new):
    if not isinstance(new, ast.Call) or ast.dump(old.func) != ast.dump(new.func):
        return False
    if len(old.args) != len(new.args) or [k.arg for k in old.keywords] != [k.arg for k in new.keywords]:
        return False
    for call in (old, new):
        for arg in call.args + [k.value for k in call.keywords]:
            ast.literal_eval(arg)
    return True


def observed_readers(path, old_tree, new_tree, old_text, new_text, changed, inserts):
    if len(changed) != 1 or len(inserts) != 1:
        return None
    line, = changed
    sites = profiles().get(f"{path}:{line}")
    if not sites:
        return None
    before, after = old_text.split("\n"), new_text.split("\n")
    i = line - 1
    if before[:i] != after[:i] or before[line:] != after[line:]:
        return None
    left, right = before[i].encode(), after[i].encode()
    for col, kind, values in sites:
        cls = ast.Compare if kind == "order" else ast.Call
        def find(tree):
            return next((n for n in ast.walk(tree) if isinstance(n, cls) and n.lineno == line
                         and n.end_lineno == line and n.col_offset == col), None)
        old, new = find(old_tree), find(new_tree)
        if old is None or new is None or left[:col] != right[:col] or left[old.end_col_offset:] != right[new.end_col_offset:]:
            continue
        try:
            if kind == "order" and ordered_equivalent(old, new, values):
                return set()
            if kind == "callback" and callback_change(old, new):
                return set(values)
        except (ValueError, TypeError, KeyError):
            continue
    return None
