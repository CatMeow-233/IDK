"""Policy checks for AI-generated and AI-repaired C code.

The repair agent is untrusted: it may - deliberately or by accident - try to
make verification pass without fixing the bug (assume the failure away,
delete the failing assertion, silence the analyzer, edit the harness, ...).
Correctness is decided by the independent tools against the approved,
hash-locked spec and harness; this module is *defence in depth* that rejects
obvious cheats before any tool runs, so they never reach the verifier and the
repair history tells the agent exactly which rule it broke.

How matching works:

* every C file is lexed by a small C front end (:class:`CSource`) that
  performs translation phase 2 (backslash-newline splicing, also with
  trailing blanks like clang), blanks comments and the *contents* of string
  and character literals (so ``/* __CPROVER_assume */`` and
  ``printf("system(")`` cause no false positives), canonicalises digraphs
  (``%:include`` is ``#include``) and blanks ``#if 0`` / ``#else``-of-``#if 1``
  regions, which the compiler never sees;
* content rules are **count based**: a construct is flagged only if the new
  file contains more occurrences of it than the old one (so a repair of code
  that already had, say, a ``#pragma GCC diagnostic`` is not blamed for it);
  ``ASSERT_REMOVED`` is the mirror image (fewer assertions than before);
* path rules are absolute: any changed path outside ``spec.sources.editable``
  is rejected, whatever the content.

Known limits: this is a lexical check, not a preprocessor.  Macro tricks
that assemble forbidden identifiers are partly covered (token pasting ``##``
and trigraphs are rejected as ``OBFUSCATION``) but not exhaustively;
conditionals other than literal ``#if 0``/``#if 1`` are treated as live; a
target defined through a macro or K&R syntax reads as "removed".  The
protected harness and the independent verifiers remain the real line of
defence.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from leanc.foundation.errors import WorkspaceError
from leanc.foundation.models import PolicyDecision, PolicyViolation
from leanc.foundation.workspace import normalize_relpath
from leanc.lang.c_lexer import BIDI_RE, CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.repair.patch import changed_paths, diff_stats
from leanc.rules.cpp_rules import find_cpp_function_definitions
from leanc.rules.definitions import FunctionDefinition, find_function_definitions
from leanc.rules.lean_rules import LEAN_RULES, check_lean_file, is_lean_path
from leanc.rules.source import source_violations
from leanc.specs.model import CPP_SUFFIXES, Spec

__all__ = [
    "MAX_ANALYZED_CHARS",
    "RULES",
    "check_candidate",
    "check_generated",
]

#: Rule id -> one-line description (also the stable order of reported violations).
RULES: dict[str, str] = {
    "PATH_INVALID": "path is absolute, contains '..' or backslashes, or is not in canonical form",
    "PATH_NOT_EDITABLE": "changes a file outside spec.sources.editable (harness, test driver, headers, spec, "
    ".leanc/ approvals, anything else)",
    "FILE_DELETED": "deletes an editable source file",
    "NO_CHANGE": "the candidate is identical to the current sources",
    "TOO_LARGE": "more changed lines than allowed, or a file too large to policy-check",
    "CPROVER_BUILTIN": "uses CBMC built-ins (__CPROVER_*, #pragma CPROVER), Lean C harness macros "
    "(LEANC_ASSUME/LEANC_REACH) or includes leanc.h",
    "ASSUMPTION_INJECTION": "adds compiler/verifier assumptions (__builtin_assume*, __builtin_unreachable, "
    "__assume, unreachable(), assume/nonnull attributes)",
    "ASSERT_REMOVED": "removes assert()/static_assert or disables them (#define NDEBUG, #undef/#define assert)",
    "WARNING_SUPPRESSION": "suppresses compiler or analyzer diagnostics (#pragma clang|GCC diagnostic, "
    "system_header, __clang_analyzer__, [[clang::suppress]])",
    "SANITIZER_SUPPRESSION": "disables or special-cases sanitizers (no_sanitize attributes, __SANITIZE_*__, "
    "__has_feature(*_sanitizer), sanitizer runtime hooks such as __lsan_disable)",
    "INCLUDE_ESCAPE": "#include/#embed with an absolute path, a '..' component or a computed header name",
    "DANGEROUS_CALL": "introduces process, network, dynamic-loading or raw-syscall functions",
    "INLINE_ASM": "introduces inline assembly (asm, __asm__, __asm)",
    "TRIVIALIZED_TARGET": "the target function is removed, replaced by a macro, or reduced to returning a constant",
    "OBFUSCATION": "trigraphs, token pasting (##) or bidirectional-control characters, which can hide code "
    "from review and from this policy",
    "MISSING_TARGET": "the generated code does not define the target function",
    # absolute source-integrity rules shared with the verify-time gate
    # (leanc.rules.source.source_violations)
    "TOOL_DIVERGENCE": "code that can differ between clang, the sanitizer build and CBMC (conditionals on "
    "compiler/optimiser/verifier macros, non-standard directives, optimisation-dependent built-ins)",
    "PRAGMA": "a #pragma / _Pragma other than '#pragma once'",
    "LINE_DIRECTIVE": "#line or GNU linemarkers",
    "SYMBOL_OVERRIDE": "defines/redefines libc or verifier hooks (__assert_fail, nondet_*, main, reserved names), "
    "weak/alias symbols, or macros named after libc functions, keywords or interface macros",
    "CONSTRUCTOR": "constructor/destructor/section attributes (code running outside the function under test)",
    "TERMINATION": "process termination or non-local control flow (exit, _exit, abort, atexit, longjmp, raise, ...)",
    "DANGEROUS_IMPORT": "process/network/loader/thread/sanitizer-control APIs",
    # ACSL trust rules for the wp gate (leanc.rules.acsl, applied via source_violations)
    "ACSL_FORBIDDEN": "ACSL that could add unproved assumptions (admit, axiom, inductive, recursive logic, ghost, ...)",
    "ACSL_CONTRACT": "an ACSL contract on the target, or one not attached to a static helper definition",
    "ACSL_MACRO": "a macro used inside ACSL, or a #define of an identifier of the approved ACSL contract",
}
# Lean proof files (spec.lean.proofs) get Lean rules instead of the C rules.
RULES.update(LEAN_RULES)

#: Files larger than this are not lexed (TOO_LARGE); real sources are far smaller.
MAX_ANALYZED_CHARS = 1_000_000

_WS = r"[^\S\n]*"  # horizontal whitespace (incl. \f, \v)

# =========================================================================== helpers
@dataclass(frozen=True)
class _Hit:
    rule: str
    key: str  # the construct (counted separately per key)
    line: int
    text: str  # normalised line text, to tell new occurrences from old ones
    shown: str  # original line, for the message
    offset: int = -1


# =========================================================================== rules
_CODE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("CPROVER_BUILTIN", re.compile(r"\b__CPROVER\w*"), "CBMC built-in"),
    ("CPROVER_BUILTIN", re.compile(r"\bLEANC_(?:ASSUME|REACH|VACUITY_CHECK)\b"), "Lean C harness macro"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b__builtin_assume\w*(?=\s*\()"), "compiler assumption"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b__builtin_unreachable(?=\s*\()"), "unreachability assumption"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b__assume(?=\s*\()"), "compiler assumption"),
    ("ASSUMPTION_INJECTION", re.compile(r"(?<![\w.])(?<!->)\bunreachable(?=\s*\(\s*\))"), "C23 unreachable()"),
    ("WARNING_SUPPRESSION", re.compile(r"\b__clang_analyzer__\b"), "static-analyzer special case"),
    ("WARNING_SUPPRESSION", re.compile(r"\banalyzer_noreturn\b"), "static-analyzer attribute"),
    (
        "SANITIZER_SUPPRESSION",
        re.compile(r"\b_*no_sanitize\w*|\bno_address_safety_analysis\b|\bdisable_sanitizer_instrumentation\b"),
        "sanitizer opt-out",
    ),
    ("SANITIZER_SUPPRESSION", re.compile(r"\b__SANITIZE_\w+__\b"), "sanitizer-dependent code"),
    (
        "SANITIZER_SUPPRESSION",
        re.compile(r"\b__has_feature\s*\(\s*\w*sanitizer\w*\s*\)"),
        "sanitizer-dependent code",
    ),
    (
        "SANITIZER_SUPPRESSION",
        re.compile(r"\b__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer)_\w+"),
        "sanitizer runtime hook",
    ),
    ("INLINE_ASM", re.compile(r"\b(?:asm|__asm__|__asm)\b"), "inline assembly"),
    ("OBFUSCATION", re.compile(r"##"), "token pasting"),
    ("ASSERT_REMOVED", re.compile(rf"^{_WS}#{_WS}define{_WS}NDEBUG\b", re.MULTILINE), "assertions disabled"),
    ("ASSERT_REMOVED", re.compile(rf"^{_WS}#{_WS}(?:undef|define){_WS}assert\b", re.MULTILINE), "assert redefined"),
]

_DANGEROUS = (
    "system", "popen", "execl", "execle", "execlp", "execv", "execve", "execvp", "execvpe", "execveat",
    "fexecve", "fork", "vfork", "posix_spawn", "posix_spawnp", "socket", "socketpair", "connect",
    "dlopen", "dlmopen", "dlsym", "ptrace", "syscall",
)  # fmt: skip
_DANGER_ALT = "|".join(_DANGEROUS)
_DANGER_CALL_RE = re.compile(rf"(?<![\w.])(?<!->)\b({_DANGER_ALT})(?=\s*\()")
# function designator used as a value: `f = system;`, `(system)("ls")`, `&system`, `return popen;`
_DANGER_VALUE_RE = re.compile(rf"(?:[=&,(:?]|\breturn\b)\s*\b({_DANGER_ALT})\b(?=\s*[;,)])")
_DEFINE_RE = re.compile(rf"^{_WS}#{_WS}define{_WS}(\w+)(\([^)\n]*\))?(.*)$", re.MULTILINE)
_DANGER_WORD_RE = re.compile(rf"\b({_DANGER_ALT})\b")

_PRAGMA_RE = re.compile(rf"^{_WS}#{_WS}pragma\b(.*)$", re.MULTILINE)
_PRAGMA_OP_RE = re.compile(r"\b(_Pragma|__pragma)\s*\(")
_PRAGMA_STR_RE = re.compile(r'\(\s*(?:L|u8|u|U)?"((?:[^"\\\n]|\\.)*)"\s*\)')
_INCLUDE_RE = re.compile(rf"^{_WS}#{_WS}(include_next|include|import|embed)\b", re.MULTILINE)
_HEADER_NAME_RE = re.compile(r'^(<[^>\n]*>|"[^"\n]*")')
_ATTRIBUTE_RE = re.compile(r"\b(__attribute__|__declspec)\s*\(")
_STD_ATTRIBUTE_RE = re.compile(r"\[\s*\[")
_SUPPRESS_ATTR_RE = re.compile(r"\bclang\s*::\s*suppress\b")
_ASSUME_ATTR_RE = re.compile(r"\b(?:\w+\s*::\s*)?_*(assume|assume_aligned|nonnull|returns_nonnull)_*\b")
_ALIAS_ATTR_RE = re.compile(r'\b_*(?:alias|weakref)_*\s*\(\s*"\s*(__CPROVER\w*|LEANC_\w*)')
_TRIGRAPH_RE = re.compile(r"\?\?[=/'()!<>\-]")
_ASSERT_RE = re.compile(r"(?<![\w.])(?<!->)\bassert(?=\s*\()")
_STATIC_ASSERT_RE = re.compile(r"\b(?:static_assert|_Static_assert)(?=\s*\()")


def _norm_key(s: str) -> str:
    """Spacing-insensitive spelling of a construct (``__has_feature ( x )``
    and ``__has_feature(x)`` are the same construct for counting)."""
    return re.sub(r"\s*([^\w\s])\s*", r"\1", " ".join(s.split()))


def _pragma_hits(body: str) -> list[tuple[str, str]]:
    """Classify the text after ``#pragma``."""
    b = " ".join(body.split())
    out: list[tuple[str, str]] = []
    m = re.match(r"^CPROVER\b\s*(\w*)", b)
    if m:
        out.append(("CPROVER_BUILTIN", f"CBMC pragma '{('#pragma CPROVER ' + m.group(1)).strip()}'"))
    m = re.match(r"^(clang|GCC)\s+(diagnostic|system_header)\b", b)
    if m:
        out.append(("WARNING_SUPPRESSION", f"diagnostic pragma '#pragma {m.group(1)} {m.group(2)}'"))
    m = re.match(r"^(system_header|warning)\b", b)
    if m:
        out.append(("WARNING_SUPPRESSION", f"diagnostic pragma '#pragma {m.group(1)}'"))
    return out


