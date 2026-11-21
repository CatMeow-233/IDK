from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanc.backends.counterexample import (
    call_stack_at_failure,
    clean_name,
    explain_counterexample,
    extract_inputs,
    failure_from_cbmc_property,
    fingerprint,
    is_internal_name,
    is_reach_probe,
    is_unwinding_property,
    location_from_cbmc,
    property_class,
    render_value,
    summarize_trace,
    unwind_failure,
    vacuity_failure,
)
from leanc.foundation.models import FailureReport, Location

JSON_DIR = Path(__file__).resolve().parent / "fixtures" / "cbmc" / "json"
HARNESS = "harness/h.c"


def results(name: str) -> list[dict]:
    data = json.loads((JSON_DIR / name).read_text())
    return next(m["result"] for m in data if "result" in m)


def prop(name: str, property_id: str) -> dict:
    return next(r for r in results(name) if r["property"] == property_id)


def failed(name: str) -> list[dict]:
    return [r for r in results(name) if r["status"] == "FAILURE"]


def ins(f) -> dict[str, str]:
    return {i.name: i.value for i in f.inputs}


def test_off_by_one_failure_report():
    r = prop("arr_get_off_by_one.verify.json", "arr_get.pointer_dereference.11")
    f = failure_from_cbmc_property(r, raw_ref="raw/cbmc-verify.json", harness=HARNESS, target_function="arr_get")
    assert f.id == "cbmc:arr_get.pointer_dereference.11"
    assert f.kind == "counterexample" and f.tool == "cbmc"
    assert f.property_id == "arr_get.pointer_dereference.11"
    assert f.description == "dereference failure: pointer outside object bounds in arr[(signed long int)idx]"
    assert f.location == Location(file="src/impl.c", line=11, function="arr_get")
    assert f.raw_ref == "raw/cbmc-verify.json"
    i = ins(f)
    assert list(i) == ["len", "idx", "arr", "out", "before", "param arr", "param len", "param idx", "param out"]
    assert i["idx"] == i["len"] == i["param idx"] == i["param len"] == "4"
    assert i["param arr"] == "&dynamic_object" and i["param out"] == "&out"
    types = {x.name: x.type for x in f.inputs}
    assert types["len"] == "size_t" and types["param out"] == "signed int *"
    assert f.call_stack == ["main (harness/h.c:20)", "arr_get (src/impl.c:11)"]
    texts = [t.text for t in f.trace]
    assert texts[0] == "call main()"
    assert "call arr_get(arr=&dynamic_object, len=4, idx=4, out=&out)" in texts
    assert f.trace[-1].kind == "failure" and "arr_get.pointer_dereference.11" in f.trace[-1].text
    for t in texts:
        assert "return_value_" not in t and "__CPROVER" not in t and "$" not in t
    e = f.explanation
    assert "arr_get.pointer_dereference.11" in e and "src/impl.c:11 in arr_get()" in e
    assert "outside the bounds" in e and "param idx = 4" in e and "param len = 4" in e
    assert e == explain_counterexample(f)


def test_every_captured_failure_is_reported_without_internal_names():
    targets = {
        "arr_get_off_by_one.verify.json": "arr_get",
        "avg.verify.json": "avg",
        "add.verify.json": "add",
        "deref.verify.json": "deref",
        "point.verify.json": "point_ratio",
    }
    for name, target in targets.items():
        reports = [
            failure_from_cbmc_property(r, raw_ref=None, harness=HARNESS, target_function=target)
            for r in failed(name)
            if not is_unwinding_property(r)
        ]
        assert reports, name
        fps = [fingerprint(f) for f in reports]
        assert len(set(fps)) == len(fps), (name, fps)
        for f in reports:
            assert f.inputs and f.call_stack and f.trace and f.explanation
            assert any(i.name.startswith("param ") for i in f.inputs)
            for i in f.inputs:
                assert not is_internal_name(i.name.removeprefix("param ")), (name, i.name)
                assert len(i.value) <= 200


