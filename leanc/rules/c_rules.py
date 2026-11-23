from __future__ import annotations

import re
from typing import NamedTuple

from leanc.foundation.models import PolicyViolation
from leanc.lang.c_lexer import CSource
from leanc.rules.acsl import ACSL_RULES, acsl_violations
from leanc.rules.c_names import (
    ALLOWED_IMPORTS,
    C_KEYWORDS,
    COMPILER_RT_ARITHMETIC_HELPER_RE,
    DANGEROUS_NAMES,
    LIBC_NAMES,
    TERMINATION_NAMES,
    forbidden_definition,
    is_dangerous_import,
    is_verifier_hook,
)
from leanc.rules.definitions import defined_functions
from leanc.rules.findings import (
    Finding,
    Problem,
    first_mention_line,
    to_violations,
    violation,
)
from leanc.rules.preprocessor import (
    AFTER_EXPANSION,
    C_HEADER_SUFFIX,
    MACRO_DEFINITION_DIRECTIVES,
    TOOL_DEPENDENT_PREDEFINED_MACRO_RE,
    UNCHECKED_DIRECTIVES,
    Directive,
    assertion_predicate_findings,
    computed_include_findings,
    defined_macro,
    defined_macro_names,
    divergent_macro_findings,
    expanded_pragma_findings,
    expanded_view,
    identifiers,
    include_path_problem,
    included_header,
    irregular_directive_findings,
    is_irregular,
    is_tool_specific_macro,
    normalized_header_path,
    parse_directives,
    pragma_directive_findings,
    pragma_operator_findings,
    redefinition_findings,
    tool_specific_identifiers,
)
from leanc.rules.stack_checks import MAX_STACK_FRAME_BYTES, STACK_FRAME_RULE
from leanc.rules.symbol_types import (
    INDIRECT_FUNCTION_TYPES,
    UNDEFINED_TYPES,
    WEAK_DEFINITION_TYPES,
    WEAK_UNDEFINED_TYPES,
    has_external_linkage,
)
from leanc.specs.model import Spec

__all__ = [
    "CBMC_TERMINATION_CONSEQUENCE",
    "RULES",
    "SECTION_RE",
    "CHeaderFacts",
    "TokenRule",
    "c_header_facts",
    "c_source_violations",
    "check_object_symbols",
    "parse_nm",
    "preprocessed_violations",
    "section_violations",
    "token_findings",
]

RULES: dict[str, str] = {
    "TOOL_DIVERGENCE": "code that can differ between the tools (conditionals/macros on compiler-, optimiser- or "
    "verifier-specific identifiers, non-standard directives, optimisation-dependent built-ins)",
    "PRAGMA": "a #pragma / _Pragma other than '#pragma once' (they can disable CBMC checks or diagnostics)",
    "LINE_DIRECTIVE": "#line or GNU linemarkers (they re-attribute code, e.g. mark it as a system header)",
    "TRIGRAPH": "trigraph sequences (??= ??/ ??' ??( ??) ??! ??< ??> ??-), comments and string literals included: "
    "clang in ISO C modes and Frama-C (gcc -std=c11) replace them while CBMC preprocesses in gcc's gnu modes "
    "without them, so a `??/` that ends a comment hides the next line from one side only",
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
    STACK_FRAME_RULE: f"a function whose stack frame exceeds {MAX_STACK_FRAME_BYTES} bytes (e.g. a huge fixed-size "
    "local array): it overflows the native stack, while CBMC has no stack limit and only runs out of memory",
    **ACSL_RULES,
}

CBMC_TERMINATION_CONSEQUENCE = "CBMC prunes such paths and the native tests can report success"
_PRAGMA_CONSEQUENCE = "pragmas can disable CBMC checks or diagnostics, or mark code as a system header"
_DIVERGENCE_CONSEQUENCE = "make clang, the sanitizer build and CBMC see different code"


_TRIGRAPH_RE = re.compile(r"\?\?[=/'()!<>-]")
_RAW_STRING_PREFIX_RE = re.compile(r'(?<!\w)(?:u8|u|U|L)?R"')
_RAW_STRING_MEANING = (
    "a raw string literal to CBMC's gcc gnu preprocessing but an identifier followed by a string literal to clang "
    "in C"
)


class TokenRule(NamedTuple):
    rule: str
    pattern: re.Pattern[str]
    what: str


