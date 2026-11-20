"""Generate a test-file dependency index from the pinned sqlglot checkout."""

import argparse
import ast
from importlib.util import resolve_name
import json
from pathlib import Path
import subprocess

REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DIALECT_PACKAGE = "sqlglot.dialects"
FIXTURES = "tests/fixtures/"
FIXTURE_LOADERS = {"load_sql_fixtures", "load_sql_fixture_pairs"}


def module_name(path):
    parts = Path(path).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(tree, module, package, modules, dialects):
    edges = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            edges.add(base)
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if base == DIALECT_PACKAGE:
                    candidate = dialects.get(alias.name.casefold(), candidate)
                edges.add(candidate)
    # Loading a submodule also executes its package initializers.
    for name in list(edges) + [module]:
        pieces = name.split(".")
        edges.update(".".join(pieces[:i]) for i in range(1, len(pieces)))
    return (edges & modules.keys()) - {module}


def test_dialects(tree, module, dialects):
    edges = set()
    named_dialect = dialects.get(module.rsplit(".test_", 1)[-1])
    if named_dialect:
        edges.add(named_dialect)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in (node.value.casefold(), node.value.removeprefix(DIALECT_PACKAGE + ".")):
                if name in dialects:
                    edges.add(dialects[name])
        elif isinstance(node, (ast.For, ast.comprehension)):
            if any(
                isinstance(part, ast.Name) and part.id in {"Dialects", "DIALECT_MODULE_NAMES"}
                or isinstance(part, ast.Attribute) and part.attr in {"classes", "DIALECT_MODULE_NAMES"}
                for part in ast.walk(node.iter)
            ):
                edges.update(dialects.values())
    return edges


def fixture_reads(tree, fixtures):
    edges = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in FIXTURE_LOADERS:
            continue
        argument = node.args[0] if node.args else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            edges.add(FIXTURES + argument.value)
        elif isinstance(argument, ast.JoinedStr) and argument.values and isinstance(argument.values[0], ast.Constant):
            prefix = FIXTURES + argument.values[0].value
            edges.update(path for path in fixtures if path.startswith(prefix))
        else:
            edges.update(fixtures)
    return edges & fixtures


def closure(seeds, graph):
    visited = set()
    queue = list(seeds)
    while queue:
        module = queue.pop()
        if module not in visited:
            visited.add(module)
            queue.extend(graph[module] - visited)
    return visited


def build(root):
    modules = {
        module_name(path.relative_to(root).as_posix()): path.relative_to(root).as_posix()
        for directory in ("sqlglot", "tests")
        for path in (root / directory).rglob("*.py")
    }
    trees = {
        module: ast.parse((root / path).read_bytes(), filename=path)
        for module, path in modules.items()
    }
    dialects = {
        module.rsplit(".", 1)[-1]: module
        for module in modules
        if module.startswith(DIALECT_PACKAGE + ".") and module != DIALECT_PACKAGE + ".dialect"
    }
    graphs = {}
    for module, path in modules.items():
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        graphs[module] = imports(trees[module], module, package, modules, dialects)
    fixtures = {
        path.relative_to(root).as_posix()
        for path in (root / FIXTURES).rglob("*")
        if path.is_file()
    }
    fixture_edges = {
        module: fixture_reads(tree, fixtures)
        for module, tree in trees.items()
        if module.startswith("tests.")
    }
    index = {}
    for module, test_path in sorted(modules.items()):
        if not test_path.startswith("tests/") or not Path(test_path).name.startswith("test_"):
            continue
        dependencies = closure({module} | test_dialects(trees[module], module, dialects), graphs)
        paths = {modules[dependency] for dependency in dependencies}
        for dependency in dependencies:
            paths.update(fixture_edges.get(dependency, ()))
        for path in paths:
            index.setdefault(path, []).append(test_path)
    return {"revision": REVISION, "test_files_by_path": index}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    git = ["git", "-C", str(root)]
    if subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip() != REVISION:
        parser.error(f"checkout must be at {REVISION}")
    if subprocess.check_output([*git, "status", "--porcelain", "--untracked-files=all"]):
        parser.error("checkout must be clean")
    args.output.write_text(json.dumps(build(root), sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
