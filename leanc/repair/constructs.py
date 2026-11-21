from __future__ import annotations

import re
from dataclasses import dataclass

from leanc.lang.c_lexer import BIDI_RE, CSource

NO_OFFSET = -1
MAX_PRAGMA_NESTING = 3
SHOWN_HEADER_NAME_CHARS = 40
HARNESS_HEADER = "leanc.h"
ASSERT_CALL = "assert()"
STATIC_ASSERT = "static_assert"

_HORIZONTAL_WS = r"[^\S\n]*"


@dataclass(frozen=True)
class Finding:
    rule: str
    construct: str
    line: int
    normalized_line: str
    shown_line: str
    offset: int = NO_OFFSET


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
    (
        "ASSERT_REMOVED",
        re.compile(rf"^{_HORIZONTAL_WS}#{_HORIZONTAL_WS}define{_HORIZONTAL_WS}NDEBUG\b", re.MULTILINE),
        "assertions disabled",
    ),
    (
        "ASSERT_REMOVED",
        re.compile(rf"^{_HORIZONTAL_WS}#{_HORIZONTAL_WS}(?:undef|define){_HORIZONTAL_WS}assert\b", re.MULTILINE),
        "assert redefined",
    ),
]

DANGEROUS_FUNCTIONS = (
    "system", "popen", "execl", "execle", "execlp", "execv", "execve", "execvp", "execvpe", "execveat",
    "fexecve", "fork", "vfork", "posix_spawn", "posix_spawnp", "socket", "socketpair", "connect",
    "dlopen", "dlmopen", "dlsym", "ptrace", "syscall",
)
_DANGEROUS_ALTERNATIVES = "|".join(DANGEROUS_FUNCTIONS)
_DANGEROUS_CALL_RE = re.compile(rf"(?<![\w.])(?<!->)\b({_DANGEROUS_ALTERNATIVES})(?=\s*\()")
_DANGEROUS_FUNCTION_AS_VALUE_RE = re.compile(rf"(?:[=&,(:?]|\breturn\b)\s*\b({_DANGEROUS_ALTERNATIVES})\b(?=\s*[;,)])")
_DANGEROUS_WORD_RE = re.compile(rf"\b({_DANGEROUS_ALTERNATIVES})\b")
_DEFINE_RE = re.compile(
    rf"^{_HORIZONTAL_WS}#{_HORIZONTAL_WS}define{_HORIZONTAL_WS}(\w+)(\([^)\n]*\))?(.*)$", re.MULTILINE
)

_PRAGMA_DIRECTIVE_RE = re.compile(rf"^{_HORIZONTAL_WS}#{_HORIZONTAL_WS}pragma\b(.*)$", re.MULTILINE)
_PRAGMA_OPERATOR_RE = re.compile(r"\b(_Pragma|__pragma)\s*\(")
_PRAGMA_STRING_ARGUMENT_RE = re.compile(r'\(\s*(?:L|u8|u|U)?"((?:[^"\\\n]|\\.)*)"\s*\)')
_INCLUDE_RE = re.compile(rf"^{_HORIZONTAL_WS}#{_HORIZONTAL_WS}(include_next|include|import|embed)\b", re.MULTILINE)
_HEADER_NAME_RE = re.compile(r'^(<[^>\n]*>|"[^"\n]*")')
_PATH_SEPARATORS_RE = re.compile(r"[\\/]+")
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")
_GNU_OR_MS_ATTRIBUTE_RE = re.compile(r"\b(__attribute__|__declspec)\s*\(")
_STD_ATTRIBUTE_RE = re.compile(r"\[\s*\[")
_SUPPRESS_ATTRIBUTE_RE = re.compile(r"\bclang\s*::\s*suppress\b")
_ASSUME_ATTRIBUTE_RE = re.compile(r"\b(?:\w+\s*::\s*)?_*(assume|assume_aligned|nonnull|returns_nonnull)_*\b")
_ALIAS_OF_VERIFIER_NAME_RE = re.compile(r'\b_*(?:alias|weakref)_*\s*\(\s*"\s*(__CPROVER\w*|LEANC_\w*)')
_TRIGRAPH_RE = re.compile(r"\?\?[=/'()!<>\-]")
_ASSERTION_PATTERNS: dict[str, re.Pattern[str]] = {
    ASSERT_CALL: re.compile(r"(?<![\w.])(?<!->)\bassert(?=\s*\()"),
    STATIC_ASSERT: re.compile(r"\b(?:static_assert|_Static_assert)(?=\s*\()"),
}

_SPACING_AROUND_PUNCTUATION_RE = re.compile(r"\s*([^\w\s])\s*")
_CBMC_PRAGMA_RE = re.compile(r"^CPROVER\b\s*(\w*)")
_COMPILER_DIAGNOSTIC_PRAGMA_RE = re.compile(r"^(clang|GCC)\s+(diagnostic|system_header)\b")
_BARE_DIAGNOSTIC_PRAGMA_RE = re.compile(r"^(system_header|warning)\b")


