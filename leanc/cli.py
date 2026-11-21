from __future__ import annotations

import argparse
import logging
import sys
import traceback

from leanc import __version__
from leanc.foundation.errors import LeanCError
from leanc.foundation.status import EXIT_INTERNAL, EXIT_USAGE
from leanc.pipeline.commands.console import FILE_ONLY_LEVEL, LOGGER_NAME, setup_logging
from leanc.pipeline.commands.report import cmd_report
from leanc.pipeline.commands.session import record_aborted_run
from leanc.pipeline.commands.setup import cmd_doctor, cmd_init
from leanc.pipeline.commands.spec import (
    cmd_spec_approve,
    cmd_spec_draft,
    cmd_spec_show,
    cmd_spec_status,
)
from leanc.pipeline.commands.workflow import (
    cmd_generate,
    cmd_repair,
    cmd_run,
    cmd_verify,
)

log = logging.getLogger(LOGGER_NAME)

EXIT_INTERRUPTED = 130
MAX_REPAIR_ROUNDS = 20
MIN_TIMEOUT_SCALE = 0.01
MAX_TIMEOUT_SCALE = 100

EPILOG = """exit codes:
  0  VERIFIED_BOUNDED / PROVED     10 COMPILE_ERROR         11 STATIC_ANALYSIS_WARNING
  12 RUNTIME_TEST_FAILED           13 COUNTEREXAMPLE_FOUND  20 UNKNOWN
  21 TIMEOUT                       30 TOOL_ERROR            40 SPEC_UNAPPROVED
  50 REPAIR_EXHAUSTED              2  usage/input error     70 internal error

VERIFIED_BOUNDED only means: no violation of the checked properties exists within the
approved harness, input constraints and unwind bound (all loops fully unwound, harness
non-vacuous). It is never reported as PROVED.

`leanc generate` exits 0 when generation succeeded (the code is NOT verified yet);
`leanc report` exits with the run's status, or 20 if a successful run's files changed since.
Integrity-gate violations (symbol overrides, tool-divergent preprocessing, termination
calls, ...) are reported as STATIC_ANALYSIS_WARNING by the mandatory `integrity` gate.
"""


def _add_common(p: argparse.ArgumentParser, *, spec: bool = True, verify: bool = False, provider: bool = False) -> None:
    if spec:
        p.add_argument("spec", help="path to the spec (YAML or JSON)")
        p.add_argument("--project-root", help="project root (default: nearest dir with leanc.yaml, else the spec's dir)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose logging to stderr")
    if verify:
        g = p.add_argument_group("verification")
        g.add_argument("--gates", help="comma-separated subset of the spec's gates (result cannot be VERIFIED_BOUNDED)")
        g.add_argument("--fail-fast", action="store_true", help="stop after the first gate that does not pass")
        g.add_argument("--timeout-scale", type=float, default=1.0, help="multiply every tool timeout (default 1.0)")
        g.add_argument("--allow-unsandboxed", action="store_true",
                       help="DANGEROUS: execute untrusted test programs even when no sandbox is available")
        g.add_argument("--no-sandbox", action="store_true",
                       help="do not use bwrap (dynamic tests are then skipped unless --allow-unsandboxed)")
        g.add_argument("--check-unapproved", action="store_true",
                       help="run gates for information even if the spec is unapproved (status stays SPEC_UNAPPROVED)")
    if provider:
        g = p.add_argument_group("LLM provider")
        g.add_argument("--provider", help="mock | openai (default from leanc.yaml, else mock)")
        g.add_argument("--mock-dir", help="fixture directory for the mock provider (default <root>/mock)")
        g.add_argument("--llm-timeout", type=float, help="LLM request timeout in seconds")


def _add_spec_commands(sub) -> None:
    s = sub.add_parser("spec", help="draft / approve / inspect specifications")
    s.set_defaults(command_help=s.print_help)
    ssub = s.add_subparsers(dest="spec_command", metavar="<action>")
    sd = ssub.add_parser("draft", help="turn a natural-language requirement into an UNTRUSTED spec draft")
    sd.add_argument("--id", required=True, help="spec id for the draft")
    sd.add_argument("--requirement", help="requirement text")
    sd.add_argument("--requirement-file", help="file containing the requirement")
    sd.add_argument("--title")
    sd.add_argument("--project-root", help="project root (default: current directory)")
    sd.add_argument("--force", action="store_true")
    _add_common(sd, spec=False, provider=True)
    sd.set_defaults(func=cmd_spec_draft)
    sa = ssub.add_parser("approve", help="approve (hash-lock and sign) a reviewed spec, harness and tests")
    sa.add_argument("--by", required=True, help="name of the human approver")
    sa.add_argument("--note", help="approval note")
    sa.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    _add_common(sa)
    sa.set_defaults(func=cmd_spec_approve)
    st = ssub.add_parser("status", help="show approval status and changes since approval")
    _add_common(st)
    st.set_defaults(func=cmd_spec_status)
    sh = ssub.add_parser("show", help="print the normalised spec, its hash and effective checks")
    _add_common(sh)
    sh.set_defaults(func=cmd_spec_show)


