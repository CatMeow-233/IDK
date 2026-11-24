from __future__ import annotations

from typing import Any

import pytest

from leanc.backends.inductive.editable_entry import (
    editable_before_claimed_label,
    editable_includes,
    entry_flags,
    uninstrumented_functions,
)
from leanc.backends.inductive.editable_objects import (
    editable_constant_objects,
    harness_constant_problems,
    keeps_initializer,
)
from leanc.backends.inductive.harness_calls import CLAIMED_CALLS_COUNTER, CLAIMED_ENTERED_FLAG, EDITABLE_ENTERED_FLAG
from leanc.backends.inductive.symbols import Symbol, SymbolTable

EDITABLE = "src/ring.c"
HEADER = "include/ring.h"
HARNESS = "harness/inductive.c"
CODE = "code"
UNSIGNED = {"id": "unsignedbv"}
CONSTANT = {"#constant": {"id": "1"}}
PP = (
    f'# 1 "{EDITABLE}"\n# 1 "{HEADER}" 1\nstatic inline int hdr(int x) {{ return x; }}\n# 2 "{EDITABLE}" 2\n'
    "int add(int *s)\n{\n    return ++*s;\n}\nstatic int peek(const int *s)\n{\n    return *s;\n}\n"
)


def symbol(name: str, file: str | None, *, type_id: str = CODE, base: str | None = None, local: bool = False,
           value: bool = True, is_type: bool = False, type_node: Any = None, refs: frozenset[str] = frozenset()) -> Symbol:
    return Symbol(name, base or name, "m", file, "1", local, value, not value or type_id != CODE, False, is_type,
                  type_id, None, type_node=type_node, value_symbols=refs)


def table(*symbols: Symbol) -> SymbolTable:
    return {item.name: item for item in symbols}


def test_every_editable_function_gets_an_entry_check_and_only_claimed_ones_assert():
    flags = entry_flags(EDITABLE, PP, ["add"], [EDITABLE])
    assert flags.problems == () and flags.functions == {(EDITABLE, "add"), (EDITABLE, "peek")}
    texts = sorted(insertion.text for insertion in flags.insertions)
    claimed = next(text for text in texts if "__CPROVER_assert" in text)
    unclaimed = next(text for text in texts if "__CPROVER_assert" not in text)
    assert f"if ({CLAIMED_CALLS_COUNTER} == 1 && !{CLAIMED_ENTERED_FLAG})" in claimed
    assert f'__CPROVER_assert({EDITABLE_ENTERED_FLAG} == 0, "{editable_before_claimed_label("add")}")' in claimed
    assert f"{CLAIMED_ENTERED_FLAG} = 1;" in claimed
    assert f"if (!{CLAIMED_ENTERED_FLAG}) {EDITABLE_ENTERED_FLAG} = 1;" in unclaimed
    assert all("\n" not in text for text in texts) and len(texts) == 2


def test_a_body_opened_with_a_digraph_cannot_take_the_entry_check():
    flags = entry_flags(EDITABLE, f'# 1 "{EDITABLE}"\nint add(int *s)\n<%\n    return *s;\n%>\n', ["add"], [EDITABLE])
    assert flags.problems == (f"{EDITABLE}:1: the body of add does not open with {{",)


def test_functions_from_another_editable_file_included_by_the_unit_get_the_check_too():
    pp = f'# 1 "src/a.c"\n# 1 "src/b.c" 1\nstatic int b(int x)\n{{\n    return x;\n}}\n# 2 "src/a.c" 2\nint a(int x)\n{{\n    return b(x);\n}}\n'
    assert entry_flags("src/a.c", pp, [], ["src/a.c", "src/b.c"]).functions == {("src/a.c", "a"), ("src/b.c", "b")}
    assert entry_flags("src/a.c", pp, [], ["src/a.c"]).functions == {("src/a.c", "a")}


def test_a_spliced_unit_cannot_take_the_entry_checks():
    flags = entry_flags(EDITABLE, f'# 1 "{EDITABLE}"\nint add(int *s)\n{{\n    return \\\n*s;\n}}\n', ["add"], [EDITABLE])
    assert flags.problems and "line splice" in flags.problems[0]


def test_editable_functions_without_an_entry_check_are_listed():
    linked = table(
        symbol("add", EDITABLE),
        symbol("peek$link1", EDITABLE, base="peek", local=True),
        symbol("hidden", EDITABLE),
        symbol("hdr", HEADER, local=True),
        symbol("main", HARNESS),
        symbol("add_decl", EDITABLE, value=False),
    )
    missing = uninstrumented_functions(linked, frozenset({(EDITABLE, "add"), (EDITABLE, "peek")}), [EDITABLE])
    assert len(missing) == 1 and missing[0].startswith("hidden() (src/ring.c:1)")


