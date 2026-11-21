from __future__ import annotations

import dataclasses
import json
import shutil
import textwrap
from pathlib import Path

import pytest

from leanc.backends.lean import STANDARD_AXIOMS, parse_lean_messages, render_checker, run_lean
from leanc.backends.lean import gate as lean_gate
from leanc.foundation.status import Outcome, Status
from leanc.lang.lean_lexer import LeanSource
from leanc.repair.policy import RULES, check_candidate, check_generated
from leanc.rules.lean_rules import lean_layout, lean_source_violations
from leanc.specs.loader import load_spec
from leanc.specs.model import Spec
from leanc.toolchain.context import VerifyOptions, make_context
from leanc.toolchain.lean import INSTALL_HINT, find_lean

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def needs_lean(needs_bwrap):
    if find_lean() is None:
        pytest.skip("Lean 4 toolchain not installed")


SPEC_LEAN = textwrap.dedent(
    """\
    namespace T

    def double (n : Nat) : Nat := n + n

    def double_even : Prop := ∀ n : Nat, double n % 2 = 0
    def double_mono : Prop := ∀ a b : Nat, a ≤ b → double a ≤ double b
    def four : Prop := (2 : Nat) + 2 = 4

    end T
    """
)

GOOD_PROOFS = textwrap.dedent(
    """\
    import Spec

    namespace T.P
    open T

    -- no sorry here: this comment must not trip the policy
    theorem double_even_proof : double_even := by
      intro n; unfold double; omega

    theorem double_mono_proof : double_mono := by
      intro a b h; unfold double; omega

    theorem four_proof : four := by unfold four; decide

    end T.P
    """
)

THEOREMS = [
    {"name": "double_even", "statement": "T.double_even", "proof": "T.P.double_even_proof"},
    {"name": "double_mono", "statement": "T.double_mono", "proof": "T.P.double_mono_proof"},
    {"name": "four", "statement": "T.four", "proof": "T.P.four_proof"},
]


def make_spec(proofs: list[str] | None = None, theorems: list[dict] | None = None, **lean_extra) -> Spec:
    return Spec.model_validate(
        {
            "spec_id": "lean-test",
            "version": 1,
            "title": "lean gate test",
            "requirement": "test",
            "target": {"function": "f", "signature": "int f(int x)", "returns": {"type": "int"}},
            "sources": {"editable": ["src/f.c"], "headers": ["include/f.h"], "include_dirs": ["include"]},
            "lean": {
                "spec_file": "lean/Spec.lean",
                "proofs": proofs or ["lean/Proofs.lean"],
                "theorems": theorems or THEOREMS,
                "timeout_s": 120,
                **lean_extra,
            },
            "gates": ["compile", "lean"],
        }
    )


def project(tmp_path: Path, proofs: dict[str, str] | str = GOOD_PROOFS, spec_lean: str = SPEC_LEAN,
            **spec_kw) -> tuple[Path, Spec]:
    root = tmp_path / "proj"
    (root / "lean").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "include").mkdir()
    (root / "include" / "f.h").write_text("int f(int x);\n")
    (root / "src" / "f.c").write_text('#include "f.h"\nint f(int x) { return x; }\n')
    (root / "lean" / "Spec.lean").write_text(spec_lean)
    if isinstance(proofs, str):
        proofs = {"lean/Proofs.lean": proofs}
    for rel, text in proofs.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    spec = make_spec(proofs=list(proofs), **spec_kw)
    return root, spec


def gate(root: Path, spec: Spec, run: str = "run"):
    ctx = make_context(spec, root, root / ".leanc" / run, VerifyOptions())
    return run_lean(ctx), ctx


def theorem(res, name: str) -> dict:
    return next(t for t in res.details["theorems"] if t["name"] == name)


def unproved(res) -> dict[str, str]:
    return {f.property_id: f.description for f in res.failures if f.kind == "unproved"}