_TOKEN_RULES: list[TokenRule] = [
    TokenRule("CPROVER_BUILTIN", re.compile(r"\b__CPROVER\w*"), "CBMC built-in"),
    TokenRule("CPROVER_BUILTIN", re.compile(r"\b__VERIFIER_\w+"), "SV-COMP verifier built-in"),
    TokenRule("CPROVER_BUILTIN", re.compile(r"\bnondet_\w+"), "harness-only nondeterministic input function"),
    TokenRule("CPROVER_BUILTIN", re.compile(r"\bLEANC_\w+"), "Lean C harness macro"),
    TokenRule("INLINE_ASM", re.compile(r"\b(?:asm|__asm__|__asm)\b"), "inline assembly"),
    TokenRule("ASSUMPTION_INJECTION", re.compile(r"\b__builtin_(?:unreachable|assume\w*)(?=\s*\()"),
               "assumption built-in"),
    TokenRule("ASSUMPTION_INJECTION", re.compile(r"\b__assume(?=\s*\()"), "assumption built-in"),
    TokenRule("ASSUMPTION_INJECTION", re.compile(r"\b_Noreturn\b"), "_Noreturn function"),
    TokenRule("ASSUMPTION_INJECTION", re.compile(r"(?<![\w.])(?<!->)\bunreachable(?=\s*\(\s*\))"),
               "C23 unreachable()"),
    TokenRule("TERMINATION", re.compile(r"\b__builtin_(?:trap|debugtrap|abort)(?=\s*\()"), "trapping built-in"),
    TokenRule("TOOL_DIVERGENCE", re.compile(r"\b__builtin_(?:constant_p|object_size|dynamic_object_size)(?=\s*\()"),
               "optimisation-dependent built-in"),
    TokenRule("DANGEROUS_IMPORT", re.compile(r"\b__(?:asan|lsan|ubsan|msan|tsan|hwasan|dfsan|sanitizer)_\w+"),
               "sanitizer runtime interface"),
    TokenRule("PRAGMA", re.compile(r"\b__pragma(?=\s*\()"), "MS-style __pragma"),
    TokenRule("UNBOUNDED_STACK", re.compile(r"(?<![\w.])(?<!->)\b(?:_?alloca|__builtin_alloca\w*)(?=\s*\()"),
               "stack allocation (alloca)"),
    TokenRule("TERMINATION", re.compile(r"\b__builtin_(?:setjmp|longjmp|unwind_init|eh_return)(?=\s*\()"),
               "non-local control-flow built-in"),
    TokenRule("TOOL_DIVERGENCE",
               re.compile(r"\b__builtin_(?:frame_address|return_address|extract_return_addr|stack_save|"
                          r"stack_restore)(?=\s*\()"),
               "stack/frame introspection built-in"),
]

_LIBRARY_API_ALTERNATIVES = "|".join(sorted(TERMINATION_NAMES | DANGEROUS_NAMES, key=len, reverse=True))
_LIBRARY_API_CALL_RE = re.compile(rf"(?<![\w.])(?<!->)\b({_LIBRARY_API_ALTERNATIVES}|pthread_\w+|thrd_\w+)(?=\s*\()")
_LIBRARY_API_VALUE_RE = re.compile(
    rf"(?:[=&,(:?]|\breturn\b)\s*\b({_LIBRARY_API_ALTERNATIVES}|pthread_\w+|thrd_\w+)\b(?=\s*[;,)])"
)

_ATTRIBUTE_START_RE = re.compile(r"\b(__attribute__|__attribute|__declspec)\s*\(|\[\s*\[")
_ATTRIBUTE_TOKEN_RE = re.compile(r"[()]|(?:([A-Za-z_]\w*)\s*::\s*)?([A-Za-z_]\w*)")
_DECLSPEC = "__declspec"
_DECLSPEC_LIST_DEPTH = 1
_GNU_ATTRIBUTE_LIST_DEPTH = 2
_STANDARD_ATTRIBUTE_LIST_DEPTH = 0
_SANITIZER_SUPPRESSION_PREFIX = "no_sanitize"
_ATTRIBUTE_RULES: dict[str, str] = {
    **dict.fromkeys(("constructor", "destructor", "section", "used", "retain", "init_priority"), "CONSTRUCTOR"),
    **dict.fromkeys(("weak", "weakref", "alias", "ifunc", "visibility", "selectany", "externally_visible"),
                    "SYMBOL_OVERRIDE"),
    **dict.fromkeys(("disable_sanitizer_instrumentation", "no_address_safety_analysis"), "SANITIZER_SUPPRESSION"),
    **dict.fromkeys(("optimize", "target", "target_clones"), "TOOL_DIVERGENCE"),
    "naked": "INLINE_ASM",
    **dict.fromkeys(("assume", "assume_aligned", "nonnull", "returns_nonnull", "noreturn", "const", "pure"),
                    "ASSUMPTION_INJECTION"),
    **dict.fromkeys(("suppress", "analyzer_noreturn"), "WARNING_SUPPRESSION"),
}