def test_inputs_are_final_values_not_declarations():
    (r,) = failed("avg.verify.json")
    f = failure_from_cbmc_property(r, raw_ref=None, harness=HARNESS, target_function="avg")
    i = ins(f)
    assert list(i) == ["total", "count", "param total", "param count"]
    assert i["count"] == i["param count"] == "0"
    assert i["total"] == i["param total"] != "0"


def test_struct_and_array_values_are_folded():
    r = prop("point.verify.json", "point_ratio.division-by-zero.1")
    i = ins(failure_from_cbmc_property(r, raw_ref=None, harness=HARNESS, target_function="point_ratio"))
    assert i["q"].startswith("{.x=5, .y=") and i["q"].endswith(".tag={'a', 0, 0}}")
    assert "$pad" not in i["q"]
    assert i["arr"].startswith("{1, ") and i["arr"].endswith(", 3}")
    assert i["flag"] in ("TRUE", "FALSE")
    assert i["param p"] == "&q"
    assert i["param k"] == i["k"]


def test_extract_inputs_without_target_call_or_target_name():
    r = prop("arr_get_off_by_one.verify.json", "main.assertion.3")
    trace = r["trace"]
    no_target = extract_inputs(trace, HARNESS, None)
    names = [x.name for x in no_target]
    assert "rc" in names and not any(n.startswith("param ") for n in names)
    wrong_target = extract_inputs(trace, HARNESS, "not_called")
    assert [x.name for x in wrong_target] == names
    other_harness = extract_inputs(trace, "harness/other.c", "arr_get")
    assert [x.name for x in other_harness] == ["param arr", "param len", "param idx", "param out"]
    assert extract_inputs(None, HARNESS, "arr_get") == []
    assert extract_inputs([1, "x", None], HARNESS, "arr_get") == []


def test_call_stack_and_failure_selection():
    r = prop("point.verify.json", "point_ratio.division-by-zero.1")
    assert call_stack_at_failure(r["trace"], r["property"]) == ["main (harness/h.c:17)", "point_ratio (src/impl.c:5)"]
    a = prop("point.verify.json", "main.assertion.1")
    steps = [s for s in a["trace"] if s.get("stepType") == "failure"]
    assert len(steps) >= 2
    assert call_stack_at_failure(a["trace"], "main.assertion.1") == ["main (harness/h.c:19)"]
    assert call_stack_at_failure(a["trace"], steps[0]["property"])[-1].startswith("point_ratio (src/impl.c:5)")
    assert call_stack_at_failure([]) == []


def test_summarize_trace_truncation():
    r = prop("arr_get_off_by_one.verify.json", "main.assertion.4")
    full = summarize_trace(r["trace"], max_steps=1000)
    assert len(full) > 12
    short = summarize_trace(r["trace"], max_steps=12)
    assert len(short) == 12
    assert short[0] == full[0] and short[-1] == full[-1]
    assert any("omitted" in t.text for t in short)
    assert summarize_trace(r["trace"], max_steps=0) == []
    assert summarize_trace("not a trace") == []


def test_unwind_failure_report():
    (r,) = failed("count_zeros_unwind3.verify.json")
    assert is_unwinding_property(r)
    f = unwind_failure(r, raw_ref="raw/cbmc-verify.json", unwind=3, harness=HARNESS, target_function="count_zeros")
    assert f.kind == "unwind" and f.id == "cbmc:count_zeros.unwind.0" and f.tool == "cbmc"
    assert f.description == "unwinding assertion loop 0"
    assert f.location.file == "src/impl.c" and f.location.function == "count_zeros"
    assert int(ins(f)["param n"]) >= 3
    assert "unwind bound 3" in f.explanation and "re-approved spec" in f.explanation
    bare = unwind_failure({"property": "f.unwind.2", "description": "unwinding assertion loop 2"})
    assert bare.kind == "unwind" and bare.inputs == [] and "unwind bound" in bare.explanation


