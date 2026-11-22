from __future__ import annotations

from pathlib import Path

from leanc.backends.clang import run_compile
from leanc.backends.integrity import run_integrity
from leanc.foundation.models import StageResult
from leanc.foundation.status import Outcome
from leanc.specs.loader import load_spec
from leanc.toolchain.context import VerifyOptions, make_context

EXAMPLE = "safe_array_get"
WP_EXAMPLE = "wp_safe_array_get"
SOURCE = "src/safe_array.c"
DEFAULT_FLAGS = "flags: [-Wall, -Wextra, -Wpedantic]"
DEFAULT_GATES = "gates: [compile, static_analysis, sanitizer, cbmc]"
READ_LINE = "    *out = arr[idx];"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def shipped(example: str = EXAMPLE) -> str:
    return (EXAMPLES / example / SOURCE).read_text()


def integrity_gate(
    example_copy,
    tmp_path: Path,
    source: str | None = None,
    spec_edits: dict[str, str] | None = None,
    example: str = EXAMPLE,
    extra_files: dict[str, str] | None = None,
    options: VerifyOptions | None = None,
) -> StageResult:
    root = example_copy(example)
    if source is not None:
        (root / SOURCE).write_text(source)
    for rel, text in (extra_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    spec_file = root / "spec.yaml"
    text = spec_file.read_text()
    for old, new in (spec_edits or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    spec_file.write_text(text)
    return compiled_integrity(make_context(load_spec(spec_file), root, tmp_path / "run", options or VerifyOptions()))


def compiled_integrity(ctx) -> StageResult:
    compiled = run_compile(ctx)
    assert compiled.outcome is Outcome.PASS, compiled.summary
    return run_integrity(ctx)


def with_flags(*flags: str) -> dict[str, str]:
    return {DEFAULT_FLAGS: f"flags: [{', '.join(flags)}]"}


def by_rule(result: StageResult, rule: str) -> list:
    return [d for d in result.diagnostics if d.rule_id == rule]