SECTION_RE = re.compile(r"^\s*\[\s*\d+\]\s+(\S+)", re.MULTILINE)
_CODE_OUTSIDE_TEST_SECTION_RE = re.compile(
    r"^\.(?:rela?\.)?(?:init_array|fini_array|preinit_array|ctors|dtors|init|fini)(?:\.|$)"
)
_CONSTRUCTOR_MENTIONS = ("constructor", "destructor", "section", "init_array")
_ARCHIVE_MEMBER_SUFFIX = ":"


class CHeaderFacts(NamedTuple):
    idents: set[str]
    macros: set[str]


def c_header_facts(headers: dict[str, str] | None) -> CHeaderFacts:
    facts = CHeaderFacts(set(), set())
    for text in (headers or {}).values():
        code = CSource(text).code
        facts.idents.update(identifiers(code))
        facts.macros.update(defined_macro_names(code))
    return facts


def _rule_summary(rule: str) -> str:
    return RULES[rule].split("(")[0].strip()


def _normalized_attribute_name(name: str) -> str:
    return name.strip("_") if name.startswith("__") and name.endswith("__") else name


class _AttributeList(NamedTuple):
    text: str
    list_depth: int
    start: int
    close: int


def _attribute_names(attributes: _AttributeList) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    depth = 0
    for token in _ATTRIBUTE_TOKEN_RE.finditer(attributes.text):
        if token.group() == "(":
            depth += 1
        elif token.group() == ")":
            depth -= 1
        elif depth == attributes.list_depth and token.group(2):
            out.append((_normalized_attribute_name(token.group(2)), token.start(2)))
    return out


def _keyword_attribute_list(src: CSource, start: re.Match[str]) -> _AttributeList:
    open_at = start.end() - 1
    close = src.pairs.get(open_at)
    if close is None:
        end_of_line = src.code.find("\n", open_at)
        close = len(src.code) if end_of_line < 0 else end_of_line
    depth = _DECLSPEC_LIST_DEPTH if start.group(1) == _DECLSPEC else _GNU_ATTRIBUTE_LIST_DEPTH
    return _AttributeList(src.code[open_at : close + 1], depth, open_at, close)


def _standard_attribute_list(src: CSource, start: re.Match[str]) -> _AttributeList | None:
    close = src.pairs.get(start.start())
    if close is None:
        return None
    return _AttributeList(src.code[start.end() : close], _STANDARD_ATTRIBUTE_LIST_DEPTH, start.end(), close)


def _attribute_rule(name: str) -> str | None:
    rule = _ATTRIBUTE_RULES.get(name)
    if rule is None and name.startswith(_SANITIZER_SUPPRESSION_PREFIX):
        return "SANITIZER_SUPPRESSION"
    return rule


def _attribute_findings(src: CSource, where: str) -> list[Finding]:
    out: list[Finding] = []
    covered = -1
    for start in _ATTRIBUTE_START_RE.finditer(src.code):
        if start.start() < covered:
            continue
        attributes = _keyword_attribute_list(src, start) if start.group(1) else _standard_attribute_list(src, start)
        if attributes is None:
            continue
        covered = attributes.close
        for name, offset in _attribute_names(attributes):
            rule = _attribute_rule(name)
            if rule is not None:
                out.append(Finding(rule, f"attribute '{name}' is not allowed ({_rule_summary(rule)}){where}",
                                   attributes.start + offset))
    return out


def _library_api_findings(src: CSource, where: str) -> list[Finding]:
    out: list[Finding] = []
    for pattern in (_LIBRARY_API_CALL_RE, _LIBRARY_API_VALUE_RE):
        for use in pattern.finditer(src.code):
            name = use.group(1)
            if name in TERMINATION_NAMES:
                out.append(Finding("TERMINATION", (f"'{name}' terminates the process or escapes control flow: "
                                   f"{CBMC_TERMINATION_CONSEQUENCE}{where}"), use.start(1)))
            else:
                out.append(Finding("DANGEROUS_IMPORT", f"'{name}' (process/network/loader/thread API) is not allowed"
                                   f"{where}", use.start(1)))
    return out


