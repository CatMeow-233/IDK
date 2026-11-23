from __future__ import annotations

import posixpath
import re
from typing import NamedTuple

from leanc.lang.c_lexer import CSource
from leanc.rules.findings import Finding
from leanc.specs.model import Spec

__all__ = [
    "AFTER_EXPANSION",
    "C_HEADER_SUFFIX",
    "MACRO_DEFINITION_DIRECTIVES",
    "TOOL_DEPENDENT_PREDEFINED_MACRO_RE",
    "UNCHECKED_DIRECTIVES",
    "Directive",
    "IncludedHeader",
    "assertion_predicate_findings",
    "computed_include_findings",
    "defined_macro",
    "defined_macro_names",
    "divergent_macro_findings",
    "expanded_pragma_findings",
    "expanded_view",
    "has_linemarker_for",
    "identifiers",
    "include_path_problem",
    "included_header",
    "irregular_directive_findings",
    "is_irregular",
    "is_tool_specific_macro",
    "normalized_header_path",
    "parse_directives",
    "pragma_directive_findings",
    "pragma_operator_findings",
    "redefinition_findings",
    "tool_specific_identifiers",
]

AFTER_EXPANSION = " (after macro expansion)"
PRAGMA_ONCE = "once"
LINE_CONTROL_DIRECTIVE = "line"
MACRO_DEFINITION_DIRECTIVES = frozenset({"define", "undef"})
UNCHECKED_DIRECTIVES = frozenset({"error", "warning", "else", "endif"})
_CONDITIONAL_TEST_DIRECTIVES = frozenset({"if", "elif"})
_INCLUDE_ESCAPE_DIRECTIVES = frozenset({"include_next", "import", "embed"})
_STANDARD_DIRECTIVES = frozenset(
    {"include", "define", "undef", "if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif",
     "error", "warning", "pragma", "line"}
)
_QUOTED_DIRECTIVE_CHARS = 40
_QUOTED_PRAGMA_CHARS = 60
_HARNESS_HEADER = "leanc.h"
C_HEADER_SUFFIX = ".h"

_HORIZONTAL_SPACE = r"[^\S\n]*"
_DIRECTIVE_RE = re.compile(rf"^{_HORIZONTAL_SPACE}#{_HORIZONTAL_SPACE}(\w*)(.*)$", re.MULTILINE)
_EXPANDED_PRAGMA_RE = re.compile(rf"^{_HORIZONTAL_SPACE}#{_HORIZONTAL_SPACE}pragma\b(.*)$", re.MULTILINE)
_HEADER_NAME_RE = re.compile(r'^(<[^>\n]*>|"[^"\n]*")')
_DEFINE_HEAD_RE = re.compile(r"^\s*(\w+)(\([^)\n]*\))?(.*)$")
_PRAGMA_OPERATOR_RE = re.compile(r"\b_Pragma\s*\(")
_PRAGMA_OPERATOR_WORD_RE = re.compile(r"\b_Pragma\b")
_PLAIN_STRING_ARGUMENT_RE = re.compile(r'\(\s*"((?:[^"\\\n]|\\.)*)"\s*\)')
_TOOL_SPECIFIC_IDENT_RE = re.compile(r"\b(__\w+|_[A-Z]\w*|LEANC_\w*|NDEBUG)\b")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_LINEMARKER_RE = re.compile(r'^#\s*(\d+)\s+"((?:[^"\\]|\\.)*)"')
_LINEMARKER_ESCAPE_RE = re.compile(r'\\(\\|"|t|n|[0-7]{3})')
_LINEMARKER_SIMPLE_ESCAPES = {"\\": b"\\", '"': b'"', "t": b"\t", "n": b"\n"}
OCTAL_BASE = 8
FILE_NAME_ENCODING = "utf-8"
FILE_NAME_ERRORS = "surrogateescape"
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")

TOOL_DEPENDENT_PREDEFINED_MACRO_RE = re.compile(
    r"\b(__clang\w*|__GNUC\w*|__OPTIMIZE\w*|__llvm\w*|__TIME__|__DATE__|__TIMESTAMP__|__COUNTER__|"
    r"__INCLUDE_LEVEL__|__FILE__|__BASE_FILE__|__FILE_NAME__|__VERSION__|__SANITIZE_\w+|__has_\w+|__is_identifier|"
    r"__NO_INLINE__|__STDC_HOSTED__)\b"
)


def identifiers(text: str) -> set[str]:
    return set(_IDENTIFIER_RE.findall(text))


class Directive(NamedTuple):
    name: str
    rest: str
    offset: int
    rest_start: int
    rest_end: int

    def body(self, src: CSource) -> str:
        return src.nocomment[self.rest_start : self.rest_end]


