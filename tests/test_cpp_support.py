from __future__ import annotations

import dataclasses
import os
import stat
from pathlib import Path

import pytest
import yaml

from leanc.backends.analyzer import analyzer_command, run_static_analysis
from leanc.backends.clang import compile_command, run_compile
from leanc.backends.integrity import run_integrity
from leanc.backends.sanitizer import (
    build_command,
    parse_build_diagnostics,
    run_sanitizer_tests,
    sanitizer_plan,
)
from leanc.cli import main
from leanc.foundation.status import Outcome, Status
from leanc.lang import cpp_lexer
from leanc.repair.policy import check_candidate, check_generated
from leanc.rules import cpp_rules as cpi
from leanc.rules.source import source_violations
from leanc.specs.loader import load_spec
from leanc.toolchain.context import VerifyOptions, make_context
from leanc.toolchain.cxx import CXX_NOT_FOUND, find_clangxx
from leanc.toolchain.environment import Toolchain, toolchain
from leanc.toolchain.tools import clang_major, tool_version

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
FIX = Path(__file__).resolve().parent / "fixtures" / "cpp"

RB = ("cpp_ring_buffer", "src/ring_buffer.cpp")
SB = ("cpp_bounds_bug", "src/sample_buffer.cpp")


def _ctx(example_copy, tmp_path: Path, example: str, files: dict[str, str] | None = None, *, report: bool = False):
    root = example_copy(example)
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    spec = load_spec(root / "spec.yaml")
    if report:
        spec.compile.warnings_policy = "report"
    return make_context(spec, root, tmp_path / "run", VerifyOptions())


def _rules(stage) -> set[str]:
    return {f.property_id for f in stage.failures}


def test_toolchain_finds_matching_clangxx(needs_clang):
    tc = toolchain()
    assert tc.clangxx and os.path.basename(tc.clangxx).startswith("clang++")
    assert clang_major(tc.clangxx_version) == clang_major(tc.clang_version)
    assert find_clangxx(tc.clang) == tc.clangxx
    assert "clang++" in tc.clangxx


