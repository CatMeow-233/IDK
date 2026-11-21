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

    rc, _ = run(capsys, "spec", "approve", spec, "--by", "alice")
    assert rc == 2, "approval without --yes is refused non-interactively"

    rc, data = run(capsys, "spec", "approve", spec, "--by", "alice", "--yes", "--json")
    assert rc == 0 and data["approved_by"] == "alice"

    rc, data = run(capsys, "verify", spec, "--json")
    assert rc == 0, data
    assert data["status"] == "VERIFIED_BOUNDED"
    stages = {s["stage"]: s["outcome"] for s in data["verification"]["stages"]}
    assert stages == {"spec": "PASS", "compile": "PASS", "integrity": "PASS", "static_analysis": "PASS",
                      "sanitizer": "PASS", "cbmc": "PASS"}
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

    weakened_after_approval = proj / "harness/safe_array_get_harness.c"
    weakened_after_approval.write_text(weakened_after_approval.read_text().replace("assert(out_cell == storage[idx]);", ""))
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


def test_generate_backs_up_and_exits_zero(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    src = proj / "src/safe_array.c"
    src.write_text(src.read_text() + "/* MY PRECIOUS EDIT */\n")
    rc, data = run(capsys, "generate", spec, "--force", "--provider", "mock", "--json")
    assert rc == 0 and data["exit_code"] == 0
    assert "PRECIOUS" not in src.read_text()
    backup = Path(data["run_dir"]) / "backup/before-generate/src/safe_array.c"
    assert "PRECIOUS" in backup.read_text()


def test_provider_failure_is_a_recorded_tool_error(capsys, example_copy, needs_toolchain):
    proj = example_copy("ring_buffer")
    (proj / "src/ring_stats.c").unlink()
    (proj / "mock/generate/src/ring_stats.c").unlink()
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    rc, data = run(capsys, "run", spec, "--provider", "mock", "--json")
    assert rc == 30 and data["status"] == "TOOL_ERROR"
    assert (Path(data["run_dir"]) / "run.json").exists()
    assert any("provider error" in m for m in data["messages"])


def test_report_flags_stale_workspace(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    rc, data = run(capsys, "verify", spec, "--json")
    assert rc == 0
    (proj / "src/safe_array.c").write_text("int broken(void) { return 0 }\n")
    rc = main(["report", data["run_dir"], "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 20 and out["stale_files"] == ["src/safe_array.c"]


def test_project_config_cannot_disable_isolation(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    (proj / "leanc.yaml").write_text("sandbox: none\nallow_unsandboxed: true\n")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    rc = main(["verify", spec, "--json"])
    cap = capsys.readouterr()
    data = json.loads(cap.out)
    stages = {s["stage"]: s["outcome"] for s in data["verification"]["stages"]}
    assert stages["sanitizer"] == "SKIPPED" and data["status"] == "UNKNOWN" and rc == 20
    assert "ignored" in cap.err


def test_init_is_atomic_and_keeps_gitignore_lines(capsys, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "spec.yaml").write_text("mine\n")
    (proj / ".gitignore").write_text("build")
    rc = main(["init", str(proj)])
    assert rc == 2 and not (proj / "README.md").exists(), "nothing is copied on conflict"
    assert main(["init", str(proj), "--force"]) == 0
    assert (proj / ".gitignore").read_text().splitlines() == ["build", ".leanc/runs/"]
    assert main(["init", str(tmp_path / "q"), "--template", ""]) == 2


def test_symlinked_runs_dir_is_refused(capsys, example_copy, tmp_path, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / ".leanc/runs").symlink_to(outside)
    capsys.readouterr()
    rc = main(["verify", spec])
    assert rc == 2 and "symbolic link" in capsys.readouterr().err
    assert not any(outside.iterdir())


def test_bad_project_root_and_requirement_file(capsys, tmp_path, example_copy):
    proj = example_copy("safe_array_get")
    assert main(["verify", str(proj / "spec.yaml"), "--project-root", str(tmp_path / "typo")]) == 2
    assert main(["spec", "draft", "--id", "x", "--requirement-file", str(tmp_path / "nope.txt"),
                 "--project-root", str(tmp_path)]) == 2
    assert "cannot read --requirement-file" in capsys.readouterr().err
    assert main(["spec"]) == 2


def test_run_regenerate_keeps_the_users_original(capsys, example_copy, needs_toolchain):
    proj = example_copy("div_by_zero")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    src = proj / "src/safe_ratio.c"
    src.write_text(src.read_text() + "/* USER MARKER */\n")
    rc, data = run(capsys, "run", spec, "--provider", "mock", "--regenerate", "--json")
    assert data["status"] == "VERIFIED_BOUNDED"
    rd = Path(data["run_dir"])
    assert "USER MARKER" in (rd / "backup/before-generate/src/safe_ratio.c").read_text()
    assert "USER MARKER" not in (rd / "backup/before-writeback/src/safe_ratio.c").read_text()


def test_changed_spec_makes_report_stale(capsys, example_copy, needs_toolchain):
    proj = example_copy("safe_array_get")
    spec = proj / "spec.yaml"
    main(["spec", "approve", str(spec), "--by", "t", "--yes"])
    capsys.readouterr()
    rc, data = run(capsys, "verify", str(spec), "--json")
    assert rc == 0
    spec.write_text(spec.read_text().replace("unwind: 9", "unwind: 2"))
    rc = main(["report", data["run_dir"], "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 20 and any("spec content" in s for s in out["stale_files"])


def test_symlinked_spec_is_refused(capsys, example_copy, tmp_path):
    proj = example_copy("safe_array_get")
    link = proj / "link.yaml"
    link.symlink_to(proj / "spec.yaml")
    assert main(["verify", str(link)]) == 2
    assert "symbolic link" in capsys.readouterr().err


def test_input_error_after_run_dir_creation_still_records_the_run(capsys, example_copy, monkeypatch):
    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    monkeypatch.delenv("LEANC_MODEL", raising=False)
    rc = main(["generate", spec, "--provider", "openai"])
    err = capsys.readouterr().err
    assert rc == 2 and err.count("error:") == 1
    runs = list((proj / ".leanc/runs").iterdir())
    assert len(runs) == 1 and (runs[0] / "run.json").exists() and (runs[0] / "report.md").exists()


def test_failed_abort_record_does_not_hide_the_user_error(capsys, example_copy, monkeypatch):
    def unwritable(_message):
        raise PermissionError("read-only run directory")

    proj = example_copy("safe_array_get")
    spec = str(proj / "spec.yaml")
    main(["spec", "approve", spec, "--by", "t", "--yes"])
    capsys.readouterr()
    monkeypatch.delenv("LEANC_MODEL", raising=False)
    monkeypatch.setattr("leanc.cli.record_aborted_run", unwritable)
    assert main(["generate", spec, "--provider", "openai"]) == 2
    err = capsys.readouterr().err
    assert "leanc: error:" in err
    assert "could not write the aborted-run record: read-only run directory" in err
