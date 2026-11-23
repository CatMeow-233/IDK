from __future__ import annotations

from collections import Counter

from leanc.foundation.errors import WorkspaceError
from leanc.foundation.models import PolicyDecision, PolicyViolation
from leanc.foundation.workspace import normalize_relpath
from leanc.lang.c_lexer import CSource
from leanc.lang.cpp_lexer import CppSource
from leanc.repair.constructs import Finding, find_assertions, scan_constructs
from leanc.repair.patch import changed_paths, diff_stats
from leanc.rules.cpp_rules import find_cpp_function_definitions
from leanc.rules.definitions import FunctionDefinition, find_function_definitions
from leanc.rules.lean_rules import LEAN_RULES, check_lean_file, is_lean_path
from leanc.rules.loop_contracts import LOOP_CONTRACT_RULES, is_loop_contract_path, loop_contract_violations
from leanc.rules.preprocessor import defined_macro_names
from leanc.rules.source import source_violations
from leanc.specs.model import CPP_SUFFIXES, LEANC_STATE_DIR, Spec

__all__ = [
    "MAX_ANALYZED_CHARS",
    "RULES",
    "check_candidate",
    "check_generated",
]

_CANDIDATE_RULES: dict[str, str] = {
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
_SOURCE_INTEGRITY_GATE_RULES: dict[str, str] = {
    "TOOL_DIVERGENCE": "code that can differ between clang, the sanitizer build and CBMC (conditionals on "
    "compiler/optimiser/verifier macros, non-standard directives, optimisation-dependent built-ins)",
    "PRAGMA": "a #pragma / _Pragma other than '#pragma once'",
    "LINE_DIRECTIVE": "#line or GNU linemarkers",
    "SYMBOL_OVERRIDE": "defines/redefines libc or verifier hooks (__assert_fail, nondet_*, main, reserved names), "
    "weak/alias symbols, or macros named after libc functions, keywords or interface macros",
    "CONSTRUCTOR": "constructor/destructor/section attributes (code running outside the function under test)",
    "TERMINATION": "process termination or non-local control flow (exit, _exit, abort, atexit, longjmp, raise, ...)",
    "DANGEROUS_IMPORT": "process/network/loader/thread/sanitizer-control APIs",
}
_WP_GATE_ACSL_RULES: dict[str, str] = {
    "ACSL_FORBIDDEN": "ACSL that could add unproved assumptions (admit, axiom, inductive, recursive logic, ghost, ...)",
    "ACSL_CONTRACT": "an ACSL contract on the target, or one not attached to a static helper definition",
    "ACSL_MACRO": "a macro used inside ACSL, or a #define of an identifier of the approved ACSL contract",
}
RULES: dict[str, str] = {
    **_CANDIDATE_RULES,
    **_SOURCE_INTEGRITY_GATE_RULES,
    **_WP_GATE_ACSL_RULES,
    **LEAN_RULES,
    **LOOP_CONTRACT_RULES,
}

MAX_ANALYZED_CHARS = 1_000_000
CONFIGURATION_SUFFIXES = (".yaml", ".yml", ".json")
SHOWN_TRIVIAL_BODY_CHARS = 100


def _describe_protected_path(spec: Spec, norm: str) -> str:
    if spec.verification is not None and norm == spec.verification.harness:
        return "the trusted verification harness"
    if spec.inductive is not None and norm == spec.inductive.harness:
        return "the trusted inductive harness"
    if spec.tests.driver and norm == spec.tests.driver:
        return "the trusted test driver"
    if norm in spec.sources.headers:
        return "a protected interface header"
    if norm in spec.protected_paths():
        return "a protected specification file (Lean model / vectors)"
    if norm == LEANC_STATE_DIR or norm.startswith(LEANC_STATE_DIR + "/"):
        return "Lean C state (approval records)"
    if norm.endswith(CONFIGURATION_SUFFIXES):
        return "a specification/configuration file"
    return "not an editable source"


def _path_violation(spec: Spec, path: str) -> PolicyViolation | None:
    if not isinstance(path, str):
        return PolicyViolation(rule="PATH_INVALID", message=f"path is not a string: {path!r}")
    if "\\" in path:
        return PolicyViolation(rule="PATH_INVALID", message=f"backslashes are not allowed in paths: {path!r}", file=path)
    try:
        norm = normalize_relpath(path)
    except WorkspaceError as exc:
        return PolicyViolation(rule="PATH_INVALID", message=str(exc), file=path)
    if norm != path:
        return PolicyViolation(
            rule="PATH_INVALID", message=f"path {path!r} is not in canonical form (use {norm!r})", file=path
        )
    editable = spec.editable_paths()
    if norm in editable:
        return None
    return PolicyViolation(
        rule="PATH_NOT_EDITABLE",
        message=f"{norm!r} is {_describe_protected_path(spec, norm)}; only spec.sources.editable, lean.proofs and "
        f"inductive.contracts may be changed: {', '.join(editable)}",
        file=norm,
    )


def _first_added_occurrence(new: list[Finding], old: list[Finding]) -> Finding:
    unmatched_old_lines = Counter(finding.normalized_line for finding in old)
    for finding in new:
        if unmatched_old_lines[finding.normalized_line] > 0:
            unmatched_old_lines[finding.normalized_line] -= 1
        else:
            return finding
    return new[len(old)] if len(new) > len(old) else new[0]


def _by_rule_and_construct(findings: list[Finding]) -> dict[tuple[str, str], list[Finding]]:
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        grouped.setdefault((finding.rule, finding.construct), []).append(finding)
    return grouped


def _new_construct_violations(
    path: str, old_src: CSource | None, new_src: CSource, target: str | None
) -> list[PolicyViolation]:
    old_groups = _by_rule_and_construct(scan_constructs(old_src, target) if old_src is not None else [])
    violations: list[PolicyViolation] = []
    for (rule, construct), new_findings in _by_rule_and_construct(scan_constructs(new_src, target)).items():
        old_findings = old_groups.get((rule, construct), [])
        if len(new_findings) <= len(old_findings):
            continue
        added = _first_added_occurrence(new_findings, old_findings)
        counts = f" ({len(old_findings)} -> {len(new_findings)} occurrences)" if old_findings else ""
        violations.append(
            PolicyViolation(
                rule=rule, message=f"introduces {construct}{counts}: `{added.shown_line}`", file=path, line=added.line
            )
        )
    return violations


def _removed_assertion_violations(path: str, old_src: CSource, new_src: CSource) -> list[PolicyViolation]:
    old_assertions, new_assertions = find_assertions(old_src), find_assertions(new_src)
    violations: list[PolicyViolation] = []
    for construct, before in old_assertions.items():
        after = new_assertions[construct]
        if len(after) >= len(before):
            continue
        gone = _first_added_occurrence(before, after)
        violations.append(
            PolicyViolation(
                rule="ASSERT_REMOVED",
                message=(
                    f"number of {construct} in {path} decreased from {len(before)} to {len(after)} "
                    f"(e.g. `{gone.shown_line}` at old line {gone.line})"
                ),
                file=path,
            )
        )
    return violations


def _content_violations(
    path: str, old_src: CSource | None, new_src: CSource, target: str | None
) -> list[PolicyViolation]:
    violations = _new_construct_violations(path, old_src, new_src, target)
    if old_src is not None:
        violations.extend(_removed_assertion_violations(path, old_src, new_src))
    return violations


def _integrity_gate_violations(spec: Spec, path: str, text: str) -> list[PolicyViolation]:
    return source_violations(spec, path, text)


def _same_finding_key(violation: PolicyViolation) -> tuple:
    if violation.line:
        return violation.rule, violation.file, violation.line
    return violation.rule, violation.file, violation.line, violation.message


def _decision(violations: list[PolicyViolation]) -> PolicyDecision:
    rule_order = {rule: position for position, rule in enumerate(RULES)}
    unique: dict[tuple, PolicyViolation] = {}
    for violation in violations:
        unique.setdefault(_same_finding_key(violation), violation)
    ordered = sorted(
        unique.values(),
        key=lambda v: (rule_order.get(v.rule, len(rule_order)), v.file or "", v.line or 0, v.message),
    )
    return PolicyDecision(allowed=not ordered, violations=ordered)


def _lexer_for(path: str, text: str) -> CSource:
    return CppSource(text) if path.endswith(CPP_SUFFIXES) else CSource(text)


class _LexedFiles:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self._cache: dict[str, CSource] = {}

    def get(self, path: str) -> CSource | None:
        if path not in self.files:
            return None
        return self.lexed(path)

    def lexed(self, path: str) -> CSource:
        if path not in self._cache:
            self._cache[path] = _lexer_for(path, self.files[path])
        return self._cache[path]


def _definitions(spec: Spec, lexed: _LexedFiles, paths: list[str]) -> list[FunctionDefinition]:
    definitions: list[FunctionDefinition] = []
    for path in paths:
        src = lexed.get(path)
        if src is None:
            continue
        if spec.language.is_cpp:
            definitions.extend(find_cpp_function_definitions(src, spec.target.function, path))
        else:
            definitions.extend(find_function_definitions(src, spec.target.function, path))
    return definitions


def _target_violations(
    spec: Spec, old: _LexedFiles | None, new: _LexedFiles, old_paths: list[str], new_paths: list[str]
) -> list[PolicyViolation]:
    name = spec.target.function
    violations: list[PolicyViolation] = []
    new_definitions = _definitions(spec, new, new_paths)
    old_definitions = _definitions(spec, old, old_paths) if old is not None else []
    if old is None and not new_definitions:
        violations.append(
            PolicyViolation(
                rule="MISSING_TARGET",
                message=f"no file-scope definition of the target function '{name}' in the generated files "
                f"({', '.join(new_paths) or 'none'})",
            )
        )
    if old is not None and old_definitions and not new_definitions:
        violations.append(
            PolicyViolation(
                rule="TRIVIALIZED_TARGET",
                message=f"the definition of the target function '{name}' was removed (or hidden behind a macro "
                "or #if 0)",
                file=old_definitions[0].file,
            )
        )
    trivial = [d for d in new_definitions if d.trivial]
    if trivial and not any(d.trivial for d in old_definitions):
        stub = trivial[0]
        body = " ".join(stub.body.split()) or "(empty)"
        violations.append(
            PolicyViolation(
                rule="TRIVIALIZED_TARGET",
                message=f"the target function '{name}' was reduced to `{{ {body[:SHOWN_TRIVIAL_BODY_CHARS]} }}`; "
                "it must implement the specified behaviour, not return a constant",
                file=stub.file,
                line=stub.line,
            )
        )
    return violations


def _size_violation(path: str, content: str) -> PolicyViolation | None:
    if len(content) > MAX_ANALYZED_CHARS:
        return PolicyViolation(
            rule="TOO_LARGE",
            message=f"{path} has {len(content)} characters; files over {MAX_ANALYZED_CHARS} are not policy-checked",
            file=path,
        )
    return None


def _too_large_to_analyze(old: dict[str, str], new: dict[str, str], path: str) -> bool:
    return max(len(old.get(path) or ""), len(new.get(path) or "")) > MAX_ANALYZED_CHARS


def _changed_lines_violations(old: dict[str, str], new: dict[str, str], limit: int) -> list[PolicyViolation]:
    added, removed = diff_stats(old, new)
    if added + removed <= limit:
        return []
    return [
        PolicyViolation(
            rule="TOO_LARGE",
            message=f"the candidate changes {added + removed} lines (+{added} -{removed}); the limit is {limit}",
        )
    ]


def _editable_macro_names(spec: Spec, lexed: _LexedFiles) -> frozenset[str]:
    names: set[str] = set()
    for path in spec.sources.editable:
        if path in lexed.files and len(lexed.files[path]) <= MAX_ANALYZED_CHARS:
            names |= defined_macro_names(lexed.lexed(path).code)
    return frozenset(names)


def _is_source_code(spec: Spec, path: str) -> bool:
    return not is_lean_path(path) and not is_loop_contract_path(spec, path)


def _file_violations(
    spec: Spec, path: str, old_content: str | None, new_content: str, old: _LexedFiles | None, new: _LexedFiles
) -> list[PolicyViolation]:
    if is_lean_path(path):
        return check_lean_file(spec, path, old_content, new_content)
    if is_loop_contract_path(spec, path):
        return loop_contract_violations(spec, path, new_content, macro_names=_editable_macro_names(spec, new))
    old_src = old.get(path) if old is not None else None
    return _content_violations(path, old_src, new.lexed(path), spec.target.function) + _integrity_gate_violations(
        spec, path, new_content
    )


def check_candidate(
    spec: Spec,
    old: dict[str, str],
    new: dict[str, str],
    *,
    max_changed_lines: int | None = None,
) -> PolicyDecision:
    changed = changed_paths(old, new)
    if not changed:
        return _decision(
            [PolicyViolation(rule="NO_CHANGE", message="the candidate is identical to the current sources")]
        )
    violations: list[PolicyViolation] = []
    content_paths: list[str] = []
    oversized: set[str] = set()
    for path in changed:
        path_violation = _path_violation(spec, path)
        if path_violation is not None:
            violations.append(path_violation)
            continue
        if path not in new:
            violations.append(
                PolicyViolation(
                    rule="FILE_DELETED", message=f"the candidate deletes the editable source {path}", file=path
                )
            )
            continue
        size_violation = _size_violation(path, new[path])
        if size_violation is not None:
            violations.append(size_violation)
            oversized.add(path)
            continue
        content_paths.append(path)
    limit = spec.repair.max_changed_lines if max_changed_lines is None else max_changed_lines
    huge = [path for path in changed if _too_large_to_analyze(old, new, path)]
    for path in huge:
        if path not in oversized and path in new:
            size_violation = _size_violation(path, new[path])
            if size_violation is not None:
                violations.append(size_violation)
    if not huge:
        violations.extend(_changed_lines_violations(old, new, limit))
    old_lexed, new_lexed = _LexedFiles(old), _LexedFiles(new)
    for path in content_paths:
        violations.extend(_file_violations(spec, path, old.get(path), new[path], old_lexed, new_lexed))
    editable = set(spec.sources.editable)
    old_paths = sorted(p for p in old if p in editable and len(old[p]) <= MAX_ANALYZED_CHARS)
    new_paths = sorted(p for p in new if p in editable and p not in oversized)
    violations.extend(_target_violations(spec, old_lexed, new_lexed, old_paths, new_paths))
    return _decision(violations)


def check_generated(spec: Spec, files: dict[str, str]) -> PolicyDecision:
    violations: list[PolicyViolation] = []
    content_paths: list[str] = []
    for path in sorted(files):
        path_violation = _path_violation(spec, path)
        if path_violation is not None:
            violations.append(path_violation)
            continue
        size_violation = _size_violation(path, files[path])
        if size_violation is not None:
            violations.append(size_violation)
            continue
        content_paths.append(path)
    lexed = _LexedFiles(files)
    for path in content_paths:
        violations.extend(_file_violations(spec, path, None, files[path], None, lexed))
    generated_sources = [path for path in content_paths if _is_source_code(spec, path)]
    violations.extend(_target_violations(spec, None, lexed, [], generated_sources))
    return _decision(violations)