def _scan(src: CSource, target: str | None, *, _depth: int = 0) -> list[_Hit]:
    """All 'new-construct' findings in one file (ASSERT_REMOVED counts and
    function definitions are computed separately)."""
    code, noc = src.code, src.nocomment
    hits: list[_Hit] = []

    def add(rule: str, key: str, offset: int) -> None:
        line = src.line_of(offset)
        hits.append(_Hit(rule, key, line, src.line_text(offset), src.shown_line(line), offset))

    for rule, rx, what in _CODE_RULES:
        for m in rx.finditer(code):
            add(rule, f"{what} '{_norm_key(m.group())}'", m.start() + len(m.group()) - len(m.group().lstrip()))

    # dangerous functions: calls, uses as a value, mentions in macro bodies
    for rx in (_DANGER_CALL_RE, _DANGER_VALUE_RE):
        for m in rx.finditer(code):
            add("DANGEROUS_CALL", f"{m.group(1)}()", m.start(1))
    for m in _DEFINE_RE.finditer(code):
        for w in _DANGER_WORD_RE.finditer(m.group(3)):
            add("DANGEROUS_CALL", f"{w.group(1)}()", m.start(3) + w.start())
        if target and m.group(1) == target:
            add("TRIVIALIZED_TARGET", f"target function '{target}' redefined as a macro", m.start(1))

    # pragmas: directives and the _Pragma / __pragma operators (whose string
    # argument is destringized and scanned like a #pragma line, recursively)
    for m in _PRAGMA_RE.finditer(code):
        for rule, key in _pragma_hits(noc[m.start(1) : m.end(1)]):
            add(rule, key, m.start())
    covered = -1
    for m in _PRAGMA_OP_RE.finditer(code):
        if m.start() < covered:
            continue
        if m.group(1) == "_Pragma":
            sm = _PRAGMA_STR_RE.match(noc, m.end() - 1)
            body = sm.group(1).replace('\\"', '"').replace("\\\\", "\\") if sm else None
        else:
            close = src.pairs.get(m.end() - 1)
            body = noc[m.end() : close] if close is not None else None
            covered = close if close is not None else covered
        if body is None and m.group(1) == "_Pragma":
            add("OBFUSCATION", "_Pragma with a non-literal argument", m.start())
            continue
        if body is None or _depth > 3:
            continue
        for h in _scan(CSource("#pragma " + body), target, _depth=_depth + 1):
            add(h.rule, h.key, m.start())

    # include-like directives
    for m in _INCLUDE_RE.finditer(code):
        end = code.find("\n", m.end())
        rest = noc[m.end() : len(code) if end < 0 else end].strip()
        hm = _HEADER_NAME_RE.match(rest)
        kind = m.group(1)
        if not hm:
            add("INCLUDE_ESCAPE", f"computed #{kind} (header name not a literal: {rest[:40]!r})", m.start())
            continue
        name = hm.group(1)[1:-1].strip()
        parts = [p for p in re.split(r"[\\/]+", name) if p]
        if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
            add("INCLUDE_ESCAPE", f"#{kind} with absolute path {hm.group(1)}", m.start())
        elif ".." in parts:
            add("INCLUDE_ESCAPE", f"#{kind} with '..' in {hm.group(1)}", m.start())
        if parts and parts[-1] == "leanc.h":
            add("CPROVER_BUILTIN", "#include of the harness header leanc.h", m.start())

    # attributes: GNU/MS style and C23 [[...]]
    # (regions are scanned once: a match inside an already-scanned attribute
    # is part of it, which keeps hostile nesting linear)
    covered = -1
    for m in _ATTRIBUTE_RE.finditer(code):
        if m.start() < covered:
            continue
        # an unclosed attribute is checked up to the end of its line
        close = src.pairs.get(m.end() - 1)
        if close is None:
            eol = code.find("\n", m.end())
            close = len(code) if eol < 0 else eol
        covered = close
        for a in _ASSUME_ATTR_RE.finditer(code, m.end(), close):
            add("ASSUMPTION_INJECTION", f"'{a.group(1)}' attribute", m.start())
        for a in _ALIAS_ATTR_RE.finditer(noc, m.end(), close):
            add("CPROVER_BUILTIN", f"alias of {a.group(1)}", m.start())
    covered = -1
    for m in _STD_ATTRIBUTE_RE.finditer(code):
        close = src.pairs.get(m.start())  # partner of the outer '['
        if m.start() < covered or close is None or close <= m.end():
            continue
        covered = close
        for a in _ASSUME_ATTR_RE.finditer(code, m.end(), close):
            add("ASSUMPTION_INJECTION", f"'{a.group(1)}' attribute", m.start())
        if _SUPPRESS_ATTR_RE.search(code, m.end(), close):
            add("WARNING_SUPPRESSION", "[[clang::suppress]]", m.start())

    # raw-text checks: trigraphs (phase 1, active even inside literals and
    # comments under -std=c*) and Trojan-Source style bidi controls
    raw = src.raw
    for rx, what in ((_TRIGRAPH_RE, "trigraph"), (BIDI_RE, "bidirectional control character")):
        for m in rx.finditer(raw):
            line = src.raw_line_of(m.start())
            shown = src.shown_line(line)
            key = f"{what} {m.group()!r}" if what == "trigraph" else f"{what} U+{ord(m.group()):04X}"
            hits.append(_Hit("OBFUSCATION", key, line, shown, shown))

    # one construct may be found by two patterns (e.g. `#define X system(...)`)
    seen: set[tuple[str, str, int]] = set()
    out: list[_Hit] = []
    for h in hits:
        if h.offset >= 0:
            if (h.rule, h.key, h.offset) in seen:
                continue
            seen.add((h.rule, h.key, h.offset))
        out.append(h)
    return out


