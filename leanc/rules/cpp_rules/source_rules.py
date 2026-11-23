from __future__ import annotations

import bisect
import re

from leanc.foundation.models import PolicyViolation
from leanc.lang.c_identifiers import (
    C_KEYWORDS,
    HARNESS_INPUT_PREFIX,
    RESERVED_IDENTIFIER_RE,
)
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.c_names import (
    DANGEROUS_NAMES,
    LIBC_NAMES,
    TERMINATION_NAMES,
    forbidden_definition,
)
from leanc.rules.c_rules import CBMC_TERMINATION_CONSEQUENCE, TokenRule, token_findings
from leanc.rules.cpp_rules.definitions import (
    Scope,
    Scopes,
    cpp_function_definitions,
    declaration_ends,
    identifier_start_before,
    space_start_before,
)
from leanc.rules.cpp_rules.headers import HeaderFacts, header_facts
from leanc.rules.findings import Finding, Problem, to_violations
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
    divergent_macro_findings,
    expanded_pragma_findings,
    expanded_view,
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
from leanc.specs.model import Spec

__all__ = ["CPP_KEYWORDS", "CPP_STD_HEADERS", "cpp_preprocessed_violations", "cpp_source_violations"]

CPP_STD_HEADERS: frozenset[str] = frozenset(
    {
        "algorithm", "array", "bit", "bitset", "cassert", "cctype", "cerrno", "cfloat", "charconv",
        "cinttypes", "climits", "cmath", "compare", "concepts", "cstddef", "cstdint", "cstdio", "cstdlib",
        "cstring", "deque", "expected", "forward_list", "functional", "initializer_list", "iterator", "limits",
        "list", "map", "mdspan", "memory", "new", "numbers", "numeric", "optional", "queue", "ranges", "ratio",
        "set", "source_location", "span", "stack", "string", "string_view", "tuple", "type_traits",
        "unordered_map", "unordered_set", "utility", "variant", "vector", "version",
    }
)
_CPP_HEADER_SUFFIXES = (".hpp", ".hh", ".hxx", ".h++")
_FORBIDDEN_STD_HEADERS: dict[str, Problem] = {
    **dict.fromkeys(
        ("thread", "mutex", "shared_mutex", "condition_variable", "future", "semaphore", "latch", "barrier",
         "stop_token", "atomic", "pthread.h", "threads.h"),
        Problem("DANGEROUS_IMPORT", "threads / synchronisation are not allowed in code under verification"),
    ),
    **dict.fromkeys(("csignal", "csetjmp", "signal.h", "setjmp.h"),
                    Problem("TERMINATION", "signals and non-local jumps escape the function under test")),
    **dict.fromkeys(("exception", "stdexcept"),
                    Problem("EXCEPTIONS", "code under verification may not use exceptions")),
    "coroutine": Problem("CPP_RUNTIME", "coroutines are not allowed in code under verification"),
    "typeinfo": Problem("CPP_RUNTIME", "RTTI (typeid) is not allowed in code under verification"),
}

CPP_KEYWORDS: frozenset[str] = C_KEYWORDS | frozenset(
    {
        "alignas", "alignof", "and", "and_eq", "asm", "bitand", "bitor", "bool", "catch", "char8_t", "char16_t",
        "char32_t", "class", "compl", "concept", "consteval", "constexpr", "constinit", "const_cast", "co_await",
        "co_return", "co_yield", "decltype", "delete", "dynamic_cast", "explicit", "export", "false", "final",
        "friend", "import", "module", "mutable", "namespace", "new", "noexcept", "not", "not_eq", "nullptr",
        "operator", "or", "or_eq", "override", "private", "protected", "public", "reinterpret_cast", "requires",
        "static_assert", "static_cast", "template", "this", "thread_local", "throw", "true", "try", "typeid",
        "typename", "using", "virtual", "wchar_t", "xor", "xor_eq",
    }
)

_LANGUAGE_VERSION_MACRO = "__cplusplus"
_FEATURE_MACRO_PREFIX = "__cpp_"
_LIBRARY_FEATURE_MACRO_PREFIX = "__cpp_lib_"
_FLAG_DEPENDENT_FEATURE_MACROS = frozenset(
    {"__cpp_exceptions", "__cpp_rtti", "__cpp_sized_deallocation", "__cpp_aligned_new", "__cpp_char8_t",
     "__cpp_impl_coroutine", "__cpp_coroutines", "__cpp_threadsafe_static_init", "__cpp_modules"}
)
_PRAGMA_CONSEQUENCE = "pragmas can disable checks or diagnostics, or mark code as a system header"
_DIVERGENCE_CONSEQUENCE = "make the tools see different code"
_TEST_TERMINATION_CONSEQUENCE = "a test could exit with success before its checks run"

