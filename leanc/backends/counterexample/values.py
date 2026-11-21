from __future__ import annotations

import copy
import re
from typing import Any

from leanc.backends.counterexample.json_fields import as_dict, as_int, as_list
from leanc.backends.counterexample.names import clean_name, clean_type, strip_decorations

MAX_VALUE_CHARS = 200
MAX_ELEMENTS = 16
MAX_RENDER_DEPTH = 4
UNKNOWN_VALUE = "?"
NULL_POINTER = "NULL"
ELLIPSIS = "..."
PADDING_MEMBER_MARKER = "$"
PLAIN_INTEGER_VALUE_NAMES = (None, "integer")

_SUFFIXED_INTEGER_RE = re.compile(r"^(-?\d+)[uUlL]*$")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_LHS_RE = re.compile(r"^(?P<base>[A-Za-z_]\w*)(?P<rest>(?:\[-?\d+[uUlL]*\]|\.[A-Za-z_]\w*)*)$")
_PATH_RE = re.compile(r"\[(-?\d+)[uUlL]*\]|\.([A-Za-z_]\w*)")

PathKey = int | str


def _more_marker(total: int, shown: int) -> str:
    return f"{ELLIPSIS} (+{total - shown} more)"


def _render_pointer(data: Any) -> str:
    if not isinstance(data, str) or not data:
        return UNKNOWN_VALUE
    if NULL_POINTER in data:
        return NULL_POINTER
    target = strip_decorations(data).strip()
    if _IDENTIFIER_RE.fullmatch(target):
        return "&" + target
    return target


def _render_array(elements: list, depth: int, max_elements: int) -> str:
    entries = [element for element in elements if isinstance(element, dict)]
    indices = [as_int(entry.get("index")) for entry in entries]
    indices_are_contiguous = all(index == position for position, index in enumerate(indices))
    parts = []
    for entry, index in zip(entries[:max_elements], indices[:max_elements]):
        rendered = _render(entry.get("value"), depth + 1, max_elements)
        shown_index = index if index is not None else UNKNOWN_VALUE
        parts.append(rendered if indices_are_contiguous else f"[{shown_index}]={rendered}")
    if len(entries) > max_elements:
        parts.append(_more_marker(len(entries), max_elements))
    return "{" + ", ".join(parts) + "}"


def _is_padding_member(member: dict) -> bool:
    return PADDING_MEMBER_MARKER in str(member.get("name", ""))


def _render_struct(members: list, depth: int, max_elements: int) -> str:
    fields = [member for member in members if isinstance(member, dict) and not _is_padding_member(member)]
    parts = [
        f".{field.get('name', UNKNOWN_VALUE)}={_render(field.get('value'), depth + 1, max_elements)}"
        for field in fields[:max_elements]
    ]
    if len(fields) > max_elements:
        parts.append(_more_marker(len(fields), max_elements))
    return "{" + ", ".join(parts) + "}"


def _render_scalar(value: dict) -> str:
    name = value.get("name")
    data = value.get("data")
    if name == "pointer":
        return _render_pointer(data)
    if data is None:
        return UNKNOWN_VALUE
    text = str(data)
    integer = _SUFFIXED_INTEGER_RE.match(text)
    if integer and name in PLAIN_INTEGER_VALUE_NAMES:
        return integer.group(1)
    return text


def _render(value: Any, depth: int, max_elements: int) -> str:
    fields = as_dict(value)
    if not fields:
        return UNKNOWN_VALUE
    if depth > MAX_RENDER_DEPTH:
        return "{" + ELLIPSIS + "}"
    if isinstance(fields.get("elements"), list):
        return _render_array(fields["elements"], depth, max_elements)
    if isinstance(fields.get("members"), list):
        return _render_struct(fields["members"], depth, max_elements)
    return _render_scalar(fields)


def render_value(value: Any, *, max_elements: int = MAX_ELEMENTS, max_chars: int = MAX_VALUE_CHARS) -> str:
    rendered = _render(value, 0, max_elements)
    if len(rendered) > max_chars:
        rendered = rendered[: max(0, max_chars - len(ELLIPSIS))] + ELLIPSIS
    return rendered


def value_type(value: Any) -> str | None:
    fields = as_dict(value)
    own_type = clean_type(fields.get("type"))
    if own_type:
        return own_type
    elements = as_list(fields.get("elements"))
    if elements:
        element_type = clean_type(as_dict(as_dict(elements[0]).get("value")).get("type"))
        if element_type:
            return f"{element_type}[]"
    return None


def parse_lhs(lhs: str) -> tuple[str, list[PathKey]] | None:
    match = _LHS_RE.match(clean_name(lhs))
    if not match:
        return None
    path: list[PathKey] = [
        int(index) if index != "" else member for index, member in _PATH_RE.findall(match.group("rest"))
    ]
    return match.group("base"), path


def _element_slot(node: dict, index: int) -> dict | None:
    elements = node.get("elements")
    if not isinstance(elements, list):
        return None
    slot = next((entry for entry in elements if isinstance(entry, dict) and as_int(entry.get("index")) == index), None)
    if slot is None:
        slot = {"index": index, "value": {}}
        elements.append(slot)
        elements.sort(key=lambda entry: as_int(as_dict(entry).get("index")) or 0)
    return slot


def _member_slot(node: dict, name: str) -> dict | None:
    members = node.get("members")
    if not isinstance(members, list):
        return None
    slot = next((member for member in members if isinstance(member, dict) and member.get("name") == name), None)
    if slot is None:
        slot = {"name": name, "value": {}}
        members.append(slot)
    return slot


def set_path(root: dict, path: list[PathKey], value: Any) -> bool:
    node = root
    for position, key in enumerate(path):
        slot = _element_slot(node, key) if isinstance(key, int) else _member_slot(node, key)
        if slot is None:
            return False
        if position == len(path) - 1:
            slot["value"] = copy.deepcopy(value)
            return True
        if not isinstance(slot.get("value"), dict):
            slot["value"] = {}
        node = slot["value"]
    return False
