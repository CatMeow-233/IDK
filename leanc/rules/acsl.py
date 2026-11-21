from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from leanc.lang.c_lexer import CSource
from leanc.rules.definitions import defined_functions
from leanc.rules.findings import Finding
from leanc.specs.model import Spec

__all__ = [
    "ACSL_RULES",
    "ACSL_TYPE_WORDS",
    "AcslAnnotation",
    "Clause",
    "ClauseHead",
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

WP_GATE = "wp"
_BINDERS_WITH_INNER_SEMICOLON = frozenset({"\\forall", "\\exists", "\\let", "\\lambda"})
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
)
_LOGIC_HEADS = frozenset({"predicate", "logic", "lemma"})
_LOOP_HEADS = frozenset({"invariant", "assigns", "variant"})
_KEYWORDS = _CONTRACT_HEADS | _LOGIC_HEADS | frozenset(
    {"behavior", "behaviors", "loop", "invariant", "variant", "assert", "check", "admit", "for", "breaks",
     "continues", "returns", "ghost", "axiom", "axiomatic", "inductive", "type", "global", "model", "volatile",
     "import", "module", "pragma", "allocation"}
)
ACSL_TYPE_WORDS = frozenset({"integer", "real", "boolean"})
_ANNOTATION_MARKER_CHARS = len("/*@")
_QUOTED_CLAUSE_TOKENS = 3

_SCAN_RE = re.compile(r"[/\"']")
_LITERAL_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'')
_NESTED_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_TOKEN_RE = re.compile(r"\\?[A-Za-z_]\w*|\d[\w.]*|<==>|==>|<==|==|!=|<=|>=|&&|\|\||::|[^\s\w]")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_MACRO_DIRECTIVE_RE = re.compile(r"^[^\S\n]*#[^\S\n]*(define|undef)[^\S\n]+([A-Za-z_]\w*)", re.MULTILINE)
_DEFINE_BODY_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(.*)$", re.M)
_DECLARATION_BOUNDARY_RE = re.compile(r"[;{}]")
_DIRECTIVE_LINE_RE = re.compile(r"(?m)^\s*#")
_STATIC_RE = re.compile(r"\bstatic\b")


@dataclass
class AcslAnnotation:
    start: int
    end: int
    body_start: int
    body: str
    raw_body: str
    line_comment: bool


class ClauseHead(NamedTuple):
    modifiers: list[str]
    word: str
    index: int


def _index_after_colon(tokens: list[str], i: int) -> int:
    while i < len(tokens) and tokens[i] != ":":
        i += 1
    return i + 1


@dataclass
class Clause:
    tokens: list[str]
    offsets: list[int]
    start: int

    def head(self) -> ClauseHead:
        toks = self.tokens
        modifiers: list[str] = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "behavior" and i + 2 < len(toks) and toks[i + 2] == ":":
                modifiers.append("behavior")
                i += 3
                continue
            if t == "for":
                modifiers.append("for")
                i = _index_after_colon(toks, i + 1)
                continue
            if t in ("check", "admit"):
                modifiers.append(t)
                i += 1
                continue
            break
        return ClauseHead(modifiers, toks[i] if i < len(toks) else "", i)


