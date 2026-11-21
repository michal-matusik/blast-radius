"""Restrict isolated literal edits to observed readers of their original value."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def profiles():
    path = Path(__file__).parent / "data" / "settings.json.gz"
    return json.loads(gzip.decompress(path.read_bytes()))


def readers(path, old_text, new_text, changed, inserts):
    if len(changed) != 1 or len(inserts) != 1:
        return None
    line, = changed
    profile = profiles().get(f"{path}:{line}")
    if profile is None:
        return None
    old, new = old_text.split("\n"), new_text.split("\n")
    i = line - 1
    if old[:i] != new[:i] or old[line:] != new[line:]:
        return None
    if old[i][:len(old[i]) - len(old[i].lstrip())] != new[i][:len(new[i]) - len(new[i].lstrip())]:
        return None
    try:
        original, = ast.parse(old[i].strip()).body
        updated, = ast.parse(new[i].strip()).body
        if type(original) is not type(updated) or not isinstance(original, (ast.Assign, ast.AnnAssign)):
            return None
        before, after = ast.literal_eval(original.value), ast.literal_eval(updated.value)
        updated.value = original.value
        if ast.dump(original) != ast.dump(updated):
            return None
    except (SyntaxError, ValueError, TypeError):
        return None
    if type(before) is int and type(after) is int:
        keys = [""]
    elif isinstance(before, dict) and isinstance(after, dict) and list(before) == list(after):
        # Compare literal syntax too: True, 1, and 1.0 have different behavior.
        orig = ast.parse(old[i].strip()).body[0].value
        edit = ast.parse(new[i].strip()).body[0].value
        if len(orig.keys) != len(before) or len(edit.keys) != len(after):
            return None
        keys = [k for k, a, b in zip(before, orig.values, edit.values) if ast.dump(a) != ast.dump(b)]
        if any(not isinstance(after[k], tuple) for k in keys):
            return None
    else:
        return None
    ids = set(profile.get("*", []))
    for key in keys:
        ids.update(profile.get(key, []))
    return None if "*" in ids else ids
