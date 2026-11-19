"""End-to-end tests of the ``leanc`` CLI (in-process ``main(argv)``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from leanc.cli import build_parser, main

REPO = Path(__file__).resolve().parent.parent


def run(capsys, *argv) -> tuple[int, dict | str]:
    rc = main(list(argv))
    out = capsys.readouterr().out
    if "--json" in argv:
        return rc, json.loads(out)
    return rc, out


@pytest.mark.parametrize(
    "cmd",
    [[], ["doctor"], ["init"], ["spec"], ["spec", "draft"], ["spec", "approve"], ["spec", "status"], ["spec", "show"],
     ["generate"], ["verify"], ["repair"], ["run"], ["report"]],
)
def test_every_command_has_help(cmd, capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([*cmd, "--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_console_script_entry_point():
    cp = subprocess.run([sys.executable, "-m", "leanc", "--version"], capture_output=True, text=True, check=False)
    assert cp.returncode == 0 and cp.stdout.startswith("leanc ")


def test_no_command_is_usage_error(capsys):
    assert main([]) == 2


def test_missing_spec_is_readable_error(capsys, tmp_path):
    rc = main(["verify", str(tmp_path / "nope.yaml")])
    assert rc == 2
    assert "spec file not found" in capsys.readouterr().err


def test_invalid_spec_is_readable_error(capsys, tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("leanc_spec: 1\nspec_id: Bad Id\n")
    rc = main(["spec", "show", str(p)])
    assert rc == 2
    assert "invalid spec" in capsys.readouterr().err


def test_doctor_json(capsys, needs_toolchain):
    rc, data = run(capsys, "doctor", "--json")
    names = {c["name"]: c for c in data["checks"]}
    for n in ("python", "clang", "clang-static-analyzer", "sanitizers", "cbmc", "sandbox"):
        assert names[n]["ok"], names[n]
    assert rc == 0 and data["ok"]
    assert names["llm-provider"]["required"] is False


def test_init_then_full_lifecycle(capsys, tmp_path, needs_toolchain):
    proj = tmp_path / "proj"
    rc, data = run(capsys, "init", str(proj), "--json")
    assert rc == 0 and "spec.yaml" in data["created"] and (proj / "leanc.yaml").exists()
    spec = str(proj / "spec.yaml")

    rc, data = run(capsys, "spec", "status", spec, "--json")
    assert rc == 40 and data["approved"] is False

    rc, data = run(capsys, "verify", spec, "--json")
    assert rc == 40 and data["status"] == "SPEC_UNAPPROVED"
    assert [s["stage"] for s in data["verification"]["stages"]] == ["spec"]

    rc, data = run(capsys, "generate", spec, "--json", "--force")
    assert rc == 40 and data["status"] == "SPEC_UNAPPROVED"

    rc, _ = run(capsys, "spec", "approve", spec, "--by", "alice")  # non-interactive without --yes
    assert rc == 2

    rc, data = run(capsys, "spec", "approve", spec, "--by", "alice", "--yes", "--json")
    assert rc == 0 and data["approved_by"] == "alice"

    rc, data = run(capsys, "verify", spec, "--json")
    assert rc == 0, data
    assert data["status"] == "VERIFIED_BOUNDED"
    stages = {s["stage"]: s["outcome"] for s in data["verification"]["stages"]}
    assert stages == {"spec": "PASS", "compile": "PASS", "static_analysis": "PASS", "sanitizer": "PASS", "cbmc": "PASS"}
    run_dir = Path(data["run_dir"])
    for f in ("run.json", "report.md", "leanc.log", "verify/verification.json", "verify/verification.md"):
        assert (run_dir / f).exists(), f
    assert (run_dir / "verify/raw/cbmc-verify.json").exists()

    rc, md = run(capsys, "report", "latest", "--project-root", str(proj))
    assert rc == 0 and "VERIFIED_BOUNDED" in md
    rc, data = run(capsys, "report", str(run_dir), "--format", "json")
    rc2 = main(["report", str(run_dir), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc2 == 0 and data["status"] == "VERIFIED_BOUNDED"

    # Weaken the trusted harness after approval -> SPEC_UNAPPROVED, nothing claimed.
    h = proj / "harness/safe_array_get_harness.c"
    h.write_text(h.read_text().replace("assert(out_cell == storage[idx]);", ""))
    rc, data = run(capsys, "verify", spec, "--json")
    assert rc == 40 and data["status"] == "SPEC_UNAPPROVED"
    assert any("modified since approval" in r for r in data["verification"]["approval"]["reasons"])


def test_gates_subset_cannot_be_verified(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    rc, data = run(capsys, "verify", spec, "--gates", "compile,cbmc", "--json")
    assert data["status"] == "UNKNOWN" and rc == 20


def test_no_sandbox_blocks_dynamic_execution(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    rc, data = run(capsys, "verify", spec, "--no-sandbox", "--json")
    stages = {s["stage"]: s for s in data["verification"]["stages"]}
    assert stages["sanitizer"]["outcome"] == "SKIPPED"
    assert data["status"] == "UNKNOWN"


def test_spec_draft_with_mock_is_untrusted(capsys, tmp_path):
    proj = tmp_path / "p"
    (proj / "mock").mkdir(parents=True)
    src = (REPO / "examples/safe_array_get/spec.yaml").read_text()
    (proj / "mock/spec_draft.yaml").write_text(src)
    (proj / "mock/harness_draft.c").write_text("int main(void){return 0;}\n")
    rc, data = run(
        capsys, "spec", "draft", "--id", "my-get", "--requirement", "安全陣列讀取", "--project-root", str(proj),
        "--provider", "mock", "--json",
    )
    assert rc == 40 and data["trusted"] is False
    assert data["provider"]["real_llm"] is False
    text = (proj / "drafts/my-get/spec.yaml").read_text()
    assert "UNTRUSTED" in text and "origin: ai-draft" in text and "spec_id: my-get" in text
    assert (proj / "drafts/my-get/harness_draft.c").read_text().startswith("/* UNTRUSTED")
