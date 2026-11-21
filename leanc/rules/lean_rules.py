from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass

from leanc.foundation.models import PolicyViolation
from leanc.lang.lean_lexer import LeanSource
from leanc.rules.findings import Finding
from leanc.specs.model import LeanConfig, Spec

__all__ = [
    "LEAN_RULES",
    "STANDARD_LEAN_IMPORT_ROOTS",
    "LeanLayout",
    "check_lean_file",
    "is_lean_path",
    "lean_layout",
    "lean_source_violations",
]

LEAN_RULES: dict[str, str] = {
    "LEAN_SORRY": "adds `sorry` / `admit` (or refers to sorryAx) in a Lean proof file",
    "LEAN_AXIOM": "declares an axiom in a Lean proof file (only propext, Classical.choice and Quot.sound are accepted)",
    "LEAN_UNSAFE": "unsafe / partial / opaque declarations or implementation overrides (@[extern], @[implemented_by])",
    "LEAN_NATIVE": "native evaluation in proofs (native_decide, decide +native, Lean.ofReduceBool, "
    "Lean.trustCompiler, bv_decide)",
    "LEAN_OPTION": "set_option other than an allow-listed limit / linter / pretty-printer option "
    "(e.g. debug.skipKernelTC)",
    "LEAN_METAPROGRAM": "code that runs at compile time or extends the parser / elaborator (#eval and other # "
    "commands, run_cmd, run_tac, by_elab, initialize, elab, macro, syntax, notation, simproc, ...)",
    "LEAN_ATTRIBUTE": "an attribute outside the allow-list (instance, extern, init, macro, csimp, ...)",
    "LEAN_INSTANCE": "typeclass instances, `deriving instance`, unification hints or `export` aliases (they change "
    "how the trusted checker elaborates)",
    "LEAN_IMPORT": "imports anything but the trusted spec module, other proof modules, Init or Std "
    "(or uses `prelude` / the module system)",
    "LEAN_OBFUSCATION": "guillemet identifiers, interpolated strings or bidirectional-control characters, which can "
    "hide code from review and from this policy",
}

STANDARD_LEAN_IMPORT_ROOTS: tuple[str, ...] = ("Init", "Std")
RESERVED_MODULE_ROOTS: frozenset[str] = frozenset(
    {"Init", "Lean", "Std", "Lake", "LeanChecker", "LeanExport", "LeanIR", "Leanc", "LeancCheck"}
)
LEAN_SUFFIX = ".lean"
_PROJECT_ROOT = ""
_ROOTLESS_COMMON_PATHS = (".", "/")

_MODULE_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")
_IDENT_RE = re.compile(r"(?<![\w'!?.])(?:[^\W\d])[\w'!?]*(?:\.[^\W\d][\w'!?]*)*")
_HASH_CMD_RE = re.compile(r"(?<![\w#])#([A-Za-z_][\w]*)")
_ATTR_OPEN_RE = re.compile(r"@\[|\battribute\s*\[")
_SET_OPTION_RE = re.compile(r"\bset_option\s+([A-Za-z_][\w.]*)")
_IMPORT_LINE_RE = re.compile(r"^[ \t]*import\b(.*)$", re.MULTILINE)
_HEADER_KW_RE = re.compile(r"^[ \t]*(prelude|module)\b", re.MULTILINE)
_QUALIFIED_IMPORT_RE = re.compile(r"^[ \t]*(?:public|private|meta)[ \t]+(?:meta[ \t]+)?import\b", re.MULTILINE)
_NATIVE_WORD_RE = re.compile(r"^(?:native|native_decide)$")
_DERIVING_INSTANCE_RE = re.compile(r"deriving\s+instance\b")
_ATTRIBUTE_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")

