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

Known limits (documented in docs/security.md): this is a lexical check, not a
preprocessor.  Macro tricks that assemble forbidden identifiers are partly
covered (token pasting ``##`` and trigraphs are rejected as ``OBFUSCATION``)
but not exhaustively; the protected harness and the independent verifiers
remain the real line of defence.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from dataclasses import dataclass
from functools import cached_property

from leanc.core.errors import WorkspaceError
from leanc.core.models import PolicyDecision, PolicyViolation
from leanc.core.workspace import normalize_relpath
from leanc.repair.patch import changed_paths, diff_stats
from leanc.specs.model import Spec

__all__ = [
    "MAX_ANALYZED_CHARS",
    "RULES",
    "CSource",
    "FunctionDefinition",
    "check_candidate",
    "check_generated",
    "find_function_definitions",
    "is_trivial_body",
    "strip_c",
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
}

#: Files larger than this are not lexed (TOO_LARGE); real sources are far smaller.
MAX_ANALYZED_CHARS = 1_000_000

_WS = r"[^\S\n]*"  # horizontal whitespace (incl. \f, \v)

# =========================================================================== lexing
_SPLICE_RE = re.compile(r"\\[ \t\f\v]*\r?\n")
_INTERESTING_RE = re.compile(r"[/\"']")
_DIGRAPHS = {"%:%:": "##  ", "<%": "{ ", "%>": "} ", "<:": "[ ", ":>": "] ", "%:": "# "}
_DIGRAPH_RE = re.compile(r"%:%:|<%|%>|<:|:>|%:")
_COND_RE = re.compile(rf"^{_WS}#{_WS}(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$")
_CONST01_RE = re.compile(r"^[\s(]*([01])[uUlL]*[\s)]*$")


def _blank(s: str) -> str:
    """Replace everything but newlines by spaces (keeps offsets and lines)."""
    return re.sub(r"[^\n]", " ", s)


def _splice(raw: str) -> tuple[str, list[int], list[int]]:
    """Translation phase 2.  Returns the spliced text and a mapping from
    spliced offsets to original line numbers (``offs``/``lines`` checkpoints)."""
    pieces: list[str] = []
    offs, lines = [0], [1]
    line, out_len, pos = 1, 0, 0

    def note(chunk: str, base: int, line: int) -> int:
        k = chunk.find("\n")
        while k != -1:
            line += 1
            offs.append(base + k + 1)
            lines.append(line)
            k = chunk.find("\n", k + 1)
        return line

    for m in _SPLICE_RE.finditer(raw):
        chunk = raw[pos : m.start()]
        line = note(chunk, out_len, line)
        pieces.append(chunk)
        out_len += len(chunk)
        line += 1
        offs.append(out_len)
        lines.append(line)
        pos = m.end()
    chunk = raw[pos:]
    note(chunk, out_len, line)
    pieces.append(chunk)
    return "".join(pieces), offs, lines


def _lex(s: str) -> tuple[str, str]:
    """Return ``(code, nocomment)``: comments blanked in both; string and
    character literal contents additionally blanked in ``code`` (quotes kept).
    A literal that is not closed on its line ends at the newline, as in the
    compiler, so a stray quote can never hide the following lines."""
    code: list[str] = []
    noc: list[str] = []
    n = len(s)
    i = plain = 0
    while True:
        m = _INTERESTING_RE.search(s, i)
        if m is None:
            break
        i = m.start()
        c = s[i]
        if c == "/":
            nxt = s[i + 1 : i + 2]
            if nxt not in ("*", "/"):
                i += 1
                continue
            if nxt == "*":
                j = s.find("*/", i + 2)
                end = n if j < 0 else j + 2
            else:
                j = s.find("\n", i + 2)
                end = n if j < 0 else j
            code.append(s[plain:i])
            noc.append(s[plain:i])
            b = _blank(s[i:end])
            code.append(b)
            noc.append(b)
            i = plain = end
            continue
        j = i + 1
        closed = False
        while j < n:
            ch = s[j]
            if ch == "\\":
                j += 2
                continue
            if ch == c:
                j += 1
                closed = True
                break
            if ch == "\n":
                break
            j += 1
        j = min(j, n)
        lit = s[i:j]
        code.append(s[plain:i])
        noc.append(s[plain:i])
        inner = lit[1:-1] if closed else lit[1:]
        code.append(c + " " * len(inner) + (c if closed else ""))
        noc.append(lit)
        i = plain = j
    code.append(s[plain:])
    noc.append(s[plain:])
    return "".join(code), "".join(noc)


def _const01(expr: str) -> int | None:
    m = _CONST01_RE.match(expr)
    return int(m.group(1)) if m else None


def _blank_dead_branches(code: str, noc: str) -> tuple[str, str]:
    """Blank ``#if 0`` regions (and the ``#else`` of ``#if 1`` / after an
    ``#elif 1``): the compiler never sees that code, so it must neither count
    as present (an assert moved into ``#if 0`` is a removed assert) nor
    trigger content rules.  Other conditionals are treated as live."""
    code_lines = code.split("\n")
    noc_lines = noc.split("\n")
    stack: list[list[bool]] = []  # [skipping, a branch is certainly taken]
    for idx, line in enumerate(code_lines):
        m = _COND_RE.match(line)
        if m:
            d, rest = m.group(1), m.group(2)
            if d in ("if", "ifdef", "ifndef"):
                outer = any(f[0] for f in stack)
                v = _const01(rest) if d == "if" else None
                stack.append([v == 0, v == 1])
            elif stack:
                outer = any(f[0] for f in stack[:-1])
                top = stack[-1]
                if d == "endif":
                    stack.pop()
                elif top[1]:
                    top[0] = True
                elif d == "else":
                    top[0], top[1] = False, True
                else:
                    v = _const01(rest) if d == "elif" else None
                    top[0], top[1] = v == 0, v == 1
            else:
                outer = False
            dead = outer
        else:
            dead = any(f[0] for f in stack)
        if dead:
            code_lines[idx] = _blank(line)
            noc_lines[idx] = _blank(noc_lines[idx])
    return "\n".join(code_lines), "\n".join(noc_lines)


class CSource:
    """Lexically cleaned views of one C file.

    * ``text``: the file after line splicing;
    * ``code``: comments and literal contents blanked, digraphs canonical,
      dead ``#if 0`` regions blanked - what the content rules match against;
    * ``nocomment``: like ``code`` but with literals kept (header names,
      ``_Pragma`` strings, attribute arguments).

    All three have the same length and line structure, so an offset found
    in one view is valid in the others; :meth:`line_of` maps it back to the
    line number in the original (unspliced) file.
    """

    def __init__(self, raw: str):
        self.raw = raw
        self.text, self._offs, self._lines = _splice(raw)
        code, noc = _lex(self.text)
        code = _DIGRAPH_RE.sub(lambda m: _DIGRAPHS[m.group()], code)
        self.code, self.nocomment = _blank_dead_branches(code, noc)

    def line_of(self, offset: int) -> int:
        return self._lines[bisect.bisect_right(self._offs, offset) - 1]

    def line_text(self, offset: int) -> str:
        """Whitespace-normalised code line containing ``offset``."""
        start = self.code.rfind("\n", 0, offset) + 1
        end = self.code.find("\n", offset)
        return " ".join(self.code[start : len(self.code) if end < 0 else end].split())

    @cached_property
    def code_nodirectives(self) -> str:
        """``code`` with preprocessor directive lines blanked (brace matching
        must not be confused by ``#define BEGIN {``)."""
        return "\n".join(_blank(ln) if ln.lstrip().startswith("#") else ln for ln in self.code.split("\n"))


def strip_c(text: str) -> str:
    """Comment- and literal-free view of C source (same line structure);
    see :class:`CSource`."""
    return CSource(text).code


# =========================================================================== helpers
def _match_close(s: str, open_at: int, open_ch: str = "(", close_ch: str = ")") -> int | None:
    depth = 0
    for k in range(open_at, len(s)):
        ch = s[k]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return None


@dataclass(frozen=True)
class _Hit:
    rule: str
    key: str  # the construct (counted separately per key)
    line: int
    text: str  # normalised line text, to tell new occurrences from old ones
    offset: int = -1


@dataclass(frozen=True)
class FunctionDefinition:
    file: str
    line: int
    body: str  # code view of the body between the braces
    trivial: bool


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
    ("ASSERT_REMOVED", re.compile(rf"^{_WS}#{_WS}define{_WS}NDEBUG\b", re.M), "assertions disabled"),
    ("ASSERT_REMOVED", re.compile(rf"^{_WS}#{_WS}(?:undef|define){_WS}assert\b", re.M), "assert redefined"),
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
_DEFINE_RE = re.compile(rf"^{_WS}#{_WS}define{_WS}(\w+)(\([^)\n]*\))?(.*)$", re.M)
_DANGER_WORD_RE = re.compile(rf"\b({_DANGER_ALT})\b")

_PRAGMA_RE = re.compile(rf"^{_WS}#{_WS}pragma\b(.*)$", re.M)
_PRAGMA_OP_RE = re.compile(r"\b(_Pragma|__pragma)\s*\(")
_PRAGMA_STR_RE = re.compile(r'\(\s*(?:L|u8|u|U)?"((?:[^"\\\n]|\\.)*)"\s*\)')
_INCLUDE_RE = re.compile(rf"^{_WS}#{_WS}(include_next|include|import|embed)\b", re.M)
_HEADER_NAME_RE = re.compile(r'^(<[^>\n]*>|"[^"\n]*")')
_ATTRIBUTE_RE = re.compile(r"\b(__attribute__|__declspec)\s*\(")
_STD_ATTRIBUTE_RE = re.compile(r"\[\s*\[(.*?)\]\s*\]", re.S)
_ASSUME_ATTR_RE = re.compile(r"\b(?:\w+\s*::\s*)?_*(assume|assume_aligned|nonnull|returns_nonnull)_*\b")
_ALIAS_ATTR_RE = re.compile(r'\b_*(?:alias|weakref)_*\s*\(\s*"\s*(__CPROVER\w*|LEANC_\w*)')
_TRIGRAPH_RE = re.compile(r"\?\?[=/'()!<>\-]")
_BIDI_RE = re.compile("[‪-‮⁦-⁩‎‏؜]")
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
        hits.append(_Hit(rule, key, src.line_of(offset), src.line_text(offset), offset))

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
    for m in _PRAGMA_OP_RE.finditer(code):
        if m.group(1) == "_Pragma":
            sm = _PRAGMA_STR_RE.match(noc, m.end() - 1)
            body = sm.group(1).replace('\\"', '"').replace("\\\\", "\\") if sm else None
        else:
            close = _match_close(noc, m.end() - 1)
            body = noc[m.end() : close] if close is not None else None
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
    for m in _ATTRIBUTE_RE.finditer(code):
        close = _match_close(code, m.end() - 1)
        body_code = code[m.end() : close if close is not None else len(code)]
        for a in _ASSUME_ATTR_RE.finditer(body_code):
            add("ASSUMPTION_INJECTION", f"'{a.group(1)}' attribute", m.start())
        for a in _ALIAS_ATTR_RE.finditer(noc[m.end() : close if close is not None else len(code)]):
            add("CPROVER_BUILTIN", f"alias of {a.group(1)}", m.start())
    for m in _STD_ATTRIBUTE_RE.finditer(code):
        for a in _ASSUME_ATTR_RE.finditer(m.group(1)):
            add("ASSUMPTION_INJECTION", f"'{a.group(1)}' attribute", m.start())
        if re.search(r"\bclang\s*::\s*suppress\b", m.group(1)):
            add("WARNING_SUPPRESSION", "[[clang::suppress]]", m.start())

    # raw-text checks: trigraphs (phase 1, active even inside literals and
    # comments under -std=c*) and Trojan-Source style bidi controls
    raw = src.raw
    for rx, what in ((_TRIGRAPH_RE, "trigraph"), (_BIDI_RE, "bidirectional control character")):
        for m in rx.finditer(raw):
            line = raw.count("\n", 0, m.start()) + 1
            ls = raw.rfind("\n", 0, m.start()) + 1
            le = raw.find("\n", m.start())
            key = f"{what} {m.group()!r}" if what == "trigraph" else f"{what} U+{ord(m.group()):04X}"
            hits.append(_Hit("OBFUSCATION", key, line, " ".join(raw[ls : len(raw) if le < 0 else le].split())))

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
            out[key].append(_Hit("ASSERT_REMOVED", key, src.line_of(m.start()), src.line_text(m.start()), m.start()))
    return out


# =========================================================================== target function
_TYPE_WORDS = (
    r"const|volatile|restrict|signed|unsigned|short|long|int|char|_Bool|bool|float|double|void|"
    r"size_t|ssize_t|ptrdiff_t|intptr_t|uintptr_t|[A-Za-z_]\w*_t|struct\s+\w+|enum\s+\w+|union\s+\w+"
)
_CAST_RE = re.compile(rf"^\(\s*(?:(?:{_TYPE_WORDS})\s*)+\**\s*\)")
_CONST_RE = re.compile(
    r"(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)[uUlLfF]*"
    r"|'[^'\n]*'|(?:L|u8|u|U)?\"[^\"\n]*\"|NULL|nullptr|true|false|[A-Z_][A-Z0-9_]+"
)
_VOID_CAST_STMT_RE = re.compile(r"^\s*(?:;|\(\s*void\s*\)\s*\(?\s*[A-Za-z_]\w*\s*\)?\s*;)")
_DEF_ATTR_RE = re.compile(r"__attribute__\s*\(|__declspec\s*\(")
_NORETURN_RE = re.compile(
    r"(?:abort|exit|_Exit|quick_exit|__builtin_trap)\s*\([^;]*\)\s*;(?:\s*return\b[^;]*;)?"
)


def _is_constant_expr(expr: str) -> bool:
    e = expr.strip()
    for _ in range(32):
        e = e.strip()
        if e.startswith("(") and _match_close(e, 0) == len(e) - 1:
            e = e[1:-1]
            continue
        m = _CAST_RE.match(e)
        if m and m.end() < len(e):
            e = e[m.end() :]
            continue
        if e[:1] in ("-", "+", "~", "!"):
            e = e[1:]
            continue
        break
    return bool(_CONST_RE.fullmatch(e.strip()))


def is_trivial_body(body: str) -> bool:
    """Heuristic: does a function body (code view, without the outer braces)
    do nothing but return a literal / macro constant (or nothing at all, or
    only call abort/exit)?  ``(void)param;`` casts and extra braces or
    semicolons are ignored."""
    s = body
    while True:
        s2 = _VOID_CAST_STMT_RE.sub("", s, count=1).strip()
        if s2.startswith("{") and _match_close(s2, 0, "{", "}") == len(s2) - 1:
            s2 = s2[1:-1]
        if s2 == s:
            break
        s = s2
    s = re.sub(r";[\s;]*$", ";", " ".join(s.split()))
    if s == "" or re.fullmatch(r"return\s*;", s):
        return True
    if _NORETURN_RE.fullmatch(s):
        return True
    m = re.fullmatch(r"return\b(.*);", s, re.S)
    return bool(m and ";" not in m.group(1) and _is_constant_expr(m.group(1)))


def find_function_definitions(src: CSource, name: str, file: str = "") -> list[FunctionDefinition]:
    """File-scope definitions (with a body) of function ``name``.

    Declarations, calls, macro-generated definitions and code in ``#if 0``
    are not definitions; the conservative consequence is that hiding the
    target behind a macro reads as "definition removed"."""
    code = src.code_nodirectives
    braces = [(k, ch) for k, ch in enumerate(code) if ch in "{}"]
    marks: list[int] = []
    depth_after: list[int] = []
    d = 0
    for k, ch in braces:
        d += 1 if ch == "{" else -1
        marks.append(k)
        depth_after.append(d)

    def depth_at(off: int) -> int:
        j = bisect.bisect_left(marks, off) - 1
        return depth_after[j] if j >= 0 else 0

    out: list[FunctionDefinition] = []
    for m in re.finditer(rf"(?<![\w.])(?<!->)\b{re.escape(name)}\s*\(", code):
        if depth_at(m.start()) > 0:
            continue
        close = _match_close(code, m.end() - 1)
        if close is None:
            continue
        j = close + 1
        while True:
            while j < len(code) and code[j].isspace():
                j += 1
            am = _DEF_ATTR_RE.match(code, j)
            if am:
                c2 = _match_close(code, am.end() - 1)
                if c2 is None:
                    break
                j = c2 + 1
                continue
            if code.startswith("[[", j):
                c2 = code.find("]]", j)
                if c2 < 0:
                    break
                j = c2 + 2
                continue
            break
        if j < len(code) and code[j] == "{":
            end = _match_close(code, j, "{", "}")
            body = code[j + 1 : end if end is not None else len(code)]
            out.append(FunctionDefinition(file, src.line_of(m.start()), body, is_trivial_body(body)))
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
    editable = spec.sources.editable
    if norm in editable:
        return None
    if norm == spec.verification.harness:
        what = "the trusted verification harness"
    elif spec.tests.driver and norm == spec.tests.driver:
        what = "the trusted test driver"
    elif norm in spec.sources.headers:
        what = "a protected interface header"
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
        out.append(PolicyViolation(rule=rule, message=f"introduces {key}{counts}: `{h.text[:120]}`", file=path, line=h.line))
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
                            f"(e.g. `{gone.text[:120]}` at old line {gone.line})"
                        ),
                        file=path,
                    )
                )
    return out