def test_proved_theorems_pass(tmp_path, needs_lean):
    root, spec = project(tmp_path)
    res, ctx = gate(root, spec)
    assert res.outcome is Outcome.PASS, (res.summary, res.diagnostics)
    assert res.status is None and not res.failures
    assert all(t["proved"] for t in res.details["theorems"])
    assert set(theorem(res, "double_even")["axioms"]) <= set(STANDARD_AXIOMS)
    assert res.details["replay"]["modules"] == {"Proofs": "ok"}
    (assumption,) = res.details["assumptions"]
    assert "MODEL" in assumption and "lean/Spec.lean" in assumption and "NOT linked" in assumption
    assert all(inv.sandbox == "bwrap" for inv in res.invocations)
    assert (ctx.build_dir / "lean" / "check" / "LeancCheck.lean").is_file()
    assert not list(ctx.snapshot_dir.rglob("*.olean"))


def test_sorry_proof_is_inconclusive(tmp_path, needs_lean):
    proofs = GOOD_PROOFS.replace("intro a b h; unfold double; omega", "sorry")
    root, spec = project(tmp_path, proofs)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE and res.status is Status.UNKNOWN
    bad = unproved(res)
    assert list(bad) == ["double_mono"] and "sorry" in bad["double_mono"]
    f = next(f for f in res.failures if f.kind == "unproved")
    assert f.tool == "lean" and f.id == "lean:unproved:double_mono" and f.raw_ref
    assert "sorryAx" in theorem(res, "double_mono")["axioms"]
    assert theorem(res, "double_even")["proved"] and theorem(res, "four")["proved"]
    assert "assumptions" not in res.details


def test_wrong_statement_type_is_inconclusive(tmp_path, needs_lean):
    proofs = GOOD_PROOFS.replace(
        "theorem double_mono_proof : double_mono := by\n  intro a b h; unfold double; omega",
        "theorem double_mono_proof : ∀ a : Nat, double a ≤ double a := by\n  intro a; exact Nat.le_refl _",
    )
    root, spec = project(tmp_path, proofs)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "kernel rejected" in unproved(res)["double_mono"]


def test_unfolded_statement_is_accepted_by_the_kernel(tmp_path, needs_lean):
    proofs = GOOD_PROOFS.replace(
        "theorem four_proof : four := by unfold four; decide", "theorem four_proof : (2 : Nat) + 2 = 4 := rfl"
    )
    root, spec = project(tmp_path, proofs)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.PASS, res.summary


def test_missing_proof_constant_and_missing_file(tmp_path, needs_lean):
    proofs = GOOD_PROOFS.replace("theorem four_proof : four := by unfold four; decide\n", "")
    root, spec = project(tmp_path, proofs)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "not defined" in unproved(res)["four"]

    root2, spec2 = project(tmp_path / "b", {"lean/Proofs.lean": GOOD_PROOFS, "lean/More.lean": "import Spec\n"})
    (root2 / "lean" / "More.lean").unlink()
    res2, _ = gate(root2, spec2)
    assert res2.outcome is Outcome.PASS
    assert res2.details["proof_files"]["lean/More.lean"] == "missing"


def test_custom_axiom_is_never_accepted(tmp_path, needs_lean):
    spec_lean = SPEC_LEAN.replace("end T", "axiom cheat : False\n\nend T")
    proofs = GOOD_PROOFS.replace("intro a b h; unfold double; omega", "exact cheat.elim")
    root, spec = project(tmp_path, proofs, spec_lean=spec_lean)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert "T.cheat" in unproved(res)["double_mono"]
    assert "T.cheat" in theorem(res, "double_mono")["axioms"]


def test_native_decide_is_never_accepted(tmp_path, needs_lean):
    spec_lean = SPEC_LEAN.replace("end T", "theorem four_by_eval : four := by unfold four; native_decide\n\nend T")
    proofs = GOOD_PROOFS.replace("theorem four_proof : four := by unfold four; decide", "theorem four_proof : four := four_by_eval")
    root, spec = project(tmp_path, proofs, spec_lean=spec_lean)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    axioms = theorem(res, "four")["axioms"]
    assert any(a == "Lean.ofReduceBool" or "native_decide" in a for a in axioms), axioms
    assert "non-standard axioms" in unproved(res)["four"]


