from __future__ import annotations

from leanc.rules.acsl import ACSL_RULES
from leanc.rules.c_rules import RULES

__all__ = ["CPP_RULES"]

_C_RULES_WITHOUT_ACSL = {rule: text for rule, text in RULES.items() if rule not in ACSL_RULES}

CPP_RULES: dict[str, str] = {
    **_C_RULES_WITHOUT_ACSL,
    "SYMBOL_OVERRIDE": RULES["SYMBOL_OVERRIDE"] + "; for C++ also: replacements of the global operator "
    "new/delete, anything in namespace std (namespace std blocks, explicit specialisations, std:: definitions), "
    "macros defined before an #include, and defined external C++ entities (functions, members, classes, "
    "template instantiations, vtables/typeinfo) the protected headers do not declare",
    "PRAGMA": "a #pragma / _Pragma other than '#pragma once' (they can disable diagnostics or analyzer checks, or "
    "mark code as a system header)",
    "TERMINATION": "process termination or non-local control flow (exit, std::_Exit, quick_exit, abort, "
    "std::terminate/set_terminate, atexit, exit-time destructors, longjmp, raise, signal, __builtin_trap, ...): a "
    "test could end with success before its checks run",
    "UNBOUNDED_STACK": "variable-length arrays (a Clang extension in C++) or alloca: input-sized stack allocations",
    "EXCEPTIONS": "C++ exceptions (throw, try/catch, function-try-blocks, <exception>/<stdexcept>, __cxa_throw & "
    "co.): the static analyzer does not follow exceptional control flow, and a catch could swallow a failed "
    "library precondition check",
    "CPP_RUNTIME": "C++ runtime features outside the verified subset: RTTI (typeid, dynamic_cast, <typeinfo>) and "
    "coroutines (co_await/co_yield/co_return, <coroutine>)",
}