def _decision(violations: list[PolicyViolation]) -> PolicyDecision:
    order = {r: k for k, r in enumerate(RULES)}
    uniq: dict[tuple, PolicyViolation] = {}
    for v in violations:
        uniq.setdefault((v.rule, v.file, v.line, v.message), v)
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
            self._cache[path] = CSource(self.files[path])
        return self._cache[path]


def _definitions(spec: Spec, srcs: _Sources, paths: list[str]) -> list[FunctionDefinition]:
    out: list[FunctionDefinition] = []
    for p in paths:
        s = srcs.get(p)
        if s is not None:
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
    relative to ``old``.  ``max_changed_lines`` defaults to
    ``spec.repair.max_changed_lines``.
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
        violations.extend(_content_violations(p, old_srcs.get(p), new_srcs.get(p), target))  # type: ignore[arg-type]
    editable = set(spec.sources.editable)
    old_paths = sorted(p for p in old if p in editable and len(old[p]) <= MAX_ANALYZED_CHARS)
    new_paths = sorted(p for p in new if p in editable and p not in oversized)
    violations.extend(_target_violations(spec, old_srcs, new_srcs, old_paths, new_paths))
    return _decision(violations)


def check_generated(spec: Spec, files: dict[str, str]) -> PolicyDecision:
    """Check freshly generated code: the content rules as for a repair
    against empty old files, ``PATH_INVALID``/``PATH_NOT_EDITABLE`` for
    every path, ``MISSING_TARGET`` if no generated editable file defines
    ``spec.target.function``, and ``TRIVIALIZED_TARGET`` for a stub body."""
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
        violations.extend(_content_violations(p, None, srcs.get(p), target))  # type: ignore[arg-type]
    violations.extend(_target_violations(spec, None, srcs, [], content_paths))
    return _decision(violations)
