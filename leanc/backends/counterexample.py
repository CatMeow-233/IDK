"""Turn raw CBMC results into normalised, explainable failure reports.

CBMC's ``--json-ui`` output gives, for every violated property, a full
symbolic-execution trace: hundreds of steps including CPROVER-internal
initialisation, hidden declarations, temporaries (``return_value_*``,
``tmp_*``, ``goto_symex$$...``) and the internals of CBMC's C library models.
That is precise but unreadable, and it is also a poor input for a repair
agent.  This module distils it into what a human (or the repair agent) needs:

* the **inputs** that trigger the failure - the final values of the user
  variables the approved harness sets up before calling the function under
  verification, plus the actual parameters of that call;
* the **call stack** at the failure;
* a **short trace** with the noise removed;
* a deterministic English **explanation** (no LLM is involved: correctness
  is never judged by a model);
* a **fingerprint** that identifies "the same failure" across repair rounds.
  CBMC numbers properties per function and class (``f.pointer_dereference.11``)
  and those numbers shift whenever the code changes, so the fingerprint uses
  the property *class*, the location and a stable qualifier instead.

All functions are pure (no I/O) and tolerant of missing or oddly-shaped
fields: CBMC's JSON is an external, versioned format, and a parsing hiccup
must degrade the report, never crash verification.
"""

from __future__ import annotations

import copy
import os
import posixpath
import re
from collections.abc import Iterable
from typing import Any

from leanc.foundation.models import CounterexampleInput, FailureReport, Location, TraceStep

#: Description prefix of a vacuity probe (see ``leanc.h``: LEANC_REACH).
REACH_PREFIX = "leanc_reach:"

MAX_VALUE_CHARS = 200
MAX_ELEMENTS = 16
_MAX_DEPTH = 4
_MAX_QUALIFIER = 120

# --------------------------------------------------------------------------- small helpers


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


def _int(x: Any) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, str) and x.strip().lstrip("-").isdigit():
        return int(x.strip())
    return None


def _rel_file(file: str | None, working_dir: str | None = None) -> str | None:
    """CBMC prints file names as given on its command line (Lean C passes
    snapshot-relative paths); absolute names inside the working directory are
    made relative so locations are workspace-relative and stable across runs."""
    if not file:
        return None
    f = str(file)
    if os.path.isabs(f):
        f = posixpath.normpath(f)
        if working_dir:
            wd = posixpath.normpath(str(working_dir)).rstrip("/")
            if f.startswith(wd + "/"):
                return f[len(wd) + 1 :]
        return f
    if f.startswith("<"):  # <builtin-library-malloc>, <built-in-additions>, ...
        return f
    f = posixpath.normpath(f)
    return f.removeprefix("./")


def location_from_cbmc(sl: Any) -> Location | None:
    """Convert a CBMC ``sourceLocation`` object into a :class:`Location`."""
    sl = _d(sl)
    if not sl:
        return None
    file = _rel_file(sl.get("file"), sl.get("workingDirectory"))
    line = _int(sl.get("line"))
    col = _int(sl.get("column"))
    func = sl.get("function") if isinstance(sl.get("function"), str) else None
    if file is None and line is None and func is None:
        return None
    return Location(file=file, line=line, column=col, function=func or None)


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na = posixpath.normpath(str(a))
    nb = posixpath.normpath(str(b))
    na = na.removeprefix("./")
    nb = nb.removeprefix("./")
    return na == nb or na.endswith("/" + nb) or nb.endswith("/" + na)


def _fn_id(step: dict) -> str | None:
    fn = _d(step.get("function"))
    ident = fn.get("identifier") or fn.get("displayName")
    return str(ident) if ident else None


_DECORATION_RE = re.compile(r"(?:![0-9]+@[0-9]+|#[0-9]+|\$object)")


def clean_name(name: str | None) -> str:
    """``storage!0@1`` -> ``storage``; ``f::1::x`` -> ``x``; ``a[0l]`` -> ``a[0]``."""
    if not name:
        return ""
    s = _DECORATION_RE.sub("", str(name))
    if "::" in s and "[" not in s and "." not in s:
        s = s.split("::")[-1]
    s = re.sub(r"\[(-?\d+)[uUlL]*\]", r"[\1]", s)
    return s