@pytest.mark.parametrize(
    "snippet, rule",
    [
        ("axiom cheat : False", "LEAN_AXIOM"),
        ("theorem x : (2:Nat) + 2 = 4 := by native_decide", "LEAN_NATIVE"),
        ("set_option debug.skipKernelTC true", "LEAN_OPTION"),
        ('#eval IO.println "hi"', "LEAN_METAPROGRAM"),
        ("instance : Inhabited Nat := ⟨1⟩", "LEAN_INSTANCE"),
    ],
)
def test_gate_rejects_forbidden_constructs_without_compiling(tmp_path, needs_lean, snippet, rule):
    proofs = GOOD_PROOFS.replace("end T.P", f"{snippet}\n\nend T.P")
    root, spec = project(tmp_path, proofs)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    pol = [f for f in res.failures if f.kind == "policy"]
    assert pol and pol[0].tool == "leanc-policy" and pol[0].property_id == rule
    assert pol[0].location and pol[0].location.file == "lean/Proofs.lean" and pol[0].location.line
    assert not any(s["step"].startswith("compile-lean/") for s in res.details["steps"])
    assert len(unproved(res)) == 3


def test_proof_compile_error_only_affects_that_file(tmp_path, needs_lean):
    good = "import Spec\nnamespace T.P\nopen T\ntheorem double_even_proof : double_even := by\n" \
           "  intro n; unfold double; omega\nend T.P\n"
    broken = "import Spec\nimport Proofs\nnamespace T.P\nopen T\n" \
             "theorem double_mono_proof : double_mono := by\n  simp\n" \
             "theorem four_proof : four := by unfold four; decide\nend T.P\n"
    root, spec = project(tmp_path, {"lean/Proofs.lean": good, "lean/Broken.lean": broken})
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert theorem(res, "double_even")["proved"]
    assert set(unproved(res)) == {"double_mono", "four"}
    assert "does not compile" in res.details["proof_files"]["lean/Broken.lean"]
    errs = [d for d in res.diagnostics if d.tool == "lean" and d.severity == "error"]
    assert errs and errs[0].location.file == "lean/Broken.lean" and errs[0].location.line == 6


def test_unapproved_layout_and_spec_errors_are_tool_errors(tmp_path, needs_lean):
    root, spec = project(tmp_path, theorems=[{"name": "nope", "statement": "T.double", "proof": "T.P.four_proof"}])
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.ERROR and "not a `Prop`" in res.summary

    root2, spec2 = project(tmp_path / "b", spec_lean="namespace T\ndef oops : Prop := Nat\nend T\n")
    res2, _ = gate(root2, spec2)
    assert res2.outcome is Outcome.ERROR and "does not compile" in res2.summary


def test_compile_time_io_is_sandboxed(tmp_path, needs_lean):
    outside = tmp_path / "pwned.txt"
    snapshot_spec = tmp_path / "proj" / ".leanc" / "run" / "snapshot" / "lean" / "Spec.lean"
    probe = textwrap.dedent(
        f"""\
        #eval show IO Unit from do
          try IO.FS.writeFile "{outside}" "x"; IO.println "WRITE-OUTSIDE-DONE"
          catch e => IO.println s!"WRITE-OUTSIDE-FAILED {{e}}"
        #eval show IO Unit from do
          try IO.FS.writeFile "{tmp_path / 'proj' / 'lean' / 'Spec.lean'}" "x"; IO.println "WRITE-PROJECT-OK"
          catch e => IO.println s!"WRITE-PROJECT-FAILED {{e}}"
        #eval show IO Unit from do
          try IO.FS.writeFile "{snapshot_spec}" "x"; IO.println "WRITE-SNAPSHOT-OK"
          catch e => IO.println s!"WRITE-SNAPSHOT-FAILED {{e}}"
        #eval show IO Unit from do
          let s ← IO.FS.readFile "/proc/net/dev"
          IO.println s!"NETDEV {{s}}"
        """
    )
    root, spec = project(tmp_path, spec_lean=SPEC_LEAN + probe)
    res, ctx = gate(root, spec)
    assert res.outcome is Outcome.PASS, res.summary
    step = next(s for s in res.details["steps"] if s["step"] == "compile-spec")
    text = (ctx.run_dir / step["raw"]).read_text()
    assert "WRITE-OUTSIDE" in text and not outside.exists()
    assert "WRITE-PROJECT-FAILED" in text and "WRITE-PROJECT-OK" not in text
    assert "WRITE-SNAPSHOT-FAILED" in text and "WRITE-SNAPSHOT-OK" not in text
    assert (root / "lean" / "Spec.lean").read_text().startswith("namespace T")
    assert snapshot_spec.read_text().startswith("namespace T")
    netdev = json.loads(next(ln for ln in text.splitlines() if "NETDEV" in ln))["data"]
    ifaces = [ln.split(":", 1)[0].strip() for ln in netdev.splitlines()[1:] if ":" in ln]
    assert ifaces == ["lo"]

    root2, spec2 = project(tmp_path / "b", GOOD_PROOFS + probe)
    res2, _ = gate(root2, spec2)
    assert res2.outcome is Outcome.INCONCLUSIVE
    assert {f.property_id for f in res2.failures if f.kind == "policy"} >= {"LEAN_METAPROGRAM", "LEAN_OBFUSCATION"}
    assert not outside.exists()


