from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from leanc.specs.loader import parse_spec_text
from leanc.specs.model import Spec

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
SAFE_ARRAY_EXAMPLE = "safe_array_get"
SAFE_ARRAY_SPEC = EXAMPLES / SAFE_ARRAY_EXAMPLE / "spec.yaml"
SAFE_ARRAY_SOURCE = "src/safe_array.c"
SAFE_ARRAY_TARGET = "safe_array_get"
INDUCTIVE_HARNESS = "inductive/state_harness.c"
INDUCTIVE_CONTRACTS = "inductive/loops.json"
INDUCTIVE_HARNESS_TEXT = '#include "leanc.h"\n#include "safe_array.h"\nint main(void) { LEANC_REACH("r"); return 0; }\n'
SECTION_KEY = "inductive"


def inductive_section(**overrides: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "harness": INDUCTIVE_HARNESS,
        "contracts": INDUCTIVE_CONTRACTS,
        "functions": [SAFE_ARRAY_TARGET],
    }
    section.update(overrides)
    return section


def spec_data(spec_path: Path = SAFE_ARRAY_SPEC) -> dict[str, Any]:
    return yaml.safe_load(spec_path.read_text(encoding="utf-8"))


def spec_text_with(section: Any, data: dict[str, Any] | None = None) -> str:
    base = spec_data() if data is None else data
    base[SECTION_KEY] = section
    return yaml.safe_dump(base, sort_keys=False, allow_unicode=True)


def spec_with(section: Any, data: dict[str, Any] | None = None) -> Spec:
    return parse_spec_text(spec_text_with(section, data))