def _spacing_insensitive(spelling: str) -> str:
    return _SPACING_AROUND_PUNCTUATION_RE.sub(r"\1", " ".join(spelling.split()))


def _classify_pragma(body: str) -> list[tuple[str, str]]:
    words = " ".join(body.split())
    classified: list[tuple[str, str]] = []
    cbmc = _CBMC_PRAGMA_RE.match(words)
    if cbmc:
        classified.append(("CPROVER_BUILTIN", f"CBMC pragma '{('#pragma CPROVER ' + cbmc.group(1)).strip()}'"))
    compiler = _COMPILER_DIAGNOSTIC_PRAGMA_RE.match(words)
    if compiler:
        classified.append(
            ("WARNING_SUPPRESSION", f"diagnostic pragma '#pragma {compiler.group(1)} {compiler.group(2)}'")
        )
    bare = _BARE_DIAGNOSTIC_PRAGMA_RE.match(words)
    if bare:
        classified.append(("WARNING_SUPPRESSION", f"diagnostic pragma '#pragma {bare.group(1)}'"))
    return classified


def _end_of_line(code: str, start: int) -> int:
    end = code.find("\n", start)
    return len(code) if end < 0 else end


class _Collector:
    def __init__(self, src: CSource):
        self.src = src
        self.findings: list[Finding] = []

    def add(self, rule: str, construct: str, offset: int) -> None:
        line = self.src.line_of(offset)
        self.findings.append(
            Finding(rule, construct, line, self.src.line_text(offset), self.src.shown_line(line), offset)
        )

    def add_raw(self, rule: str, construct: str, raw_offset: int) -> None:
        line = self.src.raw_line_of(raw_offset)
        shown = self.src.shown_line(line)
        self.findings.append(Finding(rule, construct, line, shown, shown))


def _code_rule_findings(found: _Collector) -> None:
    for rule, pattern, what in _CODE_RULES:
        for match in pattern.finditer(found.src.code):
            spelling = match.group()
            first_non_blank = match.start() + len(spelling) - len(spelling.lstrip())
            found.add(rule, f"{what} '{_spacing_insensitive(spelling)}'", first_non_blank)


def _dangerous_function_findings(found: _Collector) -> None:
    for pattern in (_DANGEROUS_CALL_RE, _DANGEROUS_FUNCTION_AS_VALUE_RE):
        for match in pattern.finditer(found.src.code):
            found.add("DANGEROUS_CALL", f"{match.group(1)}()", match.start(1))


def _macro_definition_findings(found: _Collector, target: str | None) -> None:
    for define in _DEFINE_RE.finditer(found.src.code):
        for word in _DANGEROUS_WORD_RE.finditer(define.group(3)):
            found.add("DANGEROUS_CALL", f"{word.group(1)}()", define.start(3) + word.start())
        if target and define.group(1) == target:
            found.add("TRIVIALIZED_TARGET", f"target function '{target}' redefined as a macro", define.start(1))


def _pragma_directive_findings(found: _Collector) -> None:
    src = found.src
    for directive in _PRAGMA_DIRECTIVE_RE.finditer(src.code):
        for rule, construct in _classify_pragma(src.nocomment[directive.start(1) : directive.end(1)]):
            found.add(rule, construct, directive.start())


def _destringized_pragma_argument(nocomment: str, open_paren: int) -> str | None:
    argument = _PRAGMA_STRING_ARGUMENT_RE.match(nocomment, open_paren)
    if argument is None:
        return None
    return argument.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _pragma_operator_findings(found: _Collector, target: str | None, depth: int) -> None:
    src = found.src
    scanned_until = -1
    for operator in _PRAGMA_OPERATOR_RE.finditer(src.code):
        if operator.start() < scanned_until:
            continue
        open_paren = operator.end() - 1
        if operator.group(1) == "_Pragma":
            body = _destringized_pragma_argument(src.nocomment, open_paren)
            if body is None:
                found.add("OBFUSCATION", "_Pragma with a non-literal argument", operator.start())
                continue
        else:
            close = src.pairs.get(open_paren)
            if close is None:
                continue
            scanned_until = close
            body = src.nocomment[operator.end() : close]
        if depth > MAX_PRAGMA_NESTING:
            continue
        for inner in _scan(CSource("#pragma " + body), target, depth + 1):
            found.add(inner.rule, inner.construct, operator.start())


def _is_absolute_header_name(name: str) -> bool:
    return name.startswith(("/", "\\")) or bool(_DRIVE_LETTER_RE.match(name))