_CPP_TOKEN_RULES: list[TokenRule] = [
    TokenRule("EXCEPTIONS", re.compile(r"\b(?:throw|try|catch)\b"), "C++ exception handling"),
    TokenRule(
        "EXCEPTIONS",
        re.compile(r"\b(?:rethrow_exception|rethrow_if_nested|throw_with_nested|make_exception_ptr|"
                   r"current_exception|exception_ptr|uncaught_exceptions?|__cxa_\w+)\b"),
        "exception-handling facility",
    ),
    TokenRule("TERMINATION", re.compile(r"(?<![\w.])(?<!->)\b(?:terminate|set_terminate|get_terminate)(?=\s*\()"),
              "std::terminate"),
    TokenRule("CPP_RUNTIME", re.compile(r"\b(?:typeid|dynamic_cast)\b"), "run-time type information"),
    TokenRule("CPP_RUNTIME", re.compile(r"\bco_(?:await|yield|return)\b"), "coroutine"),
    TokenRule(
        "DANGEROUS_IMPORT",
        re.compile(r"\bstd\s*::\s*(?:this_thread|thread|jthread|async|launch|stop_token|stop_source|barrier|latch|"
                   r"counting_semaphore|binary_semaphore|condition_variable\w*|mutex|recursive_mutex|timed_mutex|"
                   r"shared_mutex|shared_timed_mutex|future|shared_future|promise|packaged_task|atomic\w*|"
                   r"system|getenv)\b"),
        "thread / process facility",
    ),
    TokenRule(
        "TERMINATION",
        re.compile(r"\bstd\s*::\s*(?:exit|_Exit|quick_exit|abort|atexit|at_quick_exit|terminate|set_terminate|"
                   r"signal|raise|longjmp)\b"),
        "process termination / signal / non-local jump",
    ),
    TokenRule(
        "SYMBOL_OVERRIDE",
        re.compile(r"\bnamespace\s+(?:inline\s+)?(?:std|__\w+|_[A-Z]\w*|abi)\b"),
        "declarations in namespace std (or a reserved namespace), which can replace standard library code",
    ),
]
_EXPLICIT_SPECIALISATION_RE = re.compile(r"\btemplate\s*<\s*>")
_STD_QUALIFIER_USE_RE = re.compile(r"(?<![\w:])(?:::\s*)?std\s*::")
_STD_QUALIFIER_RE = re.compile(r"^(?:::)?std::")
_WORD_AT_RE = re.compile(r"\w+")
_LIBRARY_NAME_LOOKAHEAD_CHARS = 64
_LIBRARY_API_RULES = ("TERMINATION", "DANGEROUS_IMPORT")
_LIBRARY_API_NAMES = TERMINATION_NAMES | DANGEROUS_NAMES
_THREAD_API_PREFIXES = ("pthread_", "thrd_")
_ALLOCATION_FUNCTION_NAMES = ("operator new", "operator delete")
_NAMESPACE_STD = "std"
_C_VISIBLE_SCOPES = (Scope.GLOBAL, Scope.EXTERN_C)


def _has_same_value_in_every_cpp_gate(macro: str) -> bool:
    if macro == _LANGUAGE_VERSION_MACRO:
        return True
    return (
        macro.startswith(_FEATURE_MACRO_PREFIX)
        and not macro.startswith(_LIBRARY_FEATURE_MACRO_PREFIX)
        and macro not in _FLAG_DEPENDENT_FEATURE_MACROS
    )


def _include_problem(spec: Spec, name: str, quoted: bool) -> Problem | None:
    problem = include_path_problem(spec, name, quoted)
    if problem is not None:
        return Problem("INCLUDE_ESCAPE", problem)
    if quoted:
        return None
    base = name.strip()
    if base in _FORBIDDEN_STD_HEADERS:
        return _FORBIDDEN_STD_HEADERS[base]
    if normalized_header_path(name).endswith(C_HEADER_SUFFIX) or base in CPP_STD_HEADERS:
        return None
    if base.endswith(_CPP_HEADER_SUFFIXES):
        return None
    return Problem("INCLUDE_ESCAPE", (f"<{name}> is not an allowed C++ standard library header "
                   f"(allowed: C headers <*.h> and {', '.join(sorted(CPP_STD_HEADERS))})"))