def test_missing_lean_is_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANC_LEAN", str(tmp_path / "no" / "lean"))
    assert find_lean() is None
    root, spec = project(tmp_path)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.ERROR and res.status is Status.TOOL_ERROR
    assert res.summary == INSTALL_HINT and "LEANC_LEAN" in res.summary


def test_lean_discovery_via_env(monkeypatch, needs_lean):
    tc = find_lean()
    monkeypatch.setenv("LEANC_LEAN", tc.prefix)
    assert find_lean() == tc
    monkeypatch.setenv("LEANC_LEAN", tc.lean)
    assert find_lean() == tc and tc.version and tc.leanchecker


@pytest.fixture
def clamp_project(tmp_path, needs_lean, needs_clang):
    root = tmp_path / "lean_clamp"
    shutil.copytree(EXAMPLES / "lean_clamp", root, ignore=shutil.ignore_patterns(".leanc"))
    full = json.loads((root / "mock" / "repair" / "round-2.json").read_text())["files"]["lean/Proofs.lean"]
    (root / "lean" / "Proofs.lean").write_text(full)
    return root


def test_example_with_full_proofs_passes_with_vectors(clamp_project):
    spec = load_spec(clamp_project / "spec.yaml")
    res, _ = gate(clamp_project, spec)
    assert res.outcome is Outcome.PASS, res.summary
    assert res.details["vectors"]["status"] == "pass" and res.details["vectors"]["count"] == 1331
    assert "1331 differential test vector(s)" in res.details["assumptions"][0]
    assert "not a proof of the C code" in res.details["assumptions"][0]


def test_vector_mismatch_is_fail(clamp_project):
    src = clamp_project / "src" / "clamp.c"
    src.write_text(src.read_text().replace("if (r > hi) {", "if (r >= hi && hi > 1000) {\n        r = hi - 1;\n    }\n    if (r > hi) {"))
    spec = load_spec(clamp_project / "spec.yaml")
    res, _ = gate(clamp_project, spec)
    assert res.outcome is Outcome.FAIL and res.status is Status.RUNTIME_TEST_FAILED
    mism = [f for f in res.failures if f.tool == "lean-vectors"]
    assert mism and all(f.kind == "test-oracle" and f.property_id == "vector-mismatch" for f in mism)
    names = {i.name for i in mism[0].inputs}
    assert {"x", "lo", "hi", "expected"} <= names
    assert all(t["proved"] for t in res.details["theorems"])


def test_driver_must_report_vector_count(clamp_project):
    drv = clamp_project / "tests" / "clamp_vectors.c"
    drv.write_text(drv.read_text().replace('printf("LEANC-VECTORS-CHECKED %ld\\n", checked);', ""))
    spec = load_spec(clamp_project / "spec.yaml")
    res, _ = gate(clamp_project, spec)
    assert res.outcome is Outcome.ERROR and "LEANC-VECTORS-CHECKED" in res.summary


