"""Syntax highlighting spans for text previews, from Pygments (docs/previews.md §1.2).

The lexer is chosen by file name only: guessing from contents is slow and often wrong. Only
the first MAX_CHARS characters are highlighted; anything after that stays plain.
"""

import bisect
import re
from dataclasses import dataclass

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.token import (
    Comment,
    Keyword,
    Literal,
    Name,
    Number,
    Operator,
    String,
    _TokenType,
)
from pygments.util import ClassNotFound

MAX_CHARS = 256 * 1024

# Token types → the theme's syntax colours (ui/theme/tokens.py), most specific first.
_CATEGORIES: list[tuple[_TokenType, str]] = [
    (Comment, "comment"),
    (String, "string"),
    (Number, "number"),
    (Keyword.Constant, "number"),
    (Name.Constant, "number"),
    (Literal, "string"),
    (Keyword.Type, "type"),
    (Keyword, "keyword"),
    (Operator.Word, "keyword"),
    (Name.Function, "function"),
    (Name.Class, "type"),
    (Name.Builtin, "type"),
    (Name.Decorator, "type"),
    (Name.Exception, "type"),
    (Name.Tag, "tag"),
    (Name.Attribute, "tag"),
    (Name.Label, "tag"),
    (Name.Entity, "tag"),
    (Name.Variable, "tag"),
]

_ASTRAL = re.compile("[\U00010000-\U0010ffff]")


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # in UTF-16 code units, as Qt's QTextDocument counts positions
    length: int
    category: str


def lexer_for(name: str) -> Lexer | None:
    # Pygments matches names case-sensitively; old Windows backups are full of FILE.HTM.
    for candidate in dict.fromkeys((name, name.lower())):
        try:
            lexer = get_lexer_for_filename(candidate, stripnl=False, ensurenl=False)
        except ClassNotFound, ValueError:
            continue
        return None if isinstance(lexer, TextLexer) else lexer
    return None


def _category(token: _TokenType) -> str | None:
    for parent, category in _CATEGORIES:
        if token in parent:
            return category
    return None


def spans(text: str, lexer: Lexer, max_chars: int = MAX_CHARS) -> list[Span]:
    """Coloured spans of `text` (plain text gets none). Blocking; call from a worker thread."""
    text = text[:max_chars]
    astral = [m.start() for m in _ASTRAL.finditer(text)]
    out: list[Span] = []
    for start, token, value in lexer.get_tokens_unprocessed(text):
        category = _category(token)
        if category is None or not value:
            continue
        # Characters outside the BMP take two UTF-16 units in Qt's positions.
        before = bisect.bisect_left(astral, start)
        inside = bisect.bisect_left(astral, start + len(value)) - before
        span = Span(start + before, len(value) + inside, category)
        if out and out[-1].category == category and out[-1].start + out[-1].length == span.start:
            out[-1] = Span(out[-1].start, out[-1].length + span.length, category)
        else:
            out.append(span)
    return out
