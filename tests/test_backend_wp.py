from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanc.backends.counterexample import fingerprint
from leanc.backends.wp import build_wp_argv, goal_kind, parse_console, run_wp
from leanc.backends.wp import gate as wp_gate
from leanc.backends.wp.assumptions import compile_statements
from leanc.foundation.runner import ProcResult
from leanc.foundation.status import Outcome, Status
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec
from leanc.toolchain.context import BackendContext, make_context
from leanc.toolchain.framac import (
    INSTALL_HINT,
    FramaC,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


ADD_H = """\
#ifndef ADD_H
#define ADD_H
{contract}
int add(int a, int b);
#endif
"""

GOOD_CONTRACT = """\
/*@ requires -1000 <= a <= 1000 && -1000 <= b <= 1000;
    assigns \\nothing;
    ensures \\result == a + b; */"""

ADD_C = '#include "add.h"\n\nint add(int a, int b)\n{\n    return a + b;\n}\n'

SUM_H = """\
#ifndef SUM_H
#define SUM_H
#include <stddef.h>
/*@ requires n <= 1000;
    requires \\valid_read(a + (0 .. n - 1));
    requires \\forall integer k; 0 <= k < n ==> 0 <= a[k] <= 1000;
    assigns \\nothing;
    ensures \\result <= 1000 * n; */
unsigned count_small(const int *a, size_t n);
#endif
"""

SUM_NOINV = """\
#include "sum.h"

unsigned count_small(const int *a, size_t n)
{
    unsigned c = 0;
    for (size_t i = 0; i < n; i++) {
        if (a[i] < 10) {
            c++;
        }
    }
    return c;
}
"""

SUM_INV = SUM_NOINV.replace(
    "    for (size_t i = 0;",
    "    /*@ loop invariant 0 <= i <= n;\n"
    "        loop invariant c <= i;\n"
    "        loop assigns i, c;\n"
    "        loop variant n - i;\n"
    "    */\n"
    "    for (size_t i = 0;",
)


def make_project(
    tmp_path: Path,
    *,
    header: str,
    src: str,
    function: str = "add",
    header_name: str = "add.h",
    wp: dict | None = None,
    name: str = "proj",
) -> BackendContext:
    root = tmp_path / name
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "include" / header_name).write_text(header)
    (root / "src" / "impl.c").write_text(src)
    data = {
        "spec_id": "wp-unit",
        "version": 1,
        "title": "wp unit test",
        "requirement": "test",
        "target": {"function": function, "signature": f"int {function}(...)", "returns": {"type": "int"}},
        "sources": {"editable": ["src/impl.c"], "headers": [f"include/{header_name}"], "include_dirs": ["include"]},
        "wp": {"provers": ["alt-ergo"], "prover_timeout_s": 2, "timeout_s": 120, **(wp or {})},
        "gates": ["compile", "wp"],
    }
    spec = Spec.model_validate(data)
    return make_context(spec, root, root / ".leanc" / "runs" / "r1")


def _kinds(res) -> set[str]:
    return {f.kind for f in res.failures}


