from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from goto_pipeline_support import (
    INDUCTIVE_MODELS,
    MANDATORY_CHECK_FLAGS,
    PROPERTY_FAILED,
    dfcc_results,
    failed,
    main_wrapper,
)
from loop_contract_support import ACCEPTANCE_FIXTURES

COPY_FIXTURES = ACCEPTANCE_FIXTURES / "copy"
MODELLED_FUNCTIONS = ("memcpy", "memmove", "memset")
HAVOC = "__CPROVER_havoc_slice(dst, n);"
MODEL_LABELS = {
    "memcpy_overlap": "leanc_model: memcpy src/dst overlap",
    "memcpy_source": "leanc_model: memcpy source region readable",
    "memcpy_destination": "leanc_model: memcpy destination region writeable",
    "memmove_source": "leanc_model: memmove source region readable",
    "memmove_destination": "leanc_model: memmove destination region writeable",
    "memset_destination": "leanc_model: memset destination region writeable",
}
VALID_COPIES = """#include <stdlib.h>
#include <string.h>
struct rec { unsigned char head[8]; unsigned char body[32]; unsigned len; };
__CPROVER_size_t nondet_size_t(void);
void copies(struct rec *r, unsigned char *heap, __CPROVER_size_t cap)
{
    __CPROVER_size_t n = nondet_size_t();
    __CPROVER_assume(n <= 8);
    memcpy(r->head, r->body, n);
    memmove(r->body + 1, r->body, n);
    memmove(r->body, r->body + 3, n);
    memset(r->body, 0, sizeof r->body);
    memcpy(heap, r->body, cap < 32 ? cap : 32);
    memset(heap, 7, cap);
    memcpy(r->head, r->head, 0);
    memcpy(heap + cap, r->body, 0);
    memset(r->head + 8, 1, 0);
    memmove(r->head + 8, r->head, 0);
}
void loop_copy(unsigned char *dst, const unsigned char *src, __CPROVER_size_t len)
{
    __CPROVER_size_t i = 0;
    while (i < len)
    __CPROVER_assigns(i, __CPROVER_object_upto(dst, len))
    __CPROVER_loop_invariant(i <= len)
    __CPROVER_decreases(len - i)
    {
        __CPROVER_size_t step = len - i < 4 ? len - i : 4;
        memcpy(dst + i, src + i, step);
        i += step;
    }
}
int main(void)
{
    struct rec r;
    __CPROVER_size_t cap = nondet_size_t();
    __CPROVER_assume(cap > 0 && cap <= 1000000);
    unsigned char *heap = malloc(cap);
    __CPROVER_assume(heap != NULL);
    copies(&r, heap, cap);
    __CPROVER_size_t len = nondet_size_t();
    __CPROVER_assume(len <= 1000000);
    unsigned char *a = malloc(len);
    unsigned char *b = malloc(len);
    __CPROVER_assume(a != NULL && b != NULL);
    loop_copy(a, b, len);
    free(a);
    free(b);
    free(heap);
    return 0;
}
"""
ONE_COPY = """#include <string.h>
int main(void)
{{
    unsigned char small[4] = {{0}};
    unsigned char large[8] = {{0}};
    {copy};
    return small[0] + large[0];
}}
"""
INVALID_COPIES = {
    "memcpy_overlap": "memcpy(large + 1, large, 4)",
    "memcpy_source": "memcpy(large, small, 5)",
    "memcpy_destination": "memcpy(small, large, 5)",
    "memmove_source": "memmove(large, small, 5)",
    "memmove_destination": "memmove(small, large, 5)",
    "memset_destination": "memset(small, 0, 5)",
}
OVERLAPPING_MOVES = ("memmove(large + 1, large, 4)", "memmove(large, large + 1, 7)")
CONTENT_INDEX = """#include <string.h>
struct msg {{ unsigned char pos; unsigned char table[16]; }};
unsigned char nondet_uchar(void);
int main(void)
{{
    struct msg m;
    unsigned char wire[1];
    wire[0] = nondet_uchar();
    m.pos = 0;
    {copy};
    return m.table[m.pos];
}}
"""
CONTENT_COPIES = {
    "memcpy": "memcpy(&m.pos, wire, 1)",
    "memmove": "memmove(&m.pos, wire, 1)",
    "memset": "memset(&m.pos, wire[0], 1)",
}
ZERO_LENGTH_INTO_FREED = """#include <stdlib.h>
#include <string.h>
int main(void)
{{
    unsigned char small[4] = {{0}};
    unsigned char *gone = malloc(4);
    __CPROVER_assume(gone != NULL);
    free(gone);
    {copy};
    return small[0];
}}
"""
ZERO_LENGTH_COPIES = {
    "memcpy": "memcpy(gone, small, 0)",
    "memmove": "memmove(gone, small, 0)",
    "memset": "memset(gone, 0, 0)",
}
TABLE_INDEX = "m.table"
HAVOC_SLICE_WRITE_CHECK = "assertion havoc_slice W_OK(dst, n)"
DESTINATION_CASE_SUFFIX = "_destination"


