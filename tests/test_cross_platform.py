from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanc.backends.cbmc import argv as cbmc_argv
from leanc.backends.cbmc.bodiless import (
    no_body_property_function,
    split_violations,
    undecided_explained_by_unmodelled,
)
from leanc.backends.cbmc.compile_flags import (
    cbmc_host_profile,
    cbmc_preprocessor_macro_args,
    normalize_host_architecture,
    normalize_host_system,
)
from leanc.backends.clang_diagnostics import parse_clang_diagnostics, relative_to_base
from leanc.backends.counterexample.locations import relative_file, same_file
from leanc.backends.object_tools import (
    missing_section_tool_message,
    object_format,
    select_section_tool,
)
from leanc.backends.sanitizer import build as sanitizer_build
from leanc.backends.sanitizer import frames as sanitizer_frames
from leanc.backends.sanitizer import ownership, ownership_replay
from leanc.backends.sanitizer import plan as sanitizer_plan_mod
from leanc.backends.sarif import parse_sarif
from leanc.backends.termination import OUT_OF_MEMORY_SIGNALS, is_cpu_limit_signal
from leanc.backends.verifier_macros import (
    CLANG_MISSING,
    CLANG_TOOL,
    GCC_TOOL,
    plan_verifier_macro_dumps,
)
from leanc.foundation import runner, sandbox
from leanc.foundation.runner import (
    STDERR,
    STDOUT,
    ProcResult,
    _default_path_for,
    _PumpEnd,
    _ThreadedStreamPump,
    minimal_env,
)
from leanc.foundation.sandbox import Sandbox, detect_sandbox
from leanc.lang.c_lexer import CSource
from leanc.pipeline import doctor
from leanc.rules.c_names import ALLOWED_IMPORTS
from leanc.rules.c_rules import (
    check_object_symbols,
    parse_macho_nm,
    parse_section_names,
    section_name_violations,
    strip_leading_underscore,
)
from leanc.rules.predefined_macros import GCC_MISSING
from leanc.rules.preprocessor import expanded_view, has_linemarker_for
from leanc.rules.stack_checks import unbounded_stack_scan
from leanc.specs.compile_options import selects_linker_by_path
from leanc.specs.loader import load_spec
from leanc.specs.model import CompileConfig
from leanc.toolchain.cxx import _C_NAME_RE, _CXX_NAME_RE

REPO = Path(__file__).resolve().parent.parent
SAFE_SPEC_PATH = REPO / "examples" / "safe_array_get" / "spec.yaml"
WP_SPEC_PATH = REPO / "examples" / "wp_safe_array_get" / "spec.yaml"


@pytest.mark.parametrize(
    ("raw_sys", "raw_arch", "expected_preproc", "has_m64", "has_short_wchar", "default_uchar", "expected_define"),
    [
        ("Linux", "x86_64", "gcc", True, False, False, "-D__x86_64__"),
        ("Linux", "aarch64", "gcc", False, False, True, "-D__aarch64__"),
        ("Darwin", "arm64", "clang", False, False, False, "-D__aarch64__"),
        ("Darwin", "x86_64", "clang", True, False, False, "-D__LITTLE_ENDIAN__"),
        ("Windows", "AMD64", "gcc", True, True, False, "-D_M_X64"),
        ("win32", "ARM64", "gcc", False, True, True, "-D_M_ARM64"),
    ],
)
def test_cbmc_host_profiles_across_platforms(
    raw_sys, raw_arch, expected_preproc, has_m64, has_short_wchar, default_uchar, expected_define
):
    profile = cbmc_host_profile(raw_sys, raw_arch)
    assert profile is not None
    assert profile.preprocessor == expected_preproc
    assert ("-m64" in profile.base_args) is has_m64
    assert ("-fshort-wchar" in profile.base_args) is has_short_wchar
    assert profile.default_unsigned_char is default_uchar
    assert expected_define in profile.arch_defines
    args = cbmc_preprocessor_macro_args(CompileConfig(), "c17", system=raw_sys, architecture=raw_arch)
    assert ("-m64" in args) is has_m64
    assert ("-fshort-wchar" in args) is has_short_wchar
    assert ("-funsigned-char" in args) is default_uchar
    assert expected_define in args