def test_proved_contract_passes(tmp_path, needs_framac):
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C)
    res = run_wp(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.status is None and not res.failures
    d = res.details
    assert d["goals_proved"] == d["goals_total"] > 0
    assert d["smoke_tests"]["total"] >= 1 and d["smoke_tests"]["passed"] == d["smoke_tests"]["total"]
    assert d["frama_c_version"] and d["prover_versions"].get("alt-ergo") is not None
    assert d["contract"] and d["contract"][0]["file"] == "include/add.h"
    assert any(g["kind"].startswith("runtime-error guard") for g in d["goals"])
    argv = d["argv"]["wp"]
    for flag in ("-wp", "-wp-rte", "-wp-smoke-tests"):
        assert flag in argv
    assert argv[argv.index("-wp-cache") + 1] == "none"
    assert "-wp-prop=-@terminates,-@decreases" in argv
    assert (ctx.run_dir / d["raw"]["report_json"]).is_file() and (ctx.run_dir / d["raw"]["wp_log"]).is_file()
    text = " ".join(d["assumptions"])
    assert "ALL inputs" in text and "partial correctness" in text and "Typed memory model" in text
    assert "x86_64" in text and "libc" in text and "alt-ergo" in text
    assert res.invocations and {i.tool for i in res.invocations} <= {"frama-c", "why3"}


def test_missing_loop_invariant_is_inconclusive_with_unproved_goals(tmp_path, needs_framac):
    ctx = make_project(tmp_path, header=SUM_H, src=SUM_NOINV, function="count_small", header_name="sum.h")
    res = run_wp(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert res.status is Status.UNKNOWN
    unproved = [f for f in res.failures if f.kind == "unproved"]
    assert unproved and all(f.tool == "frama-c-wp" and f.property_id and f.location for f in unproved)
    assert any("post-condition" in f.description for f in unproved)
    assert all("not proved" in f.description and f.explanation for f in unproved)
    assert {d.rule_id for d in res.diagnostics} == {"unproved"}
    for f in unproved:
        assert str(f.location.line) not in f.description.split("`")[0]
        assert fingerprint(f).startswith("frama-c-wp:unproved:")


def test_loop_invariants_make_it_pass_and_termination_can_be_required(tmp_path, needs_framac):
    ctx = make_project(tmp_path, header=SUM_H, src=SUM_INV, function="count_small", header_name="sum.h",
                       wp={"require_termination": True})
    res = run_wp(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    assert "-wp-prop=-@terminates,-@decreases" not in res.details["argv"]["wp"]
    assert any("total correctness" in a for a in res.details["assumptions"])
    ctx2 = make_project(tmp_path, header=SUM_H, src=SUM_INV.replace("        loop variant n - i;\n", ""),
                        function="count_small", header_name="sum.h", wp={"require_termination": True}, name="p2")
    res2 = run_wp(ctx2)
    assert res2.outcome is Outcome.INCONCLUSIVE
    assert any(f.property_id.endswith("_terminates") for f in res2.failures), [f.property_id for f in res2.failures]


def test_static_helper_contract_is_a_proof_obligation(tmp_path, needs_framac):
    helper = (
        '#include "add.h"\n\n'
        "/*@ requires -1000 <= x <= 1000;\n    assigns \\nothing;\n    ensures \\result == x; */\n"
        "static int ident(int x)\n{\n    return x;\n}\n\n"
        "int add(int a, int b)\n{\n    return ident(a) + ident(b);\n}\n"
    )
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=helper)
    assert source_violations(ctx.spec, "src/impl.c", helper) == []
    res = run_wp(ctx)
    assert res.outcome is Outcome.PASS, res.summary
    kinds = {g["kind"] for g in res.details["goals"]}
    assert "precondition of ident() at a call site" in kinds
    lying = helper.replace("ensures \\result == x;", "ensures \\result == x + 1;")
    res2 = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=lying, name="p2"))
    assert res2.outcome is Outcome.INCONCLUSIVE
    assert any(f.location and f.location.function == "ident" for f in res2.failures)


def test_contract_violation_is_not_pass(tmp_path, needs_framac):
    wrong = ADD_C.replace("return a + b;", "return a - b;")
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=wrong))
    assert res.outcome is not Outcome.PASS
    assert res.outcome in (Outcome.INCONCLUSIVE, Outcome.FAIL)
    assert any("post-condition" in f.description for f in res.failures)


def test_runtime_error_overflow_is_not_pass(tmp_path, needs_framac):
    unbounded = "/*@ assigns \\nothing;\n    ensures \\result == a + b; */"
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=unbounded), src=ADD_C))
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    rte = [f for f in res.failures if "signed overflow" in f.description]
    assert rte and all(f.kind == "unproved" for f in rte)


def test_inconsistent_approved_precondition_is_caught_by_smoke_tests(tmp_path, needs_framac):
    vacuous = "/*@ requires a > 0 && a < 0;\n    assigns \\nothing;\n    ensures \\result == a + b; */"
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=vacuous), src=ADD_C))
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    vac = [f for f in res.failures if f.kind == "vacuity"]
    assert vac and "inconsistent precondition" in vac[0].description
    assert "smoke test" in res.summary
    assert res.details["smoke_tests"]["passed"] < res.details["smoke_tests"]["total"]


def test_dead_code_is_caught_by_smoke_tests(tmp_path, needs_framac):
    dead = ADD_C.replace("    return a + b;", "    if (a > 5000) {\n        return 0;\n    }\n    return a + b;")
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=dead))
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert any(f.kind == "vacuity" and "dead code" in f.description for f in res.failures)


def test_no_approved_contract_is_inconclusive(tmp_path, needs_framac):
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract="/* plain comment, no ACSL */"), src=ADD_C))
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "no approved ACSL contract" in res.summary
    assert not res.invocations


