from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from spec_inductive_support import (
    EXAMPLES,
    INDUCTIVE_CONTRACTS,
    INDUCTIVE_HARNESS,
    REPO_ROOT,
    SAFE_ARRAY_EXAMPLE,
    SAFE_ARRAY_SOURCE,
    SAFE_ARRAY_SPEC,
    SAFE_ARRAY_TARGET,
    SECTION_KEY,
    inductive_section,
    spec_data,
    spec_with,
)

from leanc.foundation.errors import SpecError
from leanc.specs.loader import (
    SECTIONS_ABSENT_FROM_CANONICAL_FORM_WHEN_UNSET,
    diff_spec_dicts,
    load_spec,
    parse_spec_text,
    spec_canonical_dict,
    spec_hash,
    spec_to_json,
    spec_to_yaml,
)
from leanc.specs.model import DEFAULT_V1_GATES, GATE_ORDER, INDUCTIVE_GATE, OPT_IN_V2_GATES, Spec

EXAMPLE_SPEC_HASHES_BEFORE_THE_INDUCTIVE_SECTION = {
    "array_oob/spec.yaml": "d2d8ea6d05c7dcc3d88b01c9c0e987abdf2c8ae380ebc5d391e5ea396abdc030",
    "cbmc_timeout/spec.yaml": "d39f0e9559a440822c651c1451fed2c127a49066a33acac55ada4a9793b370e6",
    "cheating_repair/spec.yaml": "d055206589725d49e5338b4d0f5fe3e2e85efedeb3dcf53a60814abe721d89af",
    "cpp_bounds_bug/spec.yaml": "b0bc3cd4558ff5c6347cd444d0187efab782de7e83e1f38eda4011c4b1457e40",
    "cpp_ring_buffer/spec.yaml": "2225a9bfb5d5127e5b6d57f959ca41703cf5bfb4cb6f1644b1ec83da2e86e9d7",
    "div_by_zero/spec.yaml": "1f8e8f80fcaef31d046672b5c47a9ac2d5bf38783fb88b548401be179ef41aa3",
    "int_overflow/spec.yaml": "a7f0c02e51dc97b53c60186649724cdc420bfdfbbd71de0d30fb5d69ce9bb415",
    "lean_clamp/spec.yaml": "b2dfe0dd767495754268547c7f1800d6fb92d24a3cab0abda7638c32c8c27598",
    "loop_unwind/spec.yaml": "4c9a84c081b014b9055c47cf2eda9d487b754315e1fd549142510dd069d4139f",
    "loop_unwind/spec_full_unwind.yaml": "4d33482e51990f8d3b8b03bcae97a6dddf7b58374b36d62e3e50d209b76f12dd",
    "null_deref/spec.yaml": "2fa86a0de14fccb87693bc94669d870f52887a43a447cdbd0f4a2de71ee9b4bb",
    "repair_exhausted/spec.yaml": "d58dea1078da1becb54e667df22c545904ac23a0f794ecb4fafedc1b8976d2c0",
    "ring_buffer/spec.yaml": "04d89663a43a12bf74527db57d171b1fdd93f7c8ff7db000e8ca06f48ef0e50f",
    "runtime_memory/spec.yaml": "c06ad4b7c46e9259244666f8abd988364a0eb78cca615295d78bb6dcf6f32ef7",
    "safe_array_get/mock/spec_draft.yaml": "700e19dbef8d8519b692bdbceb57a3d72053d50dedaac1d93844e2a29fe33ed1",
    "safe_array_get/spec.yaml": "700e19dbef8d8519b692bdbceb57a3d72053d50dedaac1d93844e2a29fe33ed1",
    "vacuous_harness/spec.yaml": "44bb3e501a9131dba183079682e1223091d020d3b4c8c9f50e6c0013025a8c09",
    "vacuous_harness/spec_fixed_harness.yaml": "a5fcdb10255030045a5fc050d6cd2544def1bacb564ee24999016fe05167bf4d",
    "wp_contract_violation/spec.yaml": "fa24720969f047c9b01341ee6d901aa430c440e80370f0d0b8fc2a9e6b1394b6",
    "wp_proof_repair/spec.yaml": "ef0a80871ba8572a5a94e638214eac4f993c1fc9ef53469880d3a044763e82a0",
    "wp_safe_array_get/spec.yaml": "acba7ebe16765ed4f4611e730f654cdc805ab21a37f176026f9d18062930828d",
}
TEMPLATE_SPEC = REPO_ROOT / "leanc" / "data" / "templates" / "safe-array-get" / "spec.yaml"
TEMPLATE_SPEC_HASH_BEFORE_THE_INDUCTIVE_SECTION = "700e19dbef8d8519b692bdbceb57a3d72053d50dedaac1d93844e2a29fe33ed1"
EXAMPLE_SPEC_SUFFIXES = frozenset({".yaml", ".yml"})
EXAMPLE_NON_SPEC_FILE_NAMES = frozenset({"expected.yaml"})
VARIANT_EXAMPLE_SPECS = frozenset(
    {"loop_unwind/spec_full_unwind.yaml", "vacuous_harness/spec_fixed_harness.yaml", "safe_array_get/mock/spec_draft.yaml"}
)
SAFE_ARRAY_HASH = EXAMPLE_SPEC_HASHES_BEFORE_THE_INDUCTIVE_SECTION[SAFE_ARRAY_SPEC.relative_to(EXAMPLES).as_posix()]
SAFE_ARRAY_HEADER = "include/safe_array.h"
SAFE_ARRAY_HARNESS = "harness/safe_array_get_harness.c"


