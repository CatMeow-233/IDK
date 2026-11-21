"""Policy rules for the *editable* Lean proof files of the ``lean`` gate.

Why these rules exist
---------------------
Lean code is not inert data: elaborating a file runs code at compile time
(``#eval``, ``run_cmd``, ``initialize``, user macros / elaborators, ...), and
that code can do IO.  The ``lean`` backend therefore always compiles proof
files in the sandbox, but a proof file that *steers the checking itself*
(skips the kernel, adds axioms, registers typeclass instances that change how
the trusted checker elaborates, forges output) must never reach a verdict.
The Lean kernel and the trusted checker are the real line of defence (only
the standard axioms are accepted, the statement is compared by the kernel);
these lexical rules are defence in depth that rejects such constructs *before*
anything runs and tells the repair agent exactly which rule it broke.

Two entry points:

* :func:`lean_source_violations` - the *absolute* rules, used by the
  verify-time gate (:mod:`leanc.backends.lean`) on every editable proof file;
* :func:`check_lean_file` - what the repair / generation policy
  (:mod:`leanc.repair.policy`) applies to a changed ``.lean`` file: the
  absolute rules plus, for repair candidates, the count-based ``LEAN_SORRY``
  rule (a candidate may remove ``sorry`` placeholders but never add new ones;
  freshly generated files may contain placeholders; at verify time a
  ``sorry`` simply leaves the theorem unproved).

Matching works on a lexed copy of the file in which comments (``--`` and
nested ``/- -/``) and the contents of string / char literals are blanked, so
prose like ``-- no sorry here`` causes no false positives.  Interpolated
strings (``s!"..{e}.."``) are rejected because code inside them would be
hidden by that blanking; guillemet identifiers (``«...»``) and
bidirectional-control characters are rejected for the same reason.

Module layout (shared with the backend): all Lean files of a spec (spec file,
proof files, vectors generator) live below one *root* directory, the deepest
directory containing all of them; a file's module name is its path relative to
that root with ``/`` replaced by ``.`` and ``.lean`` removed
(``lean/Spec.lean`` -> ``Spec`` when every Lean file is under ``lean/``).
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass

from leanc.foundation.models import PolicyViolation
from leanc.lang.lean_lexer import LeanSource
from leanc.specs.model import Spec

__all__ = [
    "LEAN_RULES",
    "STANDARD_LEAN_IMPORT_ROOTS",
    "LeanLayout",
    "check_lean_file",
    "is_lean_path",
    "lean_layout",
    "lean_source_violations",
]

#: Rule id -> one-line description (appended to :data:`leanc.repair.policy.RULES`).
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

#: Library roots a proof file may import (besides the spec and proof modules).
STANDARD_LEAN_IMPORT_ROOTS: tuple[str, ...] = ("Init", "Std")
#: Top-level module names that belong to the Lean toolchain (never a user module).
RESERVED_MODULE_ROOTS: frozenset[str] = frozenset(
    {"Init", "Lean", "Std", "Lake", "LeanChecker", "LeanExport", "LeanIR", "Leanc", "LeancCheck"}
)

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

_SORRY_WORDS = frozenset({"sorry", "admit", "sorryAx"})
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
)  # fmt: skip
_INSTANCE_WORDS = frozenset({"instance", "export", "unif_hint"})
#: ``#`` commands that only display information.
_ALLOWED_HASH = frozenset({"check", "print", "synth"})
#: Attributes a proof file may use (first word of each entry, after local/scoped/-).
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "simp", "reducible", "irreducible", "semireducible", "inline", "noinline", "specialize",
        "grind", "ext", "induction_eliminator", "cases_eliminator", "elab_as_elim", "match_pattern",
        "refl", "symm", "inherit_doc", "deprecated", "congr",
    }
)  # fmt: skip
#: ``set_option`` names a proof file may set (exact names, or a prefix ending in '.').
ALLOWED_OPTIONS: tuple[str, ...] = (
    "maxHeartbeats",
    "maxRecDepth",
    "synthInstance.maxHeartbeats",
    "synthInstance.maxSize",
    "linter.",
    "pp.",
)


def is_lean_path(path: str) -> bool:
    return isinstance(path, str) and path.endswith(".lean")


# =========================================================================== module layout
@dataclass(frozen=True)
class LeanLayout:
    """Where the Lean files of a spec live and what their module names are."""

    root: str  # snapshot-relative directory ("" = project root)
    spec_module: str
    proof_modules: dict[str, str]  # proof file -> module, in spec order
    generator_module: str | None = None

    def allowed_imports(self) -> set[str]:
        return {self.spec_module, *self.proof_modules.values()}


def _module_name(root: str, rel: str) -> str:
    sub = rel[len(root) + 1 :] if root else rel
    parts = sub[: -len(".lean")].split("/")
    for p in parts:
        if not _MODULE_PART_RE.match(p):
            raise ValueError(
                f"{rel}: path component {p!r} is not a plain Lean identifier, so the file has no usable module name"
            )
    if parts[0] in RESERVED_MODULE_ROOTS:
        raise ValueError(f"{rel}: module name {'.'.join(parts)!r} clashes with the Lean toolchain ({parts[0]}.*)")
    return ".".join(parts)


def lean_layout(spec: Spec) -> LeanLayout:
    """Module layout of ``spec.lean``.  Raises :class:`ValueError` for a
    layout Lean cannot import (bad path components, clashes, duplicates)."""
    if spec.lean is None:
        raise ValueError("the spec has no 'lean' section")
    lc = spec.lean
    files = [lc.spec_file, *lc.proofs] + ([lc.vectors.generator] if lc.vectors else [])
    root = posixpath.commonpath([posixpath.dirname(p) for p in files]) if files else ""
    if root in (".", "/"):
        root = ""
    spec_mod = _module_name(root, lc.spec_file)
    proofs = {p: _module_name(root, p) for p in lc.proofs}
    gen = _module_name(root, lc.vectors.generator) if lc.vectors else None
    mods = [spec_mod, *proofs.values()] + ([gen] if gen else [])
    lowered = [m.lower() for m in mods]
    dup = sorted({m for m, low in zip(mods, lowered) if lowered.count(low) > 1})
    if dup:
        raise ValueError(f"Lean files map to the same module name (up to case): {dup}")
    return LeanLayout(root=root, spec_module=spec_mod, proof_modules=proofs, generator_module=gen)


# =========================================================================== rules
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


def _split_top(s: str) -> list[str]:
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
    e = entry.strip()
    e = e.lstrip("-").strip()
    words = e.split()
    while words and words[0] in ("local", "scoped"):
        words = words[1:]
    if not words:
        return ""
    m = re.match(r"[A-Za-z_][\w.]*", words[0].lstrip("-"))
    return m.group(0) if m else words[0]


def _option_allowed(name: str) -> bool:
    return any(name == o or (o.endswith(".") and name.startswith(o)) for o in ALLOWED_OPTIONS)


def _import_allowed(module: str, layout: LeanLayout | None, own_module: str | None) -> bool:
    root = module.split(".", 1)[0]
    if root in STANDARD_LEAN_IMPORT_ROOTS:
        return True
    if layout is None:
        return False
    return module in layout.allowed_imports() and module != own_module


def _hits(src: LeanSource, layout: LeanLayout | None, own_module: str | None) -> list[tuple[str, str, int]]:
    """(rule, message, offset) for every forbidden construct in ``src``."""
    code = src.code
    found: list[tuple[str, str, int]] = []
    for m in _BIDI_RE.finditer(src.raw):
        found.append(("LEAN_OBFUSCATION", f"bidirectional-control character U+{ord(m.group(0)):04X}", m.start()))
    for off, what in src.obfuscation:
        found.append(("LEAN_OBFUSCATION", f"uses {what}", off))
    for m in _IDENT_RE.finditer(code):
        tok = m.group(0)
        parts = tok.split(".")
        last = parts[-1]
        if tok in _SORRY_WORDS or last == "sorryAx":
            found.append(("LEAN_SORRY", f"uses `{tok}`", m.start()))
        elif tok == "axiom":
            found.append(("LEAN_AXIOM", "declares an axiom", m.start()))
        elif tok in _UNSAFE_WORDS:
            found.append(("LEAN_UNSAFE", f"uses `{tok}`", m.start()))
        elif any(_NATIVE_WORD_RE.search(p) for p in parts) or any(p in _NATIVE_PARTS for p in parts):
            found.append(("LEAN_NATIVE", f"uses `{tok}` (native evaluation is trusted code, not a kernel proof)",
                          m.start()))
        elif tok in _META_WORDS:
            found.append(("LEAN_METAPROGRAM", f"uses `{tok}`", m.start()))
        elif tok in _INSTANCE_WORDS:
            found.append(("LEAN_INSTANCE", f"uses `{tok}`", m.start()))
        elif tok == "deriving" and _DERIVING_INSTANCE_RE.match(code, m.start()):
            found.append(("LEAN_INSTANCE", "uses `deriving instance`", m.start()))
    for m in _HASH_CMD_RE.finditer(code):
        if m.group(1) not in _ALLOWED_HASH:
            found.append(("LEAN_METAPROGRAM", f"uses the command `#{m.group(1)}`", m.start()))
    for m in _ATTR_OPEN_RE.finditer(code):
        open_at = code.index("[", m.start())
        close = _match_bracket(code, open_at)
        for entry in _split_top(code[open_at + 1 : close]):
            name = _attribute_name(entry)
            if name and name not in ALLOWED_ATTRIBUTES:
                found.append(("LEAN_ATTRIBUTE", f"uses the attribute `{name}`", m.start()))
    for m in _SET_OPTION_RE.finditer(code):
        if not _option_allowed(m.group(1)):
            found.append(("LEAN_OPTION", f"sets the option `{m.group(1)}`", m.start()))
    for m in _HEADER_KW_RE.finditer(code):
        found.append(("LEAN_IMPORT", f"uses `{m.group(1)}` (the module system / prelude are not supported)",
                      m.start()))
    for m in _QUALIFIED_IMPORT_RE.finditer(code):
        found.append(("LEAN_IMPORT", "uses a qualified import (public/private/meta import)", m.start()))
    for m in _IMPORT_LINE_RE.finditer(code):
        names = m.group(1).split()
        if names and names[0] == "all":
            found.append(("LEAN_IMPORT", "uses `import all`", m.start()))
            names = names[1:]
        for name in names:
            if not _import_allowed(name, layout, own_module):
                allowed = sorted((layout.allowed_imports() - {own_module}) if layout else set())
                found.append((
                    "LEAN_IMPORT",
                    f"imports `{name}`; allowed: Init, Std" + (f", {', '.join(allowed)}" if allowed else ""),
                    m.start(),
                ))
    return found


def _to_violations(src: LeanSource, rel: str, found: list[tuple[str, str, int]]) -> list[PolicyViolation]:
    out: list[PolicyViolation] = []
    seen: set[tuple[str, int, str]] = set()
    for rule, msg, off in sorted(found, key=lambda t: (t[2], t[0])):
        line = src.line_of(off)
        key = (rule, line, msg)
        if key in seen:
            continue
        seen.add(key)
        out.append(PolicyViolation(rule=rule, message=f"{msg}: `{src.shown(off)}`", file=rel, line=line))
    return out


def _layout_or_none(spec: Spec) -> LeanLayout | None:
    try:
        return lean_layout(spec)
    except ValueError:
        return None


def lean_source_violations(spec: Spec, rel: str, text: str) -> list[PolicyViolation]:
    """Absolute rules for one editable Lean proof file (everything except the
    count-based ``LEAN_SORRY``: at verify time a ``sorry`` just leaves the
    theorem unproved, which the kernel-level axiom check reports)."""
    layout = _layout_or_none(spec)
    own = layout.proof_modules.get(rel) if layout else None
    src = LeanSource(text)
    return _to_violations(src, rel, [h for h in _hits(src, layout, own) if h[0] != "LEAN_SORRY"])


def check_lean_file(spec: Spec, rel: str, old: str | None, new: str) -> list[PolicyViolation]:
    """Repair / generation policy for a changed Lean proof file: the absolute
    rules, plus ``LEAN_SORRY`` when a repair candidate has more
    ``sorry``/``admit`` occurrences than the current file.

    A freshly generated file (``old=None``) may contain placeholders: the gate
    never accepts them (``sorryAx`` leaves the theorem unproved), and the
    count-based rule then stops later repairs from trading proofs for new
    placeholders."""
    out = lean_source_violations(spec, rel, new)
    if old is None:
        return out
    new_src, old_src = LeanSource(new), LeanSource(old)
    new_sorry = [h for h in _hits(new_src, None, None) if h[0] == "LEAN_SORRY"]
    old_hits = [h for h in _hits(old_src, None, None) if h[0] == "LEAN_SORRY"]
    old_count = len(old_hits)
    old_lines: Counter[str] = Counter(old_src.shown(h[2]) for h in old_hits)
    if len(new_sorry) > old_count:
        first = new_sorry[0]
        for h in new_sorry:  # point at an occurrence on a line that did not have one before
            shown = new_src.shown(h[2])
            if old_lines[shown] > 0:
                old_lines[shown] -= 1
            else:
                first = h
                break
        counts = f" ({old_count} -> {len(new_sorry)} occurrences)"
        out.append(
            PolicyViolation(
                rule="LEAN_SORRY",
                message=f"adds a `sorry`/`admit` placeholder{counts}: `{new_src.shown(first[2])}`",
                file=rel,
                line=new_src.line_of(first[2]),
            )
        )
    return out
