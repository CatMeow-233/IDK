"""Mandatory source-integrity gate (runs at *verify* time, after a passing compile).

Why a separate gate?  The repair policy (:mod:`leanc.repair.policy`) only sees
proposals inside the repair loop; ``leanc verify`` on hand-written or
generated code never ran it, and even inside the loop a count-based lexical
check cannot see what the *tools* see.  Every verifier in the pipeline must
judge the same program, and that program must not be able to tamper with the
verification itself.  Reviewers demonstrated false ``VERIFIED_BOUNDED``
results through exactly those gaps:

* overriding verification hooks: a body for ``__assert_fail`` (every harness
  ``assert()`` becomes a no-op call in CBMC), or for ``nondet_size_t`` (the
  "symbolic" inputs get narrowed);
* tool divergence: ``#ifdef __clang__`` / ``__OPTIMIZE__`` / ``__CPROVER__``
  / ``LEANC_VACUITY_CHECK`` let clang, the sanitizer build and CBMC compile
  *different* code;
* pragmas assembled by macros (``_Pragma(#x)``) that switch CBMC checks off;
* process termination (``exit``/``abort``/``atexit``, an
  ``__attribute__((constructor))`` calling ``_exit(0)``): CBMC prunes the
  paths, the native tests report success, LeakSanitizer never runs;
* weak references (``__VERIFIER_assume`` resolves only under the verifier),
  GNU linemarkers that mark the file as a system header (analyzer findings
  vanish), ...

The gate therefore checks every editable file with **absolute** rules (not
"new relative to the old version") at three levels:

A. the source text (:func:`source_violations`, also used by the repair and
   generation policy): directives, includes, ``_Pragma``, forbidden
   identifiers, attributes and definitions;
B. the preprocessed text (``clang -E`` with the spec's flags), restricted to
   lines that belong to the editable file itself, so macro tricks are seen
   after expansion;
C. the compiled object (``nm``/``readelf``): which symbols it defines, which
   it imports (an **allowlist** of library functions CBMC models faithfully,
   because CBMC treats body-less functions as returning arbitrary values),
   weak symbols and constructor sections.

A violation is a definite, blocking finding about the code under test:
``Outcome.FAIL`` with ``Status.STATIC_ANALYSIS_WARNING``.  A tool problem
(missing ``nm``, unparsable output, ...) is ``Outcome.ERROR`` - never a pass.
"""

from __future__ import annotations

import bisect
import os
import posixpath
import re
import time
from pathlib import Path

from leanc.backends.tools import find_tool
from leanc.core.context import BackendContext
from leanc.core.models import (
    Diagnostic,
    FailureReport,
    Location,
    PolicyViolation,
    StageResult,
)
from leanc.core.redact import redact
from leanc.core.runner import Limits
from leanc.core.status import Outcome, Status, outcome_to_status
from leanc.repair.policy import CSource
from leanc.specs.model import Spec

__all__ = [
    "ALLOWED_IMPORTS",
    "RULES",
    "STAGE",
    "TOOL",
    "check_object_symbols",
    "defined_functions",
    "preprocessed_violations",
    "run_integrity",
    "section_violations",
    "source_violations",
]

STAGE = "integrity"
TOOL = "leanc-integrity"

RULES: dict[str, str] = {
    "TOOL_DIVERGENCE": "code that can differ between the tools (conditionals/macros on compiler-, optimiser- or "
    "verifier-specific identifiers, non-standard directives, optimisation-dependent built-ins)",
    "PRAGMA": "a #pragma / _Pragma other than '#pragma once' (they can disable CBMC checks or diagnostics)",
    "LINE_DIRECTIVE": "#line or GNU linemarkers (they re-attribute code, e.g. mark it as a system header)",
    "SYMBOL_OVERRIDE": "defines or redefines something it must not (libc / verifier hooks such as __assert_fail, "
    "nondet_*, main, reserved names, weak/alias symbols, external symbols not declared by the protected headers, "
    "macros named after libc functions, keywords or interface macros)",
    "OBFUSCATION": "a _Pragma whose argument is not a single plain string literal",
    "INCLUDE_ESCAPE": "#include of something other than a protected header or a system header (leanc.h, '..', "
    "absolute paths, harness/test/editable files, computed names, #include_next/#import/#embed)",
    "CPROVER_BUILTIN": "CBMC / SV-COMP verifier built-ins or harness-only names (__CPROVER_*, __VERIFIER_*, "
    "nondet_*, LEANC_*)",
    "ASSUMPTION_INJECTION": "compiler/verifier assumptions (__builtin_unreachable/assume, _Noreturn, "
    "assume/nonnull/noreturn/const/pure attributes)",
    "INLINE_ASM": "inline assembly",
    "CONSTRUCTOR": "code that runs outside the function under test (constructor/destructor attributes, "
    "section/used attributes, .init_array/.fini_array/.ctors/.dtors sections)",
    "SANITIZER_SUPPRESSION": "no_sanitize-style attributes",
    "WARNING_SUPPRESSION": "static-analyzer suppression attributes",
    "TERMINATION": "process termination or non-local control flow (exit, _exit, abort, atexit, longjmp, raise, "
    "signal, __builtin_trap, ...): CBMC prunes such paths, native tests report success",
    "DANGEROUS_IMPORT": "imports a function that is not on the allowlist of faithfully modelled library functions "
    "(process, network, dynamic loading, threads, sanitizer control, weak references, anything unmodelled)",
    "MISSING_TARGET": "no editable object defines the target function",
    "UNBOUNDED_STACK": "variable-length arrays or alloca: CBMC has no model of stack limits, so an input-sized "
    "stack allocation verifies while crashing natively",
}