def test_contract_not_seen_by_frama_c_is_not_pass(tmp_path, needs_framac):
    src = "int add(int a, int b)\n{\n    return a / 2 + b / 2;\n}\n"
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=src))
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert any(f.property_id == "contract-not-applied" for f in res.failures)


def test_extra_contract_on_target_in_editable_code_is_rejected(tmp_path, needs_framac):
    cheat = ADD_C.replace("int add(int a, int b)\n", "/*@ requires \\false; */\nint add(int a, int b)\n").replace(
        "return a + b;", "return 0;"
    )
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=cheat)
    vs = source_violations(ctx.spec, "src/impl.c", cheat, headers={"include/add.h": ADD_H.format(contract=GOOD_CONTRACT)})
    assert {v.rule for v in vs} == {"ACSL_CONTRACT"}
    res = run_wp(ctx)
    assert res.outcome is Outcome.INCONCLUSIVE and not res.invocations
    assert "ACSL_CONTRACT" in res.summary


@pytest.mark.parametrize(
    "snippet",
    [
        "/*@ axiomatic Cheat { axiom everything: \\false; } */\n",
        "/*@ admit lemma cheat: \\false; */\n",
        "/*@ logic integer f(integer x) = f(x) + 1; */\n",
    ],
)
def test_assumption_injecting_acsl_in_editable_code_is_rejected(tmp_path, needs_framac, snippet):
    src = ADD_C + snippet
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=src)
    assert {v.rule for v in source_violations(ctx.spec, "src/impl.c", src)} == {"ACSL_FORBIDDEN"}
    res = run_wp(ctx)
    assert res.outcome is not Outcome.PASS and not res.invocations


def test_admit_inside_the_body_is_rejected(tmp_path, needs_framac):
    src = ADD_C.replace("    return a + b;", "    /*@ admit a + b == 0; */\n    return a + b;")
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=src)
    assert "ACSL_FORBIDDEN" in {v.rule for v in source_violations(ctx.spec, "src/impl.c", src)}
    assert run_wp(ctx).outcome is not Outcome.PASS


def test_missing_frama_c_is_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANC_FRAMAC", str(tmp_path / "no-such-frama-c"))
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C))
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert "frama-c executable not found" in res.summary and "opam install frama-c alt-ergo" in res.summary
    assert INSTALL_HINT in res.summary


def test_too_old_frama_c_is_tool_error(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "frama-c"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho '25.0-beta (Manganese)'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("LEANC_FRAMAC", str(fake))
    res = run_wp(make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C))
    assert res.outcome is Outcome.ERROR and "required" in res.summary


def test_no_stale_cache_results_across_runs(tmp_path, needs_framac, monkeypatch):
    shared = tmp_path / "shared-cache"
    monkeypatch.setenv("FRAMAC_WP_CACHE", "update")
    monkeypatch.setenv("FRAMAC_WP_CACHEDIR", str(shared))
    ctx1 = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C)
    first = run_wp(ctx1)
    assert first.outcome is Outcome.PASS, first.summary
    root = ctx1.project_root
    (root / "src" / "impl.c").write_text(ADD_C.replace("return a + b;", "return a + b + 1;"))
    ctx2 = make_context(ctx1.spec, root, root / ".leanc" / "runs" / "r2")
    second = run_wp(ctx2)
    assert second.outcome is not Outcome.PASS, second.summary
    assert any("post-condition" in f.description for f in second.failures)
    for ctx, res in ((ctx1, first), (ctx2, second)):
        goals = json.loads((ctx.run_dir / res.details["raw"]["report_json"]).read_text())
        assert goals and not any(g.get("cached") for g in goals)
        assert "[Cache]" not in (ctx.run_dir / res.details["raw"]["wp_log"]).read_text()
        argv = res.details["argv"]["wp"]
        assert argv[argv.index("-wp-cache") + 1] == "none"
        assert argv[argv.index("-wp-session") + 1].startswith(str(ctx.build_dir))
    assert not shared.exists()


class _FakeRun:
    def __init__(self, goals, console, *, exit_code=0, timed_out=False, truncated=False):
        self.goals, self.console = goals, console
        self.exit_code, self.timed_out, self.truncated = exit_code, timed_out, truncated

    def __call__(self, ctx_self, argv, **kw):
        if "-wp-report-json" in argv and self.goals is not None:
            Path(argv[argv.index("-wp-report-json") + 1]).write_text(json.dumps(self.goals))
        return ProcResult(argv=list(argv), cwd=str(kw.get("cwd")), exit_code=self.exit_code, signal=None,
                          timed_out=self.timed_out, output_truncated=self.truncated, duration_s=0.1,
                          stdout=self.console, stderr="")