def _program(tmp_path: Path, program: str, models: str | None = None) -> list[dict]:
    (tmp_path / "models.c").write_text(INDUCTIVE_MODELS.read_text() if models is None else models, encoding="utf-8")
    (tmp_path / "program.c").write_text(program, encoding="utf-8")
    return dfcc_results(tmp_path, ["models.c", "program.c"], *MANDATORY_CHECK_FLAGS)


def _without_havoc(text: str, function: str) -> str:
    start = text.index(f"void *{function}(")
    at = text.index(HAVOC, start)
    return text[:at] + text[at + len(HAVOC) :]


def _table_index_failures(results: list[dict]) -> list[dict]:
    return [r for r in failed(results) if TABLE_INDEX in r["description"] and "upper bound" in r["description"]]


def test_the_models_define_the_three_copy_functions_each_with_a_havoc():
    text = INDUCTIVE_MODELS.read_text(encoding="utf-8")
    assert INDUCTIVE_MODELS.parent.name == "runtime"
    assert [text.count(f"void *{function}(") for function in MODELLED_FUNCTIONS] == [1, 1, 1]
    assert text.count(HAVOC) == len(MODELLED_FUNCTIONS)
    for function in MODELLED_FUNCTIONS:
        assert _without_havoc(text, function).count(HAVOC) == len(MODELLED_FUNCTIONS) - 1
    assert sorted(label for label in MODEL_LABELS.values() if f'"{label}"' in text) == sorted(MODEL_LABELS.values())


def test_valid_copies_hold_under_the_models(needs_cbmc, tmp_path):
    results = _program(tmp_path, VALID_COPIES)
    assert not failed(results)
    assert {r["description"] for r in results} >= set(MODEL_LABELS.values())


@pytest.mark.parametrize("case", sorted(INVALID_COPIES))
def test_each_model_assertion_reports_its_violation(needs_cbmc, tmp_path, case):
    results = _program(tmp_path, ONE_COPY.format(copy=INVALID_COPIES[case]))
    expected = [MODEL_LABELS[case]]
    if case.endswith(DESTINATION_CASE_SUFFIX):
        expected.append(HAVOC_SLICE_WRITE_CHECK)
    assert sorted((r["description"], r["status"]) for r in failed(results)) == [
        (description, PROPERTY_FAILED) for description in sorted(expected)
    ]


@pytest.mark.parametrize("function", MODELLED_FUNCTIONS)
def test_a_zero_length_copy_checks_the_destination_and_writes_nothing(needs_cbmc, tmp_path, function):
    results = _program(tmp_path, ZERO_LENGTH_INTO_FREED.format(copy=ZERO_LENGTH_COPIES[function]))
    label = MODEL_LABELS[function + DESTINATION_CASE_SUFFIX]
    assert [(r["description"], r["status"]) for r in failed(results)] == [(label, PROPERTY_FAILED)]


@pytest.mark.parametrize("move", OVERLAPPING_MOVES)
def test_memmove_allows_overlapping_regions(needs_cbmc, tmp_path, move):
    assert not failed(_program(tmp_path, ONE_COPY.format(copy=move)))


def test_the_assert_only_fixture_stub_hides_the_bug_the_model_reports(needs_cbmc, tmp_path):
    for name in ("h.c", "idx.c"):
        shutil.copy(COPY_FIXTURES / name, tmp_path / name)
    (tmp_path / "main.c").write_text(main_wrapper("prove_pick"), encoding="utf-8")
    units = ["models.c", "h.c", "idx.c", "main.c"]
    shutil.copy(COPY_FIXTURES / "stub_nohavoc.c", tmp_path / "models.c")
    assert not failed(dfcc_results(tmp_path, units, *MANDATORY_CHECK_FLAGS))
    shutil.copy(INDUCTIVE_MODELS, tmp_path / "models.c")
    reported = [r for r in failed(dfcc_results(tmp_path, units, *MANDATORY_CHECK_FLAGS)) if "table" in r["description"]]
    assert [r["property"].split(".")[0] for r in reported] == ["pick"]
    assert "upper bound" in reported[0]["description"]


@pytest.mark.parametrize("function", MODELLED_FUNCTIONS)
def test_each_model_havoc_is_load_bearing(needs_cbmc, tmp_path, function):
    program = CONTENT_INDEX.format(copy=CONTENT_COPIES[function])
    assert len(_table_index_failures(_program(tmp_path, program))) == 1
    mutant = _without_havoc(INDUCTIVE_MODELS.read_text(encoding="utf-8"), function)
    assert not failed(_program(tmp_path, program, mutant))