# =========================================================================== name sets
#: Library functions (and data) an editable object may import: CBMC ships a
#: faithful model of each, and they behave the same natively.  Anything else
#: is body-less for CBMC (= returns arbitrary values) and is rejected.
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "memcpy", "memmove", "memset", "memcmp", "memchr",
        "strlen", "strnlen", "strcmp", "strncmp", "strcpy", "strncpy", "strcat", "strncat",
        "strchr", "strrchr", "strstr",
        "malloc", "calloc", "realloc", "free",
        "abs", "labs", "llabs",
        "__assert_fail",
        "printf", "fprintf", "puts", "putchar", "fputs", "fputc", "putc", "stdout", "stderr",
        "__errno_location", "__stack_chk_fail", "__stack_chk_guard",
    }
)  # fmt: skip

#: compiler-rt helpers clang emits for wide arithmetic (e.g. __int128 division).
_COMPILER_RT_RE = re.compile(
    r"^__(?:u?div|u?mod|mul|mulo|ashl|ashr|lshr|neg|u?cmp|popcount|parity|clz|ctz|ffs|bswap)[a-z]*[23]$"
)

TERMINATION_NAMES: frozenset[str] = frozenset(
    {
        "exit", "_exit", "_Exit", "quick_exit", "abort", "atexit", "at_quick_exit", "on_exit",
        "__cxa_atexit", "__cxa_finalize", "setjmp", "_setjmp", "__sigsetjmp", "sigsetjmp", "longjmp",
        "_longjmp", "siglongjmp", "__longjmp_chk", "raise", "kill", "signal", "sigaction", "thrd_exit",
        "pthread_exit", "__assert", "__assert_perror_fail",
    }
)  # fmt: skip

DANGEROUS_NAMES: frozenset[str] = frozenset(
    {
        "fork", "vfork", "clone", "clone3", "daemon", "execl", "execle", "execlp", "execv", "execve",
        "execvp", "execvpe", "execveat", "fexecve", "posix_spawn", "posix_spawnp", "system", "popen",
        "dlopen", "dlmopen", "dlsym", "dlvsym", "syscall", "ptrace", "socket", "socketpair", "connect",
        "bind", "listen", "accept", "accept4", "sendto", "recvfrom", "prctl", "setrlimit", "mprotect",
    }
)  # fmt: skip
_DANGEROUS_PREFIX_RE = re.compile(r"^(?:pthread_|thrd_|__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer)_)")

#: libc / CBMC-model names an editable file must never define or #define.
LIBC_NAMES: frozenset[str] = (
    ALLOWED_IMPORTS
    | TERMINATION_NAMES
    | DANGEROUS_NAMES
    | frozenset(
        {
            "assert", "static_assert", "main", "aligned_alloc", "strdup", "strndup", "sprintf",
            "snprintf", "vprintf", "vfprintf", "vsnprintf", "getchar", "fgets", "fopen", "fclose",
            "fread", "fwrite", "read", "write", "open", "close", "rand", "srand", "time", "clock",
            "sleep", "usleep", "atoi", "atol", "strtol", "strtoul", "strtoll", "strtoull", "qsort",
            "bsearch", "errno", "offsetof", "NULL", "EOF", "true", "false", "bool", "isalpha",
            "isdigit", "isspace", "toupper", "tolower", "div", "ldiv",
        }
    )
)  # fmt: skip

C_KEYWORDS: frozenset[str] = frozenset(
    {
        "auto", "break", "case", "char", "const", "continue", "default", "do", "double", "else", "enum",
        "extern", "float", "for", "goto", "if", "inline", "int", "long", "register", "restrict", "return",
        "short", "signed", "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned", "void",
        "volatile", "while", "alignas", "alignof", "typeof", "typeof_unqual", "constexpr", "nullptr",
        "thread_local",
    }
)  # fmt: skip

_RESERVED_RE = re.compile(r"^(?:__\w*|_[A-Z]\w*)$")
_DIRECTIVE_IDENT_RE = re.compile(r"\b(__\w+|_[A-Z]\w*|LEANC_\w*|NDEBUG)\b")
_STD_DIRECTIVES = frozenset(
    {"include", "define", "undef", "if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif",
     "error", "warning", "pragma", "line"}
)  # fmt: skip

_WS = r"[^\S\n]*"
_DIRECTIVE_RE = re.compile(rf"^{_WS}#{_WS}(\w*)(.*)$", re.MULTILINE)
_HEADER_NAME_RE = re.compile(r'^(<[^>\n]*>|"[^"\n]*")')
_DEFINE_HEAD_RE = re.compile(r"^\s*(\w+)(\([^)\n]*\))?(.*)$")
_PLAIN_STRING_ARG_RE = re.compile(r'\(\s*"((?:[^"\\\n]|\\.)*)"\s*\)')

# identifiers whose value differs between clang / optimiser levels / CBMC / runs
_PREDEFINED_DIVERGENT_RE = re.compile(
    r"\b(__clang\w*|__GNUC\w*|__OPTIMIZE\w*|__llvm\w*|__TIME__|__DATE__|__TIMESTAMP__|__COUNTER__|"
    r"__INCLUDE_LEVEL__|__BASE_FILE__|__FILE_NAME__|__VERSION__|__SANITIZE_\w+|__has_\w+|__is_identifier|"
    r"__NO_INLINE__|__STDC_HOSTED__)\b"
)