def token_findings(src: CSource, where: str = "") -> list[Finding]:
    out: list[Finding] = []
    for token_rule in _TOKEN_RULES:
        for use in token_rule.pattern.finditer(src.code):
            out.append(Finding(token_rule.rule, (f"{token_rule.what} '{use.group()}' is not allowed in code under "
                               f"verification{where}"), use.start()))
    out.extend(_library_api_findings(src, where))
    out.extend(_attribute_findings(src, where))
    return out


def _lexical_divergence_findings(src: CSource) -> list[Finding]:
    trigraphs = [
        Finding("TRIGRAPH", f"trigraph '{use.group()}' is not allowed: {_DIVERGENCE_CONSEQUENCE}", use.start())
        for use in _TRIGRAPH_RE.finditer(src.text)
    ]
    raw_strings = [
        Finding("TOOL_DIVERGENCE", f"'{use.group()}' is {_RAW_STRING_MEANING}: {_DIVERGENCE_CONSEQUENCE}", use.start())
        for use in _RAW_STRING_PREFIX_RE.finditer(src.code)
    ]
    return trigraphs + raw_strings


def _include_problem(spec: Spec, name: str, quoted: bool) -> str | None:
    problem = include_path_problem(spec, name, quoted)
    if problem is None and not quoted and not normalized_header_path(name).endswith(C_HEADER_SUFFIX):
        return f"<{name}> is not a header"
    return problem


def _include_findings(spec: Spec, src: CSource, directive: Directive) -> list[Finding]:
    header = included_header(src, directive)
    if header is None:
        return computed_include_findings(src, directive)
    problem = _include_problem(spec, header.name, header.quoted)
    if problem:
        return [Finding("INCLUDE_ESCAPE", f"#include {header.spelling}: {problem}", directive.offset)]
    return []


def _redefined_entity(spec: Spec, macro: str, header_macros: set[str], header_idents: set[str]) -> str | None:
    if macro == spec.target.function:
        return "the target function"
    if macro in header_macros:
        return "a macro of the protected interface"
    if macro in C_KEYWORDS:
        return "a C keyword"
    if macro in header_idents:
        return "an identifier used by the protected interface"
    if macro in LIBC_NAMES:
        return "a C library name"
    return None


def _macro_definition_findings(
    spec: Spec, directive: Directive, header_macros: set[str], header_idents: set[str]
) -> list[Finding]:
    macro = defined_macro(directive)
    redefined = _redefined_entity(spec, macro, header_macros, header_idents)
    if redefined is None or is_tool_specific_macro(macro):
        return []
    return redefinition_findings(directive, macro, redefined)


def _directive_findings(
    spec: Spec, src: CSource, header_macros: set[str], header_idents: set[str] | None = None
) -> list[Finding]:
    header_idents = set(header_idents or ())
    out: list[Finding] = []
    for directive in parse_directives(src.code):
        if is_irregular(directive):
            out.extend(irregular_directive_findings(directive))
        elif directive.name == "pragma":
            out.extend(pragma_directive_findings(src, directive, _PRAGMA_CONSEQUENCE))
        elif directive.name == "include":
            out.extend(_include_findings(spec, src, directive))
        elif directive.name not in UNCHECKED_DIRECTIVES:
            if directive.name in MACRO_DEFINITION_DIRECTIVES:
                out.extend(_macro_definition_findings(spec, directive, header_macros, header_idents))
            out.extend(divergent_macro_findings(directive, tool_specific_identifiers(directive),
                                                _DIVERGENCE_CONSEQUENCE))
            out.extend(assertion_predicate_findings(directive))
    return out


def _predefined_macro_use_findings(src: CSource) -> list[Finding]:
    return [
        Finding("TOOL_DIVERGENCE", (f"'{use.group(1)}' has a different value (or none) under clang, the optimised "
                "sanitizer build and CBMC"), use.start())
        for use in TOOL_DEPENDENT_PREDEFINED_MACRO_RE.finditer(src.code_nodirectives)
    ]


def _forbidden_function_findings(src: CSource) -> list[Finding]:
    return [
        Finding("SYMBOL_OVERRIDE", f"defines function {name}(): {why}", offset)
        for name, offset in defined_functions(src)
        if (why := forbidden_definition(name))
    ]


