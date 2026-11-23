from __future__ import annotations

import shutil
from pathlib import Path

from goto_pipeline_support import (
    IGNORED_CLAUSE_WARNING,
    INDUCTIVE_MODELS,
    LOOP_PROPERTY_CLASSES,
    MANDATORY_CHECK_FLAGS,
    PROPERTY_FAILED,
    PROPERTY_HOLDS,
    cbmc_preprocessed,
    cbmc_results,
    described,
    failed,
    include_args,
    instrumented,
    linked,
    loop_properties,
    main_wrapper,
)
from loop_contract_support import ACCEPTANCE_FIXTURES, contracts_text, inductive_spec, loop_entry

from leanc.rules.loop_contracts import LoopContract, parse_loop_contracts
from leanc.rules.loop_sites import (
    Insertion,
    LoopSiteMatch,
    Origin,
    SiteProblemKind,
    annotate,
    contract_insertion,
    function_entries,
    function_probe_insertion,
    function_probe_label,
    loop_probe_insertion,
    loop_probe_label,
    loop_sites,
    origin_lines,
)
from leanc.rules.preprocessor import expanded_view

ISET = "src/iset.c"
SPONGE = "src/sponge.c"
FORMS = "src/forms.c"
SHAPES = "src/shapes.c"
HARNESS = "harness/inductive.c"
MAIN = "harness/main.c"
CLAIMED_ENTRY = "prove_iset_rank"
CLAIMED_FUNCTION = "iset_rank"
UNCALLED_FUNCTION = "iset_contains"
LOWER_BOUND_LINE = 11
LOWER_BOUND_LOOP = "lower_bound.0"
ISET_LOOPS = {11: "while", 92: "for", 109: "for"}
SPONGE_LOOPS = {9: "for", 35: "while", 65: "for"}
FORMS_ACCEPTED = {20: "for", 45: "for"}
FORMS_REJECTED = {
    6: SiteProblemKind.NO_LOOP,
    11: SiteProblemKind.NOT_CONTRACTABLE,
    14: SiteProblemKind.EMPTY_BODY,
    21: SiteProblemKind.BODY_NOT_BLOCK,
    29: SiteProblemKind.NO_LOOP,
    32: SiteProblemKind.NOT_CONTRACTABLE,
    39: SiteProblemKind.MACRO_LOOP,
    50: SiteProblemKind.NO_LOOP,
    51: SiteProblemKind.NO_LOOP,
}
ISET_CONTRACTS = (
    loop_entry(file=ISET, line=11, invariant=["0 <= lo", "lo <= hi", "hi <= len"], assigns=["lo", "hi"],
               decreases="hi - lo"),
    loop_entry(file=ISET, line=92, invariant=["pos <= k", "k <= s->len"],
               assigns=["k", "__CPROVER_object_whole(s->items)"], decreases="k - pos"),
    loop_entry(file=ISET, line=109, invariant=["pos <= k", "k < s->len"],
               assigns=["k", "__CPROVER_object_whole(s->items)"], decreases="s->len - k"),
)
SHAPES_SOURCE = """#include <stddef.h>
int two(int n)
{
    int a = 0;
    for (int i = 0; i < n; i++) { a++; } while (a > n) { a--; }
    do { a++; } while (a < n);
    return a;
}
int walk(const int *p, int n)
{
    int k = 0;
    while (p != NULL &&
           k < n) {
        k++;
    }
    return k;
}
"""
SHAPES_REJECTED = {5: SiteProblemKind.SEVERAL_LOOPS, 6: SiteProblemKind.SEVERAL_LOOPS}
SHAPES_WALK_LINE = 12
GAPS = "src/gaps.c"
GAP_LINES = "\n" * 10
GAPS_SOURCE = (
    "int gaps(int n)\n{\n    while (n > 0)" + GAP_LINES + "    {\n        n--;\n    }\n"
    "    for" + GAP_LINES + "    (; n < 3; n++) {\n    }\n    return n;\n}\n"
)
GAPS_SITES = {3: ("while", 3), 16: ("for", 26)}


def _contracts(*entries: dict, files: list[str]) -> tuple[LoopContract, ...]:
    contracts = parse_loop_contracts(inductive_spec(files), contracts_text(*entries))
    assert contracts.accepted, contracts.problems
    return contracts.loops