_TOKEN_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("CPROVER_BUILTIN", re.compile(r"\b__CPROVER\w*"), "CBMC built-in"),
    ("CPROVER_BUILTIN", re.compile(r"\b__VERIFIER_\w+"), "SV-COMP verifier built-in"),
    ("CPROVER_BUILTIN", re.compile(r"\bnondet_\w+"), "harness-only nondeterministic input function"),
    ("CPROVER_BUILTIN", re.compile(r"\bLEANC_\w+"), "Lean C harness macro"),
    ("INLINE_ASM", re.compile(r"\b(?:asm|__asm__|__asm)\b"), "inline assembly"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b__builtin_(?:unreachable|assume\w*)(?=\s*\()"), "assumption built-in"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b__assume(?=\s*\()"), "assumption built-in"),
    ("ASSUMPTION_INJECTION", re.compile(r"\b_Noreturn\b"), "_Noreturn function"),
    ("ASSUMPTION_INJECTION", re.compile(r"(?<![\w.])(?<!->)\bunreachable(?=\s*\(\s*\))"), "C23 unreachable()"),
    ("TERMINATION", re.compile(r"\b__builtin_(?:trap|debugtrap|abort)(?=\s*\()"), "trapping built-in"),
    (
        "TOOL_DIVERGENCE",
        re.compile(r"\b__builtin_(?:constant_p|object_size|dynamic_object_size)(?=\s*\()"),
        "optimisation-dependent built-in",
    ),
    (
        "DANGEROUS_IMPORT",
        re.compile(r"\b__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer)_\w+"),
        "sanitizer runtime interface",
    ),
    ("PRAGMA", re.compile(r"\b__pragma(?=\s*\()"), "MS-style __pragma"),
    (
        "UNBOUNDED_STACK",
        re.compile(r"(?<![\w.])(?<!->)\b(?:alloca|__builtin_alloca\w*)(?=\s*\()"),
        "stack allocation (alloca)",
    ),
    (
        "TERMINATION",
        re.compile(r"\b__builtin_(?:setjmp|longjmp|unwind_init|eh_return)(?=\s*\()"),
        "non-local control-flow built-in",
    ),
    (
        "TOOL_DIVERGENCE",
        re.compile(r"\b__builtin_(?:frame_address|return_address|extract_return_addr|stack_save|stack_restore)"
                   r"(?=\s*\()"),
        "stack/frame introspection built-in",
    ),
]

_NAMES_ALT = "|".join(sorted(TERMINATION_NAMES | DANGEROUS_NAMES, key=len, reverse=True))
_NAME_CALL_RE = re.compile(rf"(?<![\w.])(?<!->)\b({_NAMES_ALT}|pthread_\w+|thrd_\w+)(?=\s*\()")
_NAME_VALUE_RE = re.compile(rf"(?:[=&,(:?]|\breturn\b)\s*\b({_NAMES_ALT}|pthread_\w+|thrd_\w+)\b(?=\s*[;,)])")

_ATTR_START_RE = re.compile(r"\b(__attribute__|__attribute|__declspec)\s*\(|\[\s*\[")
_ATTR_TOKEN_RE = re.compile(r"[()]|(?:([A-Za-z_]\w*)\s*::\s*)?([A-Za-z_]\w*)")
_ATTR_RULES: dict[str, str] = {
    **dict.fromkeys(("constructor", "destructor", "section", "used", "retain", "init_priority"), "CONSTRUCTOR"),
    **dict.fromkeys(("weak", "weakref", "alias", "ifunc", "visibility", "selectany", "externally_visible"),
                    "SYMBOL_OVERRIDE"),
    **dict.fromkeys(("disable_sanitizer_instrumentation", "no_address_safety_analysis"), "SANITIZER_SUPPRESSION"),
    **dict.fromkeys(("optimize", "target", "target_clones"), "TOOL_DIVERGENCE"),
    "naked": "INLINE_ASM",
    **dict.fromkeys(("assume", "assume_aligned", "nonnull", "returns_nonnull", "noreturn", "const", "pure"),
                    "ASSUMPTION_INJECTION"),
    **dict.fromkeys(("suppress", "analyzer_noreturn"), "WARNING_SUPPRESSION"),
}  # fmt: skip

_DEF_CANDIDATE_RE = re.compile(r"(?<![\w.])(?<!->)\b([A-Za-z_]\w*)\s*\(")
_NOT_A_FUNCTION = frozenset(
    {"__attribute__", "__attribute", "__declspec", "sizeof", "_Alignas", "alignas", "_Alignof", "alignof",
     "__typeof__", "__typeof", "typeof", "_Static_assert", "static_assert", "__extension__", "_Generic",
     "__asm__", "__asm", "asm", "if", "while", "for", "switch", "return"}
)  # fmt: skip


# =========================================================================== helpers
def _v(rule: str, message: str, rel: str | None, line: int | None) -> PolicyViolation:
    return PolicyViolation(rule=rule, message=message, file=rel, line=line)


def _identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[A-Za-z_]\w*\b", text))


def _header_facts(headers: dict[str, str] | None) -> tuple[set[str], set[str]]:
    """(identifiers, macro names) of the protected headers, comment-stripped."""
    idents: set[str] = set()
    macros: set[str] = set()
    for text in (headers or {}).values():
        src = CSource(text)
        idents |= _identifiers(src.code)
        for m in _DIRECTIVE_RE.finditer(src.code):
            if m.group(1) == "define":
                dm = _DEFINE_HEAD_RE.match(m.group(2))
                if dm:
                    macros.add(dm.group(1))
    return idents, macros