def test_unwinding_and_probe_classification():
    assert is_unwinding_property({"property": "sum.unwind.0", "description": "unwinding assertion loop 0"})
    assert is_unwinding_property({"property": "f.recursion", "description": "recursion unwinding assertion"})
    assert is_unwinding_property({"property": "x", "description": "unwinding assertion loop 3"})
    assert not is_unwinding_property({"property": "main.assertion.1", "description": "assertion x"})
    assert not is_unwinding_property(None)
    vac = results("arr_get_contradictory.vacuity.json")
    probes = [r for r in vac if is_reach_probe(r)]
    assert [(p["description"], p["status"]) for p in probes] == [("leanc_reach:after_call", "SUCCESS")]


@pytest.mark.parametrize(
    "pid,desc,expected",
    [
        ("arr_get.pointer_dereference.11", "", "pointer_dereference"),
        ("main.array_bounds.2", "", "array_bounds"),
        ("avg.division-by-zero.1", "", "division-by-zero"),
        ("add.overflow.1", "", "overflow"),
        ("main.assertion.3", "", "assertion"),
        ("count_zeros.unwind.0", "", "unwind"),
        ("f.recursion", "recursion unwinding assertion", "recursion"),
        ("__CPROVER__start.memory-leak.1", "", "memory-leak"),
        (None, "dereference failure: pointer NULL in *p", "pointer_dereference"),
        (None, "division by zero in a / b", "division-by-zero"),
        ("weird", "arithmetic overflow on signed * in a * b", "overflow"),
        (None, "array 'a' upper bound in a[i]", "array_bounds"),
        (None, "assertion x > 0", "assertion"),
        (None, "something else", "property"),
    ],
)
def test_property_class(pid, desc, expected):
    assert property_class(pid, desc) == expected


def test_vacuity_failure_report():
    (r,) = [x for x in results("arr_get_contradictory.vacuity.json") if is_reach_probe(x)]
    f = vacuity_failure("after_call", result=r, raw_ref="raw/cbmc-vacuity.json")
    assert f.kind == "vacuity" and f.id == "cbmc:vacuity:after_call" and f.property_id == r["property"]
    assert f.location.file == HARNESS
    assert "unreachable" in f.description and "vacuously" in f.explanation
    g = vacuity_failure("ghost", reported=False)
    assert "not reported" in g.description and g.location is None
    assert "unwind bound" not in f.explanation
    h = vacuity_failure("after_loop", unwind_insufficient=True)
    assert "unwind bound were not explored" in h.explanation


def test_render_value():
    assert render_value({"data": "8ul", "name": "integer", "type": "__CPROVER_size_t"}) == "8"
    assert render_value({"data": "-5", "name": "integer"}) == "-5"
    assert render_value({"data": "'a'", "name": "integer", "type": "char"}) == "'a'"
    assert render_value({"data": "TRUE", "name": "integer", "type": "_Bool"}) == "TRUE"
    assert render_value({"data": "1.5", "name": "float"}) == "1.5"
    assert render_value({"data": "((signed int *)NULL)", "name": "pointer"}) == "NULL"
    assert render_value({"data": "storage!0@1", "name": "pointer"}) == "&storage"
    assert render_value({"name": "unknown"}) == "?"
    assert render_value(None) == "?"
    arr = {"elements": [{"index": k, "value": {"data": str(k), "name": "integer"}} for k in range(40)]}
    s = render_value(arr)
    assert s.startswith("{0, 1, 2") and "(+24 more)" in s
    sparse = {"elements": [{"index": 3, "value": {"data": "7"}}]}
    assert render_value(sparse) == "{[3]=7}"
    st = {"members": [{"name": "x", "value": {"data": "1"}}, {"name": "$pad0", "value": {"data": "0"}}]}
    assert render_value(st) == "{.x=1}"
    long = {"elements": [{"index": k, "value": {"data": "1234567890"}} for k in range(16)]}
    assert len(render_value(long, max_chars=50)) == 50 and render_value(long, max_chars=50).endswith("...")
    deep: dict = {"data": "1"}
    for _ in range(10):
        deep = {"members": [{"name": "m", "value": deep}]}
    assert "{...}" in render_value(deep)