def _placeholder_contracts(file: str, lines) -> tuple[LoopContract, ...]:
    return _contracts(*(loop_entry(file=file, line=line, invariant=["1"], assigns=["x"], decreases="0")
                        for line in lines), files=[file])


def _project(tmp_path: Path, corpus: str) -> Path:
    project = tmp_path / corpus
    shutil.copytree(ACCEPTANCE_FIXTURES / corpus / "fixed", project)
    return project


def _sites(project: Path, rel: str, lines) -> tuple[str, LoopSiteMatch]:
    pp = cbmc_preprocessed(project, rel, *include_args("include"))
    source = (project / rel).read_text(encoding="utf-8")
    return pp, loop_sites(rel, pp, source, _placeholder_contracts(rel, lines))


def _assert_sites(pp: str, match: LoopSiteMatch, rel: str, expected: dict[int, str]) -> None:
    assert match.accepted, [problem.describe() for problem in match.problems]
    origins = origin_lines(pp)
    for matched in match.matched:
        site = matched.site
        assert (site.file, site.keyword) == (rel, expected[site.line])
        assert site.header_end_line == site.line
        assert pp[site.header_close] == ")" and pp[site.body_open] == "{"
        assert origins[pp.count("\n", 0, site.header_close)] == Origin(rel, site.line)
    assert sorted(matched.site.line for matched in match.matched) == sorted(expected)


def test_sites_of_the_acceptance_iset_loops(needs_cbmc, tmp_path):
    pp, match = _sites(_project(tmp_path, "iset"), ISET, ISET_LOOPS)
    _assert_sites(pp, match, ISET, ISET_LOOPS)


def test_sites_of_the_acceptance_sponge_loops(needs_cbmc, tmp_path):
    pp, match = _sites(_project(tmp_path, "sponge"), SPONGE, SPONGE_LOOPS)
    _assert_sites(pp, match, SPONGE, SPONGE_LOOPS)


def test_expanded_view_is_the_projection_of_origin_lines_on_real_output(needs_cbmc, tmp_path):
    pp = cbmc_preprocessed(_project(tmp_path, "iset"), ISET, *include_args("include"))
    pairs = [(text, origin) for text, origin in zip(pp.split("\n"), origin_lines(pp)) if origin is not None]
    projected = [(text, origin.line) for text, origin in pairs if origin.file == ISET]
    assert expanded_view(ISET, pp) == ("\n".join(text for text, _ in projected), [line for _, line in projected])
    files = {origin.file for _, origin in pairs}
    assert ISET in files and files - {ISET}


def test_loop_forms_that_cannot_take_a_contract_are_rejected(needs_cbmc, tmp_path):
    (tmp_path / "src").mkdir()
    shutil.copy(ACCEPTANCE_FIXTURES / "loops" / "forms.c", tmp_path / FORMS)
    _, match = _sites(tmp_path, FORMS, [*FORMS_ACCEPTED, *FORMS_REJECTED])
    assert {matched.site.line: matched.site.keyword for matched in match.matched} == FORMS_ACCEPTED
    assert {problem.line: problem.kind for problem in match.problems} == FORMS_REJECTED


def test_two_loops_on_one_line_are_rejected_and_a_split_header_is_mapped(needs_cbmc, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / SHAPES).write_text(SHAPES_SOURCE, encoding="utf-8")
    pp, match = _sites(tmp_path, SHAPES, [*SHAPES_REJECTED, SHAPES_WALK_LINE])
    assert {problem.line: problem.kind for problem in match.problems} == SHAPES_REJECTED
    (matched,) = match.matched
    site = matched.site
    assert (site.keyword, site.line, site.header_end_line) == ("while", SHAPES_WALK_LINE, SHAPES_WALK_LINE + 1)
    keyword_at = pp.rfind("while (p", 0, site.header_close)
    assert "\n# " in pp[keyword_at : site.header_close]


def test_linemarkers_gcc_puts_inside_a_loop_header_are_skipped(needs_cbmc, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / GAPS).write_text(GAPS_SOURCE, encoding="utf-8")
    pp, match = _sites(tmp_path, GAPS, GAPS_SITES)
    assert match.accepted, [problem.describe() for problem in match.problems]
    found = {m.site.line: (m.site.keyword, m.site.header_end_line) for m in match.matched}
    assert found == GAPS_SITES
    for matched in match.matched:
        keyword_at = pp.rfind(matched.site.keyword, 0, matched.site.header_close)
        assert "\n# " in pp[keyword_at : matched.site.body_open]


