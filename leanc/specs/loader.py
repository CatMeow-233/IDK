"""Loading, validating, hashing and serialising specs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from leanc.foundation.errors import SpecError
from leanc.foundation.hashing import canonical_hash, canonical_json
from leanc.specs.model import Spec

MAX_SPEC_BYTES = 1024 * 1024
MAX_DIFF_VALUE_CHARS = 200


class _DuplicateKeyRejectingSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping_rejecting_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_DuplicateKeyRejectingSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_rejecting_duplicate_keys
)


def _validation_error_lines(exc: ValidationError) -> list[str]:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  - {loc or '<root>'}: {err.get('msg')}")
    return lines


def parse_spec_text(text: str, *, source: str = "<spec>") -> Spec:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError(f"{source}: spec too large")
    try:
        if source.endswith(".json"):
            data = json.loads(text)
        else:
            data = yaml.load(text, Loader=_DuplicateKeyRejectingSafeLoader)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise SpecError(f"{source}: cannot parse: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{source}: top level must be a mapping")
    try:
        return Spec.model_validate(data)
    except ValidationError as exc:
        raise SpecError(f"{source}: invalid spec:\n" + "\n".join(_validation_error_lines(exc))) from None


def load_spec(path: str | Path) -> Spec:
    p = Path(path)
    if p.is_symlink():
        raise SpecError(f"{p}: spec must not be a symbolic link")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SpecError(f"cannot read spec {p}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SpecError(f"{p}: not valid UTF-8 (byte offset {exc.start})") from None
    return parse_spec_text(text, source=str(p))


def spec_canonical_dict(spec: Spec) -> dict[str, Any]:
    return spec.model_dump(mode="json")


def spec_hash(spec: Spec) -> str:
    """Hash of the semantic content (validated, with defaults): YAML formatting or comment edits keep it unchanged."""
    return canonical_hash(spec_canonical_dict(spec))


def spec_to_yaml(spec: Spec) -> str:
    return yaml.safe_dump(spec_canonical_dict(spec), sort_keys=False, allow_unicode=True, width=100)


def spec_to_json(spec: Spec) -> str:
    return canonical_json(spec_canonical_dict(spec))


def _shown_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)[:MAX_DIFF_VALUE_CHARS]


def diff_spec_dicts(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[str]:
    """Field-level ``+`` / ``-`` / ``~`` diff, used by ``leanc spec status`` to show what changed since approval."""
    out: list[str] = []
    for k in sorted(set(old) | set(new)):
        path = f"{prefix}.{k}" if prefix else str(k)
        if k not in old:
            out.append(f"+ {path}: {_shown_value(new[k])}")
        elif k not in new:
            out.append(f"- {path}")
        elif isinstance(old[k], dict) and isinstance(new[k], dict):
            out.extend(diff_spec_dicts(old[k], new[k], path))
        elif old[k] != new[k]:
            out.append(f"~ {path}: {_shown_value(old[k])} -> {_shown_value(new[k])}")
    return out