def test_cbmc_host_profile_normalizers_and_unsupported_fail_closed():
    assert normalize_host_system("linux2") == "linux"
    assert normalize_host_system("macOS") == "darwin"
    assert normalize_host_system("win32") == "windows"
    assert normalize_host_architecture("AMD64") == "x86_64"
    assert normalize_host_architecture("aarch64") == "arm64"
    assert cbmc_host_profile("linux", "sparc64") is None
    with pytest.raises(ValueError, match="unsupported CBMC host"):
        cbmc_preprocessor_macro_args(CompileConfig(), "c17", system="linux", architecture="sparc64")


def test_plan_verifier_macro_dumps_per_platform():
    c_spec = load_spec(SAFE_SPEC_PATH)
    wp_spec = load_spec(WP_SPEC_PATH)

    arm_plan = plan_verifier_macro_dumps(
        c_spec, "/tmp/fc.h", gcc="/usr/bin/gcc", clang="/usr/bin/clang-18", system="linux", architecture="aarch64"
    )
    assert arm_plan.problems == []
    assert len(arm_plan.dumps) == 1 and arm_plan.dumps[0].tool == GCC_TOOL
    assert "-m64" not in arm_plan.dumps[0].argv and "-D__aarch64__" in arm_plan.dumps[0].argv

    arm_missing_gcc = plan_verifier_macro_dumps(
        c_spec, "/tmp/fc.h", gcc=None, clang="/usr/bin/clang-18", system="linux", architecture="aarch64"
    )
    assert arm_missing_gcc.problems == [GCC_MISSING]

    mac_plan = plan_verifier_macro_dumps(
        c_spec, "/tmp/fc.h", gcc=None, clang="/usr/bin/clang", system="darwin", architecture="arm64"
    )
    assert mac_plan.problems == []
    assert len(mac_plan.dumps) == 1 and mac_plan.dumps[0].tool == CLANG_TOOL
    assert mac_plan.dumps[0].argv[0] == "/usr/bin/clang"

    mac_missing_clang = plan_verifier_macro_dumps(
        c_spec, "/tmp/fc.h", gcc="/opt/homebrew/bin/gcc", clang=None, system="darwin", architecture="arm64"
    )
    assert mac_missing_clang.problems == [CLANG_MISSING]

    mac_wp_needs_gcc = plan_verifier_macro_dumps(
        wp_spec, "/tmp/fc.h", gcc=None, clang="/usr/bin/clang", system="darwin", architecture="arm64"
    )
    assert GCC_MISSING in mac_wp_needs_gcc.problems

    win_plan = plan_verifier_macro_dumps(
        c_spec,
        r"C:\tmp\fc.h",
        gcc=r"C:\msys64\mingw64\bin\gcc.exe",
        clang=r"C:\Program Files\LLVM\bin\clang.exe",
        system="win32",
        architecture="AMD64",
    )
    assert win_plan.problems == []
    assert "-fshort-wchar" in win_plan.dumps[0].argv and "-D_M_X64" in win_plan.dumps[0].argv

    unsupported = plan_verifier_macro_dumps(
        c_spec, "/tmp/fc.h", gcc="/usr/bin/gcc", clang="/usr/bin/clang", system="linux", architecture="mips64"
    )
    assert unsupported.dumps == [] and any("unsupported CBMC host" in p for p in unsupported.problems)


def test_cbmc_major_and_no_body_property_detection():
    assert cbmc_argv.cbmc_major("6.11.0 (cbmc-6.11.0)") == 6
    assert cbmc_argv.cbmc_major("5.95.1 (cbmc-5.95.1)") == 5
    assert cbmc_argv.cbmc_major(None) is None
    no_body_entry = {
        "property": "point_ratio.no-body.helper_without_body",
        "status": "FAILURE",
        "description": "no body for callee helper_without_body",
        "sourceLocation": {"function": "point_ratio"},
    }
    assert no_body_property_function(no_body_entry) == "helper_without_body"
    assert no_body_property_function({"property": "point_ratio.overflow.1", "description": "overflow"}) is None
    split = split_violations([no_body_entry], frozenset({"helper_without_body"}))
    assert split.real == [] and len(split.dependent) == 1
    assert undecided_explained_by_unmodelled(
        {"description": "arithmetic overflow", "sourceLocation": {"function": "point_ratio"}},
        frozenset({"helper_without_body"}),
        {"point_ratio"},
    )


