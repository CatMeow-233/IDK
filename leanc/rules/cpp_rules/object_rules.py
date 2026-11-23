from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from leanc.foundation.models import PolicyViolation
from leanc.lang.c_identifiers import HARNESS_INPUT_PREFIX, RESERVED_IDENTIFIER_RE
from leanc.lang.c_lexer import CSource
from leanc.rules.c_names import (
    ALLOWED_IMPORTS,
    COMPILER_RT_ARITHMETIC_HELPER_RE,
    TERMINATION_NAMES,
    forbidden_definition,
    is_dangerous_import,
    is_verifier_hook,
)
from leanc.rules.cpp_rules.demangle import DemangledName, parse_demangled
from leanc.rules.cpp_rules.headers import HeaderFacts
from leanc.rules.findings import Problem, first_mention_line, violation
from leanc.rules.symbol_types import (
    INDIRECT_FUNCTION_TYPES,
    STRONG_TEXT_TYPE,
    TEXT_DEFINITION_TYPES,
    UNDEFINED_TYPES,
    WEAK_DEFINITION_TYPES,
    WEAK_UNDEFINED_TYPES,
    has_external_linkage,
)
from leanc.specs.model import Spec

__all__ = [
    "CPP_ALLOWED_RUNTIME_IMPORTS",
    "CPP_RUNTIME_IMPORT_FAMILIES",
    "ITANIUM_MANGLING_PREFIX",
    "check_cpp_object_symbols",
    "defines_target",
]

ITANIUM_MANGLING_PREFIX = "_Z"
CPP_ALLOWED_RUNTIME_IMPORTS: frozenset[str] = frozenset(
    {"__cxa_pure_virtual", "__cxa_deleted_virtual", "__cxa_guard_acquire", "__cxa_guard_release",
     "__cxa_guard_abort", "__gxx_personality_v0", "_Unwind_Resume"}
)
CPP_RUNTIME_IMPORT_FAMILIES = (
    "global operator new/delete",
    "std::nothrow",
    "std::__throw_*",
    "std::__glibcxx_assert_fail",
    "vtable for __cxxabiv1::__*class_type_info",
    "std::terminate/__cxa_begin_catch (from __clang_call_terminate)",
)
_EXCEPTION_IMPORT_RE = re.compile(
    r"^(?:__cxa_(?:allocate_exception|free_exception|throw|rethrow|begin_catch|end_catch|get_exception_ptr|"
    r"current_exception_type|uncaught_exceptions?|call_unexpected|init_primary_exception)|_Unwind_RaiseException)$"
)
_RTTI_IMPORTS = frozenset({"__dynamic_cast", "__cxa_bad_cast", "__cxa_bad_typeid"})
_EXIT_TIME_IMPORTS = frozenset({"__dso_handle", "__cxa_thread_atexit", "__cxa_thread_atexit_impl"})
_NOEXCEPT_TERMINATE_HELPER = "__clang_call_terminate"
_CATCH_OF_TERMINATE_HELPER = "__cxa_begin_catch"
_CLEANUP_PERSONALITY_REFERENCE = "DW.ref.__gxx_personality_v0"
_ALLOWED_COMPILER_HELPER_TYPES: dict[str, tuple[str, ...]] = {
    _NOEXCEPT_TERMINATE_HELPER: ("W",),
    _CLEANUP_PERSONALITY_REFERENCE: ("V", "W"),
}
_STD_NAMESPACES = frozenset({"std", "__gnu_cxx", "__cxxabiv1", "abi", "__gnu_debug", "__pstl"})
_CXXABI_NAMESPACE = ("__cxxabiv1",)
_TYPEINFO_CLASSES = frozenset({"__class_type_info", "__si_class_type_info", "__vmi_class_type_info"})
_LIBSTDCXX_HELPER_RE = re.compile(r"^(?:__throw_\w+|__glibcxx_assert_fail)$")
_STD_THREADS = frozenset(
    {"thread", "jthread", "this_thread", "mutex", "recursive_mutex", "condition_variable", "future", "promise",
     "async", "__future_base", "stop_token", "barrier", "latch", "counting_semaphore", "__atomic_wait_address"}
)
_STD_TERMINATE = ("std", "terminate")
_STD_NOTHROW = ("std", "nothrow")
_STD_MANGLED_RE = re.compile(r"^_Z(?:N[KVRO]*)?S[tabsiod]")
_VTABLE_FOR = "vtable for"
_ALLOCATION_FUNCTION_PREFIXES = ("operator new", "operator delete")
_ANONYMOUS_NAMESPACE = "(anonymous namespace)"
_LOCAL_ENTITY_MARK = "{"
_OPERATOR_PREFIX = "operator"
_PLAIN_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


class _Symbol(NamedTuple):
    name: str
    nm_type: str
    demangled: str

    @property
    def is_mangled(self) -> bool:
        return self.name.startswith(ITANIUM_MANGLING_PREFIX)

    @property
    def shown(self) -> str:
        return self.demangled if self.is_mangled else self.name


@dataclass(frozen=True)
class _ObjectContext:
    facts: HeaderFacts
    defined_elsewhere: set[str]
    has_terminate_helper: bool
    target: str