def _attr_names(region: str, base_depth: int) -> list[tuple[str, int]]:
    """Attribute names (namespace stripped, ``__x__`` -> ``x``) at the
    attribute-list nesting level of ``region``, with their offsets."""
    out: list[tuple[str, int]] = []
    depth = 0
    for m in _ATTR_TOKEN_RE.finditer(region):
        tok = m.group()
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == base_depth and m.group(2):
            name = m.group(2)
            norm = name.strip("_") if name.startswith("__") and name.endswith("__") else name
            out.append((norm, m.start(2)))
    return out


def _attribute_violations(src: CSource, where: str) -> list[tuple[str, str, int]]:
    """(rule, message, offset) for forbidden attributes in GNU, MS and C23 form."""
    code = src.code
    out: list[tuple[str, str, int]] = []
    covered = -1
    for m in _ATTR_START_RE.finditer(code):
        if m.start() < covered:
            continue
        if m.group(1):  # __attribute__(( ... )) / __declspec( ... )
            open_at = m.end() - 1
            close = src.pairs.get(open_at)
            if close is None:
                eol = code.find("\n", open_at)
                close = len(code) if eol < 0 else eol
            region, base, start = code[open_at : close + 1], (1 if m.group(1) == "__declspec" else 2), open_at
        else:  # [[ ... ]]
            close = src.pairs.get(m.start())
            if close is None:
                continue
            region, base, start = code[m.end() : close], 0, m.end()
        covered = close
        for name, off in _attr_names(region, base):
            rule = _ATTR_RULES.get(name)
            if rule is None and name.startswith("no_sanitize"):
                rule = "SANITIZER_SUPPRESSION"
            if rule is not None:
                out.append((rule, f"attribute '{name}' is not allowed ({RULES[rule].split('(')[0].strip()}){where}",
                            start + off))
    return out


def _token_violations(src: CSource, where: str = "") -> list[tuple[str, str, int]]:
    """Rules that apply to both the source and its macro expansion."""
    code = src.code
    out: list[tuple[str, str, int]] = []
    for rule, rx, what in _TOKEN_RULES:
        for m in rx.finditer(code):
            out.append((rule, f"{what} '{m.group()}' is not allowed in code under verification{where}", m.start()))
    for rx in (_NAME_CALL_RE, _NAME_VALUE_RE):
        for m in rx.finditer(code):
            name = m.group(1)
            if name in TERMINATION_NAMES or name in ("pthread_exit", "thrd_exit"):
                out.append(("TERMINATION", (f"'{name}' terminates the process or escapes control flow: CBMC "
                            f"prunes such paths and the native tests can report success{where}"), m.start(1)))
            else:
                out.append(("DANGEROUS_IMPORT", (f"'{name}' (process/network/loader/thread API) is not allowed"
                            f"{where}"), m.start(1)))
    out.extend(_attribute_violations(src, where))
    return out


def defined_functions(src: CSource) -> list[tuple[str, int]]:
    """Names (and offsets) of all file-scope function definitions."""
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    marks: list[int] = []
    depth_after: list[int] = []
    d = 0
    for bm in re.finditer(r"[{}]", code):
        d += 1 if bm.group() == "{" else -1
        marks.append(bm.start())
        depth_after.append(d)
    out: list[tuple[str, int]] = []
    for m in _DEF_CANDIDATE_RE.finditer(code):
        name = m.group(1)
        if name in _NOT_A_FUNCTION:
            continue
        j = bisect.bisect_left(marks, m.start()) - 1
        if j >= 0 and depth_after[j] > 0:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        k = close + 1
        for _ in range(64):
            while k < len(code) and code[k].isspace():
                k += 1
            am = re.match(r"(?:__attribute__|__attribute|__declspec)\s*\(", code[k : k + 40])
            if am and (c2 := pairs.get(k + am.end() - 1)) is not None:
                k = c2 + 1
                continue
            if code.startswith("[[", k) and (c2 := pairs.get(k)) is not None:
                k = c2 + 1
                continue
            break
        if k < len(code) and code[k] == "{":
            out.append((name, m.start()))
    return out


def _forbidden_definition(name: str) -> str | None:
    """Why ``name`` may never be defined by code under verification."""
    if name == "main":
        return "'main' belongs to the trusted harness / test driver"
    if name.startswith("nondet_"):
        return f"'{name}' is a harness input generator: giving it a body narrows the symbolic inputs"
    if _RESERVED_RE.match(name):
        return f"'{name}' is a reserved identifier (C library / compiler / verifier hook)"
    if name in LIBC_NAMES:
        return f"'{name}' is a C library / CBMC model function"
    return None


def _include_problem(spec: Spec, name: str, quoted: bool) -> str | None:
    if not name:
        return "empty header name"
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return f"absolute or backslash header path {name!r}"
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        return f"'..' in header path {name!r}"
    norm = "/".join(parts)
    if parts and parts[-1] == "leanc.h":
        return "leanc.h is for the trusted harness only"
    others = [*([spec.verification.harness] if spec.verification else []), *spec.sources.editable] + (
        [spec.tests.driver] if spec.tests.driver else []
    )
    for p in others:
        if norm == p or posixpath.basename(p) == parts[-1] or p.endswith("/" + norm):
            return f"{name!r} names a harness, test or source file, not a header"
    if quoted:
        candidates = set()
        for h in spec.sources.headers:
            candidates.add(h)
            for d in spec.sources.include_dirs:
                if h.startswith(d.rstrip("/") + "/"):
                    candidates.add(h[len(d.rstrip("/")) + 1 :])
            candidates.add(posixpath.basename(h))
        if norm not in candidates:
            return (
                f'"{name}" is not a protected header of the spec ({", ".join(spec.sources.headers) or "none"}); '
                "use <...> for system headers"
            )
    elif not norm.endswith(".h"):
        return f"<{name}> is not a header"
    return None


