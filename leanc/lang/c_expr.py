from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from leanc.lang.c_expr_lexer import (
    END_TOKEN,
    MAX_CLAUSE_DEPTH,
    ClauseRejected,
    Rejection,
    Token,
    TokenKind,
    shown_text,
    tokenize,
)
from leanc.lang.c_identifiers import C_KEYWORDS, HARNESS_INPUT_PREFIX, RESERVED_IDENTIFIER_RE

__all__ = [
    "BINARY_PRECEDENCE",
    "FORBIDDEN_IDENTIFIER_PREFIXES",
    "INTEGER_TYPEDEF_NAMES",
    "INTEGER_TYPE_SPELLINGS",
    "LOOP_ENTRY",
    "OBJECT_UPTO",
    "OBJECT_WHOLE",
    "STANDARD_MACRO_NAMES",
    "AssignsTarget",
    "Binary",
    "BinaryOperator",
    "Cast",
    "Clause",
    "ClauseKind",
    "Expression",
    "Index",
    "IntConstant",
    "LoopEntry",
    "Member",
    "Name",
    "ObjectUpto",
    "ObjectWhole",
    "Unary",
    "UnaryOperator",
    "parse_clause",
]

LOOP_ENTRY = "__CPROVER_loop_entry"
OBJECT_WHOLE = "__CPROVER_object_whole"
OBJECT_UPTO = "__CPROVER_object_upto"
OBJECT_FORMS = frozenset({OBJECT_WHOLE, OBJECT_UPTO})
FORBIDDEN_IDENTIFIER_PREFIXES: tuple[str, ...] = (HARNESS_INPUT_PREFIX, "LEANC_", "leanc_")

_SIGNEDNESS_PREFIXES = ("", "signed ", "unsigned ")
_INTEGER_SIZES = ("char", "short", "short int", "int", "long", "long int", "long long", "long long int")
INTEGER_TYPE_SPELLINGS: frozenset[str] = frozenset(
    {prefix + size for prefix in _SIGNEDNESS_PREFIXES for size in _INTEGER_SIZES} | {"signed", "unsigned"}
)
INTEGER_TYPEDEF_NAMES: frozenset[str] = frozenset(
    {"size_t", "ptrdiff_t", "intptr_t", "uintptr_t"}
    | {f"{sign}int{bits}_t" for sign in ("", "u") for bits in (8, 16, 32, 64)}
)
_INTEGER_TYPE_KEYWORDS = frozenset({"signed", "unsigned", "char", "short", "int", "long"})
_CAST_TYPE_WORDS = _INTEGER_TYPE_KEYWORDS | INTEGER_TYPEDEF_NAMES

_LIMIT_MACRO_STEMS = ("CHAR", "SCHAR", "UCHAR", "SHRT", "USHRT", "INT", "UINT", "LONG", "ULONG", "LLONG", "ULLONG")
_EXACT_WIDTH_STEMS = tuple(f"{sign}INT{bits}" for sign in ("", "U") for bits in (8, 16, 32, 64))
_POINTER_SIZED_STEMS = ("SIZE", "PTRDIFF", "INTPTR", "UINTPTR", "INTMAX", "UINTMAX")
_LIMIT_BOUNDS = ("MIN", "MAX")
STANDARD_MACRO_NAMES: frozenset[str] = frozenset(
    {"NULL", "true", "false", "bool", "EOF", "errno", "CHAR_BIT", "offsetof"}
    | {
        f"{stem}_{bound}"
        for stem in _LIMIT_MACRO_STEMS + _EXACT_WIDTH_STEMS + _POINTER_SIZED_STEMS
        for bound in _LIMIT_BOUNDS
    }
)

_SPACE = " "


class ClauseKind(str, Enum):
    INVARIANT = "invariant"
    ASSIGNS = "assigns"
    DECREASES = "decreases"


class UnaryOperator(str, Enum):
    NEGATE = "-"
    NOT = "!"
    COMPLEMENT = "~"
    DEREFERENCE = "*"


class BinaryOperator(str, Enum):
    LOGICAL_OR = "||"
    LOGICAL_AND = "&&"
    BIT_OR = "|"
    BIT_XOR = "^"
    BIT_AND = "&"
    EQUAL = "=="
    NOT_EQUAL = "!="
    LESS = "<"
    LESS_EQUAL = "<="
    GREATER = ">"
    GREATER_EQUAL = ">="
    SHIFT_LEFT = "<<"
    SHIFT_RIGHT = ">>"
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    REMAINDER = "%"


