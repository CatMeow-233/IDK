"""C++ rules of the mandatory source-integrity gate (V2 C++ support).

The C gate (:mod:`leanc.backends.integrity`) keeps the code under test from
tampering with the verification itself.  For a C++ spec the same principles
apply, adapted to what C++ objects and sources look like; this module holds
the C++ rules, which :func:`leanc.backends.integrity.run_cpp_integrity` (via
:func:`leanc.backends.integrity.run_integrity`) and
:func:`~leanc.rules.source.source_violations` apply when
``spec.language.is_cpp``.

What is different for C++, and why
----------------------------------
* **No model checker judges C++ here** (no C++20-capable checker is
  configured; CBMC/WP are rejected for C++ by the spec schema).  The judges
  are clang++ (compile), the Clang Static Analyzer and the trusted test
  driver under ASan/UBSan/LSan, all the *same* clang++ with the same ``-std``.
  The threats are therefore: faking a passing test (process termination with
  status 0, code running before/after ``main``), hiding runtime errors from
  the sanitizers (replacing ``operator new``/``delete``, sanitizer control),
  hiding paths from the analyzer (exceptions, ``[[noreturn]]``, suppression),
  and replacing code the trusted driver relies on (definitions in namespace
  ``std``, libc names, macros that rewrite included headers).
* **Mangled names.**  Object symbols are demangled (``llvm-cxxfilt`` /
  ``c++filt``) and judged by their qualified name: every *defined* external
  symbol must be an entity named by the protected headers (namespaces,
  classes/templates, functions, operators declared there), exactly like the C
  rule "external symbols must be declared by the protected headers".
* **Weak/COMDAT definitions are normal in C++** (inline functions, template
  instantiations, vtables, typeinfo, guard variables).  A weak definition is
  accepted only if it is (a) an entity of the protected headers (e.g. the
  explicit instantiation ``template class RingBuffer<int, 4>;``, the vtable
  of a header class, a local static of a header function), (b) an implicit
  instantiation of a standard-library template (``std::array<int,
  4>::operator[]``) - which is only safe because the source rules forbid
  writing *anything* into namespace ``std`` (``namespace std``, explicit
  specialisations of ``std`` templates, ``std::``-qualified definitions) and
  forbid macros before ``#include`` (which could rewrite the library code
  this TU instantiates, an ODR violation the linker would resolve for the
  driver too), or (c) one of two compiler helpers (``__clang_call_terminate``,
  ``DW.ref.__gxx_personality_v0``).  Global ``operator new``/``delete``
  replacements, ``__cxa_*``, ``std::`` entities, libc names, ``main``,
  ``nondet_*`` and reserved names are never accepted.
* **Imports**: the C allowlist plus the C++ runtime pieces simple C++20 code
  needs: the standard global ``operator new``/``delete`` forms (ASan
  intercepts them), ``std::nothrow``, ``__cxa_pure_virtual`` /
  ``__cxa_deleted_virtual``, thread-safe-static guards, the unwinder pieces
  of cleanup landing pads (``__gxx_personality_v0``, ``_Unwind_Resume``), the
  typeinfo vtables of polymorphic header classes, the libstdc++
  precondition-failure helpers (``std::__throw_*``,
  ``std::__glibcxx_assert_fail``) and ``std::terminate`` /
  ``__cxa_begin_catch`` *only* when they come from the compiler's
  ``__clang_call_terminate`` (exception escaping a ``noexcept`` function:
  the process aborts, which fails the test - it can never fake success).

Decisions (documented in RULES)
-------------------------------
* **Exceptions are forbidden** (``throw``, ``try``/``catch``, function-try
  blocks, ``<exception>``/``<stdexcept>``, ``__cxa_throw`` & co.): the Clang
  Static Analyzer does not follow exceptional control flow, so defects on
  those paths would be invisible to one of the judges, and a ``catch`` could
  swallow the failure of a library precondition check.  Library helpers may
  still throw; an exception escaping code under test terminates the test
  process (a *failure*).
* **RTTI escapes and coroutines are forbidden** (``typeid``,
  ``dynamic_cast``, ``<typeinfo>``, ``co_await``/``co_yield``/``co_return``,
  ``<coroutine>``): they pull in unmodelled runtime support and implicit
  allocation, and coroutine bodies are only partially analysed.
* **Threads, signals, setjmp, process control** stay forbidden as in C
  (``std::thread``/``jthread``/``async``..., ``<thread>``, ``<csignal>``,
  ``<csetjmp>``, ``std::exit``/``_Exit``/``quick_exit``/``abort``/
  ``terminate``/``set_terminate``).
* **Static initialisation**: a global object with a non-trivial
  constructor makes the object carry ``.init_array`` (CONSTRUCTOR, as in C);
  a function-local static with a destructor registers it with
  ``__cxa_atexit`` (TERMINATION, as in C).
* **Preprocessor**: as in C, conditionals on reserved/compiler-specific
  macros are TOOL_DIVERGENCE - except ``__cplusplus`` and the standard
  *core-language* feature-test macros ``__cpp_*``.  Every C++ gate runs the
  same clang++ with the same approved ``-std``, so these have the same value
  in the compile, analyzer, sanitizer and integrity runs; the few feature
  macros that a compiler *flag* can change (``__cpp_exceptions``,
  ``__cpp_rtti``, ``__cpp_sized_deallocation``, ...; the analyzer run does
  not see ``compile.flags``) and the library macros ``__cpp_lib_*`` stay
  forbidden.  ``#define``/``#undef`` before an ``#include`` is forbidden.
  Angle-bracket includes must be C headers (``<x.h>``, as in C) or C++
  standard headers from :data:`CPP_STD_HEADERS`.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

import leanc.rules.c_rules as ci
from leanc.foundation.models import PolicyViolation
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.rules.definitions import NOT_A_FUNCTION, FunctionDefinition, is_trivial_body
from leanc.specs.model import Spec

__all__ = [
    "CPP_ALLOWED_RUNTIME_IMPORTS",
    "CPP_RULES",
    "CPP_STD_HEADERS",
    "HeaderFacts",
    "check_cpp_object_symbols",
    "cpp_function_definitions",
    "cpp_preprocessed_violations",
    "cpp_source_violations",
    "find_cpp_function_definitions",
    "header_facts",
    "parse_demangled",
]

CPP_RULES: dict[str, str] = {
    **{k: v for k, v in ci.RULES.items() if not k.startswith("ACSL_")},  # no ACSL/WP for C++
    "SYMBOL_OVERRIDE": ci.RULES["SYMBOL_OVERRIDE"] + "; for C++ also: replacements of the global operator "
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

# =========================================================================== header facts
_OPERATOR_RE = re.compile(
    r"\boperator\b\s*(?:(?P<kw>new|delete|co_await)\b\s*(?P<arr>\[\s*\])?"
    r"|(?P<sym>\(\s*\)|\[\s*\]|->\*?|<=>|<<=?|>>=?|&&|\|\||\+\+|--|[-+*/%^&|!=<>]=?|,|~)"
    r'|(?P<lit>""\s*\w+)'
    r"|(?P<conv>[A-Za-z_]\w*))"
)


def _operator_key(m: re.Match) -> str:
    if m.group("kw"):
        return "operator " + m.group("kw") + ("[]" if m.group("arr") else "")
    if m.group("sym"):
        return "operator" + re.sub(r"\s+", "", m.group("sym"))
    if m.group("lit"):
        return 'operator""' + m.group("lit")[2:].strip()
    return "operator " + m.group("conv")


@dataclass
class HeaderFacts:
    """What the protected headers declare (comment-/literal-free views)."""

    idents: set[str] = field(default_factory=set)
    macros: set[str] = field(default_factory=set)
    operators: set[str] = field(default_factory=set)


def header_facts(headers: dict[str, str] | None) -> HeaderFacts:
    facts = HeaderFacts()
    for text in (headers or {}).values():
        src = CppSource(text)
        facts.idents |= ci._identifiers(src.code)
        for m in ci._DIRECTIVE_RE.finditer(src.code):
            if m.group(1) == "define":
                dm = ci._DEFINE_HEAD_RE.match(m.group(2))
                if dm:
                    facts.macros.add(dm.group(1))
        for m in _OPERATOR_RE.finditer(src.code_nodirectives):
            facts.operators.add(_operator_key(m))
    return facts


# =========================================================================== demangled names
_SPECIAL_PREFIXES: tuple[str, ...] = (
    "vtable for ",
    "VTT for ",
    "typeinfo for ",
    "typeinfo name for ",
    "typeinfo fn for ",
    "guard variable for ",
    "non-virtual thunk to ",
    "virtual thunk to ",
    "covariant return thunk to ",
    "TLS init function for ",
    "TLS wrapper function for ",
    "transaction clone for ",
    "template parameter object for ",
    "hidden alias for ",
)
_REF_TEMP_RE = re.compile(r"^reference temporary #\d+ for ")
_CONSTRUCTION_VTABLE = "construction vtable for "
_SYM_OPS = sorted(
    ["()", "[]", "->*", "->", "<=>", "<<=", ">>=", "<<", ">>", "&&", "||", "++", "--", "==", "!=", "<=", ">=",
     "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "+", "-", "*", "/", "%", "^", "&", "|", "~", "!", "=", "<",
     ">", ","],
    key=len,
    reverse=True,
)  # fmt: skip
_OPENERS = {"<": ">", "(": ")", "[": "]", "{": "}"}


@dataclass(frozen=True)
class DemangledName:
    """A demangled symbol, reduced to what the rules need.

    ``components`` is the qualified name without template arguments and
    parameter lists (``lc::RingBuffer<int, 4ul>::push(int const&)`` ->
    ``("lc", "RingBuffer", "push")``); ``owner`` is the prefix up to and
    including the first *function* component (entities local to a function -
    static locals, lambdas, local classes - are judged by their function);
    ``special`` is a compiler-generated wrapper such as ``"vtable for"``;
    ``inner`` holds the entities a special symbol refers to."""

    text: str
    components: tuple[str, ...]
    owner: tuple[str, ...]
    is_function: bool
    special: str | None = None
    inner: tuple["DemangledName", ...] = ()

    @property
    def simple(self) -> str:
        return self.components[-1] if self.components else ""


def _skip_group(s: str, i: int) -> int:
    """Index after the bracket group opening at ``s[i]`` (all kinds nest)."""
    stack = [_OPENERS[s[i]]]
    j = i + 1
    while j < len(s) and stack:
        c = s[j]
        if c in _OPENERS:
            stack.append(_OPENERS[c])
        elif c == stack[-1]:
            stack.pop()
        j += 1
    return j


def _read_operator(s: str, i: int) -> tuple[str, int]:
    """Parse an operator name starting at ``s[i:] == "operator..."``."""
    j = i + len("operator")
    rest = s[j:]
    for op in _SYM_OPS:
        if rest.startswith(op):
            return "operator" + op, j + len(op)
    if rest.startswith('""'):
        k = j + 2
        while k < len(s) and s[k] == " ":
            k += 1
        e = k
        while e < len(s) and (s[e].isalnum() or s[e] == "_"):
            e += 1
        return 'operator""' + s[k:e], e
    k = j
    while k < len(s) and s[k] == " ":
        k += 1
    for kw in ("new[]", "delete[]", "new", "delete", "co_await"):
        if s.startswith(kw, k):
            return "operator " + kw, k + len(kw)
    # conversion operator: the type runs up to the parameter list
    e = k
    while e < len(s) and s[e] != "(":
        e = _skip_group(s, e) if s[e] in "<[{" else e + 1
    conv = s[k:e].strip()
    word = re.match(r"[A-Za-z_]\w*", conv)
    return "operator " + (word.group() if word else conv), e


def _parse_core(s: str) -> DemangledName | None:
    comps: list[str] = []
    owner: tuple[str, ...] = ()
    cur = ""
    is_function = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if s.startswith("operator", i) and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")) and (
            i + 8 >= n or not (s[i + 8].isalnum() or s[i + 8] == "_")
        ):
            cur, i = _read_operator(s, i)
            continue
        if s.startswith("(anonymous namespace)", i):
            cur = "(anonymous namespace)"
            i += len("(anonymous namespace)")
            continue
        if c == "{":  # {lambda(int)#1}, {unnamed type#1}, {default arg#1}
            e = _skip_group(s, i)
            hm = re.match(r"\{[^}#(]*", s[i:e])
            cur = (hm.group() if hm else "{") + "}"
            i = e
            continue
        if c == "<":
            i = _skip_group(s, i)  # template arguments are not part of the name
            continue
        if c == "[":  # ABI tag [abi:cxx11]
            i = _skip_group(s, i)
            continue
        if c == "(" and not cur and not comps:
            # declarator parentheses of a function returning a function
            # pointer: "void (*&ns::f<...>(args))()"
            i += 1
            while i < n and s[i] in "*& ":
                i += 1
            continue
        if c == "(":
            if cur:
                comps.append(cur)
                cur = ""
            if not owner:
                owner = tuple(comps)
            is_function = True
            i = _skip_group(s, i)
            # cv / ref qualifiers of a member function: " const", " &&", ...
            while i < n and s[i] != ":":
                i += 1
            if s.startswith("::", i):
                i += 2
                is_function = False  # a local entity of that function follows
                continue
            break
        if s.startswith("::", i):
            if cur:
                comps.append(cur)
            cur = ""
            i += 2
            continue
        if c == " ":
            # a return type before the name (function template specialisations)
            if cur and not comps:
                cur = ""
            elif cur or comps:
                comps, cur = [], ""
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        comps.append(cur)
    if not comps:
        return None
    return DemangledName(text=s, components=tuple(comps), owner=owner or tuple(comps), is_function=is_function)


def parse_demangled(text: str) -> DemangledName | None:
    """Parse a demangled C++ symbol name (see :class:`DemangledName`)."""
    s = text.strip()
    if s.startswith(_CONSTRUCTION_VTABLE):
        parts = s[len(_CONSTRUCTION_VTABLE) :].split("-in-")
        inner = tuple(p for p in (_parse_core(x) for x in parts) if p is not None)
        if len(inner) != len(parts):
            return None
        return DemangledName(text=s, components=inner[-1].components, owner=inner[-1].owner, is_function=False,
                             special="construction vtable for", inner=inner)
    rm = _REF_TEMP_RE.match(s)
    prefix = rm.group() if rm else next((p for p in _SPECIAL_PREFIXES if s.startswith(p)), None)
    if prefix:
        inner_name = parse_demangled(s[len(prefix) :])
        if inner_name is None:
            return None
        return DemangledName(text=s, components=inner_name.components, owner=inner_name.owner,
                             is_function=False, special=prefix.strip(), inner=(inner_name,))
    return _parse_core(s)


# =========================================================================== scopes & definitions
_NAMESPACE_HEAD_RE = re.compile(r"(?:\binline\s+)?\bnamespace\b(?:\s+[\w:]+)?\s*(?:\[\[[^\]]*\]\]\s*)?$")
_EXTERN_HEAD_RE = re.compile(r'\bextern\s*"(C|C\+\+)"\s*$')
# a class/struct/union body: the head names the class-key and has no parameter list
_CLASS_HEAD_RE = re.compile(r"\b(?:class|struct|union)\b[^()]*$")
#: bounds of the backward / forward scans (keep hostile input linear)
_HEAD_WINDOW = 512
_SCAN_WINDOW = 4096
_DECL_END_RE = re.compile(r"[{;]")
_FN_NAME_RE = re.compile(
    r"(?<![\w.])(?<!->)(?:(~)\s*)?\b(operator\b\s*(?:new\b|delete\b)\s*(?:\[\s*\])?|[A-Za-z_]\w*)\s*\("
)
_NOT_A_FUNCTION = NOT_A_FUNCTION | frozenset(
    {"decltype", "noexcept", "requires", "typeid", "catch", "throw", "new", "delete", "return", "co_return",
     "co_await", "co_yield", "static_cast", "dynamic_cast", "const_cast", "reinterpret_cast", "template",
     "explicit", "operator", "case", "do", "else", "using", "alignas", "__attribute__", "assert"}
)  # fmt: skip
_TAIL_WORDS = frozenset({"const", "volatile", "override", "final", "mutable", "constexpr", "consteval"})


def _blank_directive_lines(view: str, code: str) -> str:
    """``view`` with the lines that are directives in ``code`` blanked."""
    return "\n".join(
        re.sub(r"[^\n]", " ", v) if c.lstrip().startswith("#") else v
        for v, c in zip(view.split("\n"), code.split("\n"))
    )


@dataclass(frozen=True)
class CppDefinition:
    """A function definition at namespace scope (incl. extern "C" blocks)."""

    name: str  # unqualified: "push", "~RingBuffer", "operator new"
    qualifier: str  # "", "::", "RingBuffer<T,N>::", "lc::", ...
    offset: int  # of the name, in the code views
    scope: str  # "global" | "namespace" | "anonymous" | "extern_c"
    body: str  # code view between the braces ("" for = default / = delete)
    has_body: bool


class _Scopes:
    """Classifies every ``{`` of a file as namespace-like (``namespace``,
    ``extern "C"``/``"C++"`` blocks) or not (class, function, initializer)."""

    def __init__(self, src: CSource):
        code = src.code_nodirectives
        noc = _blank_directive_lines(src.nocomment, src.code)
        self.marks: list[int] = []
        self.state: list[tuple[int, str]] = []  # (open non-namespace scopes, innermost kind) after the mark
        stack: list[str] = []
        others = 0
        last_stmt = 0
        for m in re.finditer(r"[{};]", code):
            ch = m.group()
            if ch == "{":
                # only the end of the head matters (bounded: hostile heads cannot make this quadratic)
                lo = max(last_stmt, m.start() - _HEAD_WINDOW)
                head, chead = noc[lo : m.start()], code[lo : m.start()]
                em = _EXTERN_HEAD_RE.search(head)
                if em:
                    kind = "extern_c" if em.group(1) == "C" else "global"
                elif _NAMESPACE_HEAD_RE.search(chead):
                    kind = "anonymous" if re.search(r"namespace\s*$", chead) else "namespace"
                elif _CLASS_HEAD_RE.search(chead):
                    kind = "class"
                else:
                    kind = "other"
                stack.append(kind)
                others += kind in ("class", "other")
            elif ch == "}" and stack:
                others -= stack.pop() in ("class", "other")
            last_stmt = m.end()
            if ch != ";":
                self.marks.append(m.start())
                self.state.append((others, stack[-1] if stack else "global"))

    def innermost(self, offset: int) -> str:
        """Kind of the innermost brace scope at ``offset``."""
        j = bisect.bisect_left(self.marks, offset) - 1
        return self.state[j][1] if j >= 0 else "global"

    def at(self, offset: int) -> str | None:
        """Scope kind at ``offset``, or None inside a class/function/initializer."""
        j = bisect.bisect_left(self.marks, offset) - 1
        if j < 0:
            return "global"
        others, kind = self.state[j]
        return None if others else kind


def _qualifier_before(code: str, start: int) -> str:
    """The ``A<..>::B::`` (or ``::``) written right before offset ``start``
    (whitespace removed); "" for an unqualified name."""
    j = start
    parts: list[str] = []
    for _ in range(64):
        k = j
        while k > 0 and code[k - 1].isspace():
            k -= 1
        if code[max(0, k - 2) : k] != "::":
            break
        k -= 2
        end = k
        while k > 0 and code[k - 1].isspace():
            k -= 1
        if k > 0 and code[k - 1] == ">":  # template arguments: match backwards
            depth = 0
            stop = max(0, k - _HEAD_WINDOW)
            while k > stop:
                k -= 1
                if code[k] == ">":
                    depth += 1
                elif code[k] == "<":
                    depth -= 1
                    if depth == 0:
                        break
            while k > 0 and code[k - 1].isspace():
                k -= 1
        w = k
        while k > 0 and (code[k - 1].isalnum() or code[k - 1] == "_"):
            k -= 1
        parts.append(re.sub(r"\s+", "", code[k:end]) + "::")
        j = k
        if w == k:  # "::name": global qualifier (or something odd)
            break
    return "".join(reversed(parts))


def _definition_body(
    code: str, pairs: dict[int, int], j: int, ends: list[int] | None = None
) -> tuple[int, int] | None | str:
    """After the parameter list (``j`` = index after ``)``): the body's
    ``(open, close)``, ``"default"`` for ``= default/delete``, else None."""
    n = len(code)
    if ends is None:
        ends = [t.start() for t in _DECL_END_RE.finditer(code)]

    def decl_end(k: int) -> int | None:
        """First '{' or ';' at or after k (within the scan window)."""
        e = bisect.bisect_left(ends, k)
        return ends[e] if e < len(ends) and ends[e] - k <= _SCAN_WINDOW else None

    for _ in range(256):
        while j < n and code[j].isspace():
            j += 1
        if j >= n:
            return None
        c = code[j]
        if c == "{":
            close = pairs.get(j)
            return (j, close if close is not None else n)
        if code.startswith("[[", j) and (c2 := pairs.get(j)) is not None:
            j = c2 + 1
            continue
        am = re.match(r"(?:__attribute__|__attribute|__declspec)\s*\(", code[j : j + 40])
        if am and (c2 := pairs.get(j + am.end() - 1)) is not None:
            j = c2 + 1
            continue
        wm = re.match(r"[A-Za-z_]\w*", code[j : j + 64])
        if wm:
            w = wm.group()
            j += len(w)
            if w in _TAIL_WORDS:
                continue
            if w in ("noexcept", "throw"):
                k = j
                while k < n and code[k].isspace():
                    k += 1
                if k < n and code[k] == "(" and (c2 := pairs.get(k)) is not None:
                    j = c2 + 1
                continue
            if w == "requires":  # requires-clause: up to the body
                e = decl_end(j)
                if e is None:
                    return None
                j = e
                continue
            return None  # e.g. "try" (function-try-block) or not a declarator
        if code.startswith("&&", j):
            j += 2
            continue
        if c == "&":
            j += 1
            continue
        if code.startswith("->", j):  # trailing return type: up to the body (or ';')
            e = decl_end(j + 2)
            if e is None:
                return None
            j = e
            continue
        if c == ":" and not code.startswith("::", j):  # constructor initializer list
            j += 1
            for _ in range(256):
                while j < n and code[j].isspace():
                    j += 1
                mm = re.match(r"(?:::)?[A-Za-z_][\w:]*\s*(?:<[^;{}()]*>)?\s*", code[j : j + 400])
                if not mm:
                    return None
                j += mm.end()
                if j < n and code[j] in "({" and (c2 := pairs.get(j)) is not None:
                    j = c2 + 1
                else:
                    return None
                while j < n and code[j].isspace():
                    j += 1
                if code.startswith("...", j):
                    j += 3
                    while j < n and code[j].isspace():
                        j += 1
                if j < n and code[j] == ",":
                    j += 1
                    continue
                break
            if j < n and code[j] == "{":  # the body must follow the initializer list
                close = pairs.get(j)
                return (j, close if close is not None else n)
            return None
        if c == "=":
            rest = code[j + 1 : j + 40].lstrip()
            return "default" if rest.startswith(("default", "delete")) else None
        return None
    return None


def cpp_function_definitions(src: CSource) -> list[CppDefinition]:
    """Function definitions at namespace scope (global, named or anonymous
    namespace, ``extern "C"`` blocks), including out-of-line member
    definitions (``bool RingBuffer<T, N>::push(const T& v) noexcept {``).
    Definitions inside class bodies or produced by macros are not found."""
    code = src.code_nodirectives
    pairs = src.pairs_nodirectives
    scopes = _Scopes(src)
    ends = [t.start() for t in _DECL_END_RE.finditer(code)]
    out: list[CppDefinition] = []
    for m in _FN_NAME_RE.finditer(code):
        name = m.group(2)
        if name in _NOT_A_FUNCTION:
            continue
        scope = scopes.at(m.start())
        if scope is None:
            continue
        close = pairs.get(m.end() - 1)
        if close is None:
            continue
        body = _definition_body(code, pairs, close + 1, ends)
        if body is None:
            continue
        name = re.sub(r"\s+", " ", name).replace(" [", "[").replace("[ ", "[").replace(" ]", "]")
        if name.startswith("operator"):
            name = re.sub(r"^operator\s*", "operator ", name)
        full = ("~" if m.group(1) else "") + name
        start = m.start(1) if m.group(1) else m.start(2)
        qual = _qualifier_before(code, start)
        if body == "default":
            out.append(CppDefinition(full, qual, start, scope, "", False))
        else:
            o, c = body
            out.append(CppDefinition(full, qual, start, scope, code[o + 1 : c], True))
    return out


def find_cpp_function_definitions(src: CSource, name: str, file: str = "") -> list[FunctionDefinition]:
    """C++ counterpart of :func:`leanc.rules.definitions.find_function_definitions`
    (used by the repair/generation policy for MISSING_TARGET and
    TRIVIALIZED_TARGET): definitions with a body of a function or member
    function whose unqualified name is ``name``."""
    if not isinstance(src, CppSource):
        src = CppSource(src.raw)
    return [
        FunctionDefinition(file, src.line_of(d.offset), d.body, is_trivial_body(d.body))
        for d in cpp_function_definitions(src)
        if d.name == name and d.has_body
    ]


# =========================================================================== level A: source
#: C++ standard library headers an editable file may include with <...>
#: (besides C headers <x.h>, as in C).  Headers of forbidden facilities are
#: listed in _FORBIDDEN_STD_HEADERS with the rule they belong to; anything
#: else (iostreams, <chrono>, <random>, <filesystem>, third-party headers ...)
#: is INCLUDE_ESCAPE.
CPP_STD_HEADERS: frozenset[str] = frozenset(
    {
        "algorithm", "array", "bit", "bitset", "cassert", "cctype", "cerrno", "cfloat", "charconv",
        "cinttypes", "climits", "cmath", "compare", "concepts", "cstddef", "cstdint", "cstdio", "cstdlib",
        "cstring", "deque", "expected", "forward_list", "functional", "initializer_list", "iterator", "limits",
        "list", "map", "mdspan", "memory", "new", "numbers", "numeric", "optional", "queue", "ranges", "ratio",
        "set", "source_location", "span", "stack", "string", "string_view", "tuple", "type_traits",
        "unordered_map", "unordered_set", "utility", "variant", "vector", "version",
    }
)  # fmt: skip
_FORBIDDEN_STD_HEADERS: dict[str, tuple[str, str]] = {
    **dict.fromkeys(
        ("thread", "mutex", "shared_mutex", "condition_variable", "future", "semaphore", "latch", "barrier",
         "stop_token", "atomic", "pthread.h", "threads.h"),
        ("DANGEROUS_IMPORT", "threads / synchronisation are not allowed in code under verification"),
    ),
    **dict.fromkeys(("csignal", "csetjmp", "signal.h", "setjmp.h"),
                    ("TERMINATION", "signals and non-local jumps escape the function under test")),
    **dict.fromkeys(("exception", "stdexcept"), ("EXCEPTIONS", "code under verification may not use exceptions")),
    "coroutine": ("CPP_RUNTIME", "coroutines are not allowed in code under verification"),
    "typeinfo": ("CPP_RUNTIME", "RTTI (typeid) is not allowed in code under verification"),
}  # fmt: skip

CPP_KEYWORDS: frozenset[str] = ci.C_KEYWORDS | frozenset(
    {
        "alignas", "alignof", "and", "and_eq", "asm", "bitand", "bitor", "bool", "catch", "char8_t", "char16_t",
        "char32_t", "class", "compl", "concept", "consteval", "constexpr", "constinit", "const_cast", "co_await",
        "co_return", "co_yield", "decltype", "delete", "dynamic_cast", "explicit", "export", "false", "final",
        "friend", "import", "module", "mutable", "namespace", "new", "noexcept", "not", "not_eq", "nullptr",
        "operator", "or", "or_eq", "override", "private", "protected", "public", "reinterpret_cast", "requires",
        "static_assert", "static_cast", "template", "this", "thread_local", "throw", "true", "try", "typeid",
        "typename", "using", "virtual", "wchar_t", "xor", "xor_eq",
    }
)  # fmt: skip

#: ``__cpp_*`` feature-test macros a compiler *flag* can change (and the
#: analyzer run does not see compile.flags): never allowed in conditionals.
_FLAG_DEPENDENT_FEATURES = frozenset(
    {"__cpp_exceptions", "__cpp_rtti", "__cpp_sized_deallocation", "__cpp_aligned_new", "__cpp_char8_t",
     "__cpp_impl_coroutine", "__cpp_coroutines", "__cpp_threadsafe_static_init", "__cpp_modules"}
)  # fmt: skip


def _portable_macro(name: str) -> bool:
    """Reserved macros that have the same value in every C++ gate (see the
    module docstring): ``__cplusplus`` and core ``__cpp_*`` feature macros."""
    if name == "__cplusplus":
        return True
    return name.startswith("__cpp_") and not name.startswith("__cpp_lib_") and name not in _FLAG_DEPENDENT_FEATURES


def _cpp_include_problem(spec: Spec, name: str, quoted: bool) -> tuple[str, str] | None:
    problem = ci._include_problem(spec, name, quoted)
    base = name.strip()
    if problem is None:
        if not quoted and base in _FORBIDDEN_STD_HEADERS:
            return _FORBIDDEN_STD_HEADERS[base]
        return None
    if quoted or problem != f"<{name}> is not a header":
        return ("INCLUDE_ESCAPE", problem)
    if base in _FORBIDDEN_STD_HEADERS:
        return _FORBIDDEN_STD_HEADERS[base]
    if base in CPP_STD_HEADERS or base.endswith((".hpp", ".hh", ".hxx", ".h++")):
        return None
    return ("INCLUDE_ESCAPE", f"<{name}> is not an allowed C++ standard library header "
                              f"(allowed: C headers <*.h> and {', '.join(sorted(CPP_STD_HEADERS))})")


def _cpp_directive_violations(spec: Spec, src: CSource, header_macros: set[str]) -> list[tuple[str, str, int]]:
    """C directive rules (see c_rules._directive_violations) with the C++
    include rules, C++ keywords and portable feature-test macros."""
    code, noc = src.code, src.nocomment
    out: list[tuple[str, str, int]] = []
    override_names = ci.LIBC_NAMES | CPP_KEYWORDS | header_macros | {spec.target.function}
    last_include = -1
    defines: list[tuple[str, int]] = []
    for m in ci._DIRECTIVE_RE.finditer(code):
        name, rest = m.group(1), m.group(2)
        at = m.start()
        if name == "":
            if rest.strip():
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
        if name not in ci._STD_DIRECTIVES:
            out.append(("TOOL_DIVERGENCE", f"non-standard directive #{name} (preprocessors disagree on it)", at))
            continue
        if name == "pragma":
            body = " ".join(noc[m.start(2) : m.end(2)].split())
            if body != "once":
                out.append(("PRAGMA", (f"`#pragma {body[:60]}` is not allowed (pragmas can disable checks or "
                            "diagnostics, or mark code as a system header)"), at))
            continue
        if name == "include":
            last_include = at
            body = noc[m.start(2) : m.end(2)].strip()
            hm = ci._HEADER_NAME_RE.match(body)
            if not hm:
                out.append(("INCLUDE_ESCAPE", f"computed #include ({body[:40]!r}) cannot be checked", at))
                continue
            problem = _cpp_include_problem(spec, hm.group(1)[1:-1].strip(), hm.group(1)[0] == '"')
            if problem:
                out.append((problem[0], f"#include {hm.group(1)}: {problem[1]}", at))
            continue
        if name in ("error", "warning", "else", "endif"):
            continue
        idents = ci._DIRECTIVE_IDENT_RE.findall(rest)
        if name in ("define", "undef"):
            dm = ci._DEFINE_HEAD_RE.match(rest)
            macro = dm.group(1) if dm else ""
            defines.append((macro, at))
            if macro in override_names and not ci._DIRECTIVE_IDENT_RE.fullmatch(macro):
                what = (
                    "the target function" if macro == spec.target.function
                    else "a macro of the protected interface" if macro in header_macros
                    else "a C++ keyword" if macro in CPP_KEYWORDS
                    else "a C library name"
                )
                out.append(("SYMBOL_OVERRIDE", f"#{name} {macro}: redefining {what} is not allowed", at))
        else:
            idents = [i for i in idents if not _portable_macro(i)]
        if idents:
            uniq = ", ".join(dict.fromkeys(idents))
            out.append(("TOOL_DIVERGENCE", (f"#{name} on {uniq}: compiler-, optimiser- or verifier-specific "
                        "macros make the tools see different code"), at))
        if name in ("if", "elif") and "#" in rest:
            out.append(("TOOL_DIVERGENCE", (f"#{name} uses an assertion predicate (#...), which preprocessors "
                        "treat differently"), at))
    for macro, at in defines:
        if at < last_include:
            out.append(("SYMBOL_OVERRIDE", (f"macro '{macro}' is (un)defined before an #include: it can rewrite the "
                        "included header's inline/template code in this translation unit only (an ODR violation "
                        "the linker may resolve in favour of this copy for the trusted test driver too)"), at))
    return out


_CPP_TOKEN_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("EXCEPTIONS", re.compile(r"\b(?:throw|try|catch)\b"), "C++ exception handling"),
    (
        "EXCEPTIONS",
        re.compile(r"\b(?:rethrow_exception|rethrow_if_nested|throw_with_nested|make_exception_ptr|"
                   r"current_exception|exception_ptr|uncaught_exceptions?|__cxa_\w+)\b"),
        "exception-handling facility",
    ),
    ("TERMINATION", re.compile(r"(?<![\w.])(?<!->)\b(?:terminate|set_terminate|get_terminate)(?=\s*\()"),
     "std::terminate"),
    ("CPP_RUNTIME", re.compile(r"\b(?:typeid|dynamic_cast)\b"), "run-time type information"),
    ("CPP_RUNTIME", re.compile(r"\bco_(?:await|yield|return)\b"), "coroutine"),
    (
        "DANGEROUS_IMPORT",
        re.compile(r"\bstd\s*::\s*(?:this_thread|thread|jthread|async|launch|stop_token|stop_source|barrier|latch|"
                   r"counting_semaphore|binary_semaphore|condition_variable\w*|mutex|recursive_mutex|timed_mutex|"
                   r"shared_mutex|shared_timed_mutex|future|shared_future|promise|packaged_task|atomic\w*|"
                   r"system|getenv)\b"),
        "thread / process facility",
    ),
    (
        "TERMINATION",
        re.compile(r"\bstd\s*::\s*(?:exit|_Exit|quick_exit|abort|atexit|at_quick_exit|terminate|set_terminate|"
                   r"signal|raise|longjmp)\b"),
        "process termination / signal / non-local jump",
    ),
    (
        "SYMBOL_OVERRIDE",
        re.compile(r"\bnamespace\s+(?:inline\s+)?(?:std|__\w+|_[A-Z]\w*|abi)\b"),
        "declarations in namespace std (or a reserved namespace), which can replace standard library code",
    ),
]
_EXPLICIT_SPEC_RE = re.compile(r"\btemplate\s*<\s*>")
_STD_QUAL_RE = re.compile(r"(?<![\w:])(?:::\s*)?std\s*::")
_FROM_NAME_RULES = ci.TERMINATION_NAMES | ci.DANGEROUS_NAMES


def _member_qualified(code: str, off: int) -> bool:
    """``X::name`` / ``X<..>::name`` with ``X`` not ``std``: a member or a
    project function, not the C library function of the same name."""
    k = off
    while k > 0 and code[k - 1].isspace():
        k -= 1
    if code[max(0, k - 2) : k] != "::":
        return False
    k -= 2
    while k > 0 and code[k - 1].isspace():
        k -= 1
    if k > 0 and code[k - 1] == ">":
        return True
    e = k
    while k > 0 and (code[k - 1].isalnum() or code[k - 1] == "_"):
        k -= 1
    qual = code[k:e]
    return bool(qual) and qual != "std"


def _cpp_token_violations(src: CSource, where: str = "") -> list[tuple[str, str, int]]:
    code = src.code
    out: list[tuple[str, str, int]] = []
    scopes: _Scopes | None = None
    for rule, msg, off in ci._token_violations(src, where):
        if rule in ("TERMINATION", "DANGEROUS_IMPORT"):
            wm = re.match(r"\w+", code[off : off + 64])
            if wm and (wm.group() in _FROM_NAME_RULES or wm.group().startswith(("pthread_", "thrd_"))):
                # a member (X::signal, or declared in a class body) is not the C library function;
                # a call of a libc function hidden that way still shows up as an object import
                if _member_qualified(code, off):
                    continue
                scopes = scopes or _Scopes(src)
                if scopes.innermost(off) == "class":
                    continue
        out.append((rule, msg.replace("CBMC prunes such paths and the native tests can report success",
                                      "a test could exit with success before its checks run"), off))
    for rule, rx, what in _CPP_TOKEN_RULES:
        for m in rx.finditer(code):
            if rule == "TERMINATION" and _member_qualified(code, m.start()):
                continue
            out.append((rule, f"{what} '{' '.join(m.group().split())}' is not allowed in code under "
                        f"verification{where}", m.start()))
    specs = list(_EXPLICIT_SPEC_RE.finditer(code))
    if specs:  # linear: one pass for the terminators and the std:: qualifiers, then bisect
        ends = [t.start() for t in _DECL_END_RE.finditer(code)]
        stds = [q.start() for q in _STD_QUAL_RE.finditer(code)]
    for m in specs:
        k = bisect.bisect_left(ends, m.end())
        end = ends[k] if k < len(ends) else len(code)
        q = bisect.bisect_left(stds, m.end())
        if q < len(stds) and stds[q] < end:
            out.append(("SYMBOL_OVERRIDE", ("explicit specialisation of a standard library template: it can "
                        f"replace library code that the trusted test driver uses as well{where}"), m.start()))
    return out


def _definition_violations(src: CSource, facts: HeaderFacts, where: str = "") -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for d in cpp_function_definitions(src):
        qual = d.qualifier
        bare = d.name.lstrip("~")
        if d.name.startswith(("operator new", "operator delete")):
            if not qual.strip(":"):
                out.append(("SYMBOL_OVERRIDE", (f"defines the global {d.name}: replacing the allocation functions "
                            "hides heap errors and leaks from AddressSanitizer/LeakSanitizer and changes the "
                            f"trusted driver's allocations too{where}"), d.offset))
            elif d.name not in facts.operators:
                out.append(("SYMBOL_OVERRIDE", (f"defines {qual}{d.name}, which the protected headers do not "
                            f"declare (custom allocation hides heap errors from the sanitizers){where}"), d.offset))
            continue
        if re.match(r"^(?:::)?std::", qual):
            out.append(("SYMBOL_OVERRIDE", (f"defines {qual}{d.name}: definitions in namespace std can replace "
                        f"standard library code{where}"), d.offset))
            continue
        if qual.strip(":"):
            continue  # a member / namespace member defined out of line: judged by the object rules
        if d.scope in ("global", "extern_c"):
            why = ci._forbidden_definition(bare)
            if why:
                out.append(("SYMBOL_OVERRIDE", f"defines function {bare}(): {why}{where}", d.offset))
        elif ci._RESERVED_RE.match(bare) or bare.startswith("nondet_"):
            out.append(("SYMBOL_OVERRIDE", f"defines function {bare}(): reserved / harness-only name{where}",
                        d.offset))
    return out


def cpp_source_violations(
    spec: Spec, rel: str, text: str, *, headers: dict[str, str] | None = None
) -> list[PolicyViolation]:
    """Absolute source-level integrity rules for one editable C++ file (the
    C++ counterpart of :func:`leanc.rules.c_rules.c_source_violations`;
    also applied by the repair / generation policy)."""
    src = CppSource(text)
    facts = header_facts(headers)
    found: list[tuple[str, str, int]] = []
    found += _cpp_directive_violations(spec, src, facts.macros)
    found += ci._pragma_operator_violations(src)
    found += _cpp_token_violations(src)
    for m in ci._PREDEFINED_DIVERGENT_RE.finditer(src.code_nodirectives):
        found.append(("TOOL_DIVERGENCE", (f"'{m.group(1)}' has a different value (or none) in the compile, "
                      "analyzer and optimised sanitizer builds"), m.start()))
    found += _definition_violations(src, facts)
    return ci._to_violations(src, rel, found)


# =========================================================================== level B: preprocessed
def cpp_preprocessed_violations(rel: str, pp_output: str, facts: HeaderFacts | None = None) -> list[PolicyViolation]:
    """Rules applied to the macro-expanded code of ``rel`` (``clang++ -E``
    output, lines of ``rel`` only)."""
    text, origin = ci._expanded_view(rel, pp_output)
    src = CppSource(text)
    where = " (after macro expansion)"
    found = _cpp_token_violations(src, where)
    for m in re.finditer(rf"^{ci._WS}#{ci._WS}pragma\b(.*)$", src.code, re.MULTILINE):
        body = " ".join(src.nocomment[m.start(1) : m.end(1)].split())
        if body != "once":
            found.append(("PRAGMA", f"`#pragma {body[:60]}`{where} is not allowed", m.start()))
    for m in re.finditer(r"\b_Pragma\b", src.code):
        found.append(("PRAGMA", f"_Pragma{where} is not allowed", m.start()))
    found += _definition_violations(src, facts or HeaderFacts(), where)
    return ci._to_violations(src, rel, found, origin)


# =========================================================================== level C: objects
#: Unmangled C++ runtime entry points simple C++20 code needs (in addition to
#: c_rules.ALLOWED_IMPORTS): pure/deleted-virtual traps (abort = test
#: failure), thread-safe static guards, the unwinder pieces of cleanup
#: landing pads.  Mangled allowances (global operator new/delete,
#: std::nothrow, typeinfo vtables, std::__throw_*) are in _mangled_import_ok.
CPP_ALLOWED_RUNTIME_IMPORTS: frozenset[str] = frozenset(
    {"__cxa_pure_virtual", "__cxa_deleted_virtual", "__cxa_guard_acquire", "__cxa_guard_release",
     "__cxa_guard_abort", "__gxx_personality_v0", "_Unwind_Resume"}
)  # fmt: skip
_EXCEPTION_IMPORT_RE = re.compile(
    r"^(?:__cxa_(?:allocate_exception|free_exception|throw|rethrow|begin_catch|end_catch|get_exception_ptr|"
    r"current_exception_type|uncaught_exceptions?|call_unexpected|init_primary_exception)|_Unwind_RaiseException)$"
)
_RTTI_IMPORTS = frozenset({"__dynamic_cast", "__cxa_bad_cast", "__cxa_bad_typeid"})
_EXIT_TIME_IMPORTS = frozenset({"__dso_handle", "__cxa_thread_atexit", "__cxa_thread_atexit_impl"})
#: compiler-generated weak definitions that are allowed (name -> nm types)
_COMPILER_HELPERS: dict[str, tuple[str, ...]] = {
    "__clang_call_terminate": ("W",),  # noexcept function calling a potentially-throwing one: aborts
    "DW.ref.__gxx_personality_v0": ("V", "W"),  # personality reference of cleanup landing pads
}
_STD_NAMESPACES = frozenset({"std", "__gnu_cxx", "__cxxabiv1", "abi", "__gnu_debug", "__pstl"})
_TYPEINFO_CLASSES = frozenset({"__class_type_info", "__si_class_type_info", "__vmi_class_type_info"})
_LIBSTDCXX_HELPER_RE = re.compile(r"^(?:__throw_\w+|__glibcxx_assert_fail)$")
_STD_THREADS = frozenset(
    {"thread", "jthread", "this_thread", "mutex", "recursive_mutex", "condition_variable", "future", "promise",
     "async", "__future_base", "stop_token", "barrier", "latch", "counting_semaphore", "__atomic_wait_address"}
)  # fmt: skip


def _is_allocation_function(nm: DemangledName) -> bool:
    return len(nm.owner) == 1 and nm.owner[0].startswith(("operator new", "operator delete"))


def _undeclared(nm: DemangledName, facts: HeaderFacts) -> str | None:
    """Why ``nm`` is not an entity named by the protected headers."""
    for inner in nm.inner or (nm,):
        for comp in inner.owner:
            if comp == "(anonymous namespace)" or comp.startswith("{"):
                continue
            if comp.startswith("operator"):
                if comp in facts.operators:
                    continue
                return f"'{comp}' is not declared by the protected headers"
            if comp.lstrip("~") not in facts.idents:
                return f"'{comp.lstrip('~')}' is not declared by the protected headers"
    return None


def _definition_problem(nm: DemangledName, weak: bool, facts: HeaderFacts) -> str | None:
    for inner in nm.inner or (nm,):
        comps = inner.owner
        if _is_allocation_function(inner):
            return ("replaces the global allocation function: it hides heap errors and leaks from "
                    "AddressSanitizer/LeakSanitizer and changes the trusted driver's allocations too")
        if comps[0] in _STD_NAMESPACES:
            if weak:
                continue  # implicit instantiation of a library template (source rules forbid writing into std)
            return (f"defines an entity of namespace {comps[0]}: replacing standard library / C++ runtime code "
                    "is not allowed")
        if any(c.startswith("nondet_") for c in comps):
            return "a harness input generator (nondet_*) must not get a body"
        bad = next((c for c in comps if ci._RESERVED_RE.match(c.lstrip("~"))), None)
        if bad:
            return f"'{bad}' is a reserved identifier (C++ runtime / compiler / verifier hook)"
        und = _undeclared(inner, facts)
        if und is not None:
            return (f"{und}; give it internal linkage (static, or an anonymous namespace) or declare it in the "
                    "protected header")
    return None


def _mangled_import_problem(nm: DemangledName, facts: HeaderFacts, has_terminate_helper: bool) -> tuple[str, str] | None:
    comps = nm.components
    if _is_allocation_function(nm):
        return None  # the standard global operator new/delete (intercepted by ASan)
    if nm.special == "vtable for" and nm.inner and nm.inner[0].components[:1] == ("__cxxabiv1",) \
            and nm.inner[0].components[-1] in _TYPEINFO_CLASSES:
        return None  # typeinfo objects of polymorphic classes refer to these
    if comps == ("std", "terminate"):
        if has_terminate_helper:
            return None
        return ("TERMINATION", f"imports '{nm.text}': termination is not allowed")
    if comps[0] in _STD_NAMESPACES:
        if comps == ("std", "nothrow") or (len(comps) == 2 and _LIBSTDCXX_HELPER_RE.match(comps[1])):
            return None
        if len(comps) > 1 and comps[1] in _STD_THREADS:
            return ("DANGEROUS_IMPORT", f"imports '{nm.text}' (threads / synchronisation)")
        return ("DANGEROUS_IMPORT", (f"imports C++ library entity '{nm.text}', which is not on the allowlist "
                "(header-only library templates, the global operator new/delete, std::nothrow and the libstdc++ "
                "precondition-failure helpers std::__throw_* are)"))
    if _undeclared(nm, facts) is None:
        return None
    return ("DANGEROUS_IMPORT", f"imports '{nm.text}', which is neither declared by the protected headers nor on "
            "the allowlist")


def check_cpp_object_symbols(
    spec: Spec,
    rel: str,
    symbols: list[tuple[str, str, str]],
    *,
    facts: HeaderFacts,
    defined_elsewhere: set[str],
    src: CSource | None = None,
) -> list[PolicyViolation]:
    """Symbol-table rules for one editable C++ object.  ``symbols`` are
    ``(mangled name, nm type, demangled name)``; ``defined_elsewhere`` holds
    the (mangled) names defined by the other editable objects."""
    out: list[PolicyViolation] = []
    target = spec.target.function
    defined_here = {m for m, t, _ in symbols if t not in ("U", "w", "v")}
    has_terminate_helper = "__clang_call_terminate" in defined_here
    for name, typ, dem in symbols:
        mangled = name.startswith("_Z")
        nm = parse_demangled(dem) if mangled else None
        mention = nm.simple.lstrip("~") if nm else name
        line = ci._first_mention(src, mention) if re.fullmatch(r"[A-Za-z_]\w*", mention or "") else None
        shown = dem if mangled else name
        if typ in ("U", "w", "v"):
            if typ in ("w", "v"):
                out.append(ci._v("DANGEROUS_IMPORT", f"weak undefined reference to '{shown}': it resolves "
                                 "differently depending on what else is linked", rel, line))
                continue
            problem: tuple[str, str] | None = None
            if not mangled:
                if name in ci.TERMINATION_NAMES or name in _EXIT_TIME_IMPORTS:
                    problem = ("TERMINATION", f"imports '{name}': termination, exit-time destructors and non-local "
                               "control flow are not allowed (native tests could report success)")
                elif name.startswith(("__CPROVER", "__VERIFIER", "nondet_")):
                    problem = ("CPROVER_BUILTIN", f"imports verifier hook '{name}'")
                elif name in ci.DANGEROUS_NAMES or ci._DANGEROUS_PREFIX_RE.match(name):
                    problem = ("DANGEROUS_IMPORT", f"imports '{name}' (process/network/loader/thread/sanitizer API)")
                elif _EXCEPTION_IMPORT_RE.match(name):
                    if not (name == "__cxa_begin_catch" and has_terminate_helper):
                        problem = ("EXCEPTIONS", f"imports '{name}': C++ exceptions are not allowed")
                elif name in _RTTI_IMPORTS:
                    problem = ("CPP_RUNTIME", f"imports '{name}': RTTI (dynamic_cast/typeid) is not allowed")
                elif name in defined_elsewhere or name in facts.idents or name in ci.ALLOWED_IMPORTS \
                        or name in CPP_ALLOWED_RUNTIME_IMPORTS or ci._COMPILER_RT_RE.match(name):
                    problem = None
                else:
                    problem = ("DANGEROUS_IMPORT", f"imports '{name}', which is not on the allowlist of library "
                               "functions (allowed: " + ", ".join(sorted(ci.ALLOWED_IMPORTS)) + ", the C++ runtime "
                               "pieces " + ", ".join(sorted(CPP_ALLOWED_RUNTIME_IMPORTS)) + ")")
            elif name in defined_elsewhere:
                problem = None  # defined (and judged) in another editable object
            elif nm is None:
                problem = ("DANGEROUS_IMPORT", f"imports '{dem}', a C++ symbol that cannot be interpreted")
            else:
                problem = _mangled_import_problem(nm, facts, has_terminate_helper)
            if problem:
                out.append(ci._v(problem[0], problem[1], rel, line))
            continue
        if not (typ.isupper() or typ == "i"):
            continue  # internal linkage
        if typ in ("i", "I"):
            out.append(ci._v("SYMBOL_OVERRIDE", f"indirect function (ifunc) '{shown}' is not allowed", rel, line))
            continue
        weak = typ in ("W", "V")
        if not mangled:
            if weak and typ in _COMPILER_HELPERS.get(name, ()):
                continue
            if weak:
                out.append(ci._v("SYMBOL_OVERRIDE", f"weak definition of '{name}' is not allowed", rel, line))
                continue
            why = ci._forbidden_definition(name)
            if why:
                out.append(ci._v("SYMBOL_OVERRIDE", f"defines external symbol {name}: {why}", rel, line))
            elif name not in facts.idents and name != target:
                out.append(ci._v("SYMBOL_OVERRIDE", f"defines external symbol '{name}', which the protected "
                                 "headers do not declare; make it static or put it in an anonymous namespace",
                                 rel, line))
            continue
        if nm is None:
            if weak and _STD_MANGLED_RE.match(name):
                continue  # library template instantiation (see _definition_problem)
            out.append(ci._v("SYMBOL_OVERRIDE", f"defines '{dem}', a C++ symbol that cannot be interpreted", rel,
                             line))
            continue
        why = _definition_problem(nm, weak, facts)
        if why:
            kind = "weak (COMDAT) definition" if weak else "definition"
            out.append(ci._v("SYMBOL_OVERRIDE", f"{kind} of external C++ symbol '{dem}': {why}", rel, line))
    return out


#: Itanium manglings of entities of namespace std (``std::`` prefix and the
#: std abbreviations Sa/Sb/Ss/Si/So/Sd), with optional cv/ref qualifiers.
_STD_MANGLED_RE = re.compile(r"^_Z(?:N[KVRO]*)?S[tabsiod]")


def _defines_target(symbols: list[tuple[str, str, str]], target: str) -> bool:
    for name, typ, dem in symbols:
        if typ not in ("T", "W"):
            continue
        if not name.startswith("_Z"):
            if name == target and typ == "T":
                return True
            continue
        nm = parse_demangled(dem)
        if nm is not None and nm.special is None and nm.is_function and nm.simple == target:
            return True
    return False