def _include_findings(spec: Spec, src: CSource, directive: Directive) -> list[Finding]:
    header = included_header(src, directive)
    if header is None:
        return computed_include_findings(src, directive)
    problem = _include_problem(spec, header.name, header.quoted)
    if problem:
        return [Finding(problem.rule, f"#include {header.spelling}: {problem.message}", directive.offset)]
    return []


def _redefined_entity(spec: Spec, macro: str, header_macros: set[str]) -> str | None:
    if macro == spec.target.function:
        return "the target function"
    if macro in header_macros:
        return "a macro of the protected interface"
    if macro in CPP_KEYWORDS:
        return "a C++ keyword"
    if macro in LIBC_NAMES:
        return "a C library name"
    return None


def _macro_definition_findings(spec: Spec, directive: Directive, macro: str, header_macros: set[str]) -> list[Finding]:
    redefined = _redefined_entity(spec, macro, header_macros)
    if redefined is None or is_tool_specific_macro(macro):
        return []
    return redefinition_findings(directive, macro, redefined)


def _macros_before_include_findings(macro_definitions: list[tuple[str, int]], last_include: int) -> list[Finding]:
    return [
        Finding("SYMBOL_OVERRIDE", (f"macro '{macro}' is (un)defined before an #include: it can rewrite the included "
                "header's inline/template code in this translation unit only (an ODR violation the linker may "
                "resolve in favour of this copy for the trusted test driver too)"), at)
        for macro, at in macro_definitions
        if at < last_include
    ]


def _directive_findings(spec: Spec, src: CSource, header_macros: set[str]) -> list[Finding]:
    out: list[Finding] = []
    last_include = -1
    macro_definitions: list[tuple[str, int]] = []
    for directive in parse_directives(src.code):
        if is_irregular(directive):
            out.extend(irregular_directive_findings(directive))
        elif directive.name == "pragma":
            out.extend(pragma_directive_findings(src, directive, _PRAGMA_CONSEQUENCE))
        elif directive.name == "include":
            last_include = directive.offset
            out.extend(_include_findings(spec, src, directive))
        elif directive.name not in UNCHECKED_DIRECTIVES:
            idents = tool_specific_identifiers(directive)
            if directive.name in MACRO_DEFINITION_DIRECTIVES:
                macro = defined_macro(directive)
                macro_definitions.append((macro, directive.offset))
                out.extend(_macro_definition_findings(spec, directive, macro, header_macros))
            else:
                idents = [i for i in idents if not _has_same_value_in_every_cpp_gate(i)]
            out.extend(divergent_macro_findings(directive, idents, _DIVERGENCE_CONSEQUENCE))
            out.extend(assertion_predicate_findings(directive))
    out.extend(_macros_before_include_findings(macro_definitions, last_include))
    return out


def _qualified_by_a_class_or_namespace(code: str, offset: int) -> bool:
    k = space_start_before(code, offset)
    if code[max(0, k - 2) : k] != "::":
        return False
    k = space_start_before(code, k - 2)
    if k > 0 and code[k - 1] == ">":
        return True
    qualifier = code[identifier_start_before(code, k) : k]
    return bool(qualifier) and qualifier != _NAMESPACE_STD


def _names_a_library_api(code: str, offset: int) -> bool:
    word = _WORD_AT_RE.match(code[offset : offset + _LIBRARY_NAME_LOOKAHEAD_CHARS])
    return bool(word) and (word.group() in _LIBRARY_API_NAMES or word.group().startswith(_THREAD_API_PREFIXES))


def _c_token_findings(src: CSource, where: str) -> list[Finding]:
    code = src.code
    out: list[Finding] = []
    scopes: Scopes | None = None
    for finding in token_findings(src, where):
        if finding.rule in _LIBRARY_API_RULES and _names_a_library_api(code, finding.offset):
            if _qualified_by_a_class_or_namespace(code, finding.offset):
                continue
            scopes = scopes or Scopes(src)
            if scopes.innermost(finding.offset) == Scope.CLASS:
                continue
        message = finding.message.replace(CBMC_TERMINATION_CONSEQUENCE, _TEST_TERMINATION_CONSEQUENCE)
        out.append(finding._replace(message=message))
    return out


def _cpp_token_rule_findings(code: str, where: str) -> list[Finding]:
    out: list[Finding] = []
    for token_rule in _CPP_TOKEN_RULES:
        for use in token_rule.pattern.finditer(code):
            if token_rule.rule == "TERMINATION" and _qualified_by_a_class_or_namespace(code, use.start()):
                continue
            out.append(Finding(token_rule.rule, (f"{token_rule.what} '{' '.join(use.group().split())}' is not "
                               f"allowed in code under verification{where}"), use.start()))
    return out


