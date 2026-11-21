from __future__ import annotations

import ast
import builtins
import tomllib
from pathlib import Path

from scripts.ruleset.core import CONSTRAINTS_FILE, Constraints, ConstraintsError

SCHEMA: dict[str, dict[str, type]] = {
    "package": {"name": str, "python_trees": list, "excluded_dirs": list},
    "source": {"max_file_lines": int},
    "exceptions": {"catch_all_types": list, "catch_all_entry_point": str, "swallow_allowed": list},
    "directives": {"file": str, "layer_block_begin": str, "layer_block_end": str},
}
DYNAMIC_SECTIONS = ("layers",)
ENTRY_POINT_SEPARATOR = ":"


def _fail(message: str) -> ConstraintsError:
    return ConstraintsError(f"{CONSTRAINTS_FILE}: {message}")


def _require_table(data: dict, section: str) -> dict:
    value = data.get(section)
    if not isinstance(value, dict):
        raise _fail(f"missing table [{section}]")
    return value


def _validate_schema(data: dict) -> None:
    unknown = set(data) - set(SCHEMA) - set(DYNAMIC_SECTIONS)
    if unknown:
        raise _fail(f"unknown tables {sorted(unknown)}")
    for section, keys in SCHEMA.items():
        table = _require_table(data, section)
        if set(table) != set(keys):
            raise _fail(f"[{section}] keys must be exactly {sorted(keys)}, got {sorted(table)}")
        for key, expected in keys.items():
            value = table[key]
            if not isinstance(value, expected) or isinstance(value, bool):
                raise _fail(f"[{section}].{key} must be {expected.__name__}")
            if expected is list and not all(isinstance(item, str) for item in value):
                raise _fail(f"[{section}].{key} must be a list of strings")


def layer_ranks(layers: dict[str, tuple[str, ...]]) -> dict[str, int]:
    ranks: dict[str, int] = {}
    visiting: set[str] = set()

    def rank(layer: str) -> int:
        if layer in ranks:
            return ranks[layer]
        if layer in visiting:
            raise _fail(f"[layers] dependency cycle through {layer!r}")
        visiting.add(layer)
        ranks[layer] = 1 + max((rank(dep) for dep in layers[layer]), default=-1)
        visiting.discard(layer)
        return ranks[layer]

    for layer in sorted(layers):
        rank(layer)
    return ranks


def _validated_layers(data: dict, package_dir: Path) -> dict[str, tuple[str, ...]]:
    table = _require_table(data, "layers")
    if not table:
        raise _fail("[layers] declares no layer")
    layers: dict[str, tuple[str, ...]] = {}
    for layer, deps in table.items():
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise _fail(f"[layers].{layer} must be a list of layer names")
        if layer in deps:
            raise _fail(f"layer {layer!r} depends on itself")
        if len(set(deps)) != len(deps):
            raise _fail(f"[layers].{layer} lists a layer twice")
        if not ((package_dir / layer).is_dir() or (package_dir / f"{layer}.py").is_file()):
            raise _fail(f"layer {layer!r} has no directory or module under {package_dir.name}/")
        layers[layer] = tuple(deps)
    undeclared = sorted({d for deps in layers.values() for d in deps} - set(layers))
    if undeclared:
        raise _fail(f"[layers] references undeclared layers {undeclared}")
    layer_ranks(layers)
    return layers


def _is_builtin_exception(name: str) -> bool:
    candidate = getattr(builtins, name, None)
    return isinstance(candidate, type) and issubclass(candidate, BaseException)


def _validated_exception_names(table: dict, key: str) -> frozenset[str]:
    names = table[key]
    if not names:
        raise _fail(f"[exceptions].{key} must not be empty")
    unknown = sorted(n for n in names if not _is_builtin_exception(n))
    if unknown:
        raise _fail(f"[exceptions].{key} names unknown builtin exceptions {unknown}")
    return frozenset(names)


def _defines_top_level_function(path: Path, function: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function for n in tree.body)


def _validated_entry_point(root: Path, raw: str) -> tuple[str, str]:
    path, sep, function = raw.partition(ENTRY_POINT_SEPARATOR)
    if not sep or not path or not function:
        raise _fail(f"catch_all_entry_point must be 'path.py:function', got {raw!r}")
    if not (root / path).is_file() or not _defines_top_level_function(root / path, function):
        raise _fail(f"catch_all_entry_point {raw!r} does not name a top-level function of an existing file")
    return path, function


def _validated_trees(root: Path, package: dict) -> tuple[str, tuple[str, ...]]:
    name, trees = package["name"], tuple(package["python_trees"])
    if not (root / name).is_dir():
        raise _fail(f"package directory {name!r} does not exist")
    if name not in trees:
        raise _fail(f"[package].python_trees must include the package {name!r}")
    missing = sorted(t for t in trees if not (root / t).is_dir())
    if missing:
        raise _fail(f"[package].python_trees names missing directories {missing}")
    return name, trees


def load_constraints(root: Path) -> Constraints:
    data = tomllib.loads((root / CONSTRAINTS_FILE).read_text(encoding="utf-8"))
    _validate_schema(data)
    package, source, exceptions, directives = (data[s] for s in ("package", "source", "exceptions", "directives"))
    name, trees = _validated_trees(root, package)
    if source["max_file_lines"] <= 0:
        raise _fail("[source].max_file_lines must be positive")
    if not (root / directives["file"]).is_file():
        raise _fail(f"directives file {directives['file']!r} does not exist")
    return Constraints(
        root=root,
        package=name,
        python_trees=trees,
        excluded_dirs=frozenset(package["excluded_dirs"]),
        layers=_validated_layers(data, root / name),
        max_file_lines=source["max_file_lines"],
        catch_all_types=_validated_exception_names(exceptions, "catch_all_types"),
        catch_all_entry_point=_validated_entry_point(root, exceptions["catch_all_entry_point"]),
        swallow_allowed=_validated_exception_names(exceptions, "swallow_allowed"),
        directives_file=directives["file"],
        layer_block_begin=directives["layer_block_begin"],
        layer_block_end=directives["layer_block_end"],
    )
