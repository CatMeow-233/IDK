from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from loop_contract_support import contracts_text, inductive_spec, loop_entry

from leanc.rules.loop_contracts import parse_loop_contracts

SOURCE = "src/box.c"
CONTRACT_MARKER = "LEANC_LOOP_CONTRACT"
LOOP_LINE = 8
TOOL_TIMEOUT_S = 120
UNWIND = "10"
CHECK_FLAGS = ("--bounds-check", "--pointer-check", "--signed-overflow-check", "--div-by-zero-check")
VERIFICATION_SUCCESSFUL = "VERIFICATION SUCCESSFUL"
VERIFICATION_FAILED = "VERIFICATION FAILED"
IGNORED_CLAUSE_WARNING = "ignoring"
LOOP_PROPERTY_CLASSES = ("loop_assigns", "loop_decreases", "loop_invariant_base", "loop_invariant_step")
PROGRAM = """#include <stddef.h>
#include <stdint.h>
struct box { int count; int cells[4]; unsigned char bytes[8]; struct box *self; };
size_t nondet_size_t(void);
void fill(struct box *b, int *out, size_t n)
{
    size_t i = 0;
    while (i < n) LEANC_LOOP_CONTRACT
    {
        b->bytes[i] = 0;
        b->count = (int)i;
        *out = (int)i;
        i = i + 1;
    }
}
int main(void)
{
    struct box b;
    int out = 0;
    b.self = &b;
    size_t n = nondet_size_t();
    __CPROVER_assume(n <= 8);
    fill(&b, &out, n);
    return 0;
}
"""
EVERY_FORM_INVARIANT = [
    "i <= n",
    "n <= 8",
    "(unsigned long)i <= 8ul",
    "__CPROVER_loop_entry(i) == 0",
    "!(i > n)",
    "~0 == -1",
    "(i & 0xFF) == i % 256",
    "(i << 1) >> 1 == i",
    "(i | 0) == (i ^ 0)",
    "b->self->self == b",
    "(*b).self == b",
    "b->cells[1] == (*b).cells[1]",
    "(size_t)(int8_t)1 == 1u && -(-1) == 1 || 0",
    "i * 2 / 2 - i == 0 && i != 9 && i >= 0",
]
EVERY_FORM_ASSIGNS = [
    "i",
    "(*b).count",
    "*out",
    "b->cells[0]",
    "__CPROVER_object_upto(b->bytes, 8)",
    "__CPROVER_object_whole(out)",
]


def _tool(name: str) -> str:
    found = shutil.which(name)
    assert found, f"{name} is installed with cbmc"
    return found


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S, check=False)


def _verify(tmp_path: Path, invariant: list[str]) -> str:
    spec = inductive_spec([SOURCE])
    text = contracts_text(
        loop_entry(file=SOURCE, line=LOOP_LINE, invariant=invariant, assigns=EVERY_FORM_ASSIGNS, decreases="n - i")
    )
    contracts = parse_loop_contracts(spec, text)
    assert contracts.accepted, contracts.problems
    source = tmp_path / "box.c"
    source.write_text(PROGRAM.replace(CONTRACT_MARKER, contracts.loops[0].annotation()), encoding="utf-8")
    compiled = _run([_tool("goto-cc"), "-o", "box.goto", source.name], tmp_path)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    instrumented = _run(
        [_tool("goto-instrument"), "--dfcc", "main", "--apply-loop-contracts", "box.goto", "dfcc.goto"], tmp_path
    )
    assert instrumented.returncode == 0, instrumented.stdout + instrumented.stderr
    verified = _run([_tool("cbmc"), "dfcc.goto", "--unwind", UNWIND, "--unwinding-assertions", *CHECK_FLAGS], tmp_path)
    tool_output = instrumented.stdout + instrumented.stderr + verified.stdout + verified.stderr
    assert IGNORED_CLAUSE_WARNING not in tool_output
    assert all(f"fill.{name}." in verified.stdout for name in LOOP_PROPERTY_CLASSES), verified.stdout
    return verified.stdout


def test_every_rendered_form_compiles_and_proves_under_dfcc(needs_cbmc, tmp_path):
    assert VERIFICATION_SUCCESSFUL in _verify(tmp_path, EVERY_FORM_INVARIANT)


def test_a_false_loop_entry_clause_is_checked_by_dfcc(needs_cbmc, tmp_path):
    invariant = [clause.replace("loop_entry(i) == 0", "loop_entry(i) == 1") for clause in EVERY_FORM_INVARIANT]
    output = _verify(tmp_path, invariant)
    assert VERIFICATION_FAILED in output
    assert "loop_invariant_base" in next(line for line in output.splitlines() if "FAILURE" in line)
