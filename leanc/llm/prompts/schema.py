from __future__ import annotations

import json
import types
import typing
from collections.abc import Iterable
from functools import lru_cache
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from leanc.specs.model import ALL_GATES, MANDATORY_CHECKS, SPEC_ID_RE, Spec

MAX_SHOWN_DEFAULT_CHARS = 80

_BOUND_SYMBOLS = (("ge", ">="), ("gt", ">"), ("le", "<="), ("lt", "<"))
_JSON_TYPE_NAMES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}

SCHEMA_RULES: tuple[str, ...] = (
    f"spec_id must match {SPEC_ID_RE.pattern}; version starts at 1 and is bumped for every approved change.",
    "requirement keeps the original natural-language requirement verbatim (informative only; no tool reads it).",
    "target.signature is the exact C prototype declared in the protected interface header.",
    "sources.editable: the .c files the code generator / repair agent may write - and nothing else.",
    "Protected files (hash-locked on approval, never writable by AI): verification.harness, sources.headers, "
    "tests.driver. No file may be both editable and protected.",
    f"verification.checks lists EXTRA CBMC checks; always enabled whatever the spec says: {', '.join(MANDATORY_CHECKS)}.",
    "verification.unwind must exceed the largest loop iteration count the harness allows (unwinding assertions "
    "are checked: an insufficient bound yields UNKNOWN, never success); unwindset keys look like 'function.N'.",
    "verification.input_bounds documents the bounds the harness enforces with LEANC_ASSUME.",
    f"gates is a subset of {list(ALL_GATES)}; the 'sanitizer' gate requires tests.driver and at least one "
    "tests.cases entry.",
    "tests.cases: each case runs the test driver (built with ASan/UBSan/LSan) with `args` and expects exit "
    "status expect_exit (and exact stdout if expect_stdout is set).",
    "Harness: a C file with main() that #includes \"leanc.h\" and the interface header, obtains inputs from "
    "nondet_*() functions, constrains them ONLY by the stated preconditions with LEANC_ASSUME(cond), calls the "
    "target, checks postconditions with assert(), and has at least one LEANC_REACH(\"label\") probe after the call "
    "(ideally one per branch) so a vacuous harness is detected.",
    "Test driver: a C file with main(int argc, char **argv) that runs the case named argv[1] and returns 0 on success.",
    "Unknown keys are rejected anywhere in the spec.",
)


def _meta_constraints(metadata: Iterable[Any]) -> str:
    parts = []
    for constraint in metadata:
        for attr, symbol in _BOUND_SYMBOLS:
            bound = getattr(constraint, attr, None)
            if bound is not None:
                parts.append(f"{symbol}{bound}")
        min_length = getattr(constraint, "min_length", None)
        if min_length is not None:
            parts.append(f"at least {min_length} item(s)")
    return f" ({', '.join(parts)})" if parts else ""


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        model = _nested_model(argument)
        if model is not None:
            return model
    return None


def _type_name(annotation: Any) -> str:
    if annotation is type(None):
        return "null"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        return " | ".join(str(a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(_type_name(a) for a in args)
    if origin is list:
        inner = args[0] if args else Any
        if get_origin(inner) is Literal:
            return f"list of ({_type_name(inner)})"
        return f"list of {_type_name(inner)}"
    if origin is dict:
        return f"map {_type_name(args[0])} -> {_type_name(args[1])}" if len(args) == 2 else "map"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    if isinstance(annotation, type):
        return _JSON_TYPE_NAMES.get(annotation.__name__, annotation.__name__)
    return str(annotation)


def _json_or_repr(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return repr(value)


def _default_repr(field: FieldInfo) -> str:
    default = field.get_default(call_default_factory=True)
    if isinstance(default, BaseModel):
        return ""
    if default is None:
        return " = null"
    shown = _json_or_repr(default)
    return f" = {shown}" if len(shown) <= MAX_SHOWN_DEFAULT_CHARS else ""


def _describe_model(model: type[BaseModel], indent: int, out: list[str]) -> None:
    pad = "  " * indent
    for name, field in model.model_fields.items():
        annotation = field.annotation
        line = f"{pad}{name}: {_type_name(annotation)}{_meta_constraints(field.metadata)}"
        line += " (required)" if field.is_required() else _default_repr(field)
        if field.description:
            line += f"  # {field.description}"
        out.append(line)
        nested = _nested_model(annotation)
        if nested is None:
            continue
        if get_origin(annotation) is list:
            out.append(f"{pad}  - (each item):")
            _describe_model(nested, indent + 2, out)
        else:
            _describe_model(nested, indent + 1, out)


@lru_cache(maxsize=1)
def spec_schema_summary() -> str:
    lines = ["Lean C spec schema (YAML mapping; field: type (constraints) = default):"]
    _describe_model(Spec, 1, lines)
    lines.append("")
    lines.append("Rules:")
    lines.extend(f"- {rule}" for rule in SCHEMA_RULES)
    return "\n".join(lines)