def _iset_build(tmp_path: Path) -> tuple[Path, str]:
    project = _project(tmp_path, "iset")
    (project / "harness").mkdir(exist_ok=True)
    shutil.copy(ACCEPTANCE_FIXTURES / "iset" / "harness_inductive.c", project / HARNESS)
    (project / MAIN).write_text(main_wrapper(CLAIMED_ENTRY), encoding="utf-8")
    build = project / "build"
    build.mkdir()
    return build, cbmc_preprocessed(project, ISET, *include_args("include"))


def _write_units(build: Path, units: dict[str, str]) -> list[str]:
    for name, text in units.items():
        (build / name).write_text(text, encoding="utf-8")
    return list(units)


def _trusted_units(build: Path, *harness_args: str) -> dict[str, str]:
    project = build.parent
    return {
        "models.i": cbmc_preprocessed(project, str(INDUCTIVE_MODELS)),
        "harness.i": cbmc_preprocessed(project, HARNESS, *include_args("include"), *harness_args),
        "main.i": cbmc_preprocessed(project, MAIN),
    }


def _iset_sites(pp: str, build: Path) -> LoopSiteMatch:
    source = (build.parent / ISET).read_text(encoding="utf-8")
    match = loop_sites(ISET, pp, source, _contracts(*ISET_CONTRACTS, files=[ISET]))
    assert match.accepted and len(match.matched) == len(ISET_CONTRACTS)
    return match


def _verify(build: Path, units: list[str], *flags: str):
    done = instrumented(build, linked(build, units))
    assert IGNORED_CLAUSE_WARNING not in done.stdout + done.stderr
    return cbmc_results(build, "dfcc.goto", *flags)


def test_annotated_iset_compiles_and_dfcc_checks_the_contract(needs_cbmc, tmp_path):
    build, pp = _iset_build(tmp_path)
    insertions = [contract_insertion(matched) for matched in _iset_sites(pp, build).matched]
    units = _write_units(build, {**_trusted_units(build), "iset.i": annotate(pp, insertions)})
    results = _verify(build, units, *MANDATORY_CHECK_FLAGS)
    assert not failed(results)
    classes = loop_properties(results, LOWER_BOUND_LOOP)
    assert classes == {name: {PROPERTY_HOLDS} for name in LOOP_PROPERTY_CLASSES}


def test_without_the_annotation_dfcc_reports_no_loop_property(needs_cbmc, tmp_path):
    build, pp = _iset_build(tmp_path)
    units = _write_units(build, {**_trusted_units(build), "iset.i": pp})
    results = _verify(build, units, *MANDATORY_CHECK_FLAGS)
    assert loop_properties(results, LOWER_BOUND_LOOP) == {}
    assert any(result["property"] == f"{LOWER_BOUND_LOOP[:-2]}.unwind.0" for result in failed(results))


def test_probes_in_the_annotated_vacuity_build_are_reached(needs_cbmc, tmp_path):
    build, pp = _iset_build(tmp_path)
    match = _iset_sites(pp, build)
    entries = function_entries(ISET, pp, [CLAIMED_FUNCTION, UNCALLED_FUNCTION])
    assert not entries.problems and len(entries.entries) == 2
    insertions: list[Insertion] = [
        *(contract_insertion(matched) for matched in match.matched),
        *(loop_probe_insertion(matched) for matched in match.matched),
        *(function_probe_insertion(entry) for entry in entries.entries),
    ]
    trusted = _trusted_units(build, "-DLEANC_VACUITY_CHECK")
    units = _write_units(build, {**trusted, "iset.i": annotate(pp, insertions)})
    results = _verify(build, units)
    for label in (function_probe_label(CLAIMED_FUNCTION), loop_probe_label(ISET, LOWER_BOUND_LINE)):
        reported = described(results, label)
        assert reported and {result["status"] for result in reported} == {PROPERTY_FAILED}
    assert {r["status"] for r in described(results, "leanc_reach:rank_returned")} == {PROPERTY_FAILED}
    assert described(results, function_probe_label(UNCALLED_FUNCTION)) == []