BINARY_PRECEDENCE: dict[BinaryOperator, int] = {
    BinaryOperator.LOGICAL_OR: 1,
    BinaryOperator.LOGICAL_AND: 2,
    BinaryOperator.BIT_OR: 3,
    BinaryOperator.BIT_XOR: 4,
    BinaryOperator.BIT_AND: 5,
    BinaryOperator.EQUAL: 6,
    BinaryOperator.NOT_EQUAL: 6,
    BinaryOperator.LESS: 7,
    BinaryOperator.LESS_EQUAL: 7,
    BinaryOperator.GREATER: 7,
    BinaryOperator.GREATER_EQUAL: 7,
    BinaryOperator.SHIFT_LEFT: 8,
    BinaryOperator.SHIFT_RIGHT: 8,
    BinaryOperator.ADD: 9,
    BinaryOperator.SUBTRACT: 9,
    BinaryOperator.MULTIPLY: 10,
    BinaryOperator.DIVIDE: 10,
    BinaryOperator.REMAINDER: 10,
}
_BINARY_BY_TEXT = {operator.value: operator for operator in BinaryOperator}
_UNARY_BY_TEXT = {operator.value: operator for operator in UnaryOperator}
_ADDRESS_OF = "&"
_UNARY_PLUS = "+"


@dataclass(frozen=True)
class Name:
    identifier: str

    def render(self) -> str:
        return self.identifier

    def height(self) -> int:
        return 1


@dataclass(frozen=True)
class IntConstant:
    spelling: str

    def render(self) -> str:
        return self.spelling

    def height(self) -> int:
        return 1


@dataclass(frozen=True)
class Unary:
    operator: UnaryOperator
    operand: Expression

    def render(self) -> str:
        return f"({self.operator.value}{self.operand.render()})"

    def height(self) -> int:
        return 1 + self.operand.height()


@dataclass(frozen=True)
class Binary:
    operator: BinaryOperator
    left: Expression
    right: Expression

    def render(self) -> str:
        return f"({self.left.render()} {self.operator.value} {self.right.render()})"

    def height(self) -> int:
        return 1 + max(self.left.height(), self.right.height())


@dataclass(frozen=True)
class Cast:
    type_name: str
    operand: Expression

    def render(self) -> str:
        return f"(({self.type_name}){self.operand.render()})"

    def height(self) -> int:
        return 1 + self.operand.height()


@dataclass(frozen=True)
class Member:
    base: Expression
    field: str
    through_pointer: bool

    def render(self) -> str:
        access = "->" if self.through_pointer else "."
        return f"{self.base.render()}{access}{self.field}"

    def height(self) -> int:
        return 1 + self.base.height()


@dataclass(frozen=True)
class Index:
    base: Expression
    index: Expression

    def render(self) -> str:
        return f"{self.base.render()}[{self.index.render()}]"

    def height(self) -> int:
        return 1 + max(self.base.height(), self.index.height())


@dataclass(frozen=True)
class LoopEntry:
    operand: Expression

    def render(self) -> str:
        return f"{LOOP_ENTRY}({self.operand.render()})"

    def height(self) -> int:
        return 1 + self.operand.height()


@dataclass(frozen=True)
class ObjectWhole:
    pointer: Expression

    def render(self) -> str:
        return f"{OBJECT_WHOLE}({self.pointer.render()})"

    def height(self) -> int:
        return 1 + self.pointer.height()


@dataclass(frozen=True)
class ObjectUpto:
    pointer: Expression
    size: Expression

    def render(self) -> str:
        return f"{OBJECT_UPTO}({self.pointer.render()}, {self.size.render()})"

    def height(self) -> int:
        return 1 + max(self.pointer.height(), self.size.height())


@dataclass(frozen=True)
class _CastPrefix:
    type_name: str


Expression = Name | IntConstant | Unary | Binary | Cast | Member | Index | LoopEntry
AssignsTarget = Name | Member | Index | Unary | ObjectWhole | ObjectUpto
Clause = Expression | ObjectWhole | ObjectUpto


def _is_lvalue(node: Clause) -> bool:
    if isinstance(node, Unary):
        return node.operator is UnaryOperator.DEREFERENCE
    return isinstance(node, (Name, Member, Index))