_SORRY_WORDS = frozenset({"sorry", "admit", "sorryAx"})
_SORRY_AXIOM = "sorryAx"
_UNSAFE_WORDS = frozenset({"unsafe", "partial", "opaque", "implemented_by", "extern"})
_NATIVE_PARTS = frozenset(
    {"ofReduceBool", "ofReduceNat", "reduceBool", "reduceNat", "trustCompiler", "bv_decide", "bv_check"}
)
_META_WORDS = frozenset(
    {
        "run_cmd", "run_elab", "run_meta", "run_tac", "by_elab",
        "initialize", "builtin_initialize",
        "elab", "elab_rules", "macro", "macro_rules", "syntax", "declare_syntax_cat",
        "notation", "infix", "infixl", "infixr", "prefix", "postfix", "binder_predicate",
        "simproc", "dsimproc", "simproc_decl", "dsimproc_decl", "builtin_simproc", "builtin_dsimproc",
        "builtin_simproc_decl", "builtin_dsimproc_decl", "declare_simp_like_tactic", "register_simp_attr",
        "register_option", "register_builtin_option", "declare_config_elab", "register_tactic_tag",
        "grind_propagator", "builtin_grind_propagator",
    }
)
_INSTANCE_WORDS = frozenset({"instance", "export", "unif_hint"})
_DISPLAY_ONLY_HASH_COMMANDS = frozenset({"check", "print", "synth"})
_ATTRIBUTE_SCOPE_WORDS = ("local", "scoped")
_IMPORT_ALL = "all"
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "simp", "reducible", "irreducible", "semireducible", "inline", "noinline", "specialize",
        "grind", "ext", "induction_eliminator", "cases_eliminator", "elab_as_elim", "match_pattern",
        "refl", "symm", "inherit_doc", "deprecated", "congr",
    }
)
ALLOWED_OPTIONS: tuple[str, ...] = (
    "maxHeartbeats",
    "maxRecDepth",
    "synthInstance.maxHeartbeats",
    "synthInstance.maxSize",
    "linter.",
    "pp.",
)
_OPTION_PREFIX_END = "."


def is_lean_path(path: str) -> bool:
    return isinstance(path, str) and path.endswith(LEAN_SUFFIX)


@dataclass(frozen=True)
class LeanLayout:
    root: str
    spec_module: str
    proof_modules: dict[str, str]
    generator_module: str | None = None

    def allowed_imports(self) -> set[str]:
        return {self.spec_module, *self.proof_modules.values()}

    def all_modules(self) -> list[str]:
        return [self.spec_module, *self.proof_modules.values()] + ([self.generator_module] if self.generator_module else [])


def _module_parts(root: str, rel: str) -> list[str]:
    sub = rel[len(root) + 1 :] if root else rel
    return sub[: -len(LEAN_SUFFIX)].split("/")


def _module_name_problem(root: str, rel: str) -> str | None:
    parts = _module_parts(root, rel)
    for part in parts:
        if not _MODULE_PART_RE.match(part):
            return f"{rel}: path component {part!r} is not a plain Lean identifier, so the file has no usable module name"
    if parts[0] in RESERVED_MODULE_ROOTS:
        return f"{rel}: module name {'.'.join(parts)!r} clashes with the Lean toolchain ({parts[0]}.*)"
    return None


def _lean_files(lean: LeanConfig) -> list[str]:
    return [lean.spec_file, *lean.proofs] + ([lean.vectors.generator] if lean.vectors else [])


def _lean_root(files: list[str]) -> str:
    root = posixpath.commonpath([posixpath.dirname(p) for p in files]) if files else _PROJECT_ROOT
    return _PROJECT_ROOT if root in _ROOTLESS_COMMON_PATHS else root


def _unchecked_layout(lean: LeanConfig) -> LeanLayout:
    root = _lean_root(_lean_files(lean))

    def module(rel: str) -> str:
        return ".".join(_module_parts(root, rel))

    return LeanLayout(
        root=root,
        spec_module=module(lean.spec_file),
        proof_modules={p: module(p) for p in lean.proofs},
        generator_module=module(lean.vectors.generator) if lean.vectors else None,
    )


def _case_insensitive_duplicates(modules: list[str]) -> list[str]:
    lowered = [m.lower() for m in modules]
    return sorted({m for m, low in zip(modules, lowered) if lowered.count(low) > 1})


def _layout_problem(spec: Spec) -> str | None:
    if spec.lean is None:
        return "the spec has no 'lean' section"
    files = _lean_files(spec.lean)
    root = _lean_root(files)
    for rel in files:
        problem = _module_name_problem(root, rel)
        if problem:
            return problem
    duplicates = _case_insensitive_duplicates(_unchecked_layout(spec.lean).all_modules())
    if duplicates:
        return f"Lean files map to the same module name (up to case): {duplicates}"
    return None