def _approve_and_verify(root: Path, capsys) -> dict:
    from leanc.cli import main

    assert main(["spec", "approve", str(root / "spec.yaml"), "--by", "t", "--yes", "--json"]) == 0
    capsys.readouterr()
    main(["verify", str(root / "spec.yaml"), "--json"])
    return json.loads(capsys.readouterr().out)


def test_lean_pass_never_upgrades_to_proved(clamp_project, capsys, needs_cbmc):
    data = _approve_and_verify(clamp_project, capsys)
    assert data["status"] == "VERIFIED_BOUNDED"

    spec = clamp_project / "spec.yaml"
    spec.write_text(spec.read_text().replace("gates: [compile, static_analysis, sanitizer, cbmc, lean]",
                                             "gates: [compile, sanitizer, lean]").replace("version: 1", "version: 2"))
    data = _approve_and_verify(clamp_project, capsys)
    assert data["status"] == "UNKNOWN"


def test_sorry_proofs_are_proof_repairable(tmp_path, capsys, needs_lean, needs_toolchain):
    from leanc.pipeline.controller import verify
    from leanc.pipeline.repair_loop import proof_repairable

    root = tmp_path / "lean_clamp"
    shutil.copytree(EXAMPLES / "lean_clamp", root, ignore=shutil.ignore_patterns(".leanc"))
    from leanc.cli import main

    assert main(["spec", "approve", str(root / "spec.yaml"), "--by", "t", "--yes", "--json"]) == 0
    vr = verify(root / "spec.yaml", project_root=root)
    assert vr.status is Status.UNKNOWN and proof_repairable(vr)


PROOF_SPEC = make_spec(proofs=["lean/Proofs.lean"])
P = "lean/Proofs.lean"


def lean_rules(text: str) -> set[str]:
    return {v.rule for v in lean_source_violations(PROOF_SPEC, P, text)}


def test_lean_rules_are_registered():
    for r in ("LEAN_SORRY", "LEAN_AXIOM", "LEAN_NATIVE", "LEAN_OPTION", "LEAN_METAPROGRAM", "LEAN_IMPORT"):
        assert r in RULES


@pytest.mark.parametrize(
    "snippet, rule",
    [
        ("axiom bad : False", "LEAN_AXIOM"),
        ("unsafe def u : Nat := 1", "LEAN_UNSAFE"),
        ("opaque o : Nat", "LEAN_UNSAFE"),
        ("partial def loop (n : Nat) : Nat := loop n", "LEAN_UNSAFE"),
        ('@[extern "c_f"] def g (n : Nat) : Nat := n', "LEAN_UNSAFE"),
        ("@[implemented_by g] def h (n : Nat) : Nat := n", "LEAN_UNSAFE"),
        ("example : 1 = 1 := by native_decide", "LEAN_NATIVE"),
        ("example : 1 = 1 := by decide +native", "LEAN_NATIVE"),
        ("example : True := Lean.ofReduceBool _ _ rfl", "LEAN_NATIVE"),
        ("set_option debug.skipKernelTC true in\ntheorem t : True := trivial", "LEAN_OPTION"),
        ("#eval IO.println 1", "LEAN_METAPROGRAM"),
        ("#exit", "LEAN_METAPROGRAM"),
        ("run_cmd pure ()", "LEAN_METAPROGRAM"),
        ("example : Nat := by run_tac pure (); exact 1", "LEAN_METAPROGRAM"),
        ("example : Nat := by_elab pure (Lean.mkNatLit 1)", "LEAN_METAPROGRAM"),
        ("initialize pure ()", "LEAN_METAPROGRAM"),
        ("elab \"x\" : term => pure (Lean.mkNatLit 1)", "LEAN_METAPROGRAM"),
        ("macro \"x\" : term => `(1)", "LEAN_METAPROGRAM"),
        ("syntax \"x\" : term", "LEAN_METAPROGRAM"),
        ("notation \"⊕⊕\" => Nat.add", "LEAN_METAPROGRAM"),
        ("local infixl:65 \" +++ \" => Nat.add", "LEAN_METAPROGRAM"),
        ("instance : Inhabited Nat := ⟨5⟩", "LEAN_INSTANCE"),
        ("deriving instance Repr for Nat", "LEAN_INSTANCE"),
        ("export Nat (add)", "LEAN_INSTANCE"),
        ("@[instance] def i : Inhabited Nat := ⟨5⟩", "LEAN_ATTRIBUTE"),
        ("attribute [local instance] foo", "LEAN_ATTRIBUTE"),
        ("@[csimp] theorem c : True := trivial", "LEAN_ATTRIBUTE"),
        ('def s := s!"{1}"', "LEAN_OBFUSCATION"),
        ("def «weird name» := 1", "LEAN_OBFUSCATION"),
        ("def x := 1 -- \u202e hidden", "LEAN_OBFUSCATION"),
    ],
)
def test_forbidden_lean_constructs(snippet, rule):
    assert rule in lean_rules(f"import Spec\n\n{snippet}\n"), lean_rules(f"import Spec\n\n{snippet}\n")


