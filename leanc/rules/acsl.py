"""Trust rules for ACSL annotations in *editable* (untrusted) C sources.

With the ``wp`` gate, Frama-C reads ACSL from every file it parses.  The
approved contract of the target lives in a protected header; the editable
sources are written by the (untrusted) generation / repair agent.  ACSL in
those sources is welcome when it only adds **proof obligations** - things WP
must prove before it may use them - and must be rejected whenever it could add
an **assumption**, because WP would then prove the approved contract from a
premise nobody approved:

allowed in editable code
    * statement annotations inside function bodies: ``assert``, ``check``,
      ``loop invariant`` / ``check loop invariant``, ``loop assigns``,
      ``loop variant`` (optionally ``for <behaviour>:``);
    * function contracts of **non-target, static** helper functions, directly
      attached to the helper's *definition* (WP proves the helper against
      its contract, and every call site must establish its ``requires``);
    * non-recursive ``predicate`` / ``logic`` *definitions* and ``lemma`` /
      ``check lemma`` (lemmas are proof obligations: WP must prove each one).

rejected (``ACSL_FORBIDDEN``)
    ``admit`` in any form (assumed without proof), ``axiom`` / ``axiomatic``
    (assumed), ``inductive`` definitions, recursive or body-less logic
    definitions (WP turns recursive definitions into axioms, so an inconsistent
    one such as ``f(x) = f(x) + 1`` proves anything), ``ghost`` code,
    global / type invariants, model fields, ``import`` / ``module``,
    statement contracts and any other annotation kind.

rejected (``ACSL_CONTRACT``)
    any function contract on the **target** function - Frama-C *merges* every
    contract of a function, so an extra ``requires \\false`` (or a narrower
    ``requires``) in the editable file silently weakens the approved one;
    contracts that are not attached to the definition of a static helper
    (a contract before a prototype of a library function, or before an
    ``#include``, would attach to a declaration WP never checks);
    ``\\false`` in a helper's ``requires``.

rejected (``ACSL_MACRO``)
    Frama-C preprocesses annotations (``-pp-annot``), so macros are expanded
    *inside* ACSL: an annotation must not use a macro defined in the editable
    file (``#define X \\true; admit P`` would smuggle an ``admit`` past these
    lexical rules), and the editable file must not ``#define`` / ``#undef``
    an identifier used by the approved ACSL of the protected headers or an
    ACSL type name (``#define integer char`` before the ``#include`` would
    quietly weaken a ``\\forall integer`` in the approved contract).

These are lexical checks with conservative failure modes (a rejected
annotation is an inconvenience; an accepted assumption would be a false
``PROVED``).  They apply only to specs that use the ``wp`` gate: without it,
ACSL comments influence no verdict (and e.g. Doxygen's ``//@{`` stays legal).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import defined_functions
from leanc.specs.model import Spec

__all__ = [
    "ACSL_RULES",
    "AcslAnnotation",
    "Clause",
    "TargetContract",
    "acsl_violations",
    "find_annotations",
    "header_acsl_identifiers",
    "parse_clauses",
    "target_contracts",
]

ACSL_RULES: dict[str, str] = {
    "ACSL_FORBIDDEN": "ACSL that could add unproved assumptions or lies outside the allowed subset (admit, axiom / "
    "axiomatic, inductive, recursive or body-less logic definitions, ghost code, global / type invariants, "
    "model fields, import / module, statement contracts, unknown annotation kinds)",
    "ACSL_CONTRACT": "an ACSL function contract that editable code may not contain: any contract on the target "
    "function (Frama-C merges it with the approved one), a contract that is not directly attached to the "
    "definition of a static helper function, or \\false in a requires clause",
    "ACSL_MACRO": "macros and ACSL do not mix in editable code: an annotation uses a macro defined in the editable "
    "file, or the file #defines / #undefs an identifier of the approved ACSL contract or an ACSL type name",
}

#: Binders whose syntax contains a ``;`` that does not end a clause.
_BINDERS = frozenset({"\\forall", "\\exists", "\\let", "\\lambda"})
#: Words that never appear in an allowed editable annotation.
_FORBIDDEN_WORDS = {
    "admit": "'admit' makes Frama-C assume a property without proof",
    "axiom": "axioms are assumed without proof",
    "axiomatic": "axiomatic blocks introduce unproved axioms",
    "inductive": "inductive definitions are encoded as axioms",
    "ghost": "ghost code is not allowed in editable sources",
}
_CONTRACT_HEADS = frozenset(
    {"requires", "ensures", "assigns", "assumes", "terminates", "decreases", "exits", "allocates", "frees",
     "complete", "disjoint"}
)  # fmt: skip
_LOGIC_HEADS = frozenset({"predicate", "logic", "lemma"})
_LOOP_HEADS = frozenset({"invariant", "assigns", "variant"})
_KEYWORDS = _CONTRACT_HEADS | _LOGIC_HEADS | frozenset(
    {"behavior", "behaviors", "loop", "invariant", "variant", "assert", "check", "admit", "for", "breaks",
     "continues", "returns", "ghost", "axiom", "axiomatic", "inductive", "type", "global", "model", "volatile",
     "import", "module", "pragma", "allocation"}
)  # fmt: skip
#: ACSL logic type names a macro must never redefine.
ACSL_TYPE_WORDS = frozenset({"integer", "real", "boolean"})

_SCAN_RE = re.compile(r"[/\"']")
_LITERAL_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')
_TOKEN_RE = re.compile(r"\\?[A-Za-z_]\w*|\d[\w.]*|<==>|==>|<==|==|!=|<=|>=|&&|\|\||::|[^\s\w]")
_DIRECTIVE_RE = re.compile(r"^[^\S\n]*#[^\S\n]*(define|undef)[^\S\n]+([A-Za-z_]\w*)", re.MULTILINE)


# =========================================================================== lexing
@dataclass
class AcslAnnotation:
    """One ``/*@ ... */`` or ``//@ ...`` comment (offsets into the text that
    was scanned, i.e. :attr:`CSource.text` for sources)."""

    start: int  # offset of "/*@" / "//@"
    end: int  # offset just after the comment
    body_start: int
    body: str  # text between the markers, '@' blanked, nested // comments blanked
    raw_body: str  # text between the markers, only '@' blanked
    line_comment: bool


@dataclass
class Clause:
    tokens: list[str]
    offsets: list[int]  # offset of each token relative to the annotation body
    start: int  # offset of the clause relative to the annotation body

    def head(self) -> tuple[list[str], str, int]:
        """``(modifiers, head word, index of head)``: ``behavior b:`` and
        ``for b1, b2:`` prefixes are skipped, ``check`` / ``admit`` are
        returned as modifiers."""
        toks = self.tokens
        mods: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "behavior" and i + 2 < len(toks) and toks[i + 2] == ":":
                mods.append("behavior")
                i += 3
                continue
            if t == "for":
                j = i + 1
                while j < len(toks) and toks[j] != ":":
                    j += 1
                mods.append("for")
                i = j + 1
                continue
            if t in ("check", "admit"):
                mods.append(t)
                i += 1
                continue
            break
        return mods, (toks[i] if i < len(toks) else ""), i


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def find_annotations(text: str) -> list[AcslAnnotation]:
    """All ACSL comments of ``text`` (string / character literals and plain
    comments are skipped exactly like the C lexer does)."""
    out: list[AcslAnnotation] = []
    n = len(text)
    i = 0
    while i < n:
        m = _SCAN_RE.search(text, i)
        if m is None:
            break
        i = m.start()
        c = text[i]
        if c == "/":
            nxt = text[i + 1 : i + 2]
            if nxt == "*":
                j = text.find("*/", i + 2)
                end = n if j < 0 else j + 2
                if text.startswith("/*@", i):
                    body_end = end if j < 0 else j
                    out.append(_annotation(text, i, i + 3, body_end, end, line_comment=False))
                i = end
                continue
            if nxt == "/":
                j = text.find("\n", i + 2)
                end = n if j < 0 else j
                if text.startswith("//@", i):
                    out.append(_annotation(text, i, i + 3, end, end, line_comment=True))
                i = end
                continue
            i += 1
            continue
        # string / character literal: ends at the closing quote or the newline
        j = i + 1
        while j < n:
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == c:
                j += 1
                break
            if ch == "\n":
                break
            j += 1
        i = max(j, i + 1)
    return out


def _annotation(text: str, start: int, body_start: int, body_end: int, end: int, *, line_comment: bool) -> AcslAnnotation:
    raw = text[body_start:body_end].replace("@", " ")
    body = raw
    if not line_comment:
        body = re.sub(r"//[^\n]*", lambda mm: _blank(mm.group()), body)
    # blank ACSL string / character literal contents (keep the quotes)
    body = _LITERAL_RE.sub(lambda mm: mm.group()[0] + " " * (len(mm.group()) - 2) + mm.group()[-1], body)
    return AcslAnnotation(
        start=start, end=end, body_start=body_start, body=body, raw_body=raw, line_comment=line_comment
    )


def parse_clauses(body: str) -> list[Clause]:
    """Split an annotation body into clauses at top-level ``;`` (the ``;``
    of ``\\forall`` / ``\\exists`` / ``\\let`` / ``\\lambda`` binders does
    not end a clause)."""
    clauses: list[Clause] = []
    toks: list[str] = []
    offs: list[int] = []
    depth = 0
    pending = 0
    for m in _TOKEN_RE.finditer(body):
        t = m.group()
        if t in "([{":
            depth += 1
        elif t in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and t in _BINDERS:
            pending += 1
        elif depth == 0 and t == ";":
            if pending:
                pending -= 1
            else:
                if toks:
                    clauses.append(Clause(tokens=toks, offsets=offs, start=offs[0]))
                toks, offs = [], []
                continue
        toks.append(t)
        offs.append(m.start())
    if toks:
        clauses.append(Clause(tokens=toks, offsets=offs, start=offs[0]))
    return clauses


def _identifiers(tokens: list[str]) -> set[str]:
    return {t for t in tokens if re.fullmatch(r"[A-Za-z_]\w*", t)}


def header_acsl_identifiers(headers: dict[str, str] | None) -> set[str]:
    """Identifiers used inside the ACSL annotations of the protected headers."""
    out: set[str] = set()
    for text in (headers or {}).values():
        for a in find_annotations(CSource(text).text):
            out |= _identifiers([m.group() for m in _TOKEN_RE.finditer(a.raw_body)])
    return out


# =========================================================================== helpers
def _brace_depths(code: str) -> tuple[list[int], list[int]]:
    marks: list[int] = []
    depth_after: list[int] = []
    d = 0
    for bm in re.finditer(r"[{}]", code):
        d = d + 1 if bm.group() == "{" else max(0, d - 1)
        marks.append(bm.start())
        depth_after.append(d)
    return marks, depth_after


def _depth_at(marks: list[int], depth_after: list[int], offset: int) -> int:
    j = bisect.bisect_left(marks, offset) - 1
    return depth_after[j] if j >= 0 else 0


def _logic_name(tokens: list[str], head_idx: int) -> tuple[str | None, int | None]:
    """Name of a ``predicate`` / ``logic`` definition and the index of its
    top-level ``=`` (None if the definition has no body)."""
    depth = 0
    eq = None
    name = None
    for k in range(head_idx + 1, len(tokens)):
        t = tokens[k]
        if t in "([{":
            if depth == 0 and name is None and t in "({":
                name = _last_ident(tokens[head_idx + 1 : k])
            depth += 1
        elif t in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and t == "=":
            if name is None:
                name = _last_ident(tokens[head_idx + 1 : k])
            eq = k
            break
    if name is None:
        name = _last_ident(tokens[head_idx + 1 :])
    return name, eq


def _last_ident(tokens: list[str]) -> str | None:
    for t in reversed(tokens):
        if re.fullmatch(r"[A-Za-z_]\w*", t) and t not in _KEYWORDS:
            return t
    return None


# =========================================================================== rules
_DEFINE_BODY_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(.*)$", re.M)


def acsl_violations(spec: Spec, src: CSource, headers: dict[str, str] | None = None) -> list[tuple[str, str, int]]:
    """``(rule, message, offset)`` triples for one editable C file (offsets
    into ``src.text``, as :func:`leanc.rules.c_rules.c_source_violations`
    expects).  ``headers`` (protected header texts) enables the check that no
    identifier of the approved ACSL is redefined by a macro."""
    if "wp" not in spec.gates or spec.language.is_cpp:
        return []
    target = spec.target.function
    text = src.text
    annots = find_annotations(text)
    found: list[tuple[str, str, int]] = []

    file_macros: dict[str, int] = {}
    for m in _DIRECTIVE_RE.finditer(src.code):
        if m.group(1) == "define":
            file_macros.setdefault(m.group(2), m.start())
    protected_idents = header_acsl_identifiers(headers) | ACSL_TYPE_WORDS
    for m in _DIRECTIVE_RE.finditer(src.code):
        name = m.group(2)
        if name in protected_idents:
            what = "an ACSL type name" if name in ACSL_TYPE_WORDS else "an identifier of the approved ACSL contract"
            found.append(("ACSL_MACRO", (f"#{m.group(1)} {name}: '{name}' is {what}; Frama-C expands macros inside "
                          "annotations, so this could change the meaning of the approved contract"), m.start()))
    # A macro whose body names the target can (re)declare it out of sight of
    # this lexical analysis; Frama-C then attaches the next annotation to the
    # TARGET and merges it with the approved contract.  Forbid it outright.
    for dm in _DEFINE_BODY_RE.finditer(src.code):
        if re.search(rf"\b{re.escape(target)}\b", dm.group(2)):
            found.append(("ACSL_CONTRACT", (f"#define {dm.group(1)}: a macro mentioning the target function "
                          f"'{target}' could re-declare it and attach a contract that Frama-C merges with the "
                          "approved one"), dm.start()))
    if not annots:
        return found

    marks, depth_after = _brace_depths(src.code_nodirectives)
    defs = sorted(defined_functions(src), key=lambda d: d[1])
    def_offsets = [d[1] for d in defs]

    contract_group: list[AcslAnnotation] = []

    def flush_group() -> None:
        if contract_group:
            found.extend(_check_attachment(contract_group, src, defs, def_offsets, annots, target, file_macros))
            contract_group.clear()

    for a in annots:
        base = a.body_start
        for tm in _TOKEN_RE.finditer(a.raw_body):
            why = _FORBIDDEN_WORDS.get(tm.group())
            if why:
                found.append(("ACSL_FORBIDDEN", f"ACSL '{tm.group()}' is not allowed in editable code: {why}",
                              base + tm.start()))
        for tm in _TOKEN_RE.finditer(a.body):
            if tm.group() in file_macros:
                found.append(("ACSL_MACRO", (f"the annotation uses '{tm.group()}', a macro defined in this file; "
                              "Frama-C expands macros inside annotations, so what it proves would differ from "
                              "what is written (use the value / an ACSL logic definition instead)"), base + tm.start()))
        clauses = parse_clauses(a.body)
        if not clauses:
            continue
        depth = _depth_at(marks, depth_after, a.start)
        if depth > 0:
            flush_group()
            for c in clauses:
                found.extend(_statement_clause(c, base))
            continue
        mods, head, _ = clauses[0].head()
        if head in _LOGIC_HEADS and "behavior" not in mods:
            flush_group()
            found.extend(_logic_annotation(clauses, base))
            continue
        if head in _CONTRACT_HEADS or "behavior" in mods:
            for c in clauses:
                _cm, ch, ci = c.head()
                if ch not in _CONTRACT_HEADS:
                    found.append(("ACSL_FORBIDDEN", (f"'{' '.join(c.tokens[:3])}' is not a function-contract clause "
                                  "allowed in editable code"), base + c.start))
                elif ch == "requires" and "\\false" in c.tokens[ci:]:
                    found.append(("ACSL_CONTRACT", ("'requires \\false' makes the helper's proof vacuous and every "
                                  "call site unprovable"), base + c.start))
            # consecutive contract comments (e.g. several //@ lines) form one contract
            if contract_group and not _only_blank_between(src, contract_group[-1].end, a.start):
                flush_group()
            contract_group.append(a)
            continue
        flush_group()
        found.append(("ACSL_FORBIDDEN", (f"file-scope annotation starting with '{' '.join(clauses[0].tokens[:3])}' "
                      "is not allowed in editable code (only static-helper contracts, predicate / logic definitions "
                      "and lemmas)"), base + clauses[0].start))
    flush_group()
    return found


def _only_blank_between(src: CSource, a: int, b: int) -> bool:
    return not src.code[a:b].strip()


def _statement_clause(c: Clause, base: int) -> list[tuple[str, str, int]]:
    mods, head, i = c.head()
    toks = c.tokens
    where = base + c.start
    if "admit" in mods:
        return []  # already reported as a forbidden word
    if head == "assert":
        return []
    if head == "loop" and i + 1 < len(toks) and toks[i + 1] in _LOOP_HEADS:
        return []
    if "check" in mods and head not in _KEYWORDS:
        return []  # `check P;`
    shown = " ".join(toks[: i + 2])
    return [("ACSL_FORBIDDEN", (f"statement annotation '{shown}' is not allowed in editable code (allowed: assert, "
             "check, loop invariant / loop assigns / loop variant)"), where)]


def _logic_annotation(clauses: list[Clause], base: int) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    names: list[tuple[str, int]] = []
    for idx, c in enumerate(clauses):
        mods, head, i = c.head()
        where = base + c.start
        if head == "lemma":
            if "behavior" in mods or "for" in mods:
                out.append(("ACSL_FORBIDDEN", "malformed lemma", where))
            continue
        if head not in ("predicate", "logic") or mods:
            out.append(("ACSL_FORBIDDEN", (f"'{' '.join(c.tokens[: i + 2])}' is not allowed in a logic annotation "
                        "of editable code (only predicate / logic definitions and lemmas)"), where))
            continue
        name, eq = _logic_name(c.tokens, i)
        if eq is None:
            out.append(("ACSL_FORBIDDEN", (f"{head} {name or '?'} has no definition (= ...): abstract logic symbols "
                        "belong in the approved header"), where))
            continue
        names.append((name or "", idx))
        body = set(c.tokens[eq + 1 :])
        if name and name in body:
            out.append(("ACSL_FORBIDDEN", (f"{head} {name} is recursive: WP encodes recursive definitions as axioms "
                        "and Frama-C does not check that they terminate, so an inconsistent one would prove anything"),
                        where))
    return out


def _check_attachment(
    group: list[AcslAnnotation],
    src: CSource,
    defs: list[tuple[str, int]],
    def_offsets: list[int],
    annots: list[AcslAnnotation],
    target: str,
    file_macros: dict[str, int],
) -> list[tuple[str, str, int]]:
    """A (group of) file-scope contract annotation(s) must be followed by the
    definition of a static, non-target helper function, with nothing but
    declaration specifiers in between."""
    last = group[-1]
    at = group[0].start
    k = bisect.bisect_right(def_offsets, last.end)
    if k >= len(defs):
        return [("ACSL_CONTRACT", ("function contract not attached to a function definition (a contract on a "
                 "prototype or library declaration would be assumed, never proved)"), at)]
    name, off = defs[k]
    between = src.code[last.end : off]
    other = [a for a in annots if last.end <= a.start < off]
    attached = not (re.search(r"[;{}]", between) or re.search(r"(?m)^\s*#", between) or other)
    if not attached and not (name == target and not re.search(r"[;{}]", between)):
        return [("ACSL_CONTRACT", ("function contract not directly attached to a helper function definition "
                 f"(the next definition, {name}(), is preceded by other declarations, directives or annotations; "
                 "a contract on a declaration would be assumed, never proved, and one before an #include or a "
                 "prototype could attach to the approved target)"), at)]
    if name == target:
        return [("ACSL_CONTRACT", (f"a function contract on the target function {target}() is not allowed in "
                 "editable code: Frama-C merges it with the approved contract of the protected header (an extra "
                 "'requires' narrows it, 'requires \\false' makes every proof vacuous)"), at)]
    if name in file_macros:
        return [("ACSL_CONTRACT", f"the contract is attached to '{name}', which is also a macro of this file", at)]
    if not re.search(r"\bstatic\b", between):
        return [("ACSL_CONTRACT", (f"helper contracts are only allowed on static functions; {name}() is not "
                 "declared static here"), at)]
    return []


# =========================================================================== approved contract (headers)
@dataclass
class TargetContract:
    """The ACSL contract attached to a prototype of the target in a protected
    header."""

    file: str
    line: int
    text: str
    clauses: dict[str, int] = field(default_factory=dict)  # head -> count
    behaviors: list[str] = field(default_factory=list)

    @property
    def has_obligations(self) -> bool:
        return bool(self.clauses.get("ensures") or self.clauses.get("assigns"))


def target_contracts(target: str, headers: dict[str, str]) -> list[TargetContract]:
    """Contracts attached to prototypes of ``target`` in the protected headers
    (an annotation directly before the declaration, possibly a group of
    consecutive ``//@`` lines)."""
    out: list[TargetContract] = []
    for rel, text in sorted(headers.items()):
        src = CSource(text)
        annots = find_annotations(src.text)
        if not annots:
            continue
        marks, depth_after = _brace_depths(src.code_nodirectives)
        for m in re.finditer(rf"(?<![\w.])\b{re.escape(target)}\s*\(", src.code_nodirectives):
            if _depth_at(marks, depth_after, m.start()) > 0:
                continue
            before = [a for a in annots if a.end <= m.start()]
            if not before:
                continue
            last = before[-1]
            between = src.code[last.end : m.start()]
            if re.search(r"[;{}]", between) or re.search(r"(?m)^\s*#", between):
                continue
            group = [last]
            for a in reversed(before[:-1]):
                if src.code[a.end : group[0].start].strip():
                    break
                group.insert(0, a)
            clauses: dict[str, int] = {}
            behaviors: list[str] = []
            body = "\n".join(a.body for a in group)
            for c in parse_clauses(body):
                mods, head, i = c.head()
                if "behavior" in mods and len(c.tokens) > 1:
                    behaviors.append(c.tokens[c.tokens.index("behavior") + 1])
                if head:
                    clauses[head] = clauses.get(head, 0) + 1
            out.append(
                TargetContract(
                    file=rel,
                    line=src.line_of(group[0].start),
                    text="\n".join(src.text[a.start : a.end] for a in group),
                    clauses=clauses,
                    behaviors=behaviors,
                )
            )
    return out