def _is_example_spec_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix in EXAMPLE_SPEC_SUFFIXES
        and path.name not in EXAMPLE_NON_SPEC_FILE_NAMES
    )


def _example_spec_files() -> list[Path]:
    return sorted(path for path in EXAMPLES.rglob("*") if _is_example_spec_file(path))


def test_example_spec_files_include_the_variant_specs():
    names = {path.relative_to(EXAMPLES).as_posix() for path in _example_spec_files()}
    assert VARIANT_EXAMPLE_SPECS <= names
    assert not any(name.endswith("/expected.yaml") for name in names)


def test_every_example_spec_without_the_section_has_a_pinned_hash():
    names = sorted(path.relative_to(EXAMPLES).as_posix() for path in _example_spec_files() if SECTION_KEY not in spec_data(path))
    assert names == sorted(EXAMPLE_SPEC_HASHES_BEFORE_THE_INDUCTIVE_SECTION)


@pytest.mark.parametrize("relative_path", sorted(EXAMPLE_SPEC_HASHES_BEFORE_THE_INDUCTIVE_SECTION))
def test_example_spec_without_the_section_keeps_its_hash_and_canonical_form(relative_path):
    spec = load_spec(EXAMPLES / relative_path)
    assert spec.inductive is None
    assert SECTION_KEY not in spec_canonical_dict(spec)
    assert f'"{SECTION_KEY}"' not in spec_to_json(spec)
    assert spec_hash(spec) == EXAMPLE_SPEC_HASHES_BEFORE_THE_INDUCTIVE_SECTION[relative_path]


def test_template_spec_without_the_section_keeps_its_hash():
    spec = load_spec(TEMPLATE_SPEC)
    assert spec.inductive is None
    assert spec_hash(spec) == TEMPLATE_SPEC_HASH_BEFORE_THE_INDUCTIVE_SECTION


def test_sections_left_out_when_unset_are_optional_spec_sections():
    assert SECTION_KEY in SECTIONS_ABSENT_FROM_CANONICAL_FORM_WHEN_UNSET
    for name in SECTIONS_ABSENT_FROM_CANONICAL_FORM_WHEN_UNSET:
        field = Spec.model_fields[name]
        assert not field.is_required() and field.default is None


def test_explicit_null_section_hashes_like_an_absent_one():
    explicit = spec_with(None)
    assert explicit.inductive is None
    assert spec_hash(explicit) == SAFE_ARRAY_HASH
    assert SECTION_KEY not in yaml.safe_load(spec_to_yaml(explicit))