_INTERNAL_PREFIXES = (
    "return_value_",
    "__CPROVER",
    "tmp_",
    "__VERIFIER",
    "goto_symex",
    "symex",
    "__builtin",
)


def is_internal_name(name: str | None) -> bool:
    """Names CBMC introduces itself (temporaries, return-value slots,
    CPROVER bookkeeping) rather than variables written by a human."""
    if not name:
        return True
    n = str(name)
    if "$" in n or "#" in n:
        return True
    base = re.split(r"[\[.]", n, maxsplit=1)[0]
    base = base.split("::")[-1]
    return not base or base.startswith((*_INTERNAL_PREFIXES, "__"))


def _internal_function(fid: str | None) -> bool:
    return not fid or fid.startswith(("__CPROVER", "__VERIFIER"))


def clean_type(t: Any) -> str | None:
    if not isinstance(t, str) or not t:
        return None
    s = t.replace("__CPROVER_size_t", "size_t").replace("__CPROVER_ssize_t", "ssize_t")
    s = s.replace("__CPROVER_bool", "bool")
    return s


# --------------------------------------------------------------------------- values

_INT_DATA_RE = re.compile(r"^(-?\d+)[uUlL]*$")


def _render_pointer(data: Any) -> str:
    if not isinstance(data, str) or not data:
        return "?"
    if "NULL" in data:
        return "NULL"
    s = _DECORATION_RE.sub("", data).strip()
    if re.fullmatch(r"[A-Za-z_]\w*", s):
        return "&" + s
    return s


def _render(v: Any, depth: int, max_elements: int) -> str:
    v = _d(v)
    if not v:
        return "?"
    if depth > _MAX_DEPTH:
        return "{...}"
    if isinstance(v.get("elements"), list):
        elems = [e for e in v["elements"] if isinstance(e, dict)]
        idx = [_int(e.get("index")) for e in elems]
        contiguous = all(i == k for k, i in enumerate(idx))
        parts = []
        for e, i in zip(elems[:max_elements], idx[:max_elements]):
            s = _render(e.get("value"), depth + 1, max_elements)
            parts.append(s if contiguous else f"[{i if i is not None else '?'}]={s}")
        if len(elems) > max_elements:
            parts.append(f"... (+{len(elems) - max_elements} more)")
        return "{" + ", ".join(parts) + "}"
    if isinstance(v.get("members"), list):
        # "$pad3"-style members are padding CBMC inserts, not program data.
        mems = [m for m in v["members"] if isinstance(m, dict) and "$" not in str(m.get("name", ""))]
        parts = [
            f".{m.get('name', '?')}={_render(m.get('value'), depth + 1, max_elements)}" for m in mems[:max_elements]
        ]
        if len(mems) > max_elements:
            parts.append(f"... (+{len(mems) - max_elements} more)")
        return "{" + ", ".join(parts) + "}"
    name = v.get("name")
    data = v.get("data")
    if name == "pointer":
        return _render_pointer(data)
    if data is None:
        return "?"
    s = str(data)
    m = _INT_DATA_RE.match(s)
    if m and name in (None, "integer"):
        return m.group(1)
    return s


def render_value(value: Any, *, max_elements: int = MAX_ELEMENTS, max_chars: int = MAX_VALUE_CHARS) -> str:
    """Compact human-readable rendering of a CBMC trace value: integers
    without C suffixes (``8ul`` -> ``8``), pointers as ``NULL`` / ``&obj``,
    arrays / structs as ``{...}`` with at most ``max_elements`` entries, and
    the whole string capped at ``max_chars``."""
    s = _render(value, 0, max_elements)
    if len(s) > max_chars:
        s = s[: max(0, max_chars - 3)] + "..."
    return s