@pytest.mark.parametrize(
    "imports, ok",
    [
        ("import Spec", True),
        ("import Spec\nimport Init.Data.List", True),
        ("import Std.Data.HashMap", True),
        ("import Lean", False),
        ("import Mathlib.Tactic", False),
        ("import Proofs", False),
        ("prelude\nimport Spec", False),
        ("module\nimport Spec", False),
        ("public import Spec", False),
        ("import all Spec", False),
    ],
)
def test_lean_imports(imports, ok):
    assert ("LEAN_IMPORT" not in lean_rules(f"{imports}\n\ntheorem t : True := trivial\n")) is ok


def test_allowed_lean_constructs_have_no_false_positives():
    text = textwrap.dedent(
        """\
        import Spec

        /-! Module doc mentioning sorry, axiom, #eval and native_decide. -/
        namespace T.P
        open T
        set_option maxHeartbeats 400000 in
        set_option linter.unusedVariables false in
        @[simp] theorem double_zero : double 0 = 0 := rfl
        /-- a /- nested -/ doc comment with `sorry` -/
        theorem double_even_proof : double_even := by
          intro n; unfold double; omega  -- not native_decide
        def msg : String := "instance axiom sorry #eval"
        def c : Char := '"'
        def alternative (h' : Nat) : Nat := h' + 1
        attribute [-simp] double_zero
        #check double_even_proof
        #print axioms double_even_proof
        end T.P
        """
    )
    assert lean_rules(text) == set()
    assert LeanSource(text).code.count("sorry") == 0


def test_policy_hook_treats_proofs_as_editable_and_applies_lean_rules():
    spec = PROOF_SPEC
    old = {"src/f.c": '#include "f.h"\nint f(int x) { return x; }\n', P: GOOD_PROOFS.replace("by unfold four; decide", "by sorry")}
    new = dict(old)
    new[P] = GOOD_PROOFS + "-- system(\"x\") __CPROVER_assume(0) #pragma GCC\n"
    d = check_candidate(spec, old, new)
    assert d.allowed, d.violations
    new[P] = old[P].replace("intro n; unfold double; omega", "sorry")
    d = check_candidate(spec, old, new)
    assert not d.allowed and [v.rule for v in d.violations] == ["LEAN_SORRY"]
    assert d.violations[0].file == P and d.violations[0].line
    new[P] = GOOD_PROOFS + "\naxiom x : False\n"
    assert {v.rule for v in check_candidate(spec, old, new).violations} == {"LEAN_AXIOM"}
    new = dict(old)
    new["lean/Spec.lean"] = "-- weakened\n"
    d = check_candidate(spec, old, new)
    assert [v.rule for v in d.violations] == ["PATH_NOT_EDITABLE"] and "protected" in d.violations[0].message
    g = check_generated(spec, {"src/f.c": old["src/f.c"], P: GOOD_PROOFS})
    assert g.allowed, g.violations
    assert check_generated(spec, {"src/f.c": old["src/f.c"], P: old[P]}).allowed
    g = check_generated(spec, {"src/f.c": old["src/f.c"], P: old[P] + "\n#eval 1\n"})
    assert [v.rule for v in g.violations] == ["LEAN_METAPROGRAM"]