def test_internal_names_and_cleaning():
    for n in ("return_value_nondet_int", "return_value_nondet_size_t$0", "__CPROVER_dead_object",
              "goto_symex$$return_value$$f", "tmp_if_expr", "x#2", "", None, "__VERIFIER_nondet"):
        assert is_internal_name(n), n
    for n in ("len", "storage[0l]", "q.x", "out_cell"):
        assert not is_internal_name(n), n
    assert clean_name("storage!0@1") == "storage"
    assert clean_name("storage[3l]") == "storage[3]"
    assert clean_name("main::1::x") == "x"


def test_location_from_cbmc():
    assert location_from_cbmc({"file": "src/a.c", "line": "7", "function": "f"}) == Location(
        file="src/a.c", line=7, function="f"
    )
    loc = location_from_cbmc({"file": "/work/snapshot/src/a.c", "line": "3", "workingDirectory": "/work/snapshot"})
    assert loc.file == "src/a.c" and loc.line == 3
    assert location_from_cbmc({"file": "<builtin-library-malloc>", "line": "43"}).file == "<builtin-library-malloc>"
    assert location_from_cbmc({"file": "./x.c"}).file == "x.c"
    assert location_from_cbmc({}) is None and location_from_cbmc(None) is None


def _cx(pid: str, desc: str, line: int = 11, file: str = "src/impl.c", func: str = "arr_get") -> FailureReport:
    return FailureReport(
        id=f"cbmc:{pid}",
        kind="counterexample",
        tool="cbmc",
        property_id=pid,
        description=desc,
        location=Location(file=file, line=line, function=func),
    )


def test_fingerprint_ignores_numeric_property_suffix():
    a = _cx("arr_get.pointer_dereference.11", "dereference failure: pointer outside object bounds in arr[(signed long int)idx]")
    b = _cx("arr_get.pointer_dereference.12", "dereference failure: pointer outside object bounds in arr[(signed long int)idx]")
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) == (
        "cbmc:counterexample:pointer_dereference@src/impl.c:11:arr_get [dereference failure: pointer outside object bounds]"
    )
    c = _cx("arr_get.pointer_dereference.3", "dereference failure: pointer outside object bounds in arr[idx]")
    assert fingerprint(c) == fingerprint(a)


def test_fingerprint_distinguishes_place_and_kind():
    base = _cx("f.pointer_dereference.1", "dereference failure: pointer NULL in *p")
    assert fingerprint(base) != fingerprint(_cx("f.pointer_dereference.1", "dereference failure: pointer NULL in *p", line=12))
    assert fingerprint(base) != fingerprint(_cx("f.pointer_dereference.1", "dereference failure: pointer NULL in *p", file="src/b.c"))
    assert fingerprint(base) != fingerprint(_cx("f.pointer_dereference.2", "dereference failure: dead object in *p"))
    assert fingerprint(_cx("main.assertion.1", "assertion rc == 0", 29, HARNESS, "main")) != fingerprint(
        _cx("main.assertion.2", "assertion out == before", 29, HARNESS, "main")
    )


def test_fingerprint_of_real_reports_is_stable_across_reparse():
    r = prop("deref.verify.json", "deref.pointer_dereference.1")
    f1 = failure_from_cbmc_property(r, raw_ref="a", harness=HARNESS, target_function="deref")
    r2 = dict(r, property="deref.pointer_dereference.7")
    f2 = failure_from_cbmc_property(r2, raw_ref="b", harness=HARNESS, target_function="deref")
    assert f1.id != f2.id and fingerprint(f1) == fingerprint(f2)