def parse_directives(code: str) -> list[Directive]:
    return [Directive(m.group(1), m.group(2), m.start(), m.start(2), m.end(2)) for m in _DIRECTIVE_RE.finditer(code)]


def defined_macro(directive: Directive) -> str:
    head = _DEFINE_HEAD_RE.match(directive.rest)
    return head.group(1) if head else ""


def defined_macro_names(code: str) -> set[str]:
    return {
        head.group(1)
        for directive in parse_directives(code)
        if directive.name == "define" and (head := _DEFINE_HEAD_RE.match(directive.rest))
    }


def tool_specific_identifiers(directive: Directive) -> list[str]:
    return _TOOL_SPECIFIC_IDENT_RE.findall(directive.rest)


def is_tool_specific_macro(macro: str) -> bool:
    return bool(_TOOL_SPECIFIC_IDENT_RE.fullmatch(macro))


def is_irregular(directive: Directive) -> bool:
    return directive.name == LINE_CONTROL_DIRECTIVE or directive.name not in _STANDARD_DIRECTIVES


def irregular_directive_findings(directive: Directive) -> list[Finding]:
    name, at = directive.name, directive.offset
    if name == "":
        junk = directive.rest.strip()
        if not junk:
            return []
        return [Finding("TOOL_DIVERGENCE", f"malformed directive `#{junk[:_QUOTED_DIRECTIVE_CHARS]}`", at)]
    if name.isdigit():
        return [Finding("LINE_DIRECTIVE", ("GNU linemarkers re-attribute code (e.g. mark it as a system header, "
                        "which hides analyzer findings) and are not allowed"), at)]
    if name == LINE_CONTROL_DIRECTIVE:
        return [Finding("LINE_DIRECTIVE", "#line re-attributes code to other files/lines and is not allowed", at)]
    if name in _INCLUDE_ESCAPE_DIRECTIVES:
        return [Finding("INCLUDE_ESCAPE", f"#{name} is not allowed", at)]
    return [Finding("TOOL_DIVERGENCE", f"non-standard directive #{name} (preprocessors disagree on it)", at)]


def pragma_directive_findings(src: CSource, directive: Directive, consequence: str) -> list[Finding]:
    body = " ".join(directive.body(src).split())
    if body == PRAGMA_ONCE:
        return []
    return [Finding("PRAGMA", f"`#pragma {body[:_QUOTED_PRAGMA_CHARS]}` is not allowed ({consequence})",
                    directive.offset)]


class IncludedHeader(NamedTuple):
    spelling: str

    @property
    def name(self) -> str:
        return self.spelling[1:-1].strip()

    @property
    def quoted(self) -> bool:
        return self.spelling[0] == '"'


def included_header(src: CSource, directive: Directive) -> IncludedHeader | None:
    header = _HEADER_NAME_RE.match(directive.body(src).strip())
    return IncludedHeader(header.group(1)) if header else None


def computed_include_findings(src: CSource, directive: Directive) -> list[Finding]:
    body = directive.body(src).strip()
    return [Finding("INCLUDE_ESCAPE", f"computed #include ({body[:_QUOTED_DIRECTIVE_CHARS]!r}) cannot be checked",
                    directive.offset)]


def redefinition_findings(directive: Directive, macro: str, redefined: str) -> list[Finding]:
    return [Finding("SYMBOL_OVERRIDE", f"#{directive.name} {macro}: redefining {redefined} is not allowed",
                    directive.offset)]


def divergent_macro_findings(directive: Directive, idents: list[str], consequence: str) -> list[Finding]:
    if not idents:
        return []
    uniq = ", ".join(dict.fromkeys(idents))
    return [Finding("TOOL_DIVERGENCE", (f"#{directive.name} on {uniq}: compiler-, optimiser- or verifier-specific "
                    f"macros {consequence}"), directive.offset)]


def assertion_predicate_findings(directive: Directive) -> list[Finding]:
    if directive.name in _CONDITIONAL_TEST_DIRECTIVES and "#" in directive.rest:
        return [Finding("TOOL_DIVERGENCE", (f"#{directive.name} uses an assertion predicate (#...), which "
                        "preprocessors treat differently"), directive.offset)]
    return []


def _header_path_parts(name: str) -> list[str]:
    return [p for p in name.split("/") if p not in ("", ".")]


def normalized_header_path(name: str) -> str:
    return "/".join(_header_path_parts(name))


def _non_header_files(spec: Spec) -> list[str]:
    harness = [spec.verification.harness] if spec.verification else []
    inductive = [spec.inductive.harness, spec.inductive.contracts] if spec.inductive else []
    driver = [spec.tests.driver] if spec.tests.driver else []
    return [*harness, *inductive, *spec.sources.editable] + driver