def test_lean_layout():
    lay = lean_layout(make_spec(proofs=["lean/Proofs.lean", "lean/sub/More.lean"]))
    assert lay.root == "lean" and lay.spec_module == "Spec"
    assert lay.proof_modules == {"lean/Proofs.lean": "Proofs", "lean/sub/More.lean": "sub.More"}
    with pytest.raises(ValueError):
        lean_layout(make_spec(proofs=["lean/my-proofs.lean"]))
    with pytest.raises(ValueError):
        lean_layout(make_spec(proofs=["lean/Lean/X.lean"]))


def test_checker_rendering_and_message_parsing():
    text = render_checker("abc123", "Spec", ["Proofs", "sub.More"], [("T.double_even", "T.P.double_even_proof")])
    assert "import «Spec»\nimport «Proofs»\nimport «sub».«More»" in text
    assert "set_option Elab.async false" in text and '"abc123"' in text
    assert 'LeancCheck.check 0 (LeancCheck.nm ["T", "double_even"]) (LeancCheck.nm ["T", "P", "double_even_proof"])' \
        in text
    msgs, other = parse_lean_messages('{"severity":"error","data":"x","pos":{"line":3,"column":0}}\nnoise\n{bad json\n')
    assert len(msgs) == 1 and other == ["noise", "{bad json"]


FORGED = textwrap.dedent(
    """\
    import Spec
    import Lean
    open Lean Elab Command in
    set_option debug.skipKernelTC true in
    run_cmd liftCoreM <| addDecl (.thmDecl {name := `T.P.double_mono_proof, levelParams := [], type := mkConst `T.double_mono, value := mkConst `True.intro})
    namespace T.P
    open T
    theorem double_even_proof : double_even := by
      intro n; unfold double; omega
    theorem four_proof : four := by unfold four; decide
    end T.P
    """
)


def test_forged_declaration_is_caught_by_kernel_replay(tmp_path, needs_lean, monkeypatch):
    assert {v.rule for v in lean_source_violations(PROOF_SPEC, P, FORGED)} >= {"LEAN_IMPORT", "LEAN_OPTION",
                                                                               "LEAN_METAPROGRAM"}
    monkeypatch.setattr(lean_gate, "lean_source_violations", lambda *a, **k: [])
    root, spec = project(tmp_path, FORGED)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE
    assert res.details["replay"]["modules"] == {"Proofs": "rejected"}
    rep = [f for f in res.failures if f.tool == "leanchecker"]
    assert rep and rep[0].property_id == "kernel-replay"
    assert not any(t["proved"] for t in res.details["theorems"])


def test_no_pass_without_kernel_replay(tmp_path, needs_lean, monkeypatch):
    tc = dataclasses.replace(find_lean(), leanchecker=None)
    monkeypatch.setattr(lean_gate, "find_lean", lambda: tc)
    root, spec = project(tmp_path)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.ERROR and "leanchecker" in res.summary and "refusing to PASS" in res.summary


def test_budget_exhaustion_is_timeout(tmp_path, needs_lean):
    slow = GOOD_PROOFS.replace(
        "end T.P",
        "set_option maxHeartbeats 0 in\nset_option maxRecDepth 100000 in\n"
        "theorem slow : (List.range 20000).foldl (· + ·) 0 = 199990000 := by decide\nend T.P",
    )
    root, spec = project(tmp_path, slow, timeout_s=0.1)
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.TIMEOUT and res.status is Status.TIMEOUT


def test_proof_module_cannot_shadow_checker_or_spec_names(tmp_path, needs_lean):
    forged = textwrap.dedent(
        """\
        namespace Lean.Json
        def compress (n : Nat) : String := "forged"
        end Lean.Json
        namespace T
        def double (n : Nat) : Nat := 0
        end T
        """
    )
    root, spec = project(tmp_path, {"lean/Proofs.lean": GOOD_PROOFS, "lean/Forged.lean": forged})
    res, _ = gate(root, spec)
    assert res.outcome is Outcome.INCONCLUSIVE, res.summary
    assert all("cannot be imported together" in r for r in unproved(res).values()) and len(unproved(res)) == 3