def test_fingerprint_unwind_and_vacuity():
    (r,) = failed("count_zeros_unwind3.verify.json")
    u = unwind_failure(r)
    assert fingerprint(u) == "cbmc:unwind:unwind@src/impl.c:6:count_zeros"
    u2 = unwind_failure(dict(r, property="count_zeros.unwind.3", description="unwinding assertion loop 3"))
    assert fingerprint(u2) == fingerprint(u)
    v = vacuity_failure("after_call", result={"property": "main.assertion.1", "sourceLocation": {"file": HARNESS, "line": "20", "function": "main"}})
    assert fingerprint(v) == "cbmc:vacuity:vacuity@harness/h.c:20:main [after_call]"


def test_fingerprint_for_non_cbmc_failures():
    compile_err = FailureReport(
        id="compile:error:src/a.c:4:5", kind="compile", tool="clang", property_id=None,
        description="expected ';' after expression", location=Location(file="src/a.c", line=4, column=5),
    )
    assert fingerprint(compile_err) == "clang:compile:-@src/a.c:4:? [expected ';' after expression]"
    static = FailureReport(
        id="sa:core.NullDereference:src/a.c:9", kind="static", tool="clang-static-analyzer",
        property_id="core.NullDereference", description="Dereference of null pointer (loaded from variable 'p')",
        location=Location(file="src/a.c", line=9, function="f"),
    )
    assert fingerprint(static).startswith("clang-static-analyzer:static:core.NullDereference@src/a.c:9:f [")
    r1 = FailureReport(
        id="asan:1", kind="runtime", tool="asan", property_id="heap-buffer-overflow",
        description="==12345==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000014 at pc 0x55d1 "
        "/tmp/run-abc/build/test_driver",
        location=None,
    )
    r2 = r1.model_copy(update={"description": r1.description.replace("12345", "999").replace("0x602000000014", "0x7fff0010")
                               .replace("/tmp/run-abc/", "/var/tmp/other-run/")})
    assert fingerprint(r1) == fingerprint(r2)
    assert fingerprint(r1).startswith("asan:runtime:heap-buffer-overflow@?:?:? [asan:1: ")
    oracle = FailureReport(id="sanitizer:normal:test-oracle:exit-code#2", kind="test-oracle", tool="test-oracle",
                           property_id="exit-code", description="exit code 1, expected 0\nmore lines")
    assert fingerprint(oracle) == (
        "test-oracle:test-oracle:exit-code@?:?:? [sanitizer:normal:test-oracle:exit-code: exit code 1, expected 0]"
    )
    other = oracle.model_copy(update={"id": "sanitizer:null:test-oracle:exit-code"})
    assert fingerprint(other) != fingerprint(oracle)
    empty = FailureReport(id="x", kind="tool", tool="", description="")
    assert fingerprint(empty) == "?:tool:-@?:?:? [x]"


def test_fingerprint_never_raises_on_odd_objects():
    class Odd:
        tool = None
        kind = None
        location = object()
        property_id = 42
        description = None

    assert isinstance(fingerprint(Odd()), str)


def test_explain_other_kinds():
    f = FailureReport(id="x", kind="static", tool="clang-static-analyzer", property_id="core.NullDereference",
                      description="Dereference of null pointer", location=Location(file="src/a.c", line=3))
    assert explain_counterexample(f) == (
        "clang-static-analyzer reported a static failure (core.NullDereference) at src/a.c:3: Dereference of null pointer"
    )


def test_robust_to_malformed_results():
    f = failure_from_cbmc_property({}, raw_ref=None, harness=None, target_function=None)
    assert f.id == "cbmc:unknown" and f.kind == "counterexample" and f.inputs == []
    g = failure_from_cbmc_property(
        {"property": "p.assertion.1", "description": "assertion x", "trace": [{"stepType": "failure", "sourceLocation": {"file": "a.c", "line": "2"}}, 5]},
        raw_ref=None, harness=None, target_function=None,
    )
    assert g.location == Location(file="a.c", line=2)
    assert isinstance(fingerprint(g), str)