def _directive_violations(
    spec: Spec, rel: str, src: CSource, header_macros: set[str]
) -> list[tuple[str, str, int]]:
    code, noc = src.code, src.nocomment
    out: list[tuple[str, str, int]] = []
    override_names = LIBC_NAMES | C_KEYWORDS | header_macros | {spec.target.function}
    for m in _DIRECTIVE_RE.finditer(code):
        name, rest = m.group(1), m.group(2)
        at = m.start()
        if name == "":
            if rest.strip():  # e.g. "# 1 "x.c" 3" (digits are \w, so this is non-word junk)
                out.append(("TOOL_DIVERGENCE", f"malformed directive `#{rest.strip()[:40]}`", at))
            continue
        if name.isdigit():
            out.append(("LINE_DIRECTIVE", ("GNU linemarkers re-attribute code (e.g. mark it as a system header, "
                        "which hides analyzer findings) and are not allowed"), at))
            continue
        if name == "line":
            out.append(("LINE_DIRECTIVE", "#line re-attributes code to other files/lines and is not allowed", at))
            continue
        if name in ("include_next", "import", "embed"):
            out.append(("INCLUDE_ESCAPE", f"#{name} is not allowed", at))
            continue
        if name not in _STD_DIRECTIVES:
            out.append(("TOOL_DIVERGENCE", f"non-standard directive #{name} (preprocessors disagree on it)", at))
            continue
        if name == "pragma":
            body = " ".join(noc[m.start(2) : m.end(2)].split())
            if body != "once":
                out.append(("PRAGMA", (f"`#pragma {body[:60]}` is not allowed (pragmas can disable CBMC checks or "
                            "diagnostics, or mark code as a system header)"), at))
            continue
        if name == "include":
            body = noc[m.start(2) : m.end(2)].strip()
            hm = _HEADER_NAME_RE.match(body)
            if not hm:
                out.append(("INCLUDE_ESCAPE", f"computed #include ({body[:40]!r}) cannot be checked", at))
                continue
            problem = _include_problem(spec, hm.group(1)[1:-1].strip(), hm.group(1)[0] == '"')
            if problem:
                out.append(("INCLUDE_ESCAPE", f"#include {hm.group(1)}: {problem}", at))
            continue
        if name in ("error", "warning", "else", "endif"):
            continue
        idents = _DIRECTIVE_IDENT_RE.findall(rest)
        if name in ("define", "undef"):
            dm = _DEFINE_HEAD_RE.match(rest)
            macro = dm.group(1) if dm else ""
            if macro in override_names and not _DIRECTIVE_IDENT_RE.fullmatch(macro):
                what = (
                    "the target function" if macro == spec.target.function
                    else "a macro of the protected interface" if macro in header_macros
                    else "a C keyword" if macro in C_KEYWORDS
                    else "a C library name"
                )
                out.append(("SYMBOL_OVERRIDE", f"#{name} {macro}: redefining {what} is not allowed", at))
        if idents:
            uniq = ", ".join(dict.fromkeys(idents))
            out.append(("TOOL_DIVERGENCE", (f"#{name} on {uniq}: compiler-, optimiser- or verifier-specific "
                        "macros make clang, the sanitizer build and CBMC see different code"), at))
        if name in ("if", "elif") and "#" in rest:
            out.append(("TOOL_DIVERGENCE", (f"#{name} uses an assertion predicate (#...), which preprocessors "
                        "treat differently"), at))
    return out


def _pragma_operator_violations(src: CSource) -> list[tuple[str, str, int]]:
    code, noc = src.code, src.nocomment
    out: list[tuple[str, str, int]] = []
    for m in re.finditer(r"\b_Pragma\s*\(", code):
        sm = _PLAIN_STRING_ARG_RE.match(noc, m.end() - 1)
        if not sm:
            out.append(("OBFUSCATION", ("_Pragma(...) whose argument is not a single plain string literal "
                        "(e.g. built by a macro) cannot be checked"), m.start()))
        elif sm.group(1).strip() != "once":
            out.append(("PRAGMA", (f"_Pragma(\"{sm.group(1)[:60]}\") is not allowed (pragmas can disable CBMC "
                        "checks or diagnostics)"), m.start()))
    return out


# =========================================================================== level A: source
def source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    """Absolute source-level integrity rules for one editable file.

    Pure (no tools): directives, includes, ``_Pragma``, forbidden identifiers
    and attributes, forbidden definitions, divergent predefined macros.
    ``headers`` (protected header texts) enables the "redefines an interface
    macro" check.  Used by :func:`run_integrity` and by the repair /
    generation policy."""
    src = CSource(text)
    _, header_macros = _header_facts(headers)
    found: list[tuple[str, str, int]] = []
    found += _directive_violations(spec, rel, src, header_macros)
    found += _pragma_operator_violations(src)
    found += _token_violations(src)
    # divergent predefined macros used in ordinary code (directives were checked above)
    for m in _PREDEFINED_DIVERGENT_RE.finditer(src.code_nodirectives):
        found.append(("TOOL_DIVERGENCE", (f"'{m.group(1)}' has a different value (or none) under clang, the "
                      "optimised sanitizer build and CBMC"), m.start()))
    for name, off in defined_functions(src):
        why = _forbidden_definition(name)
        if why:
            found.append(("SYMBOL_OVERRIDE", f"defines function {name}(): {why}", off))
    return _to_violations(src, rel, found)


