from degreaser.core.preview.highlight import Span, lexer_for, spans


def categories(text: str, name: str) -> dict[str, str]:
    lexer = lexer_for(name)
    assert lexer is not None
    return {text[s.start : s.start + s.length]: s.category for s in spans(text, lexer)}


def test_python_categories() -> None:
    found = categories('import os\n# note\nx = "hi" + str(42)\ndef f(): pass\n', "a.py")
    assert found["import"] == "keyword"
    assert found["# note"] == "comment"
    assert found['"hi"'] == "string"
    assert found["42"] == "number"
    assert found["f"] == "function"
    assert found["str"] == "type"


def test_lexer_by_name_only() -> None:
    assert lexer_for("notes.txt") is None  # plain text: nothing to colour
    assert lexer_for("mystery") is None
    assert lexer_for("Makefile") is not None
    assert lexer_for("page.HTML") is not None


def test_offsets_count_utf16_units() -> None:
    # Qt positions count an emoji as two units; the string after it must still line up.
    text = 'a = "😀"\nb = "x"\n'
    lexer = lexer_for("a.py")
    assert lexer is not None
    strings = [s for s in spans(text, lexer) if s.category == "string"]
    utf16 = text.encode("utf-16-le")
    shown = [utf16[s.start * 2 : (s.start + s.length) * 2].decode("utf-16-le") for s in strings]
    assert shown == ['"😀"', '"x"']


def test_only_the_first_part_is_highlighted() -> None:
    lexer = lexer_for("a.py")
    assert lexer is not None
    text = "x = 1\n" * 1000
    result = spans(text, lexer, max_chars=60)
    assert result and max(s.start + s.length for s in result) <= 60
    assert all(isinstance(s, Span) for s in result)