def _blank(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _end_of_literal(text: str, quote_at: int) -> int:
    quote = text[quote_at]
    j = quote_at + 1
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n":
            return j
        j += 1
    return j


def _block_comment(text: str, start: int) -> tuple[AcslAnnotation | None, int]:
    close = text.find("*/", start + 2)
    end = len(text) if close < 0 else close + 2
    if not text.startswith("/*@", start):
        return None, end
    body_end = end if close < 0 else close
    return _annotation(text, start, start + _ANNOTATION_MARKER_CHARS, body_end, end, line_comment=False), end


def _line_comment(text: str, start: int) -> tuple[AcslAnnotation | None, int]:
    newline = text.find("\n", start + 2)
    end = len(text) if newline < 0 else newline
    if not text.startswith("//@", start):
        return None, end
    return _annotation(text, start, start + _ANNOTATION_MARKER_CHARS, end, end, line_comment=True), end


def find_annotations(text: str) -> list[AcslAnnotation]:
    out: list[AcslAnnotation] = []
    i = 0
    while i < len(text):
        found = _SCAN_RE.search(text, i)
        if found is None:
            break
        i = found.start()
        if text[i] != "/":
            i = max(_end_of_literal(text, i), i + 1)
            continue
        following = text[i + 1 : i + 2]
        if following == "*":
            annotation, i = _block_comment(text, i)
        elif following == "/":
            annotation, i = _line_comment(text, i)
        else:
            i += 1
            continue
        if annotation is not None:
            out.append(annotation)
    return out


def _blank_literal_contents(body: str) -> str:
    return _LITERAL_RE.sub(lambda lit: lit.group()[0] + " " * (len(lit.group()) - 2) + lit.group()[-1], body)


def _annotation(text: str, start: int, body_start: int, body_end: int, end: int, *, line_comment: bool) -> AcslAnnotation:
    raw = text[body_start:body_end].replace("@", " ")
    body = raw if line_comment else _NESTED_LINE_COMMENT_RE.sub(lambda c: _blank(c.group()), raw)
    return AcslAnnotation(
        start=start,
        end=end,
        body_start=body_start,
        body=_blank_literal_contents(body),
        raw_body=raw,
        line_comment=line_comment,
    )


def parse_clauses(body: str) -> list[Clause]:
    clauses: list[Clause] = []
    toks: list[str] = []
    offs: list[int] = []
    depth = 0
    open_binders = 0
    for token in _TOKEN_RE.finditer(body):
        t = token.group()
        if t in "([{":
            depth += 1
        elif t in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and t in _BINDERS_WITH_INNER_SEMICOLON:
            open_binders += 1
        elif depth == 0 and t == ";":
            if open_binders:
                open_binders -= 1
            else:
                if toks:
                    clauses.append(Clause(tokens=toks, offsets=offs, start=offs[0]))
                toks, offs = [], []
                continue
        toks.append(t)
        offs.append(token.start())
    if toks:
        clauses.append(Clause(tokens=toks, offsets=offs, start=offs[0]))
    return clauses


def _identifier_tokens(tokens: list[str]) -> set[str]:
    return {t for t in tokens if _IDENTIFIER_RE.fullmatch(t)}


def header_acsl_identifiers(headers: dict[str, str] | None) -> set[str]:
    out: set[str] = set()
    for text in (headers or {}).values():
        for annotation in find_annotations(CSource(text).text):
            out |= _identifier_tokens([t.group() for t in _TOKEN_RE.finditer(annotation.raw_body)])
    return out


class _BraceDepths:
    def __init__(self, code: str):
        self._marks: list[int] = []
        self._depth_after: list[int] = []
        depth = 0
        for brace in re.finditer(r"[{}]", code):
            depth = depth + 1 if brace.group() == "{" else max(0, depth - 1)
            self._marks.append(brace.start())
            self._depth_after.append(depth)

    def at(self, offset: int) -> int:
        j = bisect.bisect_left(self._marks, offset) - 1
        return self._depth_after[j] if j >= 0 else 0


class _Definitions:
    def __init__(self, src: CSource):
        self._defs = sorted(defined_functions(src), key=lambda d: d[1])
        self._offsets = [offset for _, offset in self._defs]

    def first_after(self, offset: int) -> tuple[str, int] | None:
        k = bisect.bisect_right(self._offsets, offset)
        return self._defs[k] if k < len(self._defs) else None


def _last_ident(tokens: list[str]) -> str | None:
    for t in reversed(tokens):
        if _IDENTIFIER_RE.fullmatch(t) and t not in _KEYWORDS:
            return t
    return None


def _logic_name(tokens: list[str], head_idx: int) -> tuple[str | None, int | None]:
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


def _uses_acsl(spec: Spec) -> bool:
    return WP_GATE in spec.gates and not spec.language.is_cpp


def _file_macros(src: CSource) -> set[str]:
    return {m.group(2) for m in _MACRO_DIRECTIVE_RE.finditer(src.code) if m.group(1) == "define"}


def _protected_identifier_redefinitions(src: CSource, headers: dict[str, str] | None) -> list[Finding]:
    protected = header_acsl_identifiers(headers) | ACSL_TYPE_WORDS
    found: list[Finding] = []
    for m in _MACRO_DIRECTIVE_RE.finditer(src.code):
        name = m.group(2)
        if name in protected:
            what = "an ACSL type name" if name in ACSL_TYPE_WORDS else "an identifier of the approved ACSL contract"
            found.append(Finding("ACSL_MACRO", (f"#{m.group(1)} {name}: '{name}' is {what}; Frama-C expands macros "
                                 "inside annotations, so this could change the meaning of the approved contract"),
                                 m.start()))
    return found


def _target_mentioning_macros(src: CSource, target: str) -> list[Finding]:
    mentions_target = re.compile(rf"\b{re.escape(target)}\b")
    return [
        Finding("ACSL_CONTRACT", (f"#define {define.group(1)}: a macro mentioning the target function '{target}' "
                "could re-declare it and attach a contract that Frama-C merges with the approved one"),
                define.start())
        for define in _DEFINE_BODY_RE.finditer(src.code)
        if mentions_target.search(define.group(2))
    ]


def acsl_violations(spec: Spec, src: CSource, headers: dict[str, str] | None = None) -> list[Finding]:
    if not _uses_acsl(spec):
        return []
    target = spec.target.function
    annotations = find_annotations(src.text)
    found = [*_protected_identifier_redefinitions(src, headers), *_target_mentioning_macros(src, target)]
    if not annotations:
        return found
    return found + _annotation_findings(src, annotations, target, _file_macros(src))


def _forbidden_word_findings(annotation: AcslAnnotation) -> list[Finding]:
    return [
        Finding("ACSL_FORBIDDEN", f"ACSL '{token.group()}' is not allowed in editable code: {why}",
                annotation.body_start + token.start())
        for token in _TOKEN_RE.finditer(annotation.raw_body)
        if (why := _FORBIDDEN_WORDS.get(token.group()))
    ]


def _file_macro_use_findings(annotation: AcslAnnotation, file_macros: set[str]) -> list[Finding]:
    return [
        Finding("ACSL_MACRO", (f"the annotation uses '{token.group()}', a macro defined in this file; Frama-C "
                "expands macros inside annotations, so what it proves would differ from what is written (use the "
                "value / an ACSL logic definition instead)"), annotation.body_start + token.start())
        for token in _TOKEN_RE.finditer(annotation.body)
        if token.group() in file_macros
    ]


def _is_logic_definition(head: ClauseHead) -> bool:
    return head.word in _LOGIC_HEADS and "behavior" not in head.modifiers


def _is_function_contract(head: ClauseHead) -> bool:
    return head.word in _CONTRACT_HEADS or "behavior" in head.modifiers


def _only_blank_between(src: CSource, a: int, b: int) -> bool:
    return not src.code[a:b].strip()


def _annotation_findings(
    src: CSource, annotations: list[AcslAnnotation], target: str, file_macros: set[str]
) -> list[Finding]:
    depths = _BraceDepths(src.code_nodirectives)
    definitions = _Definitions(src)
    found: list[Finding] = []
    contract: list[AcslAnnotation] = []

    def close_contract() -> None:
        if contract:
            found.extend(_attachment_findings(contract, src, definitions, annotations, target, file_macros))
            contract.clear()

    for annotation in annotations:
        found.extend(_forbidden_word_findings(annotation))
        found.extend(_file_macro_use_findings(annotation, file_macros))
        clauses = parse_clauses(annotation.body)
        if not clauses:
            continue
        base = annotation.body_start
        if depths.at(annotation.start) > 0:
            close_contract()
            for clause in clauses:
                found.extend(_statement_clause_findings(clause, base))
            continue
        head = clauses[0].head()
        if _is_logic_definition(head):
            close_contract()
            found.extend(_logic_annotation_findings(clauses, base))
            continue
        if _is_function_contract(head):
            found.extend(_contract_clause_findings(clauses, base))
            if contract and not _only_blank_between(src, contract[-1].end, annotation.start):
                close_contract()
            contract.append(annotation)
            continue
        close_contract()
        found.append(Finding("ACSL_FORBIDDEN", (f"file-scope annotation starting with "
                             f"'{' '.join(clauses[0].tokens[:_QUOTED_CLAUSE_TOKENS])}' is not allowed in editable "
                             "code (only static-helper contracts, predicate / logic definitions and lemmas)"),
                             base + clauses[0].start))
    close_contract()
    return found


def _contract_clause_findings(clauses: list[Clause], base: int) -> list[Finding]:
    found: list[Finding] = []
    for clause in clauses:
        head = clause.head()
        if head.word not in _CONTRACT_HEADS:
            found.append(Finding("ACSL_FORBIDDEN", (f"'{' '.join(clause.tokens[:_QUOTED_CLAUSE_TOKENS])}' is not a "
                                 "function-contract clause allowed in editable code"), base + clause.start))
        elif head.word == "requires" and "\\false" in clause.tokens[head.index :]:
            found.append(Finding("ACSL_CONTRACT", ("'requires \\false' makes the helper's proof vacuous and every "
                                 "call site unprovable"), base + clause.start))
    return found


def _already_reported_as_forbidden_word(head: ClauseHead) -> bool:
    return "admit" in head.modifiers


def _is_allowed_statement_annotation(clause: Clause, head: ClauseHead) -> bool:
    if head.word == "assert":
        return True
    if head.word == "loop" and head.index + 1 < len(clause.tokens) and clause.tokens[head.index + 1] in _LOOP_HEADS:
        return True
    return "check" in head.modifiers and head.word not in _KEYWORDS


def _statement_clause_findings(clause: Clause, base: int) -> list[Finding]:
    head = clause.head()
    if _already_reported_as_forbidden_word(head) or _is_allowed_statement_annotation(clause, head):
        return []
    shown = " ".join(clause.tokens[: head.index + 2])
    return [Finding("ACSL_FORBIDDEN", (f"statement annotation '{shown}' is not allowed in editable code (allowed: "
                    "assert, check, loop invariant / loop assigns / loop variant)"), base + clause.start)]


def _logic_annotation_findings(clauses: list[Clause], base: int) -> list[Finding]:
    out: list[Finding] = []
    for clause in clauses:
        head = clause.head()
        where = base + clause.start
        if head.word == "lemma":
            if "behavior" in head.modifiers or "for" in head.modifiers:
                out.append(Finding("ACSL_FORBIDDEN", "malformed lemma", where))
            continue
        if head.word not in ("predicate", "logic") or head.modifiers:
            out.append(Finding("ACSL_FORBIDDEN", (f"'{' '.join(clause.tokens[: head.index + 2])}' is not allowed in "
                               "a logic annotation of editable code (only predicate / logic definitions and lemmas)"),
                               where))
            continue
        name, eq = _logic_name(clause.tokens, head.index)
        if eq is None:
            out.append(Finding("ACSL_FORBIDDEN", (f"{head.word} {name or '?'} has no definition (= ...): abstract "
                               "logic symbols belong in the approved header"), where))
            continue
        if name and name in set(clause.tokens[eq + 1 :]):
            out.append(Finding("ACSL_FORBIDDEN", (f"{head.word} {name} is recursive: WP encodes recursive "
                               "definitions as axioms and Frama-C does not check that they terminate, so an "
                               "inconsistent one would prove anything"), where))
    return out


def _attachment_findings(
    group: list[AcslAnnotation],
    src: CSource,
    definitions: _Definitions,
    annotations: list[AcslAnnotation],
    target: str,
    file_macros: set[str],
) -> list[Finding]:
    last = group[-1]
    at = group[0].start
    following = definitions.first_after(last.end)
    if following is None:
        return [Finding("ACSL_CONTRACT", ("function contract not attached to a function definition (a contract on "
                        "a prototype or library declaration would be assumed, never proved)"), at)]
    name, offset = following
    between = src.code[last.end : offset]
    interleaved = [a for a in annotations if last.end <= a.start < offset]
    separated = bool(_DECLARATION_BOUNDARY_RE.search(between))
    attached = not (separated or _DIRECTIVE_LINE_RE.search(between) or interleaved)
    aimed_at_target = name == target and not separated
    if not attached and not aimed_at_target:
        return [Finding("ACSL_CONTRACT", ("function contract not directly attached to a helper function definition "
                        f"(the next definition, {name}(), is preceded by other declarations, directives or "
                        "annotations; a contract on a declaration would be assumed, never proved, and one before an "
                        "#include or a prototype could attach to the approved target)"), at)]
    if name == target:
        return [Finding("ACSL_CONTRACT", (f"a function contract on the target function {target}() is not allowed "
                        "in editable code: Frama-C merges it with the approved contract of the protected header (an "
                        "extra 'requires' narrows it, 'requires \\false' makes every proof vacuous)"), at)]
    if name in file_macros:
        return [Finding("ACSL_CONTRACT", f"the contract is attached to '{name}', which is also a macro of this file",
                        at)]
    if not _STATIC_RE.search(between):
        return [Finding("ACSL_CONTRACT", (f"helper contracts are only allowed on static functions; {name}() is not "
                        "declared static here"), at)]
    return []


@dataclass
class TargetContract:
    file: str
    line: int
    text: str
    clauses: dict[str, int] = field(default_factory=dict)
    behaviors: list[str] = field(default_factory=list)

    @property
    def has_obligations(self) -> bool:
        return bool(self.clauses.get("ensures") or self.clauses.get("assigns"))


def _contract_before(src: CSource, annotations: list[AcslAnnotation], offset: int) -> list[AcslAnnotation]:
    before = [a for a in annotations if a.end <= offset]
    if not before:
        return []
    last = before[-1]
    between = src.code[last.end : offset]
    if _DECLARATION_BOUNDARY_RE.search(between) or _DIRECTIVE_LINE_RE.search(between):
        return []
    group = [last]
    for annotation in reversed(before[:-1]):
        if src.code[annotation.end : group[0].start].strip():
            break
        group.insert(0, annotation)
    return group


def _target_contract(rel: str, src: CSource, group: list[AcslAnnotation]) -> TargetContract:
    clauses: dict[str, int] = {}
    behaviors: list[str] = []
    for clause in parse_clauses("\n".join(a.body for a in group)):
        head = clause.head()
        if "behavior" in head.modifiers and len(clause.tokens) > 1:
            behaviors.append(clause.tokens[clause.tokens.index("behavior") + 1])
        if head.word:
            clauses[head.word] = clauses.get(head.word, 0) + 1
    return TargetContract(
        file=rel,
        line=src.line_of(group[0].start),
        text="\n".join(src.text[a.start : a.end] for a in group),
        clauses=clauses,
        behaviors=behaviors,
    )


def target_contracts(target: str, headers: dict[str, str]) -> list[TargetContract]:
    out: list[TargetContract] = []
    for rel, text in sorted(headers.items()):
        src = CSource(text)
        annotations = find_annotations(src.text)
        if not annotations:
            continue
        depths = _BraceDepths(src.code_nodirectives)
        for call in re.finditer(rf"(?<![\w.])\b{re.escape(target)}\s*\(", src.code_nodirectives):
            if depths.at(call.start()) > 0:
                continue
            group = _contract_before(src, annotations, call.start())
            if group:
                out.append(_target_contract(rel, src, group))
    return out