def _value_type(value: Any) -> str | None:
    v = _d(value)
    t = clean_type(v.get("type"))
    if t:
        return t
    elems = _l(v.get("elements"))
    if elems:
        inner = clean_type(_d(_d(elems[0]).get("value")).get("type"))
        if inner:
            return f"{inner}[]"
    return None


_LHS_RE = re.compile(r"^(?P<base>[A-Za-z_]\w*)(?P<rest>(?:\[-?\d+[uUlL]*\]|\.[A-Za-z_]\w*)*)$")
_PATH_RE = re.compile(r"\[(-?\d+)[uUlL]*\]|\.([A-Za-z_]\w*)")


def _parse_lhs(lhs: str) -> tuple[str, list[tuple[str, Any]]] | None:
    m = _LHS_RE.match(clean_name(lhs))
    if not m:
        return None
    path: list[tuple[str, Any]] = []
    for idx, mem in _PATH_RE.findall(m.group("rest")):
        path.append(("idx", int(idx)) if idx != "" else ("mem", mem))
    return m.group("base"), path


def _set_path(root: dict, path: list[tuple[str, Any]], value: Any) -> bool:
    """Update ``root`` (a CBMC value tree) at ``path`` in place."""
    cur = root
    for n, (kind, key) in enumerate(path):
        last = n == len(path) - 1
        if kind == "idx":
            elems = cur.get("elements")
            if not isinstance(elems, list):
                return False
            slot = next((e for e in elems if isinstance(e, dict) and _int(e.get("index")) == key), None)
            if slot is None:
                slot = {"index": key, "value": {}}
                elems.append(slot)
                elems.sort(key=lambda e: _int(_d(e).get("index")) or 0)
        else:
            mems = cur.get("members")
            if not isinstance(mems, list):
                return False
            slot = next((m for m in mems if isinstance(m, dict) and m.get("name") == key), None)
            if slot is None:
                slot = {"name": key, "value": {}}
                mems.append(slot)
        if last:
            slot["value"] = copy.deepcopy(value)
            return True
        if not isinstance(slot.get("value"), dict):
            slot["value"] = {}
        cur = slot["value"]
    return False


# --------------------------------------------------------------------------- trace analysis


def _steps(trace: Any) -> list[dict]:
    return [s for s in _l(trace) if isinstance(s, dict)]


def _failure_index(steps: list[dict], property_id: str | None = None) -> int:
    """Index of the failure step for ``property_id`` (the last failure step
    when not given / not found).  CBMC traces may contain failure steps of
    *other* properties violated along the same path."""
    last = None
    for i, s in enumerate(steps):
        if s.get("stepType") == "failure":
            if property_id is not None and s.get("property") == property_id:
                return i
            last = i
    return last if last is not None else len(steps)


def _in_harness_main(step: dict, harness_file: str | None) -> bool:
    sl = _d(step.get("sourceLocation"))
    if sl.get("function") != "main":
        return False
    if not harness_file:
        return True
    return _same_file(_rel_file(sl.get("file"), sl.get("workingDirectory")), harness_file)


def extract_inputs(
    trace: Any,
    harness_file: str | None,
    target_function: str | None,
    *,
    property_id: str | None = None,
) -> list[CounterexampleInput]:
    """Inputs of a counterexample.

    (a) the final values of the user variables assigned in the harness's
    ``main`` before the (last) call to ``target_function`` - element / member
    assignments such as ``storage[3] = 7`` are folded into their variable;
    (b) the actual parameters of that call, named ``param <name>``.
    CBMC-internal names are skipped (``return_value_*``, ``__CPROVER*``,
    ``tmp_*``, names containing ``$`` or ``#``), as are hidden steps.
    """
    steps = _steps(trace)
    fail_i = _failure_index(steps, property_id)
    call_i: int | None = None
    if target_function:
        for i, s in enumerate(steps[: fail_i + 1]):
            if s.get("stepType") == "function-call" and _fn_id(s) == target_function:
                call_i = i
    limit = call_i if call_i is not None else min(fail_i, len(steps))

    values: dict[str, dict] = {}
    for s in steps[:limit]:
        if s.get("hidden") or s.get("stepType") != "assignment":
            continue
        if s.get("assignmentType") == "actual-parameter":
            continue
        if not _in_harness_main(s, harness_file):
            continue
        lhs = s.get("lhs")
        if not isinstance(lhs, str) or is_internal_name(lhs):
            continue
        parsed = _parse_lhs(lhs)
        if parsed is None:
            continue
        base, path = parsed
        val = _d(s.get("value"))
        if not path:
            values[base] = copy.deepcopy(val)
        elif base in values and _set_path(values[base], path, val):
            pass
        else:
            values[clean_name(lhs)] = copy.deepcopy(val)

    out = [CounterexampleInput(name=n, value=render_value(v), type=_value_type(v)) for n, v in values.items()]

    if call_i is not None:
        for s in steps[call_i + 1 :]:
            if s.get("stepType") == "assignment" and s.get("assignmentType") == "actual-parameter":
                name = clean_name(s.get("lhs")) or "?"
                val = _d(s.get("value"))
                out.append(CounterexampleInput(name=f"param {name}", value=render_value(val), type=_value_type(val)))
                continue
            if s.get("hidden"):
                continue
            break
    return out