def _identifier_rejection(identifier: str, macro_names: frozenset[str]) -> ClauseRejected | None:
    if identifier in INTEGER_TYPEDEF_NAMES:
        return ClauseRejected(Rejection.CAST, f"type name {identifier} outside a cast to an integer type")
    if identifier in C_KEYWORDS:
        return ClauseRejected(Rejection.KEYWORD, f"keyword {identifier} is not allowed")
    if RESERVED_IDENTIFIER_RE.match(identifier):
        return ClauseRejected(Rejection.RESERVED_IDENTIFIER, f"{identifier} is a reserved identifier")
    if identifier.startswith(FORBIDDEN_IDENTIFIER_PREFIXES):
        return ClauseRejected(Rejection.FORBIDDEN_PREFIX, f"{identifier} uses a prefix reserved for Lean C harnesses")
    if identifier in STANDARD_MACRO_NAMES or identifier in macro_names:
        return ClauseRejected(
            Rejection.MACRO, f"{identifier} is a macro; clauses are not macro-expanded, write its value"
        )
    return None


class _Parser:
    def __init__(self, tokens: tuple[Token, ...], kind: ClauseKind, macro_names: frozenset[str]) -> None:
        self._tokens = tokens
        self._position = 0
        self._kind = kind
        self._macro_names = macro_names
        self._inside_loop_entry = False

    def parse(self) -> Clause:
        clause = self._assigns_target() if self._kind is ClauseKind.ASSIGNS else self._expression()
        self._expect_end()
        if clause.height() > MAX_CLAUSE_DEPTH:
            raise ClauseRejected(Rejection.TOO_DEEP, f"expression nested deeper than {MAX_CLAUSE_DEPTH}")
        return clause

    def _peek(self, ahead: int = 0) -> Token:
        index = self._position + ahead
        return self._tokens[index] if index < len(self._tokens) else END_TOKEN

    def _advance(self) -> Token:
        token = self._peek()
        self._position += 1
        return token

    def _is_punctuator(self, text: str, ahead: int = 0) -> bool:
        token = self._peek(ahead)
        return token.kind is TokenKind.PUNCTUATOR and token.text == text

    def _unexpected(self, expected: str) -> ClauseRejected:
        token = self._peek()
        if self._is_punctuator(","):
            return ClauseRejected(Rejection.COMMA, "the comma operator is not allowed")
        found = shown_text(token.text) if token.kind is not TokenKind.END else "the end of the clause"
        return ClauseRejected(Rejection.SYNTAX, f"expected {expected}, found {found}")

    def _expect(self, text: str) -> None:
        if not self._is_punctuator(text):
            raise self._unexpected(repr(text))
        self._advance()

    def _expect_end(self) -> None:
        if self._peek().kind is not TokenKind.END:
            raise self._unexpected("the end of the clause")

    def _assigns_target(self) -> Clause:
        head = self._peek()
        if head.kind is TokenKind.IDENTIFIER and head.text in OBJECT_FORMS:
            return self._object_form(self._advance().text)
        target = self._expression()
        if not _is_lvalue(target):
            raise ClauseRejected(Rejection.NOT_LVALUE, "an assigns target must be an lvalue or an object form")
        return target

    def _object_form(self, form: str) -> ObjectWhole | ObjectUpto:
        self._expect("(")
        pointer = self._expression()
        if form == OBJECT_WHOLE:
            self._expect(")")
            return ObjectWhole(pointer)
        self._expect(",")
        size = self._expression()
        self._expect(")")
        return ObjectUpto(pointer, size)

    def _expression(self) -> Expression:
        return self._binary(min(BINARY_PRECEDENCE.values()))

    def _peek_binary(self) -> BinaryOperator | None:
        token = self._peek()
        return _BINARY_BY_TEXT.get(token.text) if token.kind is TokenKind.PUNCTUATOR else None

    def _binary(self, min_precedence: int) -> Expression:
        left = self._unary()
        operator = self._peek_binary()
        while operator is not None and BINARY_PRECEDENCE[operator] >= min_precedence:
            self._advance()
            right = self._binary(BINARY_PRECEDENCE[operator] + 1)
            left = Binary(operator, left, right)
            operator = self._peek_binary()
        return left

    def _starts_cast(self) -> bool:
        following = self._peek(1)
        is_type_word = following.kind is TokenKind.IDENTIFIER and following.text in _CAST_TYPE_WORDS
        return self._is_punctuator("(") and is_type_word

    def _cast_prefix(self) -> _CastPrefix:
        self._expect("(")
        words: list[str] = []
        while self._peek().kind is TokenKind.IDENTIFIER and self._peek().text in _CAST_TYPE_WORDS:
            words.append(self._advance().text)
        spelling = _SPACE.join(words)
        if not self._is_punctuator(")"):
            raise ClauseRejected(Rejection.CAST, f"cast to ({spelling} {self._peek().text} ...): integer types only")
        self._advance()
        if spelling not in INTEGER_TYPE_SPELLINGS and spelling not in INTEGER_TYPEDEF_NAMES:
            raise ClauseRejected(Rejection.CAST, f"cast to ({spelling}): not a plain integer type")
        return _CastPrefix(spelling)

    def _unary(self) -> Expression:
        prefixes: list[UnaryOperator | _CastPrefix] = []
        while True:
            token = self._peek()
            if token.kind is TokenKind.PUNCTUATOR and token.text in _UNARY_BY_TEXT:
                prefixes.append(_UNARY_BY_TEXT[self._advance().text])
            elif self._is_punctuator(_ADDRESS_OF):
                raise ClauseRejected(Rejection.ADDRESS_OF, "the address-of operator is not allowed")
            elif self._is_punctuator(_UNARY_PLUS):
                raise ClauseRejected(Rejection.SYNTAX, "unary + is not allowed")
            elif self._starts_cast():
                prefixes.append(self._cast_prefix())
            else:
                break
        node = self._postfix()
        for prefix in reversed(prefixes):
            node = Cast(prefix.type_name, node) if isinstance(prefix, _CastPrefix) else Unary(prefix, node)
        return node

    def _postfix(self) -> Expression:
        node = self._primary()
        while True:
            if self._is_punctuator("("):
                raise ClauseRejected(Rejection.CALL, "function calls are not allowed")
            if not (self._is_punctuator("[") or self._is_punctuator(".") or self._is_punctuator("->")):
                return node
            if isinstance(node, IntConstant):
                raise ClauseRejected(Rejection.CONSTANT, "a constant cannot be indexed or have members")
            access = self._advance().text
            if access == "[":
                index = self._expression()
                self._expect("]")
                node = Index(node, index)
            else:
                node = Member(node, self._field_name(), through_pointer=access == "->")

    def _field_name(self) -> str:
        token = self._advance()
        if token.kind is not TokenKind.IDENTIFIER:
            raise ClauseRejected(Rejection.SYNTAX, f"expected a member name, found {shown_text(token.text)}")
        self._reject_identifier(token.text)
        return token.text

    def _reject_identifier(self, identifier: str) -> None:
        rejection = _identifier_rejection(identifier, self._macro_names)
        if rejection is not None:
            raise rejection

    def _primary(self) -> Expression:
        token = self._advance()
        if token.kind is TokenKind.INTEGER:
            return IntConstant(token.text)
        if token.kind is TokenKind.IDENTIFIER:
            return self._named(token.text)
        if token.kind is TokenKind.PUNCTUATOR and token.text == "(":
            inner = self._expression()
            self._expect(")")
            return inner
        found = shown_text(token.text) if token.kind is not TokenKind.END else "the end of the clause"
        raise ClauseRejected(Rejection.SYNTAX, f"expected an operand, found {found}")

    def _named(self, identifier: str) -> Expression:
        if identifier == LOOP_ENTRY:
            return self._loop_entry()
        if identifier in OBJECT_FORMS:
            raise ClauseRejected(Rejection.MISPLACED_FORM, f"{identifier} is allowed only as a whole assigns entry")
        self._reject_identifier(identifier)
        return Name(identifier)

    def _loop_entry(self) -> LoopEntry:
        if self._kind is not ClauseKind.INVARIANT or self._inside_loop_entry:
            raise ClauseRejected(
                Rejection.MISPLACED_FORM, f"{LOOP_ENTRY} is allowed only in invariants and is not nested"
            )
        self._expect("(")
        self._inside_loop_entry = True
        operand = self._expression()
        self._inside_loop_entry = False
        self._expect(")")
        if not _is_lvalue(operand):
            raise ClauseRejected(Rejection.NOT_LVALUE, f"{LOOP_ENTRY} takes an lvalue")
        return LoopEntry(operand)


def parse_clause(text: str, kind: ClauseKind, *, macro_names: frozenset[str] = frozenset()) -> Clause:
    return _Parser(tokenize(text), kind, macro_names).parse()
