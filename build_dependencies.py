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


def import_base(node, package):
    base = node.module or ""
    return resolve_name("." * node.level + base, package) if node.level else base


def imports(tree, module, package, modules, dialects):
    edges = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, package)
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


def loaded_names(node):
    return {
        item.id for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        if prefix:
            return f"{prefix}.{node.attr}"
    return None


def imported_symbols(tree, module, package, names):
    named = set()
    qualified = set()
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            qualified.update(alias.asname or module for alias in node.names if alias.name == module)
        elif isinstance(node, ast.ImportFrom):
            base = import_base(node, package)
            if base == module:
                named.update(alias.name for alias in node.names)
            else:
                qualified.update(
                    alias.asname or alias.name for alias in node.names
                    if f"{base}.{alias.name}" == module
                )
    if qualified:
        parents = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}
        for node in nodes:
            if not isinstance(node, (ast.Attribute, ast.Name)) or dotted_name(node) not in qualified:
                continue
            parent = parents.get(id(node))
            if not isinstance(parent, ast.Attribute) or parent.value is not node:
                return None  # Module value escapes static attribute resolution.
            named.add(parent.attr)
    return named if named and named <= names else None


def symbol_references(module, trees, packages, graphs, calls):
    names = calls.keys()
    references = {}
    for importer, edges in graphs.items():
        if module in edges:
            named = imported_symbols(trees[importer], module, packages[importer], names)
            references[importer] = closure(named, calls) if named else names
    return references


def symbol_test_files(name, file_tests, test_dependencies, references):
    consumers = [
        path for path in file_tests
        if any(name in references.get(dependency, ()) for dependency in test_dependencies[path])
    ]
    # Preserve the file fallback when consumers are unknown or indistinguishable.
    return sorted(consumers) if 0 < len(consumers) < len(file_tests) else []


def method_test_dependencies(modules, trees, packages, graphs, dialects, tests):
    """Avoid attributing sibling test classes to a consumer of the shared Validator."""
    helper = "tests.dialects.test_dialect"
    if helper not in trees:
        return tests
    tree = trees[helper]
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if "Validator" not in classes:
        return tests
    # Keep the helper's bodies, eager module code, and everything evaluated
    # when sibling classes are defined. Sibling test method bodies do not run.
    scope = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name != "Validator":
            scope.extend([*node.bases, *node.keywords, *node.decorator_list])
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope.extend([*member.decorator_list, member.args])
                    if member.returns:
                        scope.append(member.returns)
                else:
                    scope.append(member)
        else:
            scope.append(node)
    names = loaded_names(ast.Module(body=scope, type_ignores=[]))
    filtered = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == DIALECT_PACKAGE
            or node.module.startswith((DIALECT_PACKAGE + ".", "sqlglot.parsers.", "sqlglot.generators."))
        ):
            aliases = [
                alias for alias in node.names
                if (alias.asname or alias.name) in names or alias.name == "*"
            ]
            if aliases:
                filtered.append(ast.ImportFrom(module=node.module, names=aliases, level=node.level))
        else:
            filtered.append(node)
    # Keep every other import unchanged, including helper calls and fixtures.
    helper_edges = imports(ast.Module(body=filtered, type_ignores=[]), helper,
                           packages[helper], modules, dialects)
    if helper_edges == graphs[helper]:
        return tests
    scoped_graph = {**graphs, helper: helper_edges}

    result = {}
    for path, dependencies in tests.items():
        test = module_name(path)
        importers = {module for module in dependencies if helper in graphs[module]}
        if test == helper or not importers or any(
            imported_symbols(trees[module], helper, packages[module], classes.keys()) != {"Validator"}
            for module in importers
        ):
            result[path] = dependencies
        else:
            result[path] = closure({test} | test_dialects(trees[test], test, dialects), scoped_graph)
    return result


