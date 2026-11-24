from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.backends.cbmc.execution import STAGE, TOOL, ToolProblem, snapshot_cwd
from leanc.backends.cbmc.preprocess import CBMC_BANNER_PREFIX, preprocess_argv, without_banner
from leanc.backends.counterexample.properties import REACH_PREFIX
from leanc.foundation.models import Diagnostic, Location, ToolInvocation
from leanc.foundation.runner import Limits, ProcResult
from leanc.foundation.status import Outcome
from leanc.lang.c_lexer import CSource
from leanc.rules.loop_sites import Origin, origin_lines
from leanc.toolchain.context import BackendContext

__all__ = [
    "DIVERGENCE_RULE",
    "DIVERGENCE_RULE_ID",
    "REACH_VACUITY_EXPANSION",
    "REACH_VERIFY_EXPANSION",
    "Divergence",
    "bounded_divergence_problem",
    "normalised_vacuity_text",
    "vacuity_divergence",
]

REACH_VERIFY_EXPANSION = "((void)0)"
REACH_VACUITY_EXPANSION = f'__CPROVER_assert(0, "{REACH_PREFIX}" label)'
DIVERGENCE_RULE = (
    "preprocesses to different programs with and without LEANC_VACUITY_CHECK outside its LEANC_REACH probes, so "
    "the vacuity run would show the reachability of another program than the one verified"
)
DIVERGENCE_RULE_ID = "vacuity-divergence"
MAX_SHOWN_LINE_CHARS = 160
END_OF_OUTPUT = "<end of the preprocessed text>"
WITH_DEFINE = " with LEANC_VACUITY_CHECK"
_BLANK = r"[ \t]*"
_LITERAL = r'"(?:[^"\\\n]|\\.)*"'
_SITE_RE = re.compile(
    rf'__CPROVER_assert{_BLANK}\({_BLANK}0{_BLANK},{_BLANK}"{re.escape(REACH_PREFIX)}"(?:{_BLANK}{_LITERAL})+{_BLANK}\)'
)
_CODE_SHAPE_RE = re.compile(rf'__CPROVER_assert{_BLANK}\({_BLANK}0{_BLANK},{_BLANK}" *"(?:{_BLANK}" *")+{_BLANK}\)')


@dataclass(frozen=True)
class Divergence:
    origin: Origin | None
    line: int
    verify_line: str
    vacuity_line: str

    @property
    def where(self) -> str:
        return f"{self.origin.file}:{self.origin.line}" if self.origin else f"preprocessed line {self.line}"

    def describe(self) -> str:
        return f"{self.where}: without the define {self.verify_line!r}, with it {self.vacuity_line!r}"


def normalised_vacuity_text(text: str) -> str:
    code = CSource(text).code
    if len(code) != len(text):
        return text
    pieces: list[str] = []
    last = position = 0
    while (match := _SITE_RE.search(text, position)) is not None:
        if _CODE_SHAPE_RE.fullmatch(code, match.start(), match.end()) is None:
            position = match.start() + 1
            continue
        pieces += [text[last : match.start()], REACH_VERIFY_EXPANSION]
        last = position = match.end()
    pieces.append(text[last:])
    return "".join(pieces)


def _shown(lines: list[str], index: int) -> str:
    return " ".join(lines[index].split())[:MAX_SHOWN_LINE_CHARS] if index < len(lines) else END_OF_OUTPUT


def vacuity_divergence(verify_text: str, vacuity_text: str) -> Divergence | None:
    normalised = normalised_vacuity_text(vacuity_text)
    if normalised == verify_text:
        return None
    verify_lines, normalised_lines = verify_text.split("\n"), normalised.split("\n")
    pairs = zip(verify_lines, normalised_lines)
    index = next((i for i, (left, right) in enumerate(pairs) if left != right), min(len(verify_lines), len(normalised_lines)))
    origins = origin_lines(verify_text)
    origin = origins[index] if index < len(origins) else None
    return Divergence(origin, index + 1, _shown(verify_lines, index), _shown(vacuity_text.split("\n"), index))


def _what(source: str, vacuity: bool) -> str:
    return f"cbmc --preprocess of {source}{WITH_DEFINE if vacuity else ''}"


def _run_problem(what: str, proc: ProcResult, limits: Limits) -> ToolProblem | None:
    if proc.error:
        return ToolProblem(Outcome.ERROR, f"{what} could not be started: {proc.error}")
    if proc.timed_out:
        return ToolProblem(Outcome.TIMEOUT, f"{what} timed out after {limits.timeout_s:g}s; no verdict")
    if proc.output_truncated:
        return ToolProblem(Outcome.ERROR, f"{what} output exceeded {limits.max_output_bytes} bytes and was truncated")
    if proc.exit_code != 0:
        excerpt = "; ".join(line.strip() for line in proc.stderr.strip().splitlines()[-3:] if line.strip())
        return ToolProblem(Outcome.ERROR, f"{what} failed with exit code {proc.exit_code}" + (f": {excerpt}" if excerpt else ""))
    return None


def _preprocessed(
    ctx: BackendContext, limits: Limits, source: str, vacuity: bool, invocations: list[ToolInvocation]
) -> str | ToolProblem:
    what = _what(source, vacuity)
    proc = ctx.run(preprocess_argv(ctx, source, vacuity=vacuity), limits=limits, cwd=snapshot_cwd(ctx))
    log_name = ctx.log_name(STAGE, f"preprocess{WITH_DEFINE if vacuity else ''} {source}")
    invocations.append(proc.to_invocation(TOOL, ctx.run_dir, log_name))
    problem = _run_problem(what, proc, limits)
    if problem is not None:
        return problem
    text = without_banner(proc.stdout)
    if text is None:
        return ToolProblem(Outcome.ERROR, f"{what} did not start its output with a '{CBMC_BANNER_PREFIX}' line")
    return text


def _divergence_problem(role: str, source: str, divergence: Divergence) -> ToolProblem:
    location = Location(file=divergence.origin.file, line=divergence.origin.line) if divergence.origin else Location(file=source)
    summary = f"the {role} {source} {DIVERGENCE_RULE}: {divergence.describe()}"
    return ToolProblem(
        Outcome.ERROR,
        summary,
        [Diagnostic(tool=TOOL, rule_id=DIVERGENCE_RULE_ID, severity="error", message=summary, location=location)],
    )


def bounded_divergence_problem(ctx: BackendContext, limits: Limits, invocations: list[ToolInvocation]) -> ToolProblem | None:
    units = [*(("editable source", rel) for rel in ctx.spec.sources.editable), ("harness", ctx.spec.verification.harness)]
    for role, source in units:
        texts: list[str] = []
        for vacuity in (False, True):
            text = _preprocessed(ctx, limits, source, vacuity, invocations)
            if isinstance(text, ToolProblem):
                return text
            texts.append(text)
        divergence = vacuity_divergence(*texts)
        if divergence is not None:
            return _divergence_problem(role, source, divergence)
    return None