def _std_specialisation_findings(code: str, where: str) -> list[Finding]:
    specialisations = list(_EXPLICIT_SPECIALISATION_RE.finditer(code))
    if not specialisations:
        return []
    ends = declaration_ends(code)
    std_uses = [use.start() for use in _STD_QUALIFIER_USE_RE.finditer(code)]
    out: list[Finding] = []
    for specialisation in specialisations:
        k = bisect.bisect_left(ends, specialisation.end())
        end = ends[k] if k < len(ends) else len(code)
        q = bisect.bisect_left(std_uses, specialisation.end())
        if q < len(std_uses) and std_uses[q] < end:
            out.append(Finding("SYMBOL_OVERRIDE", ("explicit specialisation of a standard library template: it can "
                               f"replace library code that the trusted test driver uses as well{where}"),
                               specialisation.start()))
    return out


def _token_findings(src: CSource, where: str = "") -> list[Finding]:
    return [
        *_c_token_findings(src, where),
        *_cpp_token_rule_findings(src.code, where),
        *_std_specialisation_findings(src.code, where),
    ]


def _names_a_scope(qualifier: str) -> bool:
    return bool(qualifier.strip(":"))


def _allocation_function_findings(name: str, qualifier: str, offset: int, facts: HeaderFacts,
                                  where: str) -> list[Finding]:
    if not _names_a_scope(qualifier):
        return [Finding("SYMBOL_OVERRIDE", (f"defines the global {name}: replacing the allocation functions hides "
                        "heap errors and leaks from AddressSanitizer/LeakSanitizer and changes the trusted driver's "
                        f"allocations too{where}"), offset)]
    if name not in facts.operators:
        return [Finding("SYMBOL_OVERRIDE", (f"defines {qualifier}{name}, which the protected headers do not declare "
                        f"(custom allocation hides heap errors from the sanitizers){where}"), offset)]
    return []


def _definition_findings(src: CSource, facts: HeaderFacts, where: str = "") -> list[Finding]:
    out: list[Finding] = []
    for d in cpp_function_definitions(src):
        bare = d.name.lstrip("~")
        if d.name.startswith(_ALLOCATION_FUNCTION_NAMES):
            out.extend(_allocation_function_findings(d.name, d.qualifier, d.offset, facts, where))
        elif _STD_QUALIFIER_RE.match(d.qualifier):
            out.append(Finding("SYMBOL_OVERRIDE", (f"defines {d.qualifier}{d.name}: definitions in namespace std "
                               f"can replace standard library code{where}"), d.offset))
        elif _names_a_scope(d.qualifier):
            continue
        elif d.scope in _C_VISIBLE_SCOPES:
            why = forbidden_definition(bare)
            if why:
                out.append(Finding("SYMBOL_OVERRIDE", f"defines function {bare}(): {why}{where}", d.offset))
        elif RESERVED_IDENTIFIER_RE.match(bare) or bare.startswith(HARNESS_INPUT_PREFIX):
            out.append(Finding("SYMBOL_OVERRIDE", f"defines function {bare}(): reserved / harness-only name{where}",
                               d.offset))
    return out


def _predefined_macro_use_findings(src: CSource) -> list[Finding]:
    return [
        Finding("TOOL_DIVERGENCE", (f"'{use.group(1)}' has a different value (or none) in the compile, analyzer "
                "and optimised sanitizer builds"), use.start())
        for use in TOOL_DEPENDENT_PREDEFINED_MACRO_RE.finditer(src.code_nodirectives)
    ]


def cpp_source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    src = CppSource(text)
    facts = header_facts(headers)
    found = [
        *_directive_findings(spec, src, facts.macros),
        *pragma_operator_findings(src),
        *_token_findings(src),
        *_predefined_macro_use_findings(src),
        *_definition_findings(src, facts),
    ]
    return to_violations(src, rel, found)


def cpp_preprocessed_violations(rel: str, pp_output: str, facts: HeaderFacts | None = None) -> list[PolicyViolation]:
    text, origin = expanded_view(rel, pp_output)
    src = CppSource(text)
    found = [
        *_token_findings(src, AFTER_EXPANSION),
        *expanded_pragma_findings(src),
        *_definition_findings(src, facts or HeaderFacts(), AFTER_EXPANSION),
    ]
    return to_violations(src, rel, found, origin)