def _include_findings(found: _Collector) -> None:
    src = found.src
    for directive in _INCLUDE_RE.finditer(src.code):
        rest = src.nocomment[directive.end() : _end_of_line(src.code, directive.end())].strip()
        header = _HEADER_NAME_RE.match(rest)
        kind = directive.group(1)
        if not header:
            found.add(
                "INCLUDE_ESCAPE",
                f"computed #{kind} (header name not a literal: {rest[:SHOWN_HEADER_NAME_CHARS]!r})",
                directive.start(),
            )
            continue
        spelled = header.group(1)
        name = spelled[1:-1].strip()
        components = [part for part in _PATH_SEPARATORS_RE.split(name) if part]
        if _is_absolute_header_name(name):
            found.add("INCLUDE_ESCAPE", f"#{kind} with absolute path {spelled}", directive.start())
        elif ".." in components:
            found.add("INCLUDE_ESCAPE", f"#{kind} with '..' in {spelled}", directive.start())
        if components and components[-1] == HARNESS_HEADER:
            found.add("CPROVER_BUILTIN", f"#include of the harness header {HARNESS_HEADER}", directive.start())


def _attribute_end_or_end_of_line(src: CSource, open_paren: int) -> int:
    close = src.pairs.get(open_paren)
    return _end_of_line(src.code, open_paren + 1) if close is None else close


def _gnu_attribute_findings(found: _Collector) -> None:
    src = found.src
    scanned_until = -1
    for attribute in _GNU_OR_MS_ATTRIBUTE_RE.finditer(src.code):
        if attribute.start() < scanned_until:
            continue
        close = _attribute_end_or_end_of_line(src, attribute.end() - 1)
        scanned_until = close
        for assume in _ASSUME_ATTRIBUTE_RE.finditer(src.code, attribute.end(), close):
            found.add("ASSUMPTION_INJECTION", f"'{assume.group(1)}' attribute", attribute.start())
        for alias in _ALIAS_OF_VERIFIER_NAME_RE.finditer(src.nocomment, attribute.end(), close):
            found.add("CPROVER_BUILTIN", f"alias of {alias.group(1)}", attribute.start())


def _std_attribute_findings(found: _Collector) -> None:
    src = found.src
    scanned_until = -1
    for attribute in _STD_ATTRIBUTE_RE.finditer(src.code):
        outer_bracket_partner = src.pairs.get(attribute.start())
        if attribute.start() < scanned_until or outer_bracket_partner is None or outer_bracket_partner <= attribute.end():
            continue
        scanned_until = outer_bracket_partner
        for assume in _ASSUME_ATTRIBUTE_RE.finditer(src.code, attribute.end(), outer_bracket_partner):
            found.add("ASSUMPTION_INJECTION", f"'{assume.group(1)}' attribute", attribute.start())
        if _SUPPRESS_ATTRIBUTE_RE.search(src.code, attribute.end(), outer_bracket_partner):
            found.add("WARNING_SUPPRESSION", "[[clang::suppress]]", attribute.start())


def _phase_one_trigraph_findings(found: _Collector) -> None:
    for trigraph in _TRIGRAPH_RE.finditer(found.src.raw):
        found.add_raw("OBFUSCATION", f"trigraph {trigraph.group()!r}", trigraph.start())


def _trojan_source_bidi_findings(found: _Collector) -> None:
    for control in BIDI_RE.finditer(found.src.raw):
        found.add_raw("OBFUSCATION", f"bidirectional control character U+{ord(control.group()):04X}", control.start())


def _without_duplicate_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, int]] = set()
    unique: list[Finding] = []
    for finding in findings:
        if finding.offset != NO_OFFSET:
            identity = (finding.rule, finding.construct, finding.offset)
            if identity in seen:
                continue
            seen.add(identity)
        unique.append(finding)
    return unique


def _scan(src: CSource, target: str | None, depth: int) -> list[Finding]:
    found = _Collector(src)
    _code_rule_findings(found)
    _dangerous_function_findings(found)
    _macro_definition_findings(found, target)
    _pragma_directive_findings(found)
    _pragma_operator_findings(found, target, depth)
    _include_findings(found)
    _gnu_attribute_findings(found)
    _std_attribute_findings(found)
    _phase_one_trigraph_findings(found)
    _trojan_source_bidi_findings(found)
    return _without_duplicate_findings(found.findings)


def scan_constructs(src: CSource, target: str | None) -> list[Finding]:
    return _scan(src, target, 0)


def find_assertions(src: CSource) -> dict[str, list[Finding]]:
    found = _Collector(src)
    by_construct: dict[str, list[Finding]] = {}
    for construct, pattern in _ASSERTION_PATTERNS.items():
        start = len(found.findings)
        for match in pattern.finditer(src.code):
            found.add("ASSERT_REMOVED", construct, match.start())
        by_construct[construct] = found.findings[start:]
    return by_construct