def test_object_format_and_section_tool_selection(tmp_path):
    elf = tmp_path / "a.o"
    elf.write_bytes(b"\x7fELF\x02\x01\x01\x00")
    macho = tmp_path / "b.o"
    macho.write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
    coff = tmp_path / "c.obj"
    coff.write_bytes(b"\x64\x86\x02\x00\x00\x00\x00\x00")
    other = tmp_path / "d.bin"
    other.write_bytes(b"NOPE")

    assert object_format(str(elf)) == "elf"
    assert object_format(str(macho)) == "macho"
    assert object_format(str(coff)) == "coff"
    assert object_format(str(other)) == "unknown"

    elf_tool = select_section_tool("elf", readelf="/usr/bin/readelf", otool=None, readobj="/usr/bin/llvm-readobj", objdump=None)
    assert elf_tool is not None and elf_tool.name == "readelf"
    macho_tool = select_section_tool("macho", readelf=None, otool="/usr/bin/otool", readobj=None, objdump=None)
    assert macho_tool is not None and macho_tool.name == "otool" and macho_tool.argv("b.o") == ["/usr/bin/otool", "-l", "b.o"]
    coff_tool = select_section_tool("coff", readelf=None, otool=None, readobj=r"C:\LLVM\llvm-readobj.exe", objdump=None)
    assert coff_tool is not None and coff_tool.name == "llvm-readobj"
    assert "Mach-O" in missing_section_tool_message("macho")
    assert "COFF" in missing_section_tool_message("coff")
    assert "readelf" in missing_section_tool_message("elf")


def test_section_parsing_catches_constructors_on_elf_macho_and_coff():
    otool_out = (
        "Load command 0\n"
        "Section\n"
        "  sectname __text\n"
        "   segname __TEXT\n"
        "Section\n"
        "  sectname __mod_init_func\n"
        "   segname __DATA_CONST\n"
    )
    macho_names = parse_section_names(otool_out, "otool")
    assert macho_names == ["__text", "__mod_init_func"]
    macho_viols = section_name_violations("src/a.c", macho_names, CSource("int f(void) { return 0; }\n"))
    assert [v.rule for v in macho_viols] == ["CONSTRUCTOR"]

    readobj_coff_out = (
        "Sections [\n"
        "  Section {\n"
        "    Number: 1\n"
        "    Name: .text (2E 74 65 78 74 00 00 00)\n"
        "  }\n"
        "  Section {\n"
        "    Number: 2\n"
        "    Name: .CRT$XCU (2E 43 52 54 24 58 43 55)\n"
        "  }\n"
        "]\n"
    )
    coff_names = parse_section_names(readobj_coff_out, "llvm-readobj")
    assert coff_names == [".text", ".CRT$XCU"]
    coff_viols = section_name_violations("src/a.c", coff_names)
    assert [v.rule for v in coff_viols] == ["CONSTRUCTOR"]

    objdump_out = (
        "Sections:\n"
        "Idx Name          Size     VMA              Type\n"
        "  0 .text         00000010 0000000000000000 TEXT\n"
        "  1 .CRT$XIA      00000008 0000000000000000 DATA\n"
    )
    assert parse_section_names(objdump_out, "objdump") == [".text", ".CRT$XIA"]
    assert [v.rule for v in section_name_violations("src/a.c", parse_section_names(objdump_out, "objdump"))] == [
        "CONSTRUCTOR"
    ]
    assert section_name_violations("src/a.c", [".text", ".rodata", "__text", ".rdata"]) == []


def test_macho_symbol_normalization_and_platform_allowed_imports():
    macho_nm_out = (
        "0000000000000008 (__TEXT,__text) external _safe_array_get\n"
        "---------------- (LTO,CODE) external _lto_fn\n"
        "                 (undefined) external ___assert_rtn\n"
        "                 (undefined) external ___error\n"
        "                 (undefined) weak external ___VERIFIER_assume\n"
        "00000000000000cc (__TEXT,__text) weak private external __ZNSt3__15arrayIiLm4EEixEm\n"
        "0000000000000000 (__TEXT,__text) non-external ltmp0\n"
    )
    parsed = strip_leading_underscore(parse_macho_nm(macho_nm_out))
    assert ("safe_array_get", "T") in parsed
    assert ("lto_fn", "T") in parsed
    assert ("__assert_rtn", "U") in parsed
    assert ("__error", "U") in parsed
    assert ("__VERIFIER_assume", "w") in parsed
    assert ("_ZNSt3__15arrayIiLm4EEixEm", "W") in parsed
    assert ("ltmp0", "t") in parsed

    raw = [("_safe_array_get", "T"), ("___assert_rtn", "U"), ("___error", "U"), ("__ZN2lc3fooEi", "T")]
    normalized = strip_leading_underscore(raw)
    assert normalized == [("safe_array_get", "T"), ("__assert_rtn", "U"), ("__error", "U"), ("_ZN2lc3fooEi", "T")]
    assert {"__assert_rtn", "__error", "_wassert", "_errno", "bzero"} <= ALLOWED_IMPORTS

    spec = load_spec(SAFE_SPEC_PATH)
    ok_symbols = [("safe_array_get", "T"), ("__assert_rtn", "U"), ("_wassert", "U"), ("__error", "U"), ("_errno", "U")]
    assert check_object_symbols(spec, "src/safe_array.c", ok_symbols, header_idents=set(), defined_elsewhere=set()) == []
    bad = check_object_symbols(
        spec, "src/safe_array.c", [("safe_array_get", "T"), ("system", "U")], header_idents=set(), defined_elsewhere=set()
    )
    assert [v.rule for v in bad] == ["DANGEROUS_IMPORT"]