def _protected_header_spellings(spec: Spec) -> set[str]:
    spellings: set[str] = set()
    for header in spec.sources.headers:
        spellings.add(header)
        for include_dir in spec.sources.include_dirs:
            prefix = include_dir.rstrip("/") + "/"
            if header.startswith(prefix):
                spellings.add(header[len(prefix) :])
        spellings.add(posixpath.basename(header))
    return spellings


def include_path_problem(spec: Spec, name: str, quoted: bool) -> str | None:
    if not name:
        return "empty header name"
    if "\\" in name or name.startswith("/") or _DRIVE_LETTER_RE.match(name):
        return f"absolute or backslash header path {name!r}"
    parts = _header_path_parts(name)
    if not parts:
        return f"header path {name!r} names no file"
    if ".." in parts:
        return f"'..' in header path {name!r}"
    norm = "/".join(parts)
    if parts and parts[-1] == _HARNESS_HEADER:
        return "leanc.h is for the trusted harness only"
    for path in _non_header_files(spec):
        if norm == path or posixpath.basename(path) == parts[-1] or path.endswith("/" + norm):
            return f"{name!r} names a harness, test or source file, not a header"
    if quoted and norm not in _protected_header_spellings(spec):
        return (
            f'"{name}" is not a protected header of the spec ({", ".join(spec.sources.headers) or "none"}); '
            "use <...> for system headers"
        )
    return None


def pragma_operator_findings(src: CSource) -> list[Finding]:
    found: list[Finding] = []
    for operator in _PRAGMA_OPERATOR_RE.finditer(src.code):
        argument = _PLAIN_STRING_ARGUMENT_RE.match(src.nocomment, operator.end() - 1)
        if not argument:
            found.append(Finding("OBFUSCATION", ("_Pragma(...) whose argument is not a single plain string literal "
                                 "(e.g. built by a macro) cannot be checked"), operator.start()))
        elif argument.group(1).strip() != PRAGMA_ONCE:
            found.append(Finding("PRAGMA", (f"_Pragma(\"{argument.group(1)[:_QUOTED_PRAGMA_CHARS]}\") is not allowed "
                                 "(pragmas can disable CBMC checks or diagnostics)"), operator.start()))
    return found


def _unescaped_linemarker_file(spelling: str) -> str:
    unescaped = bytearray()
    position = 0
    for escape in _LINEMARKER_ESCAPE_RE.finditer(spelling):
        unescaped += spelling[position : escape.start()].encode(FILE_NAME_ENCODING, FILE_NAME_ERRORS)
        code = escape.group(1)
        unescaped += _LINEMARKER_SIMPLE_ESCAPES.get(code) or bytes([int(code, OCTAL_BASE)])
        position = escape.end()
    unescaped += spelling[position:].encode(FILE_NAME_ENCODING, FILE_NAME_ERRORS)
    return unescaped.decode(FILE_NAME_ENCODING, FILE_NAME_ERRORS)


def has_linemarker_for(rel: str, pp_output: str) -> bool:
    for line in pp_output.split("\n"):
        marker = _LINEMARKER_RE.match(line)
        if marker and posixpath.normpath(_unescaped_linemarker_file(marker.group(2))) == posixpath.normpath(rel):
            return True
    return False


def expanded_view(rel: str, pp_output: str) -> tuple[str, list[int]]:
    lines: list[str] = []
    origin: list[int] = []
    current_file: str | None = None
    current_line = 0
    for line in pp_output.split("\n"):
        marker = _LINEMARKER_RE.match(line)
        if marker:
            current_line = int(marker.group(1))
            current_file = _unescaped_linemarker_file(marker.group(2))
            continue
        if current_file is not None and posixpath.normpath(current_file) == posixpath.normpath(rel):
            lines.append(line)
            origin.append(current_line)
        current_line += 1
    return "\n".join(lines), origin


def expanded_pragma_findings(src: CSource) -> list[Finding]:
    found: list[Finding] = []
    for pragma in _EXPANDED_PRAGMA_RE.finditer(src.code):
        body = " ".join(src.nocomment[pragma.start(1) : pragma.end(1)].split())
        if body != PRAGMA_ONCE:
            found.append(Finding("PRAGMA", f"`#pragma {body[:_QUOTED_PRAGMA_CHARS]}`{AFTER_EXPANSION} is not allowed",
                                 pragma.start()))
    for operator in _PRAGMA_OPERATOR_WORD_RE.finditer(src.code):
        found.append(Finding("PRAGMA", f"_Pragma{AFTER_EXPANSION} is not allowed", operator.start()))
    return found