def lean_layout(spec: Spec) -> LeanLayout:
    problem = _layout_problem(spec)
    if problem:
        raise ValueError(problem)
    return _unchecked_layout(spec.lean)


def _importable_layout(spec: Spec) -> LeanLayout | None:
    return None if _layout_problem(spec) else _unchecked_layout(spec.lean)


def _match_bracket(code: str, open_at: int) -> int:
    depth = 0
    for k in range(open_at, len(code)):
        if code[k] == "[":
            depth += 1
        elif code[k] == "]":
            depth -= 1
            if depth == 0:
                return k
    return len(code)


def _split_top_level_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _attribute_name(entry: str) -> str:
    words = entry.strip().lstrip("-").strip().split()
    while words and words[0] in _ATTRIBUTE_SCOPE_WORDS:
        words = words[1:]
    if not words:
        return ""
    name = _ATTRIBUTE_NAME_RE.match(words[0].lstrip("-"))
    return name.group(0) if name else words[0]


def _option_allowed(name: str) -> bool:
    return any(name == o or (o.endswith(_OPTION_PREFIX_END) and name.startswith(o)) for o in ALLOWED_OPTIONS)


def _import_allowed(module: str, layout: LeanLayout | None, own_module: str | None) -> bool:
    if module.split(".", 1)[0] in STANDARD_LEAN_IMPORT_ROOTS:
        return True
    if layout is None:
        return False
    return module in layout.allowed_imports() and module != own_module


def _obfuscation_hits(src: LeanSource) -> list[Finding]:
    found = [Finding("LEAN_OBFUSCATION", f"bidirectional-control character U+{ord(m.group(0)):04X}", m.start())
             for m in _BIDI_RE.finditer(src.raw)]
    found += [Finding("LEAN_OBFUSCATION", f"uses {what}", off) for off, what in src.obfuscation]
    return found


def _is_native_evaluation(parts: list[str]) -> bool:
    return any(_NATIVE_WORD_RE.search(p) for p in parts) or any(p in _NATIVE_PARTS for p in parts)


def _identifier_hit(code: str, ident: re.Match[str]) -> Finding | None:
    tok = ident.group(0)
    parts = tok.split(".")
    at = ident.start()
    if tok in _SORRY_WORDS or parts[-1] == _SORRY_AXIOM:
        return Finding("LEAN_SORRY", f"uses `{tok}`", at)
    if tok == "axiom":
        return Finding("LEAN_AXIOM", "declares an axiom", at)
    if tok in _UNSAFE_WORDS:
        return Finding("LEAN_UNSAFE", f"uses `{tok}`", at)
    if _is_native_evaluation(parts):
        return Finding("LEAN_NATIVE", f"uses `{tok}` (native evaluation is trusted code, not a kernel proof)", at)
    if tok in _META_WORDS:
        return Finding("LEAN_METAPROGRAM", f"uses `{tok}`", at)
    if tok in _INSTANCE_WORDS:
        return Finding("LEAN_INSTANCE", f"uses `{tok}`", at)
    if tok == "deriving" and _DERIVING_INSTANCE_RE.match(code, at):
        return Finding("LEAN_INSTANCE", "uses `deriving instance`", at)
    return None


def _identifier_hits(code: str) -> list[Finding]:
    return [hit for ident in _IDENT_RE.finditer(code) if (hit := _identifier_hit(code, ident))]


def _hash_command_hits(code: str) -> list[Finding]:
    return [Finding("LEAN_METAPROGRAM", f"uses the command `#{m.group(1)}`", m.start())
            for m in _HASH_CMD_RE.finditer(code) if m.group(1) not in _DISPLAY_ONLY_HASH_COMMANDS]


def _attribute_hits(code: str) -> list[Finding]:
    found: list[Finding] = []
    for m in _ATTR_OPEN_RE.finditer(code):
        open_at = code.index("[", m.start())
        for entry in _split_top_level_commas(code[open_at + 1 : _match_bracket(code, open_at)]):
            name = _attribute_name(entry)
            if name and name not in ALLOWED_ATTRIBUTES:
                found.append(Finding("LEAN_ATTRIBUTE", f"uses the attribute `{name}`", m.start()))
    return found