def _goal(name, *, passed=True, verdict="valid", smoke=False, **kw):
    return {"goal": f"typed_add_{name}", "property": f"add_{name}", "file": "src/impl.c", "line": 3,
            "function": "add", "smoke": smoke, "passed": passed, "verdict": verdict, "provers": [], **kw}


PASSING = [_goal("ensures"), _goal("assigns"), _goal("assert_rte_signed_overflow"),
           _goal("wp_smoke_default_requires", smoke=True, verdict="timeout")]
PASS_CONSOLE = "[wp] 4 goals scheduled\n[wp] Proved goals:    6 / 6\n  Terminating:     1\n  Unreachable:     1\n" \
               "  Qed:             3\n  Smoke Tests:     1 / 1\n"


@pytest.fixture
def fake_framac(tmp_path, monkeypatch):
    fc = FramaC(frama_c="/fake/bin/frama-c", real="/fake/bin/frama-c", bin_dir="/fake/bin", prefix="/fake",
                       why3=None)
    monkeypatch.setattr(wp_gate, "find_framac", lambda: fc)
    monkeypatch.setattr(wp_gate, "tool_version", lambda path, flag="--version": "33.0 (Arsenic)")

    def install(goals, console, **kw):
        fake = _FakeRun(goals, console, **kw)
        monkeypatch.setattr(BackendContext, "run", lambda self, argv, **kwargs: fake(self, argv, **kwargs))
        return make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C)

    return install


def test_fake_consistent_output_passes(fake_framac):
    res = run_wp(fake_framac(PASSING, PASS_CONSOLE))
    assert res.outcome is Outcome.PASS, res.summary


def test_fake_cache_hit_is_error(fake_framac):
    res = run_wp(fake_framac(PASSING, "[wp] [Cache] found:3\n" + PASS_CONSOLE))
    assert res.outcome is Outcome.ERROR and "cache" in res.summary.lower()
    goals = [dict(g) for g in PASSING]
    goals[0]["cached"] = 1
    assert run_wp(fake_framac(goals, PASS_CONSOLE)).outcome is Outcome.ERROR


def test_fake_inconsistent_totals_are_error(fake_framac):
    res = run_wp(fake_framac(PASSING, PASS_CONSOLE.replace("6 / 6", "9 / 9")))
    assert res.outcome is Outcome.ERROR and "inconsistent" in res.summary


def test_fake_missing_summary_or_report_is_error(fake_framac):
    assert run_wp(fake_framac(PASSING, "[wp] 4 goals scheduled\n")).outcome is Outcome.ERROR
    assert run_wp(fake_framac(None, PASS_CONSOLE)).outcome is Outcome.ERROR
    assert run_wp(fake_framac({"not": "a list"}, PASS_CONSOLE)).outcome is Outcome.ERROR


def test_fake_invalid_goal_is_fail(fake_framac):
    goals = [dict(g) for g in PASSING]
    goals[0].update(passed=False, verdict="invalid")
    res = run_wp(fake_framac(goals, PASS_CONSOLE.replace("6 / 6", "5 / 6")))
    assert res.outcome is Outcome.FAIL and res.status is Status.COUNTEREXAMPLE_FOUND
    assert res.failures[0].kind == "counterexample"


def test_fake_prover_failure_timeout_and_crash(fake_framac):
    goals = [dict(g) for g in PASSING]
    goals[0].update(passed=False, verdict="failed")
    assert run_wp(fake_framac(goals, PASS_CONSOLE.replace("6 / 6", "5 / 6"))).outcome is Outcome.ERROR
    res = run_wp(fake_framac(PASSING, "", timed_out=True))
    assert res.outcome is Outcome.TIMEOUT and res.status is Status.TIMEOUT
    assert run_wp(fake_framac(PASSING, PASS_CONSOLE, truncated=True)).outcome is Outcome.ERROR
    crash = "[kernel] User Error: something\n[kernel] Frama-C aborted: invalid user input.\n"
    assert run_wp(fake_framac(PASSING, crash, exit_code=1)).outcome is Outcome.ERROR


