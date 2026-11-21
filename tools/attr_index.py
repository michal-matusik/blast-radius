"""Index where the pinned sources read names assigned in class bodies.

Runs during `docker build` on the pinned sources only (no labels):

    python attr_index.py SRC OUT.marshal

The result maps each class-level name to [(file, line, via_self)] for every
attribute or bare-name load of it in sqlglot/ and tests/. `via_self` marks
`self.NAME`, which reads the instance's class hierarchy.
"""
import ast
import marshal
import sys
from pathlib import Path


def class_level_names(trees):
    names = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] \
                        if isinstance(stmt, ast.AnnAssign) else []
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    trees = {}
    for path in sorted(list((src / "sqlglot").rglob("*.py")) + list((src / "tests").rglob("*.py"))):
        try:
            trees[path.relative_to(src).as_posix()] = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
    names = class_level_names(trees)
    sites = {}
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in names and isinstance(node.ctx, ast.Load):
                via_self = isinstance(node.value, ast.Name) and node.value.id == "self"
                sites.setdefault(node.attr, set()).add((rel, node.lineno, via_self))
            elif isinstance(node, ast.Name) and node.id in names and isinstance(node.ctx, ast.Load):
                sites.setdefault(node.id, set()).add((rel, node.lineno, False))
    out.write_bytes(marshal.dumps({k: sorted(v) for k, v in sites.items()}))


if __name__ == "__main__":
    main()