def _add_workflow_commands(sub) -> None:
    max_rounds_help = f"override spec.repair.max_rounds (cannot exceed {MAX_REPAIR_ROUNDS})"
    no_write_help = "do not write the verified candidate back to the workspace"
    g = sub.add_parser("generate", help="generate the editable C sources for an approved spec")
    g.add_argument("--force", action="store_true", help="regenerate existing editable sources")
    _add_common(g, provider=True)
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("verify", help="compile, analyse, sanitize and model-check the current sources")
    _add_common(v, verify=True)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("repair", help="verify, then run the counterexample-driven repair loop")
    r.add_argument("--max-rounds", type=int, help=max_rounds_help)
    r.add_argument("--no-write", action="store_true", help=no_write_help)
    _add_common(r, verify=True, provider=True)
    r.set_defaults(func=cmd_repair)

    rn = sub.add_parser("run", help="full workflow: spec check -> generate -> verify -> repair -> re-verify -> report")
    rn.add_argument("--max-rounds", type=int, help=max_rounds_help)
    rn.add_argument("--regenerate", action="store_true", help="regenerate editable sources even if they exist")
    rn.add_argument("--no-repair", action="store_true", help="stop after the first verification")
    rn.add_argument("--no-write", action="store_true", help=no_write_help)
    _add_common(rn, verify=True, provider=True)
    rn.set_defaults(func=cmd_run)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leanc",
        description="Lean C: spec-first, verifier-checked AI code generation for C17 (research prototype)",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"leanc {__version__}")
    p.set_defaults(command_help=p.print_help)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    d = sub.add_parser("doctor", help="check Python, Clang, analyzer, sanitizers, CBMC, sandbox, LLM config")
    _add_common(d, spec=False)
    d.add_argument("--no-sandbox", action="store_true", help="skip the sandbox check")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="create a project from a template")
    i.add_argument("directory", nargs="?", default=".")
    i.add_argument("--template", default="safe-array-get", help="template name (default safe-array-get)")
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    _add_common(i, spec=False)
    i.set_defaults(func=cmd_init)

    _add_spec_commands(sub)
    _add_workflow_commands(sub)

    rp = sub.add_parser("report", help="show the report of a run (default: latest)")
    rp.add_argument("run", nargs="?", help="run id, run directory, run.json path, or 'latest'")
    rp.add_argument("--project-root", help="project root (default: current directory)")
    rp.add_argument("--format", choices=["md", "json"], default="md")
    rp.set_defaults(func=cmd_report)
    return p


def _out_of_range_option(args) -> str | None:
    max_rounds = getattr(args, "max_rounds", None)
    if max_rounds is not None and not (0 <= max_rounds <= MAX_REPAIR_ROUNDS):
        return f"--max-rounds must be between 0 and {MAX_REPAIR_ROUNDS}"
    timeout_scale = getattr(args, "timeout_scale", None)
    if timeout_scale is not None and not (MIN_TIMEOUT_SCALE <= timeout_scale <= MAX_TIMEOUT_SCALE):
        return f"--timeout-scale must be between {MIN_TIMEOUT_SCALE} and {MAX_TIMEOUT_SCALE}"
    return None


def _record_aborted_run_or_report(message: str) -> None:
    try:
        record_aborted_run(message)
    except OSError as exc:
        print(f"leanc: warning: could not write the aborted-run record: {exc}", file=sys.stderr)


def _run_command(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.command_help()
        return EXIT_USAGE
    usage_problem = _out_of_range_option(args)
    if usage_problem:
        print(f"leanc: error: {usage_problem}", file=sys.stderr)
        return EXIT_USAGE
    if not logging.getLogger(LOGGER_NAME).handlers:
        setup_logging(None, getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except LeanCError as exc:
        log.log(FILE_ONLY_LEVEL, "error: %s", exc)
        print(f"leanc: error: {exc}", file=sys.stderr)
        _record_aborted_run_or_report(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("leanc: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_command(argv)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        log.log(FILE_ONLY_LEVEL, "internal error", exc_info=True)
        print(f"leanc: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