def _is_allocation_function(nm: DemangledName) -> bool:
    return len(nm.owner) == 1 and nm.owner[0].startswith(_ALLOCATION_FUNCTION_PREFIXES)


def _undeclared(nm: DemangledName, facts: HeaderFacts) -> str | None:
    for inner in nm.inner or (nm,):
        for component in inner.owner:
            if component == _ANONYMOUS_NAMESPACE or component.startswith(_LOCAL_ENTITY_MARK):
                continue
            if component.startswith(_OPERATOR_PREFIX):
                if component in facts.operators:
                    continue
                return f"'{component}' is not declared by the protected headers"
            if component.lstrip("~") not in facts.idents:
                return f"'{component.lstrip('~')}' is not declared by the protected headers"
    return None


def _mangled_definition_problem(nm: DemangledName, weak: bool, facts: HeaderFacts) -> str | None:
    for inner in nm.inner or (nm,):
        components = inner.owner
        if _is_allocation_function(inner):
            return ("replaces the global allocation function: it hides heap errors and leaks from "
                    "AddressSanitizer/LeakSanitizer and changes the trusted driver's allocations too")
        if components[0] in _STD_NAMESPACES:
            if weak:
                continue
            return (f"defines an entity of namespace {components[0]}: replacing standard library / C++ runtime code "
                    "is not allowed")
        if any(c.startswith(HARNESS_INPUT_PREFIX) for c in components):
            return "a harness input generator (nondet_*) must not get a body"
        reserved = next((c for c in components if RESERVED_IDENTIFIER_RE.match(c.lstrip("~"))), None)
        if reserved:
            return f"'{reserved}' is a reserved identifier (C++ runtime / compiler / verifier hook)"
        undeclared = _undeclared(inner, facts)
        if undeclared is not None:
            return (f"{undeclared}; give it internal linkage (static, or an anonymous namespace) or declare it in "
                    "the protected header")
    return None


def _is_typeinfo_class_vtable(nm: DemangledName) -> bool:
    return (
        nm.special == _VTABLE_FOR
        and bool(nm.inner)
        and nm.inner[0].components[:1] == _CXXABI_NAMESPACE
        and nm.inner[0].components[-1] in _TYPEINFO_CLASSES
    )


def _is_allowed_library_helper(components: tuple[str, ...]) -> bool:
    return components == _STD_NOTHROW or (len(components) == 2 and bool(_LIBSTDCXX_HELPER_RE.match(components[1])))


def _mangled_import_problem(nm: DemangledName, context: _ObjectContext) -> Problem | None:
    components = nm.components
    if _is_allocation_function(nm) or _is_typeinfo_class_vtable(nm):
        return None
    if components == _STD_TERMINATE:
        if context.has_terminate_helper:
            return None
        return Problem("TERMINATION", f"imports '{nm.text}': termination is not allowed")
    if components[0] in _STD_NAMESPACES:
        if _is_allowed_library_helper(components):
            return None
        if len(components) > 1 and components[1] in _STD_THREADS:
            return Problem("DANGEROUS_IMPORT", f"imports '{nm.text}' (threads / synchronisation)")
        return Problem("DANGEROUS_IMPORT", (f"imports C++ library entity '{nm.text}', which is not on the allowlist "
                       "(header-only library templates, the global operator new/delete, std::nothrow and the "
                       "libstdc++ precondition-failure helpers std::__throw_* are)"))
    if _undeclared(nm, context.facts) is None:
        return None
    return Problem("DANGEROUS_IMPORT", (f"imports '{nm.text}', which is neither declared by the protected headers "
                   "nor on the allowlist"))


def _is_allowed_plain_import(name: str, context: _ObjectContext) -> bool:
    return bool(
        name in context.defined_elsewhere
        or name in context.facts.idents
        or name in ALLOWED_IMPORTS
        or name in CPP_ALLOWED_RUNTIME_IMPORTS
        or COMPILER_RT_ARITHMETIC_HELPER_RE.match(name)
    )


def _plain_import_problem(name: str, context: _ObjectContext) -> Problem | None:
    if name in TERMINATION_NAMES or name in _EXIT_TIME_IMPORTS:
        return Problem("TERMINATION", (f"imports '{name}': termination, exit-time destructors and non-local "
                       "control flow are not allowed (native tests could report success)"))
    if is_verifier_hook(name):
        return Problem("CPROVER_BUILTIN", f"imports verifier hook '{name}'")
    if is_dangerous_import(name):
        return Problem("DANGEROUS_IMPORT", f"imports '{name}' (process/network/loader/thread/sanitizer API)")
    if _EXCEPTION_IMPORT_RE.match(name):
        if name == _CATCH_OF_TERMINATE_HELPER and context.has_terminate_helper:
            return None
        return Problem("EXCEPTIONS", f"imports '{name}': C++ exceptions are not allowed")
    if name in _RTTI_IMPORTS:
        return Problem("CPP_RUNTIME", f"imports '{name}': RTTI (dynamic_cast/typeid) is not allowed")
    if _is_allowed_plain_import(name, context):
        return None
    return Problem("DANGEROUS_IMPORT", (f"imports '{name}', which is not on the allowlist of library functions "
                   "(allowed: " + ", ".join(sorted(ALLOWED_IMPORTS)) + ", the C++ runtime pieces "
                   + ", ".join(sorted(CPP_ALLOWED_RUNTIME_IMPORTS)) + ")"))