def _asserts(src: CSource) -> dict[str, list[_Hit]]:
    out: dict[str, list[_Hit]] = {"assert()": [], "static_assert": []}
    for key, rx in (("assert()", _ASSERT_RE), ("static_assert", _STATIC_ASSERT_RE)):
        for m in rx.finditer(src.code):
            line = src.line_of(m.start())
            out[key].append(_Hit("ASSERT_REMOVED", key, line, src.line_text(m.start()), src.shown_line(line), m.start()))
    return out


# =========================================================================== checks
def _path_problem(spec: Spec, p: str) -> PolicyViolation | None:
    if not isinstance(p, str):
        return PolicyViolation(rule="PATH_INVALID", message=f"path is not a string: {p!r}")
    if "\\" in p:
        return PolicyViolation(rule="PATH_INVALID", message=f"backslashes are not allowed in paths: {p!r}", file=p)
    try:
        norm = normalize_relpath(p)
    except WorkspaceError as exc:
        return PolicyViolation(rule="PATH_INVALID", message=str(exc), file=p)
    if norm != p:
        return PolicyViolation(
            rule="PATH_INVALID", message=f"path {p!r} is not in canonical form (use {norm!r})", file=p
        )
    editable = spec.editable_paths()
    if norm in editable:
        return None
    if spec.verification is not None and norm == spec.verification.harness:
        what = "the trusted verification harness"
    elif spec.tests.driver and norm == spec.tests.driver:
        what = "the trusted test driver"
    elif norm in spec.sources.headers:
        what = "a protected interface header"
    elif norm in spec.protected_paths():
        what = "a protected specification file (Lean model / vectors)"
    elif norm == ".leanc" or norm.startswith(".leanc/"):
        what = "Lean C state (approval records)"
    elif norm.endswith((".yaml", ".yml", ".json")):
        what = "a specification/configuration file"
    else:
        what = "not an editable source"
    return PolicyViolation(
        rule="PATH_NOT_EDITABLE",
        message=f"{norm!r} is {what}; only spec.sources.editable may be changed: {', '.join(editable)}",
        file=norm,
    )