def test_a_harness_unit_with_lines_of_an_editable_source_is_listed_once():
    pp = f'# 1 "{HARNESS}"\nint main(void);\n# 1 "{EDITABLE}" 1\nint add(int *s);\nint peek(int *s);\n# 3 "{HARNESS}" 2\n'
    assert editable_includes(pp, [EDITABLE]) == [f"the harness translation unit includes the editable source {EDITABLE}"]
    assert editable_includes(pp, ["src/other.c"]) == []


STRUCT_WITH_CONSTANT = {"id": "struct", "namedSub": {"components": {"sub": [
    {"id": "", "namedSub": {"name": {"id": "limit"}, "type": {"id": "unsignedbv", "namedSub": CONSTANT}}},
    {"id": "", "namedSub": {"name": {"id": "spare"}, "type": UNSIGNED}},
]}}}
PLAIN_STRUCT = {"id": "struct", "namedSub": {"components": {"sub": [{"id": "", "namedSub": {"type": UNSIGNED}}]}}}
TAGS = table(
    symbol("tag-cfg", HEADER, type_id="struct", is_type=True, value=False, type_node=STRUCT_WITH_CONSTANT),
    symbol("tag-plain", HEADER, type_id="struct", is_type=True, value=False, type_node=PLAIN_STRUCT),
)


@pytest.mark.parametrize(
    "node, kept",
    [
        ({"id": "unsignedbv", "namedSub": CONSTANT}, True),
        (UNSIGNED, False),
        ({"id": "unsignedbv", "namedSub": {"#volatile": {"id": "1"}}}, False),
        ({"id": "array", "sub": [{"id": "unsignedbv", "namedSub": CONSTANT}]}, True),
        ({"id": "array", "sub": [UNSIGNED]}, False),
        ({"id": "struct_tag", "namedSub": {"identifier": {"id": "tag-cfg"}}}, True),
        ({"id": "struct_tag", "namedSub": {"identifier": {"id": "tag-plain"}}}, False),
        ({"id": "struct_tag", "namedSub": {"identifier": {"id": "tag-missing"}}}, True),
        ({"id": "pointer", "sub": [{"id": "signedbv", "namedSub": CONSTANT}]}, False),
        ({"id": "pointer", "namedSub": CONSTANT, "sub": [{"id": "signedbv"}]}, True),
        ({"id": "array"}, True),
        (None, True),
        ({"namedSub": {}}, True),
    ],
    ids=["const", "plain", "volatile", "const_elements", "plain_elements", "const_member", "plain_record",
         "unknown_tag", "pointer_to_const", "const_pointer", "array_without_element", "no_type", "no_kind"],
)
def test_objects_keep_their_initialiser_exactly_when_they_are_const_or_have_const_parts(node, kept):
    assert keeps_initializer(node, TAGS) is kept


def test_only_external_editable_objects_that_keep_their_initialiser_are_fixed_by_the_code():
    linked = {
        **TAGS,
        **table(
            symbol("ring_limit", EDITABLE, type_id="unsignedbv", type_node={"id": "unsignedbv", "namedSub": CONSTANT}),
            symbol("ring_spare", EDITABLE, type_id="unsignedbv", type_node=UNSIGNED),
            symbol("local_limit", EDITABLE, type_id="unsignedbv", local=True,
                   type_node={"id": "unsignedbv", "namedSub": CONSTANT}),
            symbol("harness_limit", HARNESS, type_id="unsignedbv", type_node={"id": "unsignedbv", "namedSub": CONSTANT}),
            symbol("add", EDITABLE),
        ),
    }
    assert sorted(editable_constant_objects(linked, [EDITABLE])) == ["ring_limit"]


def test_harness_reads_of_fixed_objects_are_found_in_bodies_and_in_initialisers():
    objects = table(symbol("ring_limit", EDITABLE, type_id="unsignedbv"))
    pp = (
        f'# 1 "{HARNESS}"\nextern const unsigned ring_limit;\nstruct s {{ unsigned ring_limit; }};\n'
        "int main(void)\n{\n    unsigned n = ring_limit;\n    return (int)n;\n}\n"
    )
    definitions = table(symbol("alias", HARNESS, type_id="pointer", refs=frozenset({"ring_limit", "other"})))
    problems = harness_constant_problems(pp, definitions, objects)
    assert len(problems) == 2
    assert problems[0].startswith(f"{HARNESS}:5: the harness reads ring_limit, defined at {EDITABLE}:1")
    assert "the initialiser of alias refers to ring_limit" in problems[1]
    assert harness_constant_problems(pp.replace("ring_limit;\n    return", "0;\n    return"), {}, objects) == []