def summarize_trace(trace: Any, max_steps: int = 80) -> list[TraceStep]:
    """A readable excerpt of a CBMC trace.

    Hidden steps, CBMC-internal variables, CPROVER library internals and
    pure location / loop-head markers are dropped; the actual parameters of a
    call are folded into the call step (``call f(len=4, idx=4)``).  When more
    than ``max_steps`` remain, the beginning and - more importantly - the end
    (leading to the failure) are kept, with a marker for the omitted middle.
    """
    steps = _steps(trace)
    out: list[TraceStep] = []
    pending: tuple[TraceStep, list[str], str] | None = None  # (call step, args, function)
    last_loc: Location | None = None
    for s in steps:
        st = s.get("stepType")
        loc = location_from_cbmc(s.get("sourceLocation"))
        prev_loc = last_loc
        if loc is not None:
            last_loc = loc
        if s.get("hidden"):
            continue
        if st == "function-call":
            fid = _fn_id(s) or "?"
            if _internal_function(fid):
                pending = None
                continue
            if loc is None:
                # Some call steps carry no location: use the call site (the
                # caller's last position), else the callee's definition.
                loc = prev_loc or location_from_cbmc(_d(s.get("function")).get("sourceLocation"))
            call = TraceStep(kind="function-call", location=loc, text=f"call {fid}()")
            out.append(call)
            pending = (call, [], fid)
            continue
        if st == "assignment":
            lhs = s.get("lhs") if isinstance(s.get("lhs"), str) else ""
            if s.get("assignmentType") == "actual-parameter":
                if pending is not None:
                    call, args, fid = pending
                    args.append(f"{clean_name(lhs)}={render_value(s.get('value'), max_chars=60)}")
                    call.text = f"call {fid}({', '.join(args)})"
                continue
            pending = None
            if is_internal_name(lhs):
                continue
            if loc is not None and loc.file and loc.file.startswith("<"):
                continue  # inside a CPROVER library model (malloc, memcpy, ...)
            out.append(TraceStep(kind="assignment", location=loc, text=f"{clean_name(lhs)} = {render_value(s.get('value'))}"))
            continue
        pending = None
        if st == "function-return":
            fid = _fn_id(s) or "?"
            if _internal_function(fid):
                continue
            out.append(TraceStep(kind="function-return", location=loc, text=f"return from {fid}()"))
        elif st == "failure":
            reason = s.get("reason") or "property violated"
            prop = s.get("property")
            out.append(TraceStep(kind="failure", location=loc, text=f"FAILURE {prop}: {reason}" if prop else f"FAILURE: {reason}"))
        elif st == "assumption":
            out.append(TraceStep(kind="assume", location=loc, text="assumption"))
        elif st == "output":
            vals = ", ".join(render_value(v, max_chars=60) for v in _l(s.get("values")))
            label = s.get("outputID") or "output"
            out.append(TraceStep(kind="output", location=loc, text=f"{label}: {vals}".strip()))
        # location-only, loop-head, goto, declaration ...: noise for humans
    if max_steps <= 0:
        return []
    if len(out) <= max_steps:
        return out
    head_n = min(10, max_steps // 4)
    tail_n = max_steps - head_n - 1
    if tail_n < 1:
        return out[-max_steps:]
    omitted = len(out) - head_n - tail_n
    marker = TraceStep(kind="location", text=f"... {omitted} step(s) omitted ...")
    return [*out[:head_n], marker, *out[-tail_n:]]


def call_stack_at_failure(trace: Any, property_id: str | None = None) -> list[str]:
    """Call stack (outermost first) at the failure step, each frame as
    ``function (file:line)`` with the frame's current position."""
    steps = _steps(trace)
    stack: list[list[Any]] = []  # [function, Location | None]
    snapshot: list[list[Any]] | None = None
    for s in steps:
        st = s.get("stepType")
        loc = location_from_cbmc(s.get("sourceLocation"))
        if st == "function-call":
            if stack and loc is not None:
                stack[-1][1] = loc  # the call site, in the caller
            stack.append([_fn_id(s) or "?", None])
            continue
        if st == "function-return":
            fid = _fn_id(s)
            if stack and (fid is None or stack[-1][0] == fid):
                stack.pop()
            elif stack:
                # unbalanced trace: pop up to the matching frame if present
                for k in range(len(stack) - 1, -1, -1):
                    if stack[k][0] == fid:
                        del stack[k:]
                        break
            continue
        if loc is not None and stack and loc.function == stack[-1][0]:
            stack[-1][1] = loc
        if st == "failure":
            frozen = [list(f) for f in stack]
            if property_id is not None and s.get("property") == property_id:
                snapshot = frozen
                break
            snapshot = frozen
    frames = snapshot if snapshot is not None else stack
    out = []
    for fid, loc in frames:
        if _internal_function(fid):
            continue
        if loc is not None and (loc.file or loc.line is not None):
            where = ":".join(str(x) for x in (loc.file or "?", loc.line) if x is not None)
            out.append(f"{fid} ({where})")
        else:
            out.append(str(fid))
    return out


# --------------------------------------------------------------------------- classification

_PROP_RE = re.compile(r"^(?P<func>.+?)\.(?P<cls>[A-Za-z_][A-Za-z0-9_-]*)\.(?P<num>\d+)$")


def is_unwinding_property(result: Any) -> bool:
    """Unwinding (and recursion-unwinding) assertions are CBMC's way of saying
    "the bound was not enough", not a defect in the program."""
    r = _d(result)
    prop = str(r.get("property") or "")
    desc = str(r.get("description") or "").lower()
    return (
        ".unwind." in prop
        or prop.endswith(".recursion")
        or desc.startswith(("unwinding assertion", "recursion unwinding assertion"))
    )


def is_reach_probe(result: Any) -> bool:
    return str(_d(result).get("description") or "").startswith(REACH_PREFIX)


def property_class(property_id: str | None, description: str | None = None) -> str:
    """The CBMC property class: ``pointer_dereference``, ``array_bounds``,
    ``division-by-zero``, ``overflow``, ``assertion``, ``unwind``, ...
    Derived from the property id (``<function>.<class>.<n>``), falling back to
    the description."""
    pid = property_id or ""
    desc = (description or "").lower()
    if ".unwind." in pid or desc.startswith("unwinding assertion"):
        return "unwind"
    if pid.endswith(".recursion") or desc.startswith("recursion unwinding assertion"):
        return "recursion"
    m = _PROP_RE.match(pid)
    if m:
        return m.group("cls")
    if "dereference failure" in desc:
        return "pointer_dereference"
    if "division by zero" in desc:
        return "division-by-zero"
    if "overflow" in desc:
        return "overflow"
    if "array" in desc and "bound" in desc:
        return "array_bounds"
    if desc.startswith(("assertion", REACH_PREFIX)):
        return "assertion"
    if "shift" in desc:
        return "undefined-shift"
    return "property"


def _qualifier(cls: str, description: str | None) -> str:
    """The stable part of a CBMC description: the expression after `` in ``
    (which changes with any edit of the code) is dropped, the kind of
    violation (``pointer NULL``, ``signed +``, ``upper bound``...) is kept.
    Assertion texts come from the protected harness and are kept whole."""
    d = " ".join(str(description or "").split())
    if cls != "assertion" and " in " in d:
        d = d.split(" in ", 1)[0]
    return d[:_MAX_QUALIFIER]


# --------------------------------------------------------------------------- reports


def failure_from_cbmc_property(
    result: dict,
    *,
    raw_ref: str | None,
    harness: str | None,
    target_function: str | None,
) -> FailureReport:
    """FailureReport(kind="counterexample") for one FAILED CBMC property."""
    r = _d(result)
    prop = str(r.get("property") or "unknown")
    desc = str(r.get("description") or prop)
    trace = r.get("trace")
    steps = _steps(trace)
    loc = location_from_cbmc(r.get("sourceLocation"))
    if loc is None:
        fi = _failure_index(steps, prop)
        if fi < len(steps):
            loc = location_from_cbmc(steps[fi].get("sourceLocation"))
    f = FailureReport(
        id=f"cbmc:{prop}",
        kind="counterexample",
        tool="cbmc",
        property_id=prop,
        description=desc,
        location=loc,
        inputs=extract_inputs(steps, harness, target_function, property_id=prop),
        call_stack=call_stack_at_failure(steps, prop),
        trace=summarize_trace(steps),
        raw_ref=raw_ref,
    )
    f.explanation = explain_counterexample(f)
    return f


def unwind_failure(
    result: dict,
    *,
    raw_ref: str | None = None,
    unwind: int | None = None,
    harness: str | None = None,
    target_function: str | None = None,
) -> FailureReport:
    """FailureReport(kind="unwind") for a failed unwinding assertion: the
    bound was too small to explore every execution of that loop."""
    r = _d(result)
    prop = str(r.get("property") or "unknown.unwind")
    desc = str(r.get("description") or "unwinding assertion")
    steps = _steps(r.get("trace"))
    f = FailureReport(
        id=f"cbmc:{prop}",
        kind="unwind",
        tool="cbmc",
        property_id=prop,
        description=desc,
        location=location_from_cbmc(r.get("sourceLocation")),
        inputs=extract_inputs(steps, harness, target_function, property_id=prop) if steps else [],
        call_stack=call_stack_at_failure(steps, prop) if steps else [],
        trace=summarize_trace(steps, max_steps=30) if steps else [],
        raw_ref=raw_ref,
    )
    f.explanation = explain_counterexample(f, unwind=unwind)
    return f


def vacuity_failure(
    label: str,
    *,
    result: dict | None = None,
    raw_ref: str | None = None,
    reported: bool = True,
    unwind_insufficient: bool = False,
) -> FailureReport:
    """FailureReport(kind="vacuity") for a LEANC_REACH probe that CBMC proved
    unreachable (or did not report at all, ``reported=False``).

    With ``unwind_insufficient`` (unwinding assertions failed in the same
    verification) the explanation notes that the probe may be unreachable
    only because executions beyond the unwind bound were cut off."""
    r = _d(result)
    prop = r.get("property") if isinstance(r.get("property"), str) else None
    if reported:
        desc = f'vacuity probe LEANC_REACH("{label}") is unreachable under the harness assumptions'
    else:
        desc = (
            f'vacuity probe LEANC_REACH("{label}") was not reported by CBMC '
            "(the code containing it is not reachable from the harness entry point)"
        )
    f = FailureReport(
        id=f"cbmc:vacuity:{label}",
        kind="vacuity",
        tool="cbmc",
        property_id=prop,
        description=desc,
        location=location_from_cbmc(r.get("sourceLocation")),
        raw_ref=raw_ref,
    )
    f.explanation = explain_counterexample(f)
    if unwind_insufficient:
        f.explanation += (
            " Unwinding assertions also failed in this verification: the probe may be unreachable only "
            "because executions needing more loop iterations than the unwind bound were not explored."
        )
    return f


_DEREF_HINTS = (
    ("pointer null", "A NULL pointer is dereferenced."),
    ("outside object bounds", "Memory outside the bounds of an object is accessed (for example an off-by-one index)."),
    ("deallocated dynamic object", "Memory is accessed after it was freed (use after free)."),
    ("dead object", "An object is accessed after its lifetime ended (e.g. a pointer to a returned local variable)."),
    ("invalid integer address", "A pointer made from an arbitrary integer is dereferenced."),
    ("pointer invalid", "An invalid pointer is dereferenced."),
)

_CLASS_HINTS = {
    "array_bounds": "An array is indexed outside its declared bounds.",
    "division-by-zero": "A division or remainder by zero can occur.",
    "undefined-shift": "A shift is undefined in C (negative or too-large shift distance, or shifting a negative value).",
    "assertion": "An assertion of the approved harness (a functional postcondition or frame condition) does not hold.",
    "memory-leak": "Dynamically allocated memory is not freed.",
    "pointer_arithmetic": "Pointer arithmetic leaves the bounds of an object or overflows.",
    "pointer_primitives": "A pointer primitive is applied to an invalid pointer.",
    "precondition_instance": "A precondition of a C library function (as modelled by CBMC) is violated.",
    "NaN": "A floating-point operation produces NaN.",
    "enum-range-check": "An enum-typed expression holds a value outside the enumeration.",
}


def _hint(cls: str, description: str) -> str:
    d = description.lower()
    if cls == "pointer_dereference":
        for key, text in _DEREF_HINTS:
            if key in d:
                return text
        return "An invalid pointer dereference can occur."
    if cls == "overflow":
        if "signed" in d and "unsigned" not in d:
            return "A signed integer operation overflows, which is undefined behaviour in C."
        if "unsigned" in d:
            return "An unsigned integer operation wraps around (enabled by the unsigned-overflow check)."
        if "conversion" in d or "type conversion" in d:
            return "A value is converted to a type that cannot represent it."
        if "float" in d:
            return "A floating-point operation overflows to infinity."
        return "An arithmetic overflow can occur."
    return _CLASS_HINTS.get(cls, "")


def _fmt_inputs(inputs: Iterable[CounterexampleInput], limit: int = 12) -> str:
    items = list(inputs)
    parts = [f"{i.name} = {i.value}" for i in items[:limit]]
    if len(items) > limit:
        parts.append(f"... (+{len(items) - limit} more)")
    return ", ".join(parts)


def explain_counterexample(f: FailureReport, *, unwind: int | None = None) -> str:
    """Deterministic English explanation: which property failed, where, and
    with which inputs (no model is involved)."""
    where = f" at {f.location.short()}" if f.location else ""
    if f.kind == "counterexample":
        cls = property_class(f.property_id, f.description)
        parts = [
            (
                f"CBMC found an execution of the approved harness that violates property "
                f"{f.property_id or '?'} ({f.description}){where}."
            )
        ]
        hint = _hint(cls, f.description)
        if hint:
            parts.append(hint)
        if f.call_stack:
            parts.append(f"Call stack at the failure: {' -> '.join(f.call_stack)}.")
        if f.inputs:
            parts.append(f"Inputs leading to the failure: {_fmt_inputs(f.inputs)}.")
        else:
            parts.append("No harness inputs could be extracted from the trace.")
        parts.append(
            "The counterexample is a concrete execution under CBMC's C semantics within the approved "
            "harness constraints and bounds; see the raw trace for every step."
        )
        return " ".join(parts)
    if f.kind == "unwind":
        what = "recursion" if property_class(f.property_id, f.description) == "recursion" else "loop"
        bound = f"the unwind bound {unwind}" if unwind is not None else "the unwind bound"
        parts = [
            (
                f"CBMC's unwinding assertion {f.property_id or '?'} for the {what}{where} failed: some execution "
                f"allowed by the harness needs more iterations than {bound} permits."
            )
        ]
        if f.inputs:
            parts.append(f"For example: {_fmt_inputs(f.inputs)}.")
        parts.append(
            "Executions beyond the bound were not explored, so the result is not sound for the whole input "
            "space. Raising the bound (verification.unwind / unwindset) changes the specification and "
            "requires a re-approved spec; it is not a defect of the code under test unless the loop "
            "should terminate sooner."
        )
        return " ".join(parts)
    if f.kind == "vacuity":
        return (
            f"{f.description}{where}. The harness assumptions (LEANC_ASSUME) or the code make this point "
            "unreachable, so every check after it holds vacuously and proves nothing. The harness or its "
            "assumptions must be reviewed (a change requires re-approval)."
        )
    rule = f" ({f.property_id})" if f.property_id else ""
    return f"{f.tool} reported a {f.kind} failure{rule}{where}: {f.description}"


# --------------------------------------------------------------------------- fingerprint

_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_PID_RE = re.compile(r"==\d+==")
_LONGNUM_RE = re.compile(r"\b\d{5,}\b")
_ABSDIR_RE = re.compile(r"(?:/[^\s:/'\"()<>]+)+/")
_SUFFIX_RE = re.compile(r"(?:[.#]\d+)+$")


def _normalize_message(msg: Any) -> str:
    """First line of a message with run-specific noise masked (addresses,
    PIDs, long numbers, absolute directory prefixes), so the same finding in
    two different runs / run directories yields the same text."""
    s = str(msg or "").strip().splitlines()[0] if str(msg or "").strip() else ""
    s = _PID_RE.sub("", s)
    s = _HEX_RE.sub("0x?", s)
    s = _ABSDIR_RE.sub("", s)
    s = _LONGNUM_RE.sub("N", s)
    return " ".join(s.split())[:_MAX_QUALIFIER]


def fingerprint(f: FailureReport) -> str:
    """Stable identity of a failure across verification runs / repair rounds.

    ``<tool>:<kind>:<class>@<file>:<line>:<function> [<qualifier>]``

    For CBMC the class is the property class and the qualifier the stable
    part of the description - never the numeric property suffix, which
    shifts after edits.  For every other kind (compile / static / runtime /
    test-oracle / ...) the class is the rule / property id without trailing
    counters and the qualifier the normalised message (prefixed with the
    report id when there is no location).  Never raises.
    """
    tool = str(getattr(f, "tool", None) or "?").strip() or "?"
    kind = str(getattr(f, "kind", None) or "?")
    loc = getattr(f, "location", None)
    file = line = func = "?"
    if loc is not None:
        lf = getattr(loc, "file", None)
        if lf:
            file = _rel_file(str(lf)) or "?"
            if os.path.isabs(file):
                file = _ABSDIR_RE.sub("", file) or file
        if getattr(loc, "line", None) is not None:
            line = str(loc.line)
        if getattr(loc, "function", None):
            func = str(loc.function)
    pid = getattr(f, "property_id", None)
    pid = str(pid) if pid else None
    desc = str(getattr(f, "description", None) or "")
    if tool == "cbmc" and kind in ("counterexample", "unwind"):
        cls = property_class(pid, desc)
        qual = "" if kind == "unwind" else _qualifier(cls, desc)
    elif kind == "vacuity":
        cls = "vacuity"
        fid = str(getattr(f, "id", "") or "")
        qual = fid.split("cbmc:vacuity:", 1)[1] if "cbmc:vacuity:" in fid else _normalize_message(desc)
    else:
        cls = _SUFFIX_RE.sub("", pid) if pid else "-"
        qual = _normalize_message(desc)
        if file == "?":
            # Without a location (e.g. a test-oracle verdict) the report id -
            # built from the test case and rule, minus de-duplication
            # counters - is what tells two such failures apart.
            fid = _SUFFIX_RE.sub("", str(getattr(f, "id", "") or ""))
            if fid:
                qual = f"{fid}: {qual}" if qual else fid
    fp = f"{tool}:{kind}:{cls or '-'}@{file}:{line}:{func}"
    if qual:
        fp += f" [{qual}]"
    return fp