def _first_new(new: list[_Hit], old: list[_Hit]) -> _Hit:
    """The first occurrence in ``new`` whose line does not also appear among
    the old occurrences (so the reported line points at the added code)."""
    pool = Counter(h.text for h in old)
    for h in new:
        if pool[h.text] > 0:
            pool[h.text] -= 1
        else:
            return h
    return new[len(old)] if len(new) > len(old) else new[0]


def _content_violations(path: str, old_src: CSource | None, new_src: CSource, target: str | None) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    old_hits = _scan(old_src, target) if old_src is not None else []
    new_hits = _scan(new_src, target)
    by_key_old: dict[tuple[str, str], list[_Hit]] = {}
    by_key_new: dict[tuple[str, str], list[_Hit]] = {}
    for h in old_hits:
        by_key_old.setdefault((h.rule, h.key), []).append(h)
    for h in new_hits:
        by_key_new.setdefault((h.rule, h.key), []).append(h)
    for (rule, key), hs in by_key_new.items():
        olds = by_key_old.get((rule, key), [])
        if len(hs) <= len(olds):
            continue
        h = _first_new(hs, olds)
        counts = f" ({len(olds)} -> {len(hs)} occurrences)" if olds else ""
        out.append(PolicyViolation(rule=rule, message=f"introduces {key}{counts}: `{h.shown}`", file=path, line=h.line))
    if old_src is not None:
        a_old, a_new = _asserts(old_src), _asserts(new_src)
        for key in a_old:
            if len(a_new[key]) < len(a_old[key]):
                gone = _first_new(a_old[key], a_new[key])
                out.append(
                    PolicyViolation(
                        rule="ASSERT_REMOVED",
                        message=(
                            f"number of {key} in {path} decreased from {len(a_old[key])} to {len(a_new[key])} "
                            f"(e.g. `{gone.shown}` at old line {gone.line})"
                        ),
                        file=path,
                    )
                )
    return out