def test_set_section_is_part_of_the_canonical_form_and_the_hash():
    spec = spec_with(inductive_section())
    canonical = spec_canonical_dict(spec)
    assert canonical[SECTION_KEY] == {
        "harness": INDUCTIVE_HARNESS,
        "contracts": INDUCTIVE_CONTRACTS,
        "functions": [SAFE_ARRAY_TARGET],
    }
    assert spec_hash(spec) != SAFE_ARRAY_HASH
    assert spec_hash(parse_spec_text(spec_to_yaml(spec))) == spec_hash(spec)
    plain = spec_canonical_dict(load_spec(EXAMPLES / SAFE_ARRAY_EXAMPLE / "spec.yaml"))
    changes = diff_spec_dicts(plain, canonical)
    assert len(changes) == 1 and changes[0].startswith(f"+ {SECTION_KEY}: ")


@pytest.mark.parametrize(
    "changed",
    [
        {"harness": "inductive/other_harness.c"},
        {"contracts": "inductive/other.json"},
        {"functions": [SAFE_ARRAY_TARGET, "helper"]},
    ],
    ids=["harness", "contracts", "functions"],
)
def test_every_section_field_is_hashed(changed):
    assert spec_hash(spec_with(inductive_section(**changed))) != spec_hash(spec_with(inductive_section()))


def test_section_paths_are_normalised_and_classified():
    spec = spec_with(inductive_section(harness="inductive/./state_harness.c", contracts="inductive//loops.json"))
    assert (spec.inductive.harness, spec.inductive.contracts) == (INDUCTIVE_HARNESS, INDUCTIVE_CONTRACTS)
    assert spec.protected_paths() == [SAFE_ARRAY_HARNESS, SAFE_ARRAY_HEADER, "tests/test_safe_array_get.c", INDUCTIVE_HARNESS]
    assert spec.editable_paths() == [SAFE_ARRAY_SOURCE, INDUCTIVE_CONTRACTS]
    assert spec.sources.editable == [SAFE_ARRAY_SOURCE]


def test_absent_section_adds_no_protected_or_editable_path():
    spec = load_spec(EXAMPLES / SAFE_ARRAY_EXAMPLE / "spec.yaml")
    assert spec.editable_paths() == [SAFE_ARRAY_SOURCE]
    assert INDUCTIVE_HARNESS not in spec.protected_paths()


def test_claimed_functions_keep_their_order_and_may_name_helpers():
    spec = spec_with(inductive_section(functions=["helper_rank", SAFE_ARRAY_TARGET, "_internal"]))
    assert spec.inductive.functions == ["helper_rank", SAFE_ARRAY_TARGET, "_internal"]


def _without(key: str) -> dict:
    section = inductive_section()
    del section[key]
    return section


@pytest.mark.parametrize(
    "section, match",
    [
        (inductive_section(unclaimed=["helper"]), r"inductive\.unclaimed: Extra inputs are not permitted"),
        (_without("harness"), r"inductive\.harness: Field required"),
        (_without("contracts"), r"inductive\.contracts: Field required"),
        (_without("functions"), r"inductive\.functions: Field required"),
        ("inductive/state_harness.c", r"inductive: Input should be a valid dictionary"),
        (inductive_section(functions=[]), r"inductive\.functions: List should have at least 1 item"),
        (inductive_section(functions=SAFE_ARRAY_TARGET), r"inductive\.functions: Input should be a valid list"),
        (inductive_section(functions=["1st"]), r"not a C identifier: '1st'"),
        (inductive_section(functions=[SAFE_ARRAY_TARGET, "a-b"]), r"not a C identifier: 'a-b'"),
        (inductive_section(functions=[SAFE_ARRAY_TARGET, "f()"]), r"not a C identifier: 'f\(\)'"),
        (inductive_section(functions=[""]), r"not a C identifier: ''"),
        (inductive_section(functions=[SAFE_ARRAY_TARGET, 7]), r"inductive\.functions\.1: Input should be a valid string"),
        (
            inductive_section(functions=[SAFE_ARRAY_TARGET, "helper", SAFE_ARRAY_TARGET]),
            r"duplicate claimed functions: \['safe_array_get'\]",
        ),
        (inductive_section(functions=["helper"]), r"inductive\.functions must include the target function 'safe_array_get'"),
        (inductive_section(functions=["Safe_array_get"]), r"must include the target function 'safe_array_get'"),
        (inductive_section(contracts="inductive/loops.yaml"), r"inductive\.contracts must end in \.json"),
        (inductive_section(contracts="inductive/loops.c"), r"inductive\.contracts must end in \.json"),
        (inductive_section(contracts=".leanc/approvals/safe-array-get.json"), r"must not be inside \.leanc/"),
        (inductive_section(contracts="./.leanc/runs/x.json"), r"must not be inside \.leanc/"),
        (inductive_section(harness="../state_harness.c"), r"path traversal"),
        (inductive_section(contracts="inductive/../../loops.json"), r"path traversal"),
        (inductive_section(harness="/abs/state_harness.c"), r"absolute paths are not allowed"),
        (inductive_section(harness=SAFE_ARRAY_SOURCE), r"files cannot be both editable and protected"),
        (inductive_section(harness=INDUCTIVE_CONTRACTS), r"files cannot be both editable and protected"),
    ],
)
def test_invalid_sections_are_rejected(section, match):
    with pytest.raises(SpecError, match=match):
        spec_with(section)