def test_windows_and_macos_runner_env_paths_and_stream_pump(monkeypatch):
    assert "/opt/homebrew/bin" in _default_path_for("darwin")
    win_path = _default_path_for("win32")
    assert ";" in win_path and r"System32" in win_path

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-must-not-leak")
    monkeypatch.setenv("LEANC_API_KEY", "sk-leanc-secret")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    env = minimal_env({"EXTRA": "1"})
    assert "OPENAI_API_KEY" not in env and "LEANC_API_KEY" not in env
    assert env["SYSTEMROOT"] == r"C:\Windows" and env["EXTRA"] == "1"
    assert env["TEMP"] and env["TMP"]

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello' * 20); sys.stderr.write('err')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pump = _ThreadedStreamPump(proc, stdin_data=None, max_output_bytes=32)
    end = pump.run_until(time.monotonic() + 10)
    pump.close()
    proc.wait(timeout=10)
    assert end is _PumpEnd.OUTPUT_CAP_EXCEEDED
    assert pump.truncated is True
    assert len(pump.output[STDOUT]) <= 32
    assert pump.output[STDERR] in (b"", b"err")


def test_non_linux_sandbox_refuses_to_fake_active(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    try:
        for platform_name in ("darwin", "win32"):
            detect_sandbox.cache_clear()
            monkeypatch.setattr(sandbox.sys, "platform", platform_name)
            sb = detect_sandbox("auto")
            assert sb.active is False and sb.kind == "none"
            assert "Linux" in sb.note
    finally:
        detect_sandbox.cache_clear()


def test_windows_exe_compiler_matching_and_linker_path_rejection():
    assert _C_NAME_RE.match("clang.exe") and _C_NAME_RE.match("clang-18.exe")
    assert _CXX_NAME_RE.match("clang++.exe") and _CXX_NAME_RE.match("clang++-18.exe")
    assert selects_linker_by_path("-fuse-ld=/tmp/evil")
    assert selects_linker_by_path(r"-fuse-ld=C:\tmp\evil.exe")
    assert not selects_linker_by_path("-fuse-ld=lld")


def test_windows_paths_in_diagnostics_sarif_frames_cbmc_and_stack_checks(monkeypatch):
    diags = parse_clang_diagnostics(
        r"C:\proj\src\a.c:12:5: error: use of undeclared identifier 'x'" + "\n"
        r"lld-link.exe: error: undefined symbol: missing_fn" + "\n",
        base_dir=Path("/nonexistent"),
    )
    assert diags[0].location is not None
    assert diags[0].location.file == "C:/proj/src/a.c" and diags[0].location.line == 12
    assert diags[1].rule_id == "linker-error"

    assert relative_to_base(r"C:\proj\src\a.c", None) == "C:/proj/src/a.c"

    sarif_data = {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": "core.NullDereference",
                        "level": "warning",
                        "message": {"text": "Dereference of null pointer"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "file:///C:/proj/src/a.c"},
                                    "region": {"startLine": 7, "startColumn": 3},
                                }
                            }
                        ],
                    }
                ]
            }
        ]
    }
    sarif_diags = parse_sarif(sarif_data, base_dir=None)
    assert sarif_diags[0].location is not None and sarif_diags[0].location.file == "C:/proj/src/a.c"

    build_diags = sanitizer_build.parse_build_diagnostics(
        r"C:\proj\src\f.c:4:9: error: expected ';'" + "\n"
        r"C:\Program Files\LLVM\bin\clang.exe: error: linker command failed with exit code 1" + "\n",
        base_dir=r"C:\proj",
    )
    assert build_diags[0].location is not None and build_diags[0].location.file == "src/f.c"
    assert build_diags[1].rule_id == "link-error"

    assert sanitizer_frames.is_system_path(r"C:\Windows\System32\ntdll.dll")
    assert sanitizer_frames.is_system_path("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    assert sanitizer_frames.relative_to_snapshot(r"C:\proj\src\f.c", r"C:\proj") == "src/f.c"
    assert relative_file(r"C:\proj\src\f.c", r"C:\proj") == "src/f.c"
    assert same_file(r"src\f.c", "src/f.c")

    scan = unbounded_stack_scan(
        r"C:\proj\src\f.c:5:9: error: variable length array used [-Werror,-Wvla]" + "\n",
        "reason",
        project_root=r"C:\proj",
    )
    assert len(scan.violations) == 1 and scan.violations[0].file == "src/f.c" and scan.violations[0].line == 5

    monkeypatch.setattr(os, "name", "nt")
    pp = '# 1 "src\\\\a.c"\nint x = 1;\n'
    assert has_linemarker_for("src/a.c", pp)
    view, _ = expanded_view("src/a.c", pp)
    assert "int x = 1;" in view


def test_doctor_integrity_and_platform_checks_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "cbmc_host_profile", lambda: None)
    monkeypatch.setattr(doctor, "normalize_host_system", lambda: "linux")
    monkeypatch.setattr(doctor, "normalize_host_architecture", lambda: "sparc64")
    monkeypatch.setattr(doctor, "find_tool", lambda name: f"/usr/bin/{name}")
    chk = doctor._integrity_check(None, tmp_path)
    assert chk.ok is False and chk.required is True
    assert "unsupported CBMC host" in chk.detail

    arm_profile = cbmc_host_profile("linux", "aarch64")
    monkeypatch.setattr(doctor, "cbmc_host_profile", lambda: arm_profile)
    monkeypatch.setattr(
        doctor,
        "run_command",
        lambda argv, **kw: ProcResult(
            argv=argv, cwd=str(tmp_path), exit_code=1, signal=None, timed_out=False,
            output_truncated=False, duration_s=0.01, stdout="", stderr="gcc: error: unrecognized option '-m64'",
        ),
    )
    broken_mirror = doctor._integrity_check(None, tmp_path)
    assert broken_mirror.ok is False and "CBMC macro mirror failed" in broken_mirror.detail

    mac_profile = cbmc_host_profile("darwin", "arm64")
    monkeypatch.setattr(doctor, "cbmc_host_profile", lambda: mac_profile)
    monkeypatch.setattr(doctor, "find_tool", lambda name: None)
    mac_gcc = doctor._gcc_check()
    assert mac_gcc.ok is False and mac_gcc.required is False

    monkeypatch.setattr(doctor.sys, "platform", "win32")
    win_sb = doctor._sandbox_check("auto", Sandbox(kind="none", note="no bwrap"), tmp_path)
    assert win_sb.ok is False and win_sb.required is True and "Linux container/VM" in win_sb.fix