def _integrity_violations(spec: Spec, path: str, text: str) -> list[PolicyViolation]:
    """The verify-time gate's absolute source rules, applied early so a
    repair round or a generation that the gate would reject never reaches
    the verifier."""
    return source_violations(spec, path, text)


def _decision(violations: list[PolicyViolation]) -> PolicyDecision:
    order = {r: k for k, r in enumerate(RULES)}
    uniq: dict[tuple, PolicyViolation] = {}
    for v in violations:
        # the count-based and the absolute (integrity) rules may report the same construct
        uniq.setdefault((v.rule, v.file, v.line) if v.line else (v.rule, v.file, v.line, v.message), v)
    vs = sorted(uniq.values(), key=lambda v: (order.get(v.rule, len(order)), v.file or "", v.line or 0, v.message))
    return PolicyDecision(allowed=not vs, violations=vs)


class _Sources:
    """Lazily lexed files (each file is lexed at most once per check)."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self._cache: dict[str, CSource] = {}

    def get(self, path: str) -> CSource | None:
        if path not in self.files:
            return None
        if path not in self._cache:
            if path.endswith(CPP_SUFFIXES):  # C++ lexing (digit separators, raw strings)
                self._cache[path] = CppSource(self.files[path])
            else:
                self._cache[path] = CSource(self.files[path])
        return self._cache[path]


def _definitions(spec: Spec, srcs: _Sources, paths: list[str]) -> list[FunctionDefinition]:
    out: list[FunctionDefinition] = []
    for p in paths:
        s = srcs.get(p)
        if s is not None and spec.language.is_cpp:  # namespaces, out-of-line members, noexcept, ...
            out.extend(find_cpp_function_definitions(s, spec.target.function, p))
        elif s is not None:
            out.extend(find_function_definitions(s, spec.target.function, p))
    return out


def _target_violations(
    spec: Spec, old: _Sources | None, new: _Sources, old_paths: list[str], new_paths: list[str]
) -> list[PolicyViolation]:
    name = spec.target.function
    out: list[PolicyViolation] = []
    new_defs = _definitions(spec, new, new_paths)
    old_defs = _definitions(spec, old, old_paths) if old is not None else []
    if old is None and not new_defs:
        out.append(
            PolicyViolation(
                rule="MISSING_TARGET",
                message=f"no file-scope definition of the target function '{name}' in the generated files "
                f"({', '.join(new_paths) or 'none'})",
            )
        )
    if old is not None and old_defs and not new_defs:
        out.append(
            PolicyViolation(
                rule="TRIVIALIZED_TARGET",
                message=f"the definition of the target function '{name}' was removed (or hidden behind a macro "
                "or #if 0)",
                file=old_defs[0].file,
            )
        )
    trivial = [d for d in new_defs if d.trivial]
    if trivial and not any(d.trivial for d in old_defs):
        d = trivial[0]
        body = " ".join(d.body.split()) or "(empty)"
        out.append(
            PolicyViolation(
                rule="TRIVIALIZED_TARGET",
                message=f"the target function '{name}' was reduced to `{{ {body[:100]} }}`; it must implement "
                "the specified behaviour, not return a constant",
                file=d.file,
                line=d.line,
            )
        )
    return out


def _size_violation(path: str, content: str) -> PolicyViolation | None:
    if len(content) > MAX_ANALYZED_CHARS:
        return PolicyViolation(
            rule="TOO_LARGE",
            message=f"{path} has {len(content)} characters; files over {MAX_ANALYZED_CHARS} are not policy-checked",
            file=path,
        )
    return None


def check_candidate(
    spec: Spec,
    old: dict[str, str],
    new: dict[str, str],
    *,
    max_changed_lines: int | None = None,
) -> PolicyDecision:
    """Check a repair candidate ``new`` against the current sources ``old``.

    ``allowed`` is true iff there are no violations.  Path rules apply to
    every changed path (added, modified or removed); content rules apply to
    the changed editable files and flag only constructs that are new
    relative to ``old``; in addition the changed editable files must satisfy
    the *absolute* source-integrity rules of the verify-time gate
    (:func:`leanc.rules.source.source_violations`).
    ``max_changed_lines`` defaults to ``spec.repair.max_changed_lines``.
    """
    changed = changed_paths(old, new)
    if not changed:
        return _decision(
            [PolicyViolation(rule="NO_CHANGE", message="the candidate is identical to the current sources")]
        )
    violations: list[PolicyViolation] = []
    content_paths: list[str] = []
    oversized: set[str] = set()
    for p in changed:
        pv = _path_problem(spec, p)
        if pv is not None:
            violations.append(pv)
            continue
        if p not in new:
            violations.append(
                PolicyViolation(rule="FILE_DELETED", message=f"the candidate deletes the editable source {p}", file=p)
            )
            continue
        sv = _size_violation(p, new[p])
        if sv is not None:
            violations.append(sv)
            oversized.add(p)
            continue
        content_paths.append(p)
    limit = spec.repair.max_changed_lines if max_changed_lines is None else max_changed_lines
    huge = [p for p in changed if max(len(old.get(p) or ""), len(new.get(p) or "")) > MAX_ANALYZED_CHARS]
    for p in huge:
        if p not in oversized and p in new:
            sv = _size_violation(p, new[p])
            if sv is not None:
                violations.append(sv)
    if not huge:  # diffing megabytes of text is pointless: the candidate is rejected anyway
        added, removed = diff_stats(old, new)
        if added + removed > limit:
            violations.append(
                PolicyViolation(
                    rule="TOO_LARGE",
                    message=f"the candidate changes {added + removed} lines (+{added} -{removed}); "
                    f"the limit is {limit}",
                )
            )
    old_srcs, new_srcs = _Sources(old), _Sources(new)
    target = spec.target.function
    for p in content_paths:
        if is_lean_path(p):
            violations.extend(check_lean_file(spec, p, old.get(p), new[p]))
            continue
        violations.extend(_content_violations(p, old_srcs.get(p), new_srcs.get(p), target))  # type: ignore[arg-type]
        violations.extend(_integrity_violations(spec, p, new[p]))
    editable = set(spec.sources.editable)
    old_paths = sorted(p for p in old if p in editable and len(old[p]) <= MAX_ANALYZED_CHARS)
    new_paths = sorted(p for p in new if p in editable and p not in oversized)
    violations.extend(_target_violations(spec, old_srcs, new_srcs, old_paths, new_paths))
    return _decision(violations)


def check_generated(spec: Spec, files: dict[str, str]) -> PolicyDecision:
    """Check freshly generated code: the content rules as for a repair
    against empty old files, ``PATH_INVALID``/``PATH_NOT_EDITABLE`` for
    every path, ``MISSING_TARGET`` if no generated editable file defines
    ``spec.target.function``, ``TRIVIALIZED_TARGET`` for a stub body, and the
    absolute source-integrity rules of the verify-time gate."""
    violations: list[PolicyViolation] = []
    content_paths: list[str] = []
    for p in sorted(files):
        pv = _path_problem(spec, p)
        if pv is not None:
            violations.append(pv)
            continue
        sv = _size_violation(p, files[p])
        if sv is not None:
            violations.append(sv)
            continue
        content_paths.append(p)
    srcs = _Sources(files)
    target = spec.target.function
    for p in content_paths:
        if is_lean_path(p):
            violations.extend(check_lean_file(spec, p, None, files[p]))
            continue
        violations.extend(_content_violations(p, None, srcs.get(p), target))  # type: ignore[arg-type]
        violations.extend(_integrity_violations(spec, p, files[p]))
    violations.extend(_target_violations(spec, None, srcs, [], [p for p in content_paths if not is_lean_path(p)]))
    return _decision(violations)