def _option_hits(code: str) -> list[Finding]:
    return [Finding("LEAN_OPTION", f"sets the option `{m.group(1)}`", m.start())
            for m in _SET_OPTION_RE.finditer(code) if not _option_allowed(m.group(1))]


def _module_system_hits(code: str) -> list[Finding]:
    found = [Finding("LEAN_IMPORT", f"uses `{m.group(1)}` (the module system / prelude are not supported)",
                     m.start()) for m in _HEADER_KW_RE.finditer(code)]
    found += [Finding("LEAN_IMPORT", "uses a qualified import (public/private/meta import)", m.start())
              for m in _QUALIFIED_IMPORT_RE.finditer(code)]
    return found


def _import_hits(code: str, layout: LeanLayout | None, own_module: str | None) -> list[Finding]:
    found: list[Finding] = []
    for m in _IMPORT_LINE_RE.finditer(code):
        names = m.group(1).split()
        if names and names[0] == _IMPORT_ALL:
            found.append(Finding("LEAN_IMPORT", "uses `import all`", m.start()))
            names = names[1:]
        for name in names:
            if not _import_allowed(name, layout, own_module):
                allowed = sorted((layout.allowed_imports() - {own_module}) if layout else set())
                found.append(Finding(
                    "LEAN_IMPORT",
                    f"imports `{name}`; allowed: Init, Std" + (f", {', '.join(allowed)}" if allowed else ""),
                    m.start(),
                ))
    return found


def _hits(src: LeanSource, layout: LeanLayout | None, own_module: str | None) -> list[Finding]:
    code = src.code
    return [
        *_obfuscation_hits(src),
        *_identifier_hits(code),
        *_hash_command_hits(code),
        *_attribute_hits(code),
        *_option_hits(code),
        *_module_system_hits(code),
        *_import_hits(code, layout, own_module),
    ]


def _to_violations(src: LeanSource, rel: str, found: list[Finding]) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    seen: set[tuple[str, int, str]] = set()
    for hit in sorted(found, key=lambda h: (h.offset, h.rule)):
        line = src.line_of(hit.offset)
        key = (hit.rule, line, hit.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(PolicyViolation(rule=hit.rule, message=f"{hit.message}: `{src.shown(hit.offset)}`", file=rel,
                                   line=line))
    return out


def lean_source_violations(spec: Spec, rel: str, text: str) -> list[PolicyViolation]:
    layout = _importable_layout(spec)
    own = layout.proof_modules.get(rel) if layout else None
    src = LeanSource(text)
    return _to_violations(src, rel, [h for h in _hits(src, layout, own) if h.rule != "LEAN_SORRY"])


def _placeholder_hits(src: LeanSource) -> list[Finding]:
    return [h for h in _hits(src, None, None) if h.rule == "LEAN_SORRY"]


def _first_new_placeholder(new_src: LeanSource, new_hits: list[Finding], old_src: LeanSource,
                           old_hits: list[Finding]) -> Finding:
    old_lines: Counter[str] = Counter(old_src.shown(h.offset) for h in old_hits)
    for hit in new_hits:
        shown = new_src.shown(hit.offset)
        if old_lines[shown] > 0:
            old_lines[shown] -= 1
        else:
            return hit
    return new_hits[0]


def check_lean_file(spec: Spec, rel: str, old: str | None, new: str) -> list[PolicyViolation]:
    out = lean_source_violations(spec, rel, new)
    if old is None:
        return out
    new_src, old_src = LeanSource(new), LeanSource(old)
    new_hits, old_hits = _placeholder_hits(new_src), _placeholder_hits(old_src)
    if len(new_hits) > len(old_hits):
        first = _first_new_placeholder(new_src, new_hits, old_src, old_hits)
        counts = f" ({len(old_hits)} -> {len(new_hits)} occurrences)"
        out.append(
            PolicyViolation(
                rule="LEAN_SORRY",
                message=f"adds a `sorry`/`admit` placeholder{counts}: `{new_src.shown(first.offset)}`",
                file=rel,
                line=new_src.line_of(first.offset),
            )
        )
    return out
