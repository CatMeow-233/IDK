from __future__ import annotations

import re
import string
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "END_TOKEN",
    "MAX_CLAUSE_CHARS",
    "MAX_CLAUSE_DEPTH",
    "MAX_CLAUSE_TOKENS",
    "QUANTIFIERS",
    "ClauseRejected",
    "Rejection",
    "Token",
    "TokenKind",
    "shown_text",
    "tokenize",
]

MAX_CLAUSE_CHARS = 500
MAX_CLAUSE_TOKENS = 256
MAX_CLAUSE_DEPTH = 32
MAX_UNSIGNED_CONSTANT = 2**64 - 1
MAX_SIGNED_CONSTANT = 2**63 - 1
HEX_BASE = 16
OCTAL_BASE = 8
DECIMAL_BASE = 10
QUANTIFIERS = frozenset({"__CPROVER_forall", "__CPROVER_exists"})

_ALLOWED_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_ ()[].-><=!~+*/%&|^,")
_PRINTABLE_ASCII_FIRST, _PRINTABLE_ASCII_LAST = " ", "~"
_BRACES = frozenset("{}")
_CONDITIONAL_CHARACTERS = frozenset("?:")
_COMMENT_OPENERS = ("//", "/*")
_DIGRAPHS = ("<:", ":>", "<%", "%>", "%:")
_INCREMENT_OPERATORS = ("++", "--")
_ASSIGNMENT_OPERATORS = ("<<=", ">>=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=")
_TWO_CHARACTER_PUNCTUATORS = frozenset({"->", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||"})
_ONE_CHARACTER_PUNCTUATORS = frozenset("()[].!~-+*/%<>&|^,")
_ASSIGNMENT = "="
_MEMBER_ACCESS = "."
_SPACE = " "
_OPENING_BRACKET_OF = {")": "(", "]": "["}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"[0-9][0-9A-Za-z_.]*")
_INTEGER_CONSTANT_RE = re.compile(
    r"(?P<digits>0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)(?P<suffix>[uU](?:ll|LL|l|L)?|(?:ll|LL|l|L)[uU]?)?"
)
_HEX_PREFIXES = ("0x", "0X")
_OCTAL_PREFIX = "0"
_UNSIGNED_SUFFIX_LETTERS = frozenset("uU")
_SHOWN_TOKEN_CHARS = 20


class Rejection(str, Enum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    TOO_MANY_TOKENS = "too_many_tokens"
    TOO_DEEP = "too_deep"
    CHARACTER = "character"
    QUANTIFIER = "quantifier"
    BRACE = "brace"
    COMMENT = "comment"
    DIGRAPH = "digraph"
    INCREMENT = "increment"
    CONDITIONAL = "conditional"
    ASSIGNMENT = "assignment"
    CONSTANT = "constant"
    UNBALANCED = "unbalanced"
    COMMA = "comma"
    ADDRESS_OF = "address_of"
    CALL = "call"
    KEYWORD = "keyword"
    RESERVED_IDENTIFIER = "reserved_identifier"
    FORBIDDEN_PREFIX = "forbidden_prefix"
    MACRO = "macro"
    CAST = "cast"
    MISPLACED_FORM = "misplaced_form"
    NOT_LVALUE = "not_lvalue"
    SYNTAX = "syntax"


class ClauseRejected(ValueError):
    def __init__(self, rejection: Rejection, detail: str) -> None:
        super().__init__(f"{rejection.value}: {detail}")
        self.rejection = rejection
        self.detail = detail


class TokenKind(str, Enum):
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    PUNCTUATOR = "punctuator"
    END = "end"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str


END_TOKEN = Token(TokenKind.END, "")


def shown_text(text: str) -> str:
    return repr(text[:_SHOWN_TOKEN_CHARS])


def _first_word_in(text: str, words: frozenset[str]) -> str | None:
    return next((word for word in _WORD_RE.findall(text) if word in words), None)


def _first_sequence_in(text: str, sequences: tuple[str, ...]) -> str | None:
    return next((sequence for sequence in sequences if sequence in text), None)


def _first_character_in(text: str, characters: frozenset[str]) -> str | None:
    return next((character for character in text if character in characters), None)


def _is_printable_ascii(character: str) -> bool:
    return _PRINTABLE_ASCII_FIRST <= character <= _PRINTABLE_ASCII_LAST


def _reject_size_and_encoding(text: str) -> None:
    if len(text) > MAX_CLAUSE_CHARS:
        raise ClauseRejected(Rejection.TOO_LONG, f"{len(text)} characters; at most {MAX_CLAUSE_CHARS}")
    if not text.strip(_SPACE):
        raise ClauseRejected(Rejection.EMPTY, "the clause is empty")
    unprintable = next((c for c in text if not _is_printable_ascii(c)), None)
    if unprintable is not None:
        raise ClauseRejected(Rejection.CHARACTER, f"U+{ord(unprintable):04X}: only printable ASCII and spaces")


def _reject_forbidden_sequences(text: str) -> None:
    quantifier = _first_word_in(text, QUANTIFIERS)
    if quantifier is not None:
        raise ClauseRejected(Rejection.QUANTIFIER, f"{quantifier} is not allowed")
    brace = _first_character_in(text, _BRACES)
    if brace is not None:
        raise ClauseRejected(
            Rejection.BRACE, f"{brace!r}: statement expressions, compound literals and blocks are not allowed"
        )
    for rejection, sequences in (
        (Rejection.COMMENT, _COMMENT_OPENERS),
        (Rejection.DIGRAPH, _DIGRAPHS),
        (Rejection.INCREMENT, _INCREMENT_OPERATORS),
    ):
        found = _first_sequence_in(text, sequences)
        if found is not None:
            raise ClauseRejected(rejection, f"{found!r} is not allowed")
    conditional = _first_character_in(text, _CONDITIONAL_CHARACTERS)
    if conditional is not None:
        raise ClauseRejected(Rejection.CONDITIONAL, f"{conditional!r}: the conditional operator is not allowed")
    other = next((c for c in text if c not in _ALLOWED_CHARACTERS), None)
    if other is not None:
        raise ClauseRejected(Rejection.CHARACTER, f"{other!r} is not allowed")


def _integer_value(digits: str) -> int:
    if digits.startswith(_HEX_PREFIXES):
        return int(digits, HEX_BASE)
    if digits.startswith(_OCTAL_PREFIX):
        return int(digits, OCTAL_BASE)
    return int(digits, DECIMAL_BASE)


def _integer_token(spelling: str) -> Token:
    match = _INTEGER_CONSTANT_RE.fullmatch(spelling)
    if match is None:
        raise ClauseRejected(
            Rejection.CONSTANT, f"{shown_text(spelling)}: only decimal, hex or octal integer constants"
        )
    digits, suffix = match.group("digits"), match.group("suffix") or ""
    signed_decimal = not digits.startswith(_OCTAL_PREFIX) and not (_UNSIGNED_SUFFIX_LETTERS & set(suffix))
    limit = MAX_SIGNED_CONSTANT if signed_decimal else MAX_UNSIGNED_CONSTANT
    if _integer_value(digits) > limit:
        raise ClauseRejected(Rejection.CONSTANT, f"{shown_text(spelling)} is out of range")
    return Token(TokenKind.INTEGER, spelling)


def _punctuator_at(text: str, position: int) -> str:
    assignment = next((op for op in _ASSIGNMENT_OPERATORS if text.startswith(op, position)), None)
    two = text[position : position + 2]
    if assignment is None and two in _TWO_CHARACTER_PUNCTUATORS:
        return two
    if assignment is not None or text[position] == _ASSIGNMENT:
        raise ClauseRejected(Rejection.ASSIGNMENT, f"{assignment or _ASSIGNMENT!r}: assignment is not allowed")
    if text[position] not in _ONE_CHARACTER_PUNCTUATORS:
        raise ClauseRejected(Rejection.CHARACTER, f"{text[position]!r} is not allowed")
    return text[position]


def _starts_floating_constant(text: str, position: int) -> bool:
    return text[position] == _MEMBER_ACCESS and text[position + 1 : position + 2].isdigit()


def _next_token(text: str, position: int) -> tuple[Token, int]:
    word = _WORD_RE.match(text, position)
    if word is not None:
        return Token(TokenKind.IDENTIFIER, word.group()), word.end()
    number = _NUMBER_RE.match(text, position)
    if number is not None:
        return _integer_token(number.group()), number.end()
    if _starts_floating_constant(text, position):
        raise ClauseRejected(Rejection.CONSTANT, "floating constants are not allowed")
    punctuator = _punctuator_at(text, position)
    return Token(TokenKind.PUNCTUATOR, punctuator), position + len(punctuator)


def _reject_unbalanced_or_deep_brackets(tokens: list[Token]) -> None:
    open_brackets: list[str] = []
    for token in tokens:
        if token.kind is not TokenKind.PUNCTUATOR:
            continue
        if token.text in _OPENING_BRACKET_OF.values():
            open_brackets.append(token.text)
            if len(open_brackets) > MAX_CLAUSE_DEPTH:
                raise ClauseRejected(Rejection.TOO_DEEP, f"brackets nested deeper than {MAX_CLAUSE_DEPTH}")
        elif token.text in _OPENING_BRACKET_OF:
            if not open_brackets or open_brackets.pop() != _OPENING_BRACKET_OF[token.text]:
                raise ClauseRejected(Rejection.UNBALANCED, f"unmatched {token.text!r}")
    if open_brackets:
        raise ClauseRejected(Rejection.UNBALANCED, f"unclosed {open_brackets[-1]!r}")


def tokenize(text: str) -> tuple[Token, ...]:
    _reject_size_and_encoding(text)
    _reject_forbidden_sequences(text)
    tokens: list[Token] = []
    position = 0
    while position < len(text):
        if text[position] == _SPACE:
            position += 1
            continue
        token, position = _next_token(text, position)
        tokens.append(token)
    if len(tokens) > MAX_CLAUSE_TOKENS:
        raise ClauseRejected(Rejection.TOO_MANY_TOKENS, f"{len(tokens)} tokens; at most {MAX_CLAUSE_TOKENS}")
    _reject_unbalanced_or_deep_brackets(tokens)
    return tuple(tokens)