def _to_violations(
    src: CSource, rel: str, found: list[tuple[str, str, int]], line_map: list[int] | None = None
) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    seen: set[tuple[str, int]] = set()
    for rule, message, off in sorted(found, key=lambda f: (f[2], f[0])):
        line = src.line_of(off)
        if line_map is not None:
            line = line_map[line - 1] if 0 < line <= len(line_map) else 0
        if (rule, line) in seen:
            continue
        seen.add((rule, line))
        shown = src.shown_line(src.line_of(off))
        out.append(_v(rule, f"{message}: `{shown}`" if shown else message, rel, line or None))
    return out


# =========================================================================== level B: preprocessed
_LINEMARKER_RE = re.compile(r'^#\s*(\d+)\s+"((?:[^"\\]|\\.)*)"')


def _expanded_view(rel: str, pp: str) -> tuple[str, list[int]]:
    """Lines of ``clang -E`` output attributed to ``rel`` itself, with the
    source line number of each (macro expansions appear on the line that
    uses the macro)."""
    lines: list[str] = []
    origin: list[int] = []
    cur_file: str | None = None
    cur_line = 0
    for line in pp.split("\n"):
        m = _LINEMARKER_RE.match(line)
        if m:
            cur_line = int(m.group(1))
            cur_file = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
            continue
        if cur_file is not None and posixpath.normpath(cur_file) == posixpath.normpath(rel):
            lines.append(line)
            origin.append(cur_line)
        cur_line += 1
    return "\n".join(lines), origin


def preprocessed_violations(rel: str, pp_output: str) -> list[PolicyViolation]:
    """Rules applied to the macro-expanded code of ``rel`` (``clang -E``
    output): pragmas produced by ``_Pragma``/macros, built-ins, attributes,
    termination/dangerous calls that only appear after expansion."""
    text, origin = _expanded_view(rel, pp_output)
    src = CSource(text)
    where = " (after macro expansion)"
    found = _token_violations(src, where)
    for m in re.finditer(rf"^{_WS}#{_WS}pragma\b(.*)$", src.code, re.MULTILINE):
        body = " ".join(src.nocomment[m.start(1) : m.end(1)].split())
        if body != "once":
            found.append(("PRAGMA", f"`#pragma {body[:60]}`{where} is not allowed", m.start()))
    for m in re.finditer(r"\b_Pragma\b", src.code):
        found.append(("PRAGMA", f"_Pragma{where} is not allowed", m.start()))
    return _to_violations(src, rel, found, origin)


# =========================================================================== level C: objects
_SECTION_RE = re.compile(r"^\s*\[\s*\d+\]\s+(\S+)", re.MULTILINE)
_BAD_SECTION_RE = re.compile(r"^\.(?:rela?\.)?(?:init_array|fini_array|preinit_array|ctors|dtors|init|fini)(?:\.|$)")


def parse_nm(output: str) -> list[tuple[str, str]]:
    """``(name, type)`` pairs from ``nm -P`` output."""
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[1]) == 1 and not line.endswith(":"):
            out.append((parts[0], parts[1]))
    return out


def _first_mention(src: CSource | None, name: str) -> int | None:
    if src is None:
        return None
    m = re.search(rf"\b{re.escape(name)}\b", src.code)
    return src.line_of(m.start()) if m else None


def check_object_symbols(
    spec: Spec,
    rel: str,
    symbols: list[tuple[str, str]],
    *,
    header_idents: set[str],
    defined_elsewhere: set[str],
    src: CSource | None = None,
) -> list[PolicyViolation]:
    """Symbol-table rules for one editable object."""
    out: list[PolicyViolation] = []
    target = spec.target.function
    allowed_defs = header_idents | {target}
    for name, typ in symbols:
        line = _first_mention(src, name)
        if typ in ("U", "w", "v"):
            if typ in ("w", "v"):
                out.append(_v("DANGEROUS_IMPORT", f"weak undefined reference to '{name}': it resolves differently "
                              "under the verifier and natively (e.g. __VERIFIER_assume)", rel, line))
            elif name in TERMINATION_NAMES:
                out.append(_v("TERMINATION", f"imports '{name}': termination / non-local control flow is not "
                              "allowed (CBMC prunes such paths; native tests can fake success)", rel, line))
            elif name.startswith(("__CPROVER", "__VERIFIER", "nondet_")):
                out.append(_v("CPROVER_BUILTIN", f"imports verifier hook '{name}'", rel, line))
            elif name in DANGEROUS_NAMES or _DANGEROUS_PREFIX_RE.match(name):
                out.append(_v("DANGEROUS_IMPORT", f"imports '{name}' (process/network/loader/thread/sanitizer API)",
                              rel, line))
            elif name in defined_elsewhere or name in header_idents or name in ALLOWED_IMPORTS \
                    or _COMPILER_RT_RE.match(name):
                continue
            else:
                out.append(_v("DANGEROUS_IMPORT", f"imports '{name}', which is not on the allowlist of library "
                              "functions CBMC models faithfully (a body-less function returns arbitrary values "
                              f"under CBMC); allowed: {', '.join(sorted(ALLOWED_IMPORTS))}", rel, line))
            continue
        if not (typ.isupper() or typ == "i"):
            continue  # local (static) symbols
        if typ in ("W", "V"):
            out.append(_v("SYMBOL_OVERRIDE", f"weak definition of '{name}' is not allowed", rel, line))
            continue
        if typ in ("i", "I"):
            out.append(_v("SYMBOL_OVERRIDE", f"indirect function (ifunc) '{name}' is not allowed", rel, line))
            continue
        why = _forbidden_definition(name)
        if why:
            out.append(_v("SYMBOL_OVERRIDE", f"defines external symbol {name}: {why}", rel, line))
        elif name not in allowed_defs:
            out.append(_v("SYMBOL_OVERRIDE", f"defines external symbol '{name}', which the protected headers do "
                          "not declare; make it static", rel, line))
    return out