def test_fake_missing_specification_is_inconclusive(fake_framac):
    console = ("[kernel:annot:missing-spec] src/impl.c:4: Warning: \n  Neither code nor specification for function "
               "ext, generating default exits, assigns and terminates.\n" + PASS_CONSOLE)
    res = run_wp(fake_framac(PASSING, console))
    assert res.outcome is Outcome.INCONCLUSIVE and "ext" in res.summary


def test_non_automatic_prover_is_rejected(tmp_path, fake_framac):
    ctx = fake_framac(PASSING, PASS_CONSOLE)
    ctx.spec.wp.provers = ["script"]
    res = run_wp(ctx)
    assert res.outcome is Outcome.ERROR and "not an automatic prover" in res.summary


def test_parse_console_summary_and_messages():
    text = (
        "[kernel] Parsing src/impl.c (with preprocessing)\n"
        "[wp] src/impl.c:9: Warning: Missing assigns clause (assigns 'everything' instead)\n"
        "[wp] 11 goals scheduled\n"
        "[wp] [Timeout] typed_f_ensures (Qed 3ms) (Alt-Ergo)\n"
        "[wp] Proved goals:    6 / 12\n"
        "  Unreachable:     1\n"
        "  Qed:             0 (0.91ms-3ms-7ms)\n"
        "  Alt-Ergo :       5\n"
        "  Timeout:         6\n"
        "  Smoke Tests:     5 / 5\n"
    )
    c = parse_console(text)
    assert (c.proved, c.total) == (6, 12) and c.smoke == (5, 5)
    assert c.categories["Unreachable"] == 1 and c.categories["Timeout"] == 6 and c.categories["Alt-Ergo"] == 5
    assert len(c.warnings) == 1 and not c.errors and not c.cached


def test_goal_kinds():
    def g(prop, **kw):
        return {"goal": f"typed_f_{prop}", "property": f"f_{prop}", "function": "f", **kw}

    assert goal_kind(g("ensures_2")) == "post-condition"
    assert goal_kind(g("assert_rte_mem_access_2")) == "runtime-error guard (mem access)"
    assert goal_kind(g("loop_invariant_2_preserved")) == "loop invariant (preserved)"
    assert goal_kind(g("call_memcpy_requires_valid_dest")) == "precondition of memcpy() at a call site"
    assert goal_kind(g("in_range_ensures", behavior="in_range")) == "post-condition"
    assert goal_kind(g("terminates")) == "termination"
    assert goal_kind(g("wp_smoke_default_requires")) == "smoke test"
    assert goal_kind({"goal": "typed_f_call_h_2_requires", "property": "h_requires_2", "function": "f"}) == \
        "precondition of h() at a call site"
    assert goal_kind({"goal": "typed_h_wp_smoke_dead_call_s5", "property": "h_wp_smoke_dead_call_s5",
                      "function": "f", "smoke": True}) == "smoke test"


def test_argv_validates_preprocessor_arguments(tmp_path):
    ctx = make_project(tmp_path, header=ADD_H.format(contract=GOOD_CONTRACT), src=ADD_C)
    fc = FramaC(frama_c="frama-c", real="frama-c", bin_dir="/x/bin", prefix="/x", why3=None)
    argv = build_wp_argv(ctx, fc, report_json=tmp_path / "r.json", work_dir=tmp_path / "w", why3_conf=None)
    assert "-cpp-extra-args=-Iinclude" in argv and argv[-1] == "src/impl.c"
    ctx.spec.compile.defines = ["X=1; rm -rf /"]
    with pytest.raises(ValueError):
        build_wp_argv(ctx, fc, report_json=tmp_path / "r.json", work_dir=tmp_path / "w", why3_conf=None)


def test_example_specs_declare_wp_gate():
    for name in ("wp_safe_array_get", "wp_proof_repair", "wp_contract_violation"):
        data = yaml.safe_load((EXAMPLES / name / "spec.yaml").read_text())
        assert "wp" in data["gates"] and data["wp"]["provers"]


def test_wp_discloses_the_compile_flags_and_standard_it_does_not_apply():
    spec = load_spec(EXAMPLES / "wp_safe_array_get" / "spec.yaml")
    spec.compile.flags = ["-Wall", "-funsigned-char", "-O2", "-g"]
    statements = compile_statements(spec)
    assert any(s.startswith("compile.flags not applied by Frama-C: -funsigned-char, -O2 - ") for s in statements)
    assert any(s.startswith(f"C language standard {spec.language.standard}: ") and "c11" in s for s in statements)
    spec.compile.flags = ["-Wall", "-Wextra"]
    assert not any("compile.flags" in s for s in compile_statements(spec))