def test_ownership_and_replay_portability(tmp_path, monkeypatch):
    fake_inputs = SimpleNamespace(
        ctx=SimpleNamespace(spec=SimpleNamespace(language=SimpleNamespace(is_cpp=False))),
        plan=SimpleNamespace(detect_leaks=True),
    )
    orig_platform = ownership.sys.platform
    monkeypatch.setattr(ownership.sys, "platform", "darwin")
    assert "Linux" in (ownership._unsupported(fake_inputs, "/opt/homebrew/bin/llvm-symbolizer") or "")
    monkeypatch.setattr(ownership.sys, "platform", "win32")
    assert "Linux" in (ownership._unsupported(fake_inputs, r"C:\LLVM\llvm-symbolizer.exe") or "")
    monkeypatch.setattr(ownership.sys, "platform", orig_platform)

    target = tmp_path / "real.log"
    target.write_text("E 0x1 0x2\n")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    monkeypatch.delattr(ownership_replay.os, "O_NOFOLLOW", raising=False)
    read = ownership_replay.read_trace_log(link)
    assert read.problem is not None and "symbolic link" in read.problem

    assert isinstance(OUT_OF_MEMORY_SIGNALS, frozenset)
    assert is_cpu_limit_signal(None) is False
    sym = sanitizer_plan_mod.find_llvm_symbolizer(None, None, sandbox_active=False)
    assert sym is None or os.path.isfile(sym)
