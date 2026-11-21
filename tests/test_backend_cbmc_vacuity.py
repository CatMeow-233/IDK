from __future__ import annotations

from cbmc_support import make_cbmc_project

from leanc.backends.cbmc import run_cbmc
from leanc.foundation.status import Outcome, Status
from leanc.specs.loader import load_spec
from leanc.toolchain.context import BackendContext, make_context


def test_contradictory_assumptions_are_vacuous(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_contradictory_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    assert res.details["properties_failed"] == 0
    assert res.details["vacuity"]["status"] == "vacuous"
    assert res.details["vacuity"]["probes"] == {"after_call": False}
    (f,) = res.failures
    assert f.kind == "vacuity" and f.id == "cbmc:vacuity:after_call"
    assert f.location.file == "harness/h.c"
    assert f.raw_ref == "raw/cbmc-vacuity.json"
    assert "after_call" in res.summary and "vacuous" in res.summary
    assert [d.rule_id for d in res.diagnostics] == ["vacuity"]


def test_partially_vacuous_harness_names_the_probe(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_partial_vacuity_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert res.details["vacuity"]["probes"] == {"after_call": True, "huge_array": False}
    assert [f.id for f in res.failures] == ["cbmc:vacuity:huge_array"]
    assert "huge_array" in res.summary and "after_call" not in res.summary


def test_no_probes_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_no_probe_harness", function="arr_get")
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "no LEANC_REACH probes" in res.summary and "vacuity cannot be checked" in res.summary
    assert res.details["vacuity"]["status"] == "unchecked"
    assert len(res.invocations) == 1
    assert res.failures == []


def test_vacuity_check_disabled_is_inconclusive(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(
        tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get", vacuity_check=False
    )
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "vacuity check disabled in spec; cannot exclude vacuous verification" in res.summary
    assert res.details["vacuity"]["status"] == "disabled"
    assert len(res.invocations) == 1


def _patch_harness(ctx: BackendContext, old: str, new: str, count: int = 1) -> None:
    h = ctx.snapshot_dir / ctx.spec.verification.harness
    text = h.read_text()
    assert text.count(old) >= count, old
    h.write_text(text.replace(old, new, count))


def _example_with_harness_edits(example_copy, tmp_path, edits: list[tuple[str, str]]):
    root = example_copy("safe_array_get")
    h = root / "harness" / "safe_array_get_harness.c"
    text = h.read_text()
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    h.write_text(text)
    spec = load_spec(root / "spec.yaml")
    return run_cbmc(make_context(spec, root, tmp_path / "dup-run"))


_RELABEL = [
    ('LEANC_REACH("null_input")', 'LEANC_REACH("branch")'),
    ('LEANC_REACH("out_of_range")', 'LEANC_REACH("branch")'),
    ('LEANC_REACH("in_range")', 'LEANC_REACH("branch")'),
]
_OVERCONSTRAIN = [("LEANC_ASSUME(len <= MAX_LEN);", "LEANC_ASSUME(len <= MAX_LEN);\n    LEANC_ASSUME(idx >= len);")]


def test_duplicate_labels_cannot_hide_an_unreachable_probe(example_copy, tmp_path, needs_cbmc):
    res = _example_with_harness_edits(example_copy, tmp_path, _OVERCONSTRAIN + _RELABEL)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    vac = res.details["vacuity"]
    assert vac["status"] == "vacuous"
    assert vac["duplicate_labels"] == {"branch": 3}
    assert vac["probes"] == {"after_call": True, "branch": False}
    reach = sorted(p["reachable"] for p in vac["properties"].values() if p["label"] == "branch")
    assert reach == [False, True, True]
    (f,) = res.failures
    assert f.kind == "vacuity" and f.id.startswith("cbmc:vacuity:branch@harness/safe_array_get_harness.c:")
    assert f.location.line == 43
    assert "branch (harness/safe_array_get_harness.c:43)" in res.summary
    assert any("duplicate LEANC_REACH label" in n for n in res.details["notes"])


def test_unique_labels_flag_the_same_unreachable_branch(example_copy, tmp_path, needs_cbmc):
    res = _example_with_harness_edits(example_copy, tmp_path, _OVERCONSTRAIN)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert [f.id for f in res.failures] == ["cbmc:vacuity:in_range"]


def test_duplicate_labels_all_reachable_pass_with_note(example_copy, tmp_path, needs_cbmc):
    res = _example_with_harness_edits(example_copy, tmp_path, _RELABEL)
    assert res.outcome is Outcome.PASS, res.summary
    vac = res.details["vacuity"]
    assert vac["status"] == "ok" and vac["duplicate_labels"] == {"branch": 3}
    assert len(vac["properties"]) == 4 and "all 4 vacuity probe(s) reachable" in res.summary
    assert any("duplicate LEANC_REACH label" in n for n in res.details["notes"])


def test_compiled_out_probe_counts_as_unreachable(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, '    LEANC_REACH("after_call");\n', '    LEANC_REACH("after_call");\n#if 0\n    LEANC_REACH("ghost");\n#endif\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    vac = res.details["vacuity"]
    assert vac["occurrences"] == 4 and len(vac["properties"]) == 3 and vac["unreported_occurrences"] == 1
    assert vac["probes"]["ghost"] is False
    (f,) = res.failures
    assert f.id == "cbmc:vacuity:ghost" and "not reported" in f.description
    assert "not reported by CBMC (ghost)" in res.summary


def test_partially_reported_duplicate_label_is_vacuous(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, "    free(arr);\n", '#ifdef NEVER_DEFINED\n    LEANC_REACH("after_call");\n#endif\n    free(arr);\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    (f,) = res.failures
    assert f.id == "cbmc:vacuity:after_call@unreported"
    assert f.description.startswith('1 of 2 occurrence(s) of LEANC_REACH("after_call")')


def test_unreported_probe_with_non_literal_label(tmp_path, needs_cbmc):
    ctx = make_cbmc_project(tmp_path, src="arr_get_ok", harness="arr_get_harness", function="arr_get")
    _patch_harness(ctx, "    free(arr);\n", '#define GHOST_LABEL "ghost"\n#if 0\n    LEANC_REACH(GHOST_LABEL);\n#endif\n    free(arr);\n')
    res = run_cbmc(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert [f.id for f in res.failures] == ["cbmc:vacuity:unreported-probes"]
    assert "non-literal labels" in res.summary


def test_overridden_assert_machinery_is_error(tmp_path, needs_cbmc):
    honest = make_cbmc_project(tmp_path / "honest", src="arr_get_wrong_code", harness="arr_get_harness", function="arr_get")
    res = run_cbmc(honest)
    assert res.outcome is Outcome.FAIL
    assert any(f.property_id.startswith("main.assertion.") for f in res.failures)
    cheat = make_cbmc_project(
        tmp_path / "cheat", src="arr_get_assert_override", harness="arr_get_harness", function="arr_get"
    )
    res = run_cbmc(cheat)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR, res.summary
    assert res.summary.startswith(
        "harness assertions missing from CBMC's property list (assert machinery overridden?)"
    )
    assert res.details["harness_assertions"] == {"in_source": 4, "cbmc_properties": 0}
    assert res.diagnostics[-1].rule_id == "missing-assertions"
    assert res.failures == [] and len(res.invocations) == 1