def test_clangxx_versioned_sibling_is_preferred(tmp_path, monkeypatch):
    monkeypatch.delenv("LEANC_CLANGXX", raising=False)
    for name in ("clang-18", "clang++-18", "clang++"):
        p = tmp_path / name
        major = "17" if name == "clang++" else "18"
        p.write_text(f"#!/bin/sh\necho 'fake clang version {major}.1.0'\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    tool_version.cache_clear()
    assert find_clangxx(str(tmp_path / "clang-18")) == str(tmp_path / "clang++-18")
    tool_version.cache_clear()


def test_clangxx_env_override(tmp_path, monkeypatch, needs_clang):
    monkeypatch.setenv("LEANC_CLANGXX", str(tmp_path / "missing-clang++"))
    assert find_clangxx("/usr/bin/clang") is None
    real = toolchain().clang
    monkeypatch.setenv("LEANC_CLANGXX", real)
    assert find_clangxx("/whatever/clang") == real


def test_toolchain_dataclass_is_backwards_compatible():
    tc = Toolchain(clang="/x/clang", cbmc=None, clang_version="clang version 18", cbmc_version=None)
    assert tc.clangxx is None and tc.clangxx_version is None
    assert Toolchain("/x/clang", None, "v", None) == tc.__class__("/x/clang", None, "v", None)


def test_c_specs_keep_using_clang(example_copy, tmp_path, needs_clang):
    ctx = _ctx(example_copy, tmp_path, "safe_array_get")
    rel = ctx.spec.sources.editable[0]
    assert compile_command(ctx, rel, tmp_path / "x.o")[0] == ctx.tools.clang
    assert analyzer_command(ctx, rel, tmp_path / "x.sarif")[0] == ctx.tools.clang
    assert build_command(ctx, tmp_path / "bin", sanitizer_plan(["address"]))[0] == ctx.tools.clang
    assert "-std=c17" in compile_command(ctx, rel, tmp_path / "x.o")


def test_cpp20_class_template_passes_every_gate(example_copy, tmp_path, needs_clang, needs_bwrap):
    ctx = _ctx(example_copy, tmp_path, "cpp_ring_buffer")
    comp = run_compile(ctx)
    assert comp.outcome is Outcome.PASS, comp.summary
    argv = comp.details["files"][0]["argv"]
    assert os.path.basename(argv[0]).startswith("clang++") and "-std=c++20" in argv
    assert comp.details["compiler"] == ctx.tools.clangxx
    integ = run_integrity(ctx)
    assert integ.outcome is Outcome.PASS, [f.description for f in integ.failures]
    assert integ.details["language"] == "c++"
    defined = integ.details["files"][0]["defined"]
    assert "lc::RingBuffer<int, 4ul>::push(int const&)" in defined
    assert any(d.startswith("std::array<int, 4ul>::operator[]") for d in defined)
    sa = run_static_analysis(ctx)
    assert sa.outcome is Outcome.PASS and sa.details["format"] == "sarif", sa.summary
    assert os.path.basename(sa.details["files"][0]["argv"][0]).startswith("clang++")
    san = run_sanitizer_tests(ctx)
    assert san.outcome is Outcome.PASS, san.summary
    build = san.details["build"]["argv"]
    assert os.path.basename(build[0]).startswith("clang++") and "tests/test_ring_buffer.cpp" in build
    assert san.details["counts"]["passed"] == 4


def test_cpp_compile_error_is_located(example_copy, tmp_path, needs_clang):
    text = (EXAMPLES / RB[0] / RB[1]).read_text().replace("    ++count_;\n", "    ++count_ = nullptr;\n", 1)
    ctx = _ctx(example_copy, tmp_path, RB[0], {RB[1]: text})
    res = run_compile(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.COMPILE_ERROR
    line = text.splitlines().index("    ++count_ = nullptr;") + 1
    locs = {(f.location.file, f.location.line) for f in res.failures if f.location}
    assert (RB[1], line) in locs, res.failures
    assert res.details["errors"] == len([d for d in res.diagnostics if d.severity == "error"])


def test_missing_clangxx_is_a_tool_error(example_copy, tmp_path):
    ctx = _ctx(example_copy, tmp_path, "cpp_ring_buffer")
    ctx.tools = dataclasses.replace(ctx.tools, clangxx=None, clangxx_version=None)
    for gate in (run_compile, run_static_analysis, run_sanitizer_tests):
        res = gate(ctx)
        assert res.outcome is Outcome.ERROR and res.summary == CXX_NOT_FOUND, (gate, res.summary)
    integ = run_integrity(ctx)
    assert integ.outcome is Outcome.ERROR


def _mini_cpp_project(root: Path, header: str, source: str, gates: list[str]) -> Path:
    (root / "include").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "include" / "nodes.hpp").write_text(header)
    (root / "src" / "nodes.cpp").write_text(source)
    spec = {
        "leanc_spec": 1,
        "spec_id": "cpp-mini",
        "version": 1,
        "title": "mini",
        "requirement": "test",
        "language": {"name": "cpp", "standard": "c++20"},
        "target": {"function": "first_value", "signature": "int lc::first_value(const Node*) noexcept",
                   "returns": {"type": "int"}},
        "sources": {"editable": ["src/nodes.cpp"], "headers": ["include/nodes.hpp"], "include_dirs": ["include"]},
        "gates": gates,
    }
    (root / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return root / "spec.yaml"


NODES_HPP = """#pragma once
namespace lc {
struct Node {
    int value;
    Node* next;
};
int first_value(const Node* head) noexcept;
int scratch_sum(int n);
}  // namespace lc
"""
NODES_CPP = """#include "nodes.hpp"

namespace lc {

int first_value(const Node* head) noexcept {
    const Node* p = head;
    if (head == nullptr) {
        return p->value;
    }
    return head->value;
}

int scratch_sum(int n) {
    int* tmp = new int[4]();
    if (n > 3) {
        return -1;
    }
    int s = 0;
    for (int i = 0; i < n; ++i) {
        s += tmp[i];
    }
    delete[] tmp;
    return s;
}

}  // namespace lc
"""


def test_analyzer_finds_cpp_null_deref_and_new_leak(tmp_path, needs_clang):
    spec_path = _mini_cpp_project(tmp_path / "p", NODES_HPP, NODES_CPP, ["compile", "static_analysis"])
    spec = load_spec(spec_path)
    ctx = make_context(spec, spec_path.parent, tmp_path / "run", VerifyOptions())
    assert run_compile(ctx).outcome is Outcome.PASS
    assert run_integrity(ctx).outcome is Outcome.PASS
    res = run_static_analysis(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.STATIC_ANALYSIS_WARNING
    by_rule = {f.property_id: f for f in res.failures}
    assert {"core.NullDereference", "cplusplus.NewDeleteLeaks"} <= set(by_rule), by_rule
    assert by_rule["core.NullDereference"].location.file == "src/nodes.cpp"
    assert by_rule["core.NullDereference"].location.line == 8
    assert by_rule["cplusplus.NewDeleteLeaks"].location.line == 16
    assert by_rule["core.NullDereference"].trace


def test_sanitizer_catches_out_of_bounds_in_cpp_class(example_copy, tmp_path, needs_clang, needs_bwrap):
    ctx = _ctx(example_copy, tmp_path, "cpp_bounds_bug")
    assert run_compile(ctx).outcome is Outcome.PASS
    assert run_integrity(ctx).outcome is Outcome.PASS
    assert run_static_analysis(ctx).outcome is Outcome.PASS
    res = run_sanitizer_tests(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.RUNTIME_TEST_FAILED
    failed = {c["name"] for c in res.details["cases"] if c["status"] == "fail"}
    assert failed == {"fill_to_capacity", "capacity_one"}
    asan = [f for f in res.failures if f.tool == "asan"]
    assert asan and all(f.property_id == "heap-buffer-overflow" for f in asan)
    loc = asan[0].location
    assert (loc.file, loc.line) == ("src/sample_buffer.cpp", 14)
    assert loc.function and loc.function.startswith("lc::SampleBuffer::append(int)")


def test_clangxx_linker_errors_are_parsed():
    text = (
        "/usr/bin/ld: /tmp/x-1.o: in function `main':\n"
        "tests/t.cpp:9:(.text+0x1c): undefined reference to `lc::SampleBuffer::append(int)'\n"
        "clang++: error: linker command failed with exit code 1 (use -v to see invocation)\n"
    )
    diags = parse_build_diagnostics(text)
    assert [d.rule_id for d in diags] == ["undefined-reference", "link-error"]
    assert "lc::SampleBuffer::append(int)" in diags[0].message


ATTACKS = [
    ("attack_opnew.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_ctor_exit.cpp", {"CONSTRUCTOR", "TERMINATION"}, {"TERMINATION"}),
    ("attack_ctor_exit2.cpp", {"CONSTRUCTOR", "TERMINATION", "SYMBOL_OVERRIDE"}, {"TERMINATION"}),
    ("attack_throw.cpp", {"EXCEPTIONS"}, {"EXCEPTIONS"}),
    ("attack_thread.cpp", {"DANGEROUS_IMPORT"}, {"DANGEROUS_IMPORT"}),
    ("attack_clang.cpp", {"TOOL_DIVERGENCE"}, {"TOOL_DIVERGENCE"}),
    ("attack_libc.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_assertfail.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_nsstd.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_stdspec.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_macro.cpp", {"SYMBOL_OVERRIDE"}, {"SYMBOL_OVERRIDE"}),
    ("attack_rtti.cpp", {"CPP_RUNTIME"}, {"CPP_RUNTIME"}),
    ("attack_noreturn.cpp", {"ASSUMPTION_INJECTION"}, {"ASSUMPTION_INJECTION"}),
    ("attack_helper.cpp", {"SYMBOL_OVERRIDE"}, set()),
    ("attack_static_dtor.cpp", {"TERMINATION"}, set()),
]


@pytest.mark.parametrize("fixture,gate_rules,policy_rules", ATTACKS, ids=[a[0] for a in ATTACKS])
def test_integrity_gate_rejects_cpp_attacks(fixture, gate_rules, policy_rules, example_copy, tmp_path, needs_clang):
    text = (FIX / fixture).read_text()
    ctx = _ctx(example_copy, tmp_path, RB[0], {RB[1]: text}, report=True)
    comp = run_compile(ctx)
    assert comp.outcome is Outcome.PASS, comp.summary
    res = run_integrity(ctx)
    assert res.outcome is Outcome.FAIL and res.status is Status.STATIC_ANALYSIS_WARNING
    assert gate_rules <= _rules(res), (gate_rules, [(f.property_id, f.description) for f in res.failures])
    original = (EXAMPLES / RB[0] / RB[1]).read_text()
    decision = check_candidate(ctx.spec, {RB[1]: original}, {RB[1]: text})
    rules = {v.rule for v in decision.violations}
    assert policy_rules <= rules, (policy_rules, decision.violations)
    if policy_rules:
        assert not decision.allowed


def test_integrity_attack_is_never_executed(example_copy, tmp_path, capsys, needs_clang, needs_bwrap):
    root = example_copy(RB[0])
    (root / RB[1]).write_text((FIX / "attack_ctor_exit.cpp").read_text())
    assert main(["spec", "approve", str(root / "spec.yaml"), "--by", "t", "--yes", "--json"]) == 0
    capsys.readouterr()
    main(["verify", str(root / "spec.yaml"), "--json"])
    import json

    data = json.loads(capsys.readouterr().out)
    stages = {s["stage"]: s for s in data["verification"]["stages"]}
    assert data["status"] == "STATIC_ANALYSIS_WARNING"
    assert stages["integrity"]["outcome"] == "FAIL"
    assert stages["sanitizer"]["outcome"] == "SKIPPED"


def test_legit_cpp20_code_passes_integrity_policy_and_tests(example_copy, tmp_path, needs_clang, needs_bwrap):
    text = (FIX / "legit_ring_buffer.cpp").read_text()
    ctx = _ctx(example_copy, tmp_path, RB[0], {RB[1]: text})
    assert run_compile(ctx).outcome is Outcome.PASS
    integ = run_integrity(ctx)
    assert integ.outcome is Outcome.PASS, [f.description for f in integ.failures]
    assert run_static_analysis(ctx).outcome is Outcome.PASS
    assert run_sanitizer_tests(ctx).outcome is Outcome.PASS
    original = (EXAMPLES / RB[0] / RB[1]).read_text()
    assert check_candidate(ctx.spec, {RB[1]: original}, {RB[1]: text}).allowed
    assert check_generated(ctx.spec, {RB[1]: text}).allowed


@pytest.mark.parametrize("example,rel", [RB, SB])
def test_policy_accepts_cpp_examples(example, rel):
    spec = load_spec(EXAMPLES / example / "spec.yaml")
    for path in (EXAMPLES / example / rel, EXAMPLES / example / "mock" / "generate" / rel):
        text = path.read_text()
        decision = check_generated(spec, {rel: text})
        assert decision.allowed, decision.violations
        headers = {h: (EXAMPLES / example / h).read_text() for h in spec.sources.headers}
        assert source_violations(spec, rel, text, headers=headers) == []


def test_policy_accepts_the_cpp_repair_fixture():
    import json

    spec = load_spec(EXAMPLES / SB[0] / "spec.yaml")
    fixture = json.loads((EXAMPLES / SB[0] / "mock" / "repair" / "round-1.json").read_text())
    old = {SB[1]: (EXAMPLES / SB[0] / SB[1]).read_text()}
    decision = check_candidate(spec, old, fixture["files"])
    assert decision.allowed, decision.violations


def test_policy_cpp_target_rules():
    spec = load_spec(EXAMPLES / RB[0] / "spec.yaml")
    original = (EXAMPLES / RB[0] / RB[1]).read_text()
    start = original.index("bool RingBuffer<T, N>::push")
    end = original.index("template <std::copyable T, std::size_t N>\nbool RingBuffer<T, N>::pop")
    stub = original[:start] + "bool RingBuffer<T, N>::push(const T& value) noexcept {\n    (void)value;\n" \
        "    return true;\n}\n\n" + original[end:]
    rules = {v.rule for v in check_candidate(spec, {RB[1]: original}, {RB[1]: stub}).violations}
    assert "TRIVIALIZED_TARGET" in rules
    no_push = original[:start] + original[end:]
    rules = {v.rule for v in check_generated(spec, {RB[1]: no_push}).violations}
    assert "MISSING_TARGET" in rules
    defs = cpi.find_cpp_function_definitions(cpp_lexer.CppSource(original), "push", RB[1])
    assert len(defs) == 1 and not defs[0].trivial and defs[0].line == original[:start].count("\n") + 1


def _src_rules(text: str, example: str = RB[0]) -> set[str]:
    spec = load_spec(EXAMPLES / example / "spec.yaml")
    return {v.rule for v in source_violations(spec, "src/ring_buffer.cpp", text)}


@pytest.mark.parametrize(
    "text,rule",
    [
        ("int f() { try { return 1; } catch (...) { return 0; } }\n", "EXCEPTIONS"),
        ("#include <stdexcept>\n", "EXCEPTIONS"),
        ("struct B { virtual ~B(); };\nstruct D : B {};\nbool f(B* b) { return dynamic_cast<D*>(b); }\n",
         "CPP_RUNTIME"),
        ("#include <coroutine>\n", "CPP_RUNTIME"),
        ("task f() { co_await g(); }\n", "CPP_RUNTIME"),
        ("#include <iostream>\n", "INCLUDE_ESCAPE"),
        ("#include <csetjmp>\n", "TERMINATION"),
        ("void (*f)(int) = &std::exit;\n", "TERMINATION"),
        ("void f() { std::terminate(); }\n", "TERMINATION"),
        ("void f() { std::set_terminate(nullptr); }\n", "TERMINATION"),
        ("#if __cpp_exceptions\n#endif\n", "TOOL_DIVERGENCE"),
        ("#ifdef __cpp_lib_span\n#endif\n", "TOOL_DIVERGENCE"),
        ("#define __cplusplus 1\n", "TOOL_DIVERGENCE"),
        ("#if defined(__GNUC__)\n#endif\n", "TOOL_DIVERGENCE"),
        ('#include "ring_buffer.hpp"\n#define size(x) 0\n#include <array>\n', "SYMBOL_OVERRIDE"),
        ("#define class struct\n", "SYMBOL_OVERRIDE"),
        ("namespace std::ranges { int x; }\n", "SYMBOL_OVERRIDE"),
        ("int std::foo(int x) { return x; }\n", "SYMBOL_OVERRIDE"),
        ("void operator delete[](void* p) noexcept { }\n", "SYMBOL_OVERRIDE"),
        ("struct X { int v; };\nvoid* X::operator new(unsigned long n) { return nullptr; }\n", "SYMBOL_OVERRIDE"),
        ("int main() { return 0; }\n", "SYMBOL_OVERRIDE"),
        ('extern "C" { int nondet_int() { return 0; } }\n', "SYMBOL_OVERRIDE"),
        ("int f() { int x = 1'0; __builtin_unreachable(); }\n", "ASSUMPTION_INJECTION"),
        ('auto s = R"(a\nb)"; int g() { exit(0); }\n', "TERMINATION"),
        ("#pragma clang diagnostic ignored \"-Wall\"\n", "PRAGMA"),
    ],
)
def test_cpp_source_rules(text, rule):
    assert rule in _src_rules(text), (rule, _src_rules(text))


@pytest.mark.parametrize(
    "text",
    [
        "#include <array>\n#include <cstdint>\n#include <concepts>\n#include <span>\n#include <cstdlib>\n",
        "#if __cplusplus >= 202002L\n#endif\n#ifdef __cpp_concepts\n#endif\n",
        "constexpr unsigned long long kBig = 1'000'000'000ULL;\nconstexpr char c = '\\'';\n",
        'const char* doc = R"x(exit(0); throw 1; #ifdef __clang__ namespace std {)x";\n',
        "struct Monitor { int signal(int); };\nint Monitor::signal(int s) { return s; }\n"
        "int use(Monitor& m) { return m.signal(1); }\n",
        "[[nodiscard]] constexpr int f(int x) noexcept { if (x > 0) [[likely]] { return x; } return -x; }\n",
        "namespace { struct H { int v; }; }\nnamespace lc { int g(); }\n",
        "#include <new>\nint* make(void* buf) { return new (buf) int(1); }\n",
        "void* p = ::operator new(16);\n",
        "template <typename T> requires (sizeof(T) > 1) T id(T x) noexcept { return x; }\n",
    ],
)
def test_cpp_source_rules_no_false_positives(text):
    assert _src_rules(text) == set(), source_violations(load_spec(EXAMPLES / RB[0] / "spec.yaml"),
                                                        "src/ring_buffer.cpp", text)


def test_cpp_normalize_keeps_offsets():
    text = "int a = 1'000; auto r = R\"d(x\ny\"z)d\"; exit(0);\nchar c = 'q';\n"
    out = cpp_lexer.cpp_normalize(text)
    assert len(out) == len(text) and out.count("\n") == text.count("\n")
    assert "1'000" not in out and "exit(0)" in out and "'q'" in out
    src = cpp_lexer.CppSource(text)
    assert src.raw == text
    assert "exit" in src.code.split("\n")[1]


HEADER = """#pragma once
namespace lc {
template <class T, int N> class RingBuffer {
public:
    bool push(const T& v) noexcept;
    bool operator==(const RingBuffer& o) const noexcept;
};
struct Shape { virtual ~Shape(); virtual int area() const = 0; };
}
"""


@pytest.mark.parametrize(
    "symbols,rules",
    [
        ([("_ZN2lc10RingBufferIiLi4EE4pushERKi", "W", "lc::RingBuffer<int, 4>::push(int const&)")], set()),
        ([("_ZN2lc10RingBufferIiLi4EE4pushERKi", "T", "lc::RingBuffer<int, 4>::push(int const&)")], set()),
        ([("_ZNK2lc10RingBufferIiLi4EEeqERKS1_", "W",
           "lc::RingBuffer<int, 4>::operator==(lc::RingBuffer<int, 4> const&) const")], set()),
        ([("_ZZN2lc10RingBufferIiLi4EE4pushERKiE1k", "V", "lc::RingBuffer<int, 4>::push(int const&)::k")], set()),
        ([("_ZTVN2lc5ShapeE", "V", "vtable for lc::Shape"), ("_ZTIN2lc5ShapeE", "V", "typeinfo for lc::Shape"),
          ("_ZTVN10__cxxabiv117__class_type_infoE", "U", "vtable for __cxxabiv1::__class_type_info"),
          ("__cxa_pure_virtual", "U", "__cxa_pure_virtual")], set()),
        ([("_ZNKSt5arrayIiLm4EEixEm", "W", "std::array<int, 4ul>::operator[](unsigned long) const")], set()),
        ([("_ZSt3minImERKT_S2_S2_", "W", "unsigned long const& std::min<unsigned long>(unsigned long const&, "
           "unsigned long const&)")], set()),
        ([("_Znwm", "U", "operator new(unsigned long)"), ("_ZdlPvm", "U", "operator delete(void*, unsigned long)"),
          ("_ZSt20__throw_length_errorPKc", "U", "std::__throw_length_error(char const*)"),
          ("__cxa_guard_acquire", "U", "__cxa_guard_acquire"), ("memcpy", "U", "memcpy")], set()),
        ([("__clang_call_terminate", "W", "__clang_call_terminate"), ("_ZSt9terminatev", "U", "std::terminate()"),
          ("__cxa_begin_catch", "U", "__cxa_begin_catch"), ("DW.ref.__gxx_personality_v0", "V",
          "DW.ref.__gxx_personality_v0"), ("__gxx_personality_v0", "U", "__gxx_personality_v0")], set()),
        ([("_ZNKSt5arrayIiLm4EEixEm", "T", "std::array<int, 4ul>::operator[](unsigned long) const")],
         {"SYMBOL_OVERRIDE"}),
        ([("_Znwm", "T", "operator new(unsigned long)")], {"SYMBOL_OVERRIDE"}),
        ([("_ZdlPv", "W", "operator delete(void*)")], {"SYMBOL_OVERRIDE"}),
        ([("_ZN2lc6helperEv", "T", "lc::helper()")], {"SYMBOL_OVERRIDE"}),
        ([("_ZN6HelperC2Ev", "W", "Helper::Helper()")], {"SYMBOL_OVERRIDE"}),
        ([("_ZTVN2lc6HiddenE", "V", "vtable for lc::Hidden")], {"SYMBOL_OVERRIDE"}),
        ([("strlen", "T", "strlen")], {"SYMBOL_OVERRIDE"}),
        ([("_ZSt9terminatev", "U", "std::terminate()")], {"TERMINATION"}),
        ([("__cxa_begin_catch", "U", "__cxa_begin_catch")], {"EXCEPTIONS"}),
        ([("__cxa_throw", "U", "__cxa_throw"), ("__cxa_allocate_exception", "U", "__cxa_allocate_exception")],
         {"EXCEPTIONS"}),
        ([("__dynamic_cast", "U", "__dynamic_cast")], {"CPP_RUNTIME"}),
        ([("__cxa_atexit", "U", "__cxa_atexit")], {"TERMINATION"}),
        ([("exit", "U", "exit")], {"TERMINATION"}),
        ([("_ZNSt6thread4joinEv", "U", "std::thread::join()")], {"DANGEROUS_IMPORT"}),
        ([("_ZNSo3putEc", "U", "std::ostream::put(char)")], {"DANGEROUS_IMPORT"}),
        ([("_ZN2lc5otherEv", "U", "lc::other()")], {"DANGEROUS_IMPORT"}),
        ([("__pthread_key_create", "w", "__pthread_key_create")], {"DANGEROUS_IMPORT"}),
    ],
)
def test_cpp_object_symbol_rules(symbols, rules):
    spec = load_spec(EXAMPLES / RB[0] / "spec.yaml")
    facts = cpi.header_facts({"include/ring_buffer.hpp": HEADER})
    found = {v.rule for v in cpi.check_cpp_object_symbols(spec, RB[1], symbols, facts=facts, defined_elsewhere=set())}
    assert found == rules, cpi.check_cpp_object_symbols(spec, RB[1], symbols, facts=facts, defined_elsewhere=set())


@pytest.mark.parametrize(
    "text,components,owner",
    [
        ("lc::RingBuffer<int, 4ul>::push(int const&)", ("lc", "RingBuffer", "push"), None),
        ("lc::RingBuffer<int, 4ul>::size[abi:cxx11]() const", ("lc", "RingBuffer", "size"), None),
        ("unsigned long lc::f<int>(int)", ("lc", "f"), None),
        ("lc::f()::{lambda(int)#1}::operator()(int) const", ("lc", "f", "{lambda}", "operator()"), ("lc", "f")),
        ("guard variable for lc::f()::x", ("lc", "f", "x"), ("lc", "f")),
        ("operator new[](unsigned long, std::nothrow_t const&)", ("operator new[]",), None),
        ("lc::X::operator<(lc::X const&) const", ("lc", "X", "operator<"), None),
        ("lc::X::operator bool() const", ("lc", "X", "operator bool"), None),
        ("void (*&std::__get_helper<0ul, void (*)()>(std::_Tuple_impl<0ul, void (*)()>&))()",
         ("std", "__get_helper"), None),
    ],
)
def test_parse_demangled(text, components, owner):
    nm = cpi.parse_demangled(text)
    assert nm is not None and nm.components == components
    assert nm.owner == (owner or components)


def test_cpp_specs_get_cpp_system_prompts():
    from leanc.llm.base import CodeGenRequest
    from leanc.llm.prompts import CODEGEN_SYSTEM, build_codegen_prompt

    c_spec = load_spec(EXAMPLES / "safe_array_get" / "spec.yaml")
    c_prompt = build_codegen_prompt(CodeGenRequest(c_spec, "x: 1", {}, c_spec.sources.editable))
    assert c_prompt.system.strip() == CODEGEN_SYSTEM.strip()
    spec = load_spec(EXAMPLES / RB[0] / "spec.yaml")
    hpp = (EXAMPLES / RB[0] / "include" / "ring_buffer.hpp").read_text()
    prompt = build_codegen_prompt(CodeGenRequest(spec, "x: 1", {"include/ring_buffer.hpp": hpp}, [RB[1]]))
    assert "clang++ (C++20" in prompt.system and "C17" not in prompt.system
    assert "anonymous namespace" in prompt.system and "{std}" not in prompt.system
    assert "```cpp" in prompt.user


def test_cpp_definition_finder_shapes():
    text = (
        'extern "C" { int c_api(int x) { return x; } }\n'
        "namespace lc {\n"
        "A::A(int x) : a_(x), b_{x} { g(); }\n"
        "auto RingBuffer<T, N>::size() const noexcept -> std::size_t { return count_; }\n"
        "template <typename T> requires (sizeof(T) > 1) T h(T x) { return x; }\n"
        "namespace { int hidden(int) { return 2; } }\n"
        "int decl(int);\n"
        "int dflt() = delete;\n"
        "struct S { int inside() { return 1; } };\n"
        "}\n"
    )
    defs = {d.name: d for d in cpi.cpp_function_definitions(cpp_lexer.CppSource(text))}
    assert defs["c_api"].scope == "extern_c" and defs["c_api"].has_body
    assert defs["A"].qualifier == "A::" and "g();" in defs["A"].body
    assert defs["size"].qualifier == "RingBuffer<T,N>::" and "count_" in defs["size"].body
    assert "h" in defs and defs["hidden"].scope == "anonymous"
    assert "decl" not in defs and not defs["dflt"].has_body
    assert "inside" not in defs


@pytest.mark.parametrize(
    "pattern",
    ["f() -> ", "g() requires ", "A::A(int x) : ", "A::A() : a(1), ", "template<> ", "class ", ">", "f(x) ", "'",
     'R"(', "1'2 ", "{"],
)
def test_cpp_rules_stay_linear_on_hostile_input(pattern):
    import time

    spec = load_spec(EXAMPLES / RB[0] / "spec.yaml")
    text = pattern * (200_000 // len(pattern))
    t0 = time.monotonic()
    source_violations(spec, RB[1], text)
    check_generated(spec, {RB[1]: text})
    assert time.monotonic() - t0 < 20