def method_index(modules, trees, packages, graphs, index, test_dependencies, root):
    """Resolve method consumers through the owning class and its relationships."""
    result = {}
    for module, path in modules.items():
        if not path.startswith(("sqlglot/parsers/", "sqlglot/generators/")) or path not in index:
            continue
        tree = trees[module]
        definitions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        classes = {
            name: node for name, node in definitions.items()
            if isinstance(node, ast.ClassDef) and not (node.decorator_list or node.keywords)
        }
        if not classes:
            continue
        # An exported function, sibling class, or subclass can reach the owner.
        # Keep dependencies even when an override exists: it can delegate via super().
        names = definitions.keys()
        calls = {name: loaded_names(node) & names for name, node in definitions.items()}
        references = symbol_references(module, trees, packages, graphs, calls)
        module_uses = set()
        for node in tree.body:
            if node not in definitions.values():
                module_uses.update(loaded_names(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for expression in [*node.decorator_list, node.args, node.returns]:
                    if expression:
                        module_uses.update(loaded_names(expression))
            elif node.decorator_list or node.keywords:
                module_uses.update(loaded_names(node))
        eager = closure(module_uses & names, calls)

        spans = []
        for name, owner in classes.items():
            if name in eager:
                continue
            consumers = symbol_test_files(name, index[path], test_dependencies, references)
            if not consumers:
                continue
            source = (root / path).read_bytes().splitlines()
            for node in owner.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.decorator_list or node.name.startswith("__") or node.body[0].lineno <= node.lineno:
                    continue
                first = node.body[0].lineno
                # Require inserted code to stay below the method's signature
                # indentation. A dedent could change the surrounding class.
                signature = source[node.lineno - 1]
                indent = len(signature) - len(signature.lstrip(b" \t"))
                spans.append([first, node.end_lineno, consumers, indent + 1])
        if spans:
            result[path] = spans
    return result


def symbol_index(modules, trees, packages, graphs, index, test_dependencies):
    result = {}
    for module, path in modules.items():
        if not path.startswith("sqlglot/") or path.endswith("/__init__.py"):
            continue
        functions = {
            node.name: node for node in trees[module].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.decorator_list
        }
        if len(functions) < 2 or path not in index:
            continue
        names = functions.keys()
        # Module-scope uses include registries, aliases, classes and decorators.
        # Those references cannot be separated from importing the module.
        module_uses = set()
        for node in trees[module].body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
                module_uses.update(loaded_names(node))
            else:
                module_uses.update(loaded_names(node.args))
                if node.returns:
                    module_uses.update(loaded_names(node.returns))
        calls = {name: loaded_names(node) & names for name, node in functions.items()}

        references = symbol_references(module, trees, packages, graphs, calls)

        spans = []
        for name, function in functions.items():
            if name in closure(module_uses & names, calls):
                continue
            consumers = symbol_test_files(name, index[path], test_dependencies, references)
            if not consumers:
                continue
            first = function.body[0].lineno
            if first > function.lineno:
                spans.append([first, function.end_lineno, consumers])
        if spans:
            result[path] = spans
    return result


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
    packages = {
        module: module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for module, path in modules.items()
    }
    graphs = {
        module: imports(tree, module, packages[module], modules, dialects)
        for module, tree in trees.items()
    }
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
    test_dependencies = {}
    for module, test_path in sorted(modules.items()):
        if not test_path.startswith("tests/") or not Path(test_path).name.startswith("test_"):
            continue
        dependencies = closure({module} | test_dialects(trees[module], module, dialects), graphs)
        test_dependencies[test_path] = dependencies
        paths = {modules[dependency] for dependency in dependencies}
        for dependency in dependencies:
            paths.update(fixture_edges.get(dependency, ()))
        for path in paths:
            index.setdefault(path, []).append(test_path)
    return {
        "revision": REVISION,
        "test_files_by_path": index,
        "symbols_by_path": symbol_index(modules, trees, packages, graphs, index, test_dependencies),
        "methods_by_path": method_index(
            modules, trees, packages, graphs, index,
            method_test_dependencies(modules, trees, packages, graphs, dialects, test_dependencies),
            root,
        ),
    }


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