def c_source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    src = CSource(text)
    header_idents, header_macros = c_header_facts(headers)
    found = [
        *_directive_findings(spec, src, header_macros, header_idents),
        *pragma_operator_findings(src),
        *_lexical_divergence_findings(src),
        *token_findings(src),
        *_predefined_macro_use_findings(src),
        *_forbidden_function_findings(src),
        *acsl_violations(spec, src, headers),
    ]
    return to_violations(src, rel, found)


def preprocessed_violations(rel: str, pp_output: str) -> list[PolicyViolation]:
    text, origin = expanded_view(rel, pp_output)
    src = CSource(text)
    found = [*token_findings(src, AFTER_EXPANSION), *expanded_pragma_findings(src)]
    return to_violations(src, rel, found, origin)


def _is_nm_symbol_line(line: str, fields: list[str]) -> bool:
    return len(fields) >= 2 and len(fields[1]) == 1 and not line.endswith(_ARCHIVE_MEMBER_SUFFIX)


def parse_nm(output: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        if _is_nm_symbol_line(line, fields):
            out.append((fields[0], fields[1]))
    return out


def _import_problem(
    name: str, nm_type: str, header_idents: set[str], defined_elsewhere: set[str]
) -> Problem | None:
    if nm_type in WEAK_UNDEFINED_TYPES:
        return Problem("DANGEROUS_IMPORT", (f"weak undefined reference to '{name}': it resolves differently under "
                       "the verifier and natively (e.g. __VERIFIER_assume)"))
    if name in TERMINATION_NAMES:
        return Problem("TERMINATION", (f"imports '{name}': termination / non-local control flow is not allowed "
                       "(CBMC prunes such paths; native tests can fake success)"))
    if is_verifier_hook(name):
        return Problem("CPROVER_BUILTIN", f"imports verifier hook '{name}'")
    if is_dangerous_import(name):
        return Problem("DANGEROUS_IMPORT", f"imports '{name}' (process/network/loader/thread/sanitizer API)")
    if (name in defined_elsewhere or name in header_idents or name in ALLOWED_IMPORTS
            or COMPILER_RT_ARITHMETIC_HELPER_RE.match(name)):
        return None
    return Problem("DANGEROUS_IMPORT", (f"imports '{name}', which is not on the allowlist of library functions "
                   "CBMC models faithfully (a body-less function returns arbitrary values under CBMC); allowed: "
                   f"{', '.join(sorted(ALLOWED_IMPORTS))}"))


def _definition_problem(name: str, nm_type: str, allowed_definitions: set[str]) -> Problem | None:
    if nm_type in WEAK_DEFINITION_TYPES:
        return Problem("SYMBOL_OVERRIDE", f"weak definition of '{name}' is not allowed")
    if nm_type in INDIRECT_FUNCTION_TYPES:
        return Problem("SYMBOL_OVERRIDE", f"indirect function (ifunc) '{name}' is not allowed")
    why = forbidden_definition(name)
    if why:
        return Problem("SYMBOL_OVERRIDE", f"defines external symbol {name}: {why}")
    if name not in allowed_definitions:
        return Problem("SYMBOL_OVERRIDE", (f"defines external symbol '{name}', which the protected headers do not "
                       "declare; make it static"))
    return None


def check_object_symbols(
    spec: Spec,
    rel: str,
    symbols: list[tuple[str, str]],
    *,
    header_idents: set[str],
    defined_elsewhere: set[str],
    src: CSource | None = None,
) -> list[PolicyViolation]:
    allowed_definitions = header_idents | {spec.target.function}
    out: list[PolicyViolation] = []
    for name, nm_type in symbols:
        if nm_type in UNDEFINED_TYPES:
            problem = _import_problem(name, nm_type, header_idents, defined_elsewhere)
        elif has_external_linkage(nm_type):
            problem = _definition_problem(name, nm_type, allowed_definitions)
        else:
            continue
        if problem is not None:
            out.append(violation(problem.rule, problem.message, rel, first_mention_line(src, name)))
    return out


def section_violations(rel: str, readelf_output: str, src: CSource | None = None) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    for name in dict.fromkeys(SECTION_RE.findall(readelf_output)):
        if _CODE_OUTSIDE_TEST_SECTION_RE.match(name):
            line = next((ln for word in _CONSTRUCTOR_MENTIONS if (ln := first_mention_line(src, word))), None)
            out.append(violation("CONSTRUCTOR", (f"object has section {name}: code would run before/after the "
                                 "function under test (constructors/destructors)"), rel, line))
    return out
