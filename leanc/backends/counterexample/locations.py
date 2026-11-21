from __future__ import annotations

import os
import posixpath
from typing import Any

from leanc.backends.counterexample.json_fields import as_dict, as_int
from leanc.foundation.models import Location

CPROVER_PSEUDO_FILE_PREFIX = "<"
CURRENT_DIR_PREFIX = "./"


def is_cprover_pseudo_file(file: str) -> bool:
    return file.startswith(CPROVER_PSEUDO_FILE_PREFIX)


def _relative_to_working_dir(absolute_file: str, working_dir: str | None) -> str:
    if working_dir:
        prefix = posixpath.normpath(str(working_dir)).rstrip("/") + "/"
        if absolute_file.startswith(prefix):
            return absolute_file[len(prefix) :]
    return absolute_file


def relative_file(file: str | None, working_dir: str | None = None) -> str | None:
    if not file:
        return None
    name = str(file)
    if os.path.isabs(name):
        return _relative_to_working_dir(posixpath.normpath(name), working_dir)
    if is_cprover_pseudo_file(name):
        return name
    return posixpath.normpath(name).removeprefix(CURRENT_DIR_PREFIX)


def location_from_cbmc(source_location: Any) -> Location | None:
    fields = as_dict(source_location)
    if not fields:
        return None
    file = relative_file(fields.get("file"), fields.get("workingDirectory"))
    line = as_int(fields.get("line"))
    column = as_int(fields.get("column"))
    function = fields.get("function") if isinstance(fields.get("function"), str) else None
    if file is None and line is None and function is None:
        return None
    return Location(file=file, line=line, column=column, function=function or None)


def same_file(first: str | None, second: str | None) -> bool:
    if not first or not second:
        return False
    first_name = posixpath.normpath(str(first)).removeprefix(CURRENT_DIR_PREFIX)
    second_name = posixpath.normpath(str(second)).removeprefix(CURRENT_DIR_PREFIX)
    return (
        first_name == second_name
        or first_name.endswith("/" + second_name)
        or second_name.endswith("/" + first_name)
    )
