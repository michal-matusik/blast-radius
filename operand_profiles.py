"""Recognize isolated comparison edits equivalent on observed operand domains."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path

# Built-in comparisons with None return identity equality for these owners.
NONE_COMPARISON = {
    "builtins.NoneType", "builtins.object", "builtins.str", "builtins.bytes", "builtins.int",
    "builtins.float", "builtins.list", "builtins.tuple", "builtins.dict",
    "builtins.set", "builtins.frozenset",
}
ALTERNATES = {b"is": b"==", b"==": b"is", b"is not": b"!=", b"!=": b"is not"}


@lru_cache(maxsize=1)
def profiles():
    data = Path(__file__).parent / "data" / "operands.json.gz"
    return json.loads(gzip.decompress(data.read_bytes()))


def equivalent_types(left, right):
    # Profiles hold the concrete type, the owners of __eq__ and __ne__, and
    # whether a string enum member is canonical for its exact string payload.
    types, _ = profiles()
    for li in left:
        for ri in right:
            a, b = types[li], types[ri]
            if all(t[1] == t[2] == "builtins.object" for t in (a, b)):
                continue
            if a[0] == "builtins.NoneType" or b[0] == "builtins.NoneType":
                if all(t[1] in NONE_COMPARISON and t[2] in NONE_COMPARISON for t in (a, b)):
                    continue
            if a[0] == b[0] and all(t[3] and t[1] == t[2] == "builtins.str" for t in (a, b)):
                continue
            return False
    return bool(left and right)


def neutral_comparison(path, base_tree, new_tree, base_text, new_text, changed, inserts):
    if len(changed) != 1 or len(inserts) != 1:
        return False
    line, = changed
    before = base_text.split("\n")
    after = new_text.split("\n")
    i = line - 1
    if len(before) != len(after) or before[:i] != after[:i] or before[line:] != after[line:]:
        return False
    old, new = before[i].encode(), after[i].encode()
    if not any(word in old and replacement in new for word, replacement in ALTERNATES.items()):
        return False
    for node in ast.walk(base_tree):
        if not isinstance(node, ast.Compare) or node.lineno != line or len(node.ops) != 1:
            continue
        if node.end_lineno != line:
            continue
        start, end = node.left.end_col_offset, node.comparators[0].col_offset
        if not new.startswith(old[:start]) or not new.endswith(old[end:]):
            continue
        new_end = len(new) - len(old[end:])
        if new_end < start or ALTERNATES.get(old[start:end].strip()) != new[start:new_end].strip():
            continue
        # The caller parses the edited file. Require operand boundaries as well,
        # so "x == None" cannot become a name like "xisNone".
        if not any(isinstance(n, ast.Compare) and n.lineno == line and
                   n.col_offset == node.col_offset and len(n.ops) == 1 and
                   ast.dump(n.left) == ast.dump(node.left) and
                   ast.dump(n.comparators[0]) == ast.dump(node.comparators[0])
                   for n in ast.walk(new_tree)):
            continue
        _, sites = profiles()
        loc = f"{path}:{line}:{node.col_offset}"
        return equivalent_types(sites.get(loc + ":0", []), sites.get(loc + ":1", []))
    return False
