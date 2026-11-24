from __future__ import annotations

import re

import pytest

from leanc.backends.cbmc.vacuity_divergence import (
    END_OF_OUTPUT,
    REACH_VACUITY_EXPANSION,
    REACH_VERIFY_EXPANSION,
    normalised_vacuity_text,
    vacuity_divergence,
)
from leanc.rules.loop_sites import Origin
from leanc.toolchain.context import leanc_include_dir

REACH_DEFINITION_RE = re.compile(r"^#\s*define\s+LEANC_REACH\(label\)\s+(.*?)\s*$", re.MULTILINE)
LEANC_H_REACH_BODIES = [REACH_VACUITY_EXPANSION, REACH_VERIFY_EXPANSION, REACH_VERIFY_EXPANSION]
MARKER = '# 1 "harness/h.c"\n'
PROBE_SITE = '__CPROVER_assert(0, "leanc_reach:" "after_call")'


def _unit(*lines: str) -> str:
    return MARKER + "int main(void)\n{\n" + "".join(f"    {line}\n" for line in lines) + "}\n"


def test_the_expansions_match_the_leanc_reach_definitions_of_leanc_h():
    text = (leanc_include_dir() / "leanc.h").read_text(encoding="utf-8")
    assert REACH_DEFINITION_RE.findall(text) == LEANC_H_REACH_BODIES


@pytest.mark.parametrize(
    "site",
    [
        PROBE_SITE,
        '__CPROVER_assert(0, "leanc_reach:" "a" "b")',
        '__CPROVER_assert( 0 , "leanc_reach:"  "esc\\"aped" )',
    ],
    ids=["single_label", "concatenated_label", "spacing_and_escape"],
)
def test_a_probe_site_in_code_position_normalises_to_the_verify_expansion(site):
    verify = _unit("f();", f"{REACH_VERIFY_EXPANSION};", "g();")
    vacuity = _unit("f();", f"{site};", "g();")
    assert normalised_vacuity_text(vacuity) == verify
    assert vacuity_divergence(verify, vacuity) is None


def test_identical_texts_without_probes_do_not_diverge():
    text = _unit("f();")
    assert vacuity_divergence(text, text) is None


def test_a_statement_only_in_the_vacuity_text_diverges_at_its_harness_line():
    verify = _unit(f"{REACH_VERIFY_EXPANSION};", "return 0;")
    vacuity = _unit("if (x > 16)", f"{PROBE_SITE};", "return 0;")
    divergence = vacuity_divergence(verify, vacuity)
    assert divergence is not None
    assert divergence.origin == Origin("harness/h.c", 3) and divergence.where == "harness/h.c:3"
    assert divergence.verify_line == f"{REACH_VERIFY_EXPANSION};" and divergence.vacuity_line == "if (x > 16)"


def test_a_shorter_vacuity_text_names_the_end_of_the_preprocessed_text():
    verify = _unit("f();", "g();")
    vacuity = MARKER + "int main(void)\n{\n    f();"
    divergence = vacuity_divergence(verify, vacuity)
    assert divergence is not None and divergence.where == "harness/h.c:4"
    assert divergence.verify_line == "g();" and divergence.vacuity_line == END_OF_OUTPUT


def test_a_stringized_probe_expansion_is_not_a_probe_site():
    verify = _unit('if ("((void)0)"[1] != \'(\') { f(); }')
    vacuity = _unit('if ("__CPROVER_assert(0, \\"leanc_reach:\\" \\"after_call\\")"[1] != \'(\') { f(); }')
    assert normalised_vacuity_text(vacuity) == vacuity
    assert vacuity_divergence(verify, vacuity) is not None


def test_a_probe_shaped_text_that_starts_inside_a_string_literal_is_not_a_probe_site():
    verify = _unit(f'const char *s = "{REACH_VERIFY_EXPANSION}";')
    vacuity = _unit(f'const char *s = "{PROBE_SITE}";')
    assert normalised_vacuity_text(vacuity) == vacuity
    assert vacuity_divergence(verify, vacuity) is not None


def test_a_probe_site_split_over_two_lines_is_not_normalised():
    verify = _unit(f"{REACH_VERIFY_EXPANSION};")
    vacuity = _unit('__CPROVER_assert(0, "leanc_reach:"', '"after_call");')
    assert normalised_vacuity_text(vacuity) == vacuity
    assert vacuity_divergence(verify, vacuity) is not None


def test_a_text_with_a_line_splice_is_compared_unchanged():
    vacuity = _unit(f"{PROBE_SITE}; \\", "g();")
    assert normalised_vacuity_text(vacuity) == vacuity
