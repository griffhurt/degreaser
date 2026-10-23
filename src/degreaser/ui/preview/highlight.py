"""Applies precomputed syntax spans (core/preview/highlight.py) to a text document."""

import bisect

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from degreaser.core.preview.highlight import Span
from degreaser.ui.theme import theme
from degreaser.ui.theme.tokens import Tokens


def formats(t: Tokens) -> dict[str, QTextCharFormat]:
    colours = {
        "keyword": t.syntax_keyword,
        "string": t.syntax_string,
        "comment": t.syntax_comment,
        "number": t.syntax_number,
        "function": t.syntax_function,
        "type": t.syntax_type,
        "tag": t.syntax_tag,
    }
    out = {}
    for category, colour in colours.items():
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        if category == "comment":
            fmt.setFontItalic(True)
        if category == "keyword":
            fmt.setFontWeight(QFont.Weight.Medium)
        out[category] = fmt
    return out


class SpanHighlighter(QSyntaxHighlighter):
    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._spans: list[Span] = []
        self._starts: list[int] = []
        self._formats = formats(theme().tokens)
        theme().changed.connect(self._on_theme)

    def set_spans(self, spans: list[Span]) -> None:
        self._spans = spans
        self._starts = [s.start for s in spans]
        self.rehighlight()

    def _on_theme(self, tokens: Tokens) -> None:
        self._formats = formats(tokens)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if not self._spans:
            return
        block = self.currentBlock()
        begin = block.position()
        end = begin + block.length()
        # The last span starting before this block may run into it (multi-line strings).
        i = max(0, bisect.bisect_right(self._starts, begin) - 1)
        while i < len(self._spans):
            span = self._spans[i]
            if span.start >= end:
                break
            lo, hi = max(span.start, begin), min(span.start + span.length, end)
            if hi > lo:
                self.setFormat(lo - begin, hi - lo, self._formats[span.category])
            i += 1