def section_violations(rel: str, readelf_output: str, src: CSource | None = None) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    for name in dict.fromkeys(_SECTION_RE.findall(readelf_output)):
        if _BAD_SECTION_RE.match(name):
            line = next(
                (ln for w in ("constructor", "destructor", "section", "init_array") if (ln := _first_mention(src, w))),
                None,
            )
            out.append(_v("CONSTRUCTOR", f"object has section {name}: code would run before/after the function "
                          "under test (constructors/destructors)", rel, line))
    return out


# =========================================================================== the gate
def _find(names: tuple[str, ...]) -> str | None:
    for n in names:
        p = find_tool(n)
        if p:
            return p
    return None


_STACK_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):\d+: (?:error|warning): (?P<msg>.*?)\[-W(?:error,)?-?W?(?:vla|alloca)[^\]]*\]",
    re.M,
)


def run_integrity(ctx: BackendContext) -> StageResult:
    """Mandatory source-integrity gate; see the module docstring."""
    t0 = time.monotonic()
    spec = ctx.spec
    snap = Path(os.path.realpath(ctx.snapshot_dir))
    obj_root = ctx.build_dir / "obj"
    raw_root = ctx.raw_dir / STAGE
    raw_root.mkdir(parents=True, exist_ok=True)
    nm = _find(("llvm-nm-18", "llvm-nm", "nm"))
    readelf = _find(("llvm-readelf-18", "llvm-readelf", "readelf"))
    details: dict = {
        "tools": {"clang": ctx.tools.clang, "nm": nm, "readelf": readelf},
        "rules": sorted(RULES),
        "allowed_imports": sorted(ALLOWED_IMPORTS),
        "files": [],
        "violations": 0,
    }
    invocations = []
    problems: list[str] = []
    timeouts: list[str] = []
    limits = Limits(timeout_s=ctx.timeout(60), max_output_bytes=16 * 1024 * 1024)
    violations: list[PolicyViolation] = []
    raw_refs: dict[tuple[str, str], str] = {}

    headers: dict[str, str] = {}
    for h in spec.sources.headers:
        p = snap / h
        if p.is_file():
            headers[h] = p.read_text(encoding="utf-8", errors="replace")
        else:
            problems.append(f"protected header {h} is missing from the snapshot")
    header_idents, _ = _header_facts(headers)

    def run(tool: str, argv: list[str], rel: str, what: str) -> str | None:
        proc = ctx.run(argv, limits=limits, cwd=snap)
        invocations.append(proc.to_invocation(tool, ctx.run_dir, ctx.log_name(STAGE, f"{what}-{rel}")))
        raw = raw_root / f"{what}-{rel.replace('/', '_')}.txt"
        raw.write_text(redact(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")),
                       encoding="utf-8")
        raw_refs[(rel, what)] = ctx.relrun(raw)
        if proc.timed_out:
            timeouts.append(f"{what} of {rel} timed out")
            return None
        if not proc.ok or proc.output_truncated:
            reason = proc.error or ("output truncated" if proc.output_truncated else f"exit {proc.exit_code}")
            problems.append(f"{what} of {rel} failed ({reason}): {redact(proc.stderr.strip())[:300]}")
            return None
        return proc.stdout

    per_file: dict[str, dict] = {}
    for rel in spec.sources.editable:
        entry: dict = {"source": rel, "checks": [], "argv": {}}
        details["files"].append(entry)
        path = snap / rel
        if not path.is_file():
            problems.append(f"editable source {rel} is missing from the snapshot")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        src = CSource(text)
        per_file[rel] = {"src": src, "entry": entry}
        violations += source_violations(spec, rel, text, headers=headers)
        entry["checks"].append("source")

        if ctx.tools.clang:
            argv = [
                ctx.tools.clang,
                f"-std={spec.language.standard}",
                *spec.compile.flags,
                *[d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines],
                *ctx.include_args(),
                "-E",
                rel,
            ]
            entry["argv"]["preprocess"] = argv
            pp = run("clang", argv, rel, "preprocess")
            if pp is not None:
                violations += preprocessed_violations(rel, pp)
                entry["checks"].append("preprocessed")
            # Semantic check the preprocessor cannot do: VLAs (also those hidden
            # behind typedefs or macros) and alloca.  CBMC treats any stack
            # allocation as valid, so they must never reach a verdict.
            sargv = [
                ctx.tools.clang,
                f"-std={spec.language.standard}",
                *[d if d.startswith("-D") else f"-D{d}" for d in spec.compile.defines],
                *ctx.include_args(),
                "-fsyntax-only",
                "-fno-color-diagnostics",
                "-fno-caret-diagnostics",
                "-Wno-everything",
                "-Wvla",
                "-Walloca",
                "-Werror=vla",
                "-Werror=alloca",
                rel,
            ]
            entry["argv"]["stack"] = sargv
            proc = ctx.run(sargv, limits=limits, cwd=snap)
            invocations.append(proc.to_invocation("clang", ctx.run_dir, ctx.log_name(STAGE, f"stack-{rel}")))
            if proc.timed_out:
                timeouts.append(f"stack check of {rel} timed out")
            elif proc.error:
                problems.append(f"stack check of {rel} failed: {proc.error}")
            else:
                hits = 0
                for m in _STACK_DIAG_RE.finditer(proc.stderr):
                    hits += 1
                    line = int(m.group("line"))
                    violations.append(
                        _v("UNBOUNDED_STACK", f"{m.group('msg').strip()} (CBMC cannot model stack exhaustion)",
                           m.group("file") if m.group("file") == rel else rel, line)
                    )
                if proc.exit_code not in (0, None) and not hits:
                    problems.append(f"stack check of {rel} failed (exit {proc.exit_code}): "
                                    f"{redact(proc.stderr.strip())[:300]}")
                entry["checks"].append("stack")
        else:
            problems.append("clang not found: cannot check the preprocessed source")

        obj = obj_root / (rel[:-2] + ".o" if rel.endswith(".c") else rel + ".o")
        if not obj.is_file():
            problems.append(f"object file for {rel} not found ({obj}); the compile gate must run first")
            continue
        real_obj = os.path.realpath(obj)
        if nm:
            argv = [nm, "-P", real_obj]
            entry["argv"]["nm"] = argv
            out = run("nm", argv, rel, "nm")
            if out is not None:
                syms = parse_nm(out)
                per_file[rel]["symbols"] = syms
                entry["defined"] = sorted(n for n, t in syms if t.isupper() and t != "U")
                entry["undefined"] = sorted(n for n, t in syms if t in ("U", "w", "v"))
                entry["checks"].append("symbols")
        else:
            problems.append("nm / llvm-nm not found: cannot check object symbols")
        if readelf:
            argv = [readelf, "-S", "-W", real_obj]
            entry["argv"]["readelf"] = argv
            out = run("readelf", argv, rel, "sections")
            if out is not None:
                entry["sections"] = [s for s in dict.fromkeys(_SECTION_RE.findall(out)) if s != "NULL"]
                violations += section_violations(rel, out, src)
                entry["checks"].append("sections")
        else:
            problems.append("readelf / llvm-readelf not found: cannot check object sections")

    # symbol rules need every object's definitions (calls between editable files are fine)
    all_defined: dict[str, set[str]] = {
        rel: {n for n, t in info.get("symbols", []) if t.isupper() and t not in ("U",)} for rel, info in per_file.items()
    }
    target_defined = any(
        (spec.target.function, "T") in info.get("symbols", []) for info in per_file.values()
    )
    for rel, info in per_file.items():
        if "symbols" not in info:
            continue
        elsewhere = set().union(*(d for r, d in all_defined.items() if r != rel)) if len(all_defined) > 1 else set()
        violations += check_object_symbols(
            spec, rel, info["symbols"], header_idents=header_idents, defined_elsewhere=elsewhere, src=info["src"]
        )
    if per_file and all("symbols" in i for i in per_file.values()) and not target_defined:
        violations.append(_v("MISSING_TARGET", f"no editable object defines the target function "
                             f"'{spec.target.function}' (as a global function)", None, None))

    # report
    order = {r: k for k, r in enumerate(RULES)}
    uniq: dict[tuple, PolicyViolation] = {}
    for v in violations:
        uniq.setdefault((v.rule, v.file, v.line) if v.line else (v.rule, v.file, v.message), v)
    vs = sorted(uniq.values(), key=lambda v: (v.file or "", v.line or 0, order.get(v.rule, 99), v.message))
    details["violations"] = len(vs)
    details["problems"] = problems + timeouts
    diagnostics: list[Diagnostic] = []
    failures: list[FailureReport] = []
    seen_ids: set[str] = set()
    for v in vs:
        loc = Location(file=v.file, line=v.line) if v.file else None
        diagnostics.append(Diagnostic(tool=TOOL, rule_id=v.rule, severity="error", message=v.message, location=loc))
        base = f"{STAGE}:{v.rule}:{v.file or '-'}:{v.line or 0}"
        fid, n = base, 2
        while fid in seen_ids:
            fid, n = f"{base}#{n}", n + 1
        seen_ids.add(fid)
        failures.append(
            FailureReport(
                id=fid,
                kind="policy",
                tool=TOOL,
                property_id=v.rule,
                description=v.message,
                location=loc,
                explanation=RULES.get(v.rule, ""),
                raw_ref=raw_refs.get((v.file or "", "nm")) if v.rule in ("SYMBOL_OVERRIDE", "DANGEROUS_IMPORT") else None,
            )
        )

    def result(outcome: Outcome, summary: str, status: Status | None) -> StageResult:
        return StageResult(
            stage=STAGE,
            outcome=outcome,
            status=status,
            summary=summary,
            diagnostics=diagnostics,
            failures=failures,
            invocations=invocations,
            details=details,
            duration_s=round(time.monotonic() - t0, 3),
        )

    if vs:
        rules = ", ".join(dict.fromkeys(v.rule for v in vs))
        incomplete = f"; some checks could not run: {'; '.join(problems + timeouts)}" if problems or timeouts else ""
        return result(
            Outcome.FAIL,
            f"{len(vs)} source-integrity violation(s) ({rules}): the code under test tampers with or diverges "
            f"between the verification tools{incomplete}",
            Status.STATIC_ANALYSIS_WARNING,
        )
    if timeouts:
        return result(Outcome.TIMEOUT, "; ".join(timeouts), outcome_to_status(STAGE, Outcome.TIMEOUT))
    if problems:
        return result(Outcome.ERROR, "source-integrity checks could not run: " + "; ".join(problems),
                      outcome_to_status(STAGE, Outcome.ERROR))
    checked = ", ".join(f"{f['source']} ({'/'.join(f['checks'])})" for f in details["files"])
    return result(Outcome.PASS, f"source integrity OK: {checked}", None)