def _import_problem(symbol: _Symbol, nm: DemangledName | None, context: _ObjectContext) -> Problem | None:
    if symbol.nm_type in WEAK_UNDEFINED_TYPES:
        return Problem("DANGEROUS_IMPORT", (f"weak undefined reference to '{symbol.shown}': it resolves differently "
                       "depending on what else is linked"))
    if not symbol.is_mangled:
        return _plain_import_problem(symbol.name, context)
    if symbol.name in context.defined_elsewhere:
        return None
    if nm is None:
        return Problem("DANGEROUS_IMPORT", f"imports '{symbol.demangled}', a C++ symbol that cannot be interpreted")
    return _mangled_import_problem(nm, context)


def _plain_definition_problem(symbol: _Symbol, weak: bool, context: _ObjectContext) -> Problem | None:
    name = symbol.name
    if weak and symbol.nm_type in _ALLOWED_COMPILER_HELPER_TYPES.get(name, ()):
        return None
    if weak:
        return Problem("SYMBOL_OVERRIDE", f"weak definition of '{name}' is not allowed")
    why = forbidden_definition(name)
    if why:
        return Problem("SYMBOL_OVERRIDE", f"defines external symbol {name}: {why}")
    if name not in context.facts.idents and name != context.target:
        return Problem("SYMBOL_OVERRIDE", (f"defines external symbol '{name}', which the protected headers do not "
                       "declare; make it static or put it in an anonymous namespace"))
    return None


def _is_library_template_instantiation(symbol: _Symbol, weak: bool) -> bool:
    return weak and bool(_STD_MANGLED_RE.match(symbol.name))


def _definition_problem(symbol: _Symbol, nm: DemangledName | None, context: _ObjectContext) -> Problem | None:
    if symbol.nm_type in INDIRECT_FUNCTION_TYPES:
        return Problem("SYMBOL_OVERRIDE", f"indirect function (ifunc) '{symbol.shown}' is not allowed")
    weak = symbol.nm_type in WEAK_DEFINITION_TYPES
    if not symbol.is_mangled:
        return _plain_definition_problem(symbol, weak, context)
    if nm is None:
        if _is_library_template_instantiation(symbol, weak):
            return None
        return Problem("SYMBOL_OVERRIDE", f"defines '{symbol.demangled}', a C++ symbol that cannot be interpreted")
    why = _mangled_definition_problem(nm, weak, context.facts)
    if not why:
        return None
    kind = "weak (COMDAT) definition" if weak else "definition"
    return Problem("SYMBOL_OVERRIDE", f"{kind} of external C++ symbol '{symbol.demangled}': {why}")


def _mention_line(src: CSource | None, symbol: _Symbol, nm: DemangledName | None) -> int | None:
    mention = nm.simple.lstrip("~") if nm else symbol.name
    if not _PLAIN_IDENTIFIER_RE.fullmatch(mention or ""):
        return None
    return first_mention_line(src, mention)


def _defines_terminate_helper(symbols: list[tuple[str, str, str]]) -> bool:
    return _NOEXCEPT_TERMINATE_HELPER in {name for name, nm_type, _ in symbols if nm_type not in UNDEFINED_TYPES}


def check_cpp_object_symbols(
    spec: Spec,
    rel: str,
    symbols: list[tuple[str, str, str]],
    *,
    facts: HeaderFacts,
    defined_elsewhere: set[str],
    src: CSource | None = None,
) -> list[PolicyViolation]:
    context = _ObjectContext(facts, defined_elsewhere, _defines_terminate_helper(symbols), spec.target.function)
    out: list[PolicyViolation] = []
    for name, nm_type, demangled in symbols:
        symbol = _Symbol(name, nm_type, demangled)
        nm = parse_demangled(demangled) if symbol.is_mangled else None
        if nm_type in UNDEFINED_TYPES:
            problem = _import_problem(symbol, nm, context)
        elif has_external_linkage(nm_type):
            problem = _definition_problem(symbol, nm, context)
        else:
            continue
        if problem:
            out.append(violation(problem.rule, problem.message, rel, _mention_line(src, symbol, nm)))
    return out


def _is_target_definition(name: str, nm_type: str, demangled: str, target: str) -> bool:
    if nm_type not in TEXT_DEFINITION_TYPES:
        return False
    if not name.startswith(ITANIUM_MANGLING_PREFIX):
        return name == target and nm_type == STRONG_TEXT_TYPE
    nm = parse_demangled(demangled)
    return nm is not None and nm.special is None and nm.is_function and nm.simple == target


def defines_target(symbols: list[tuple[str, str, str]], target: str) -> bool:
    return any(_is_target_definition(name, nm_type, demangled, target) for name, nm_type, demangled in symbols)