def test_contracts_path_cannot_be_a_protected_file():
    data = spec_data()
    data["sources"]["headers"] = [SAFE_ARRAY_HEADER, INDUCTIVE_CONTRACTS]
    with pytest.raises(SpecError, match=r"files cannot be both editable and protected: \['inductive/loops\.json'\]"):
        spec_with(inductive_section(), data)


def test_harness_cannot_also_be_listed_as_an_editable_source():
    data = spec_data()
    data["sources"]["editable"] = [SAFE_ARRAY_SOURCE, INDUCTIVE_HARNESS]
    with pytest.raises(SpecError, match=r"files cannot be both editable and protected: \['inductive/state_harness\.c'\]"):
        spec_with(inductive_section(), data)


def test_section_text_rules_apply():
    with pytest.raises(SpecError, match=r"inductive\.harness: NUL character"):
        spec_with(inductive_section(harness="inductive/x\x00.c"))


def _with_gates(gates: list[str], **verification) -> dict:
    data = spec_data()
    data["gates"] = gates
    data["verification"].update(verification)
    return data


def test_gate_with_the_section_and_the_cbmc_gate_is_accepted():
    spec = spec_with(inductive_section(), _with_gates([*spec_data()["gates"], INDUCTIVE_GATE]))
    assert spec.gates[-1] == INDUCTIVE_GATE and spec.inductive is not None


def test_gate_runs_right_after_cbmc_and_before_the_proof_gates():
    assert GATE_ORDER.index(INDUCTIVE_GATE) == GATE_ORDER.index("cbmc") + 1
    assert GATE_ORDER[GATE_ORDER.index(INDUCTIVE_GATE) + 1 :] == OPT_IN_V2_GATES
    assert INDUCTIVE_GATE not in DEFAULT_V1_GATES


def test_gate_requires_the_section():
    data = _with_gates(["compile", "cbmc", INDUCTIVE_GATE])
    with pytest.raises(SpecError, match=r"gate 'inductive' requires an 'inductive' section"):
        parse_spec_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


@pytest.mark.parametrize("gates", [["compile", INDUCTIVE_GATE], ["compile", "sanitizer", INDUCTIVE_GATE, "wp"]])
def test_gate_requires_the_cbmc_gate(gates):
    with pytest.raises(SpecError, match=r"gate 'inductive' requires the 'cbmc' gate"):
        spec_with(inductive_section(), _with_gates(gates))


def test_gate_rejects_malloc_may_fail():
    data = _with_gates(["compile", "cbmc", INDUCTIVE_GATE], malloc_may_fail=True)
    with pytest.raises(SpecError, match=r"gate 'inductive' does not support verification\.malloc_may_fail: true"):
        spec_with(inductive_section(), data)


def test_section_without_the_gate_still_allows_malloc_may_fail():
    spec = spec_with(inductive_section(), _with_gates(["compile", "cbmc"], malloc_may_fail=True))
    assert spec.verification.malloc_may_fail and INDUCTIVE_GATE not in spec.gates


def test_gate_may_be_listed_only_once():
    with pytest.raises(SpecError, match=r"duplicate gates"):
        spec_with(inductive_section(), _with_gates(["compile", "cbmc", INDUCTIVE_GATE, INDUCTIVE_GATE]))
