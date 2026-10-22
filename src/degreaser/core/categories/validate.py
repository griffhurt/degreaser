"""Tag validation and category input parsing (docs/triage-engine.md §7.2, §7.3)."""

from dataclasses import dataclass, field

MAX_TAG_LENGTH = 100
MAX_DEPTH = 16

_FORBIDDEN = set('/\\:*?"<>|')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_tag(name: str) -> str | None:
    """Return why `name` can't be a tag (it must work as a folder name on every OS), or None."""
    if name == "":
        return "Tag is empty."
    if len(name) > MAX_TAG_LENGTH:
        return f"Tag is longer than {MAX_TAG_LENGTH} characters."
    bad = sorted({c for c in name if c in _FORBIDDEN})
    if bad:
        return "Tags can't contain " + " ".join(bad) + "."
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "Tags can't contain control characters."
    if name in (".", ".."):
        return "Tag can't be . or .."
    if name.endswith((".", " ")):
        return "Tags can't end with a dot or a space."
    if name.split(".")[0].rstrip(" ").upper() in _RESERVED:
        return f"{name.split('.')[0]} is a reserved name on Windows."
    return None


@dataclass(frozen=True, slots=True)
class TagIssue:
    index: int  # position of the tag in the category
    start: int  # character span in the input text, for underlining
    end: int
    message: str


@dataclass(frozen=True, slots=True)
class ParsedCategory:
    tags: list[str] = field(default_factory=list)
    issues: list[TagIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def parse_category(text: str) -> ParsedCategory:
    """Split comma-separated input into trimmed tags, reporting problems with their spans.

    Empty pieces are dropped. Repeating a tag (case-insensitively) is refused.
    """
    tags: list[str] = []
    issues: list[TagIssue] = []
    seen: set[str] = set()
    pos = 0
    for piece in text.split(","):
        stripped = piece.strip()
        if stripped:
            start = pos + piece.index(stripped)
            end = start + len(stripped)
            index = len(tags)
            if (message := validate_tag(stripped)) is not None:
                issues.append(TagIssue(index, start, end, message))
            elif stripped.casefold() in seen:
                issues.append(
                    TagIssue(index, start, end, f"{stripped} is already in this category.")
                )
            elif index >= MAX_DEPTH:
                issues.append(
                    TagIssue(
                        index, start, end, f"Categories can't be deeper than {MAX_DEPTH} tags."
                    )
                )
            seen.add(stripped.casefold())
            tags.append(stripped)
        pos += len(piece) + 1
    return ParsedCategory(tags, issues)


DISPLAY_SEPARATOR = " \u203a "  # single right-pointing angle quote


def format_category(tags: tuple[str, ...] | list[str]) -> str:
    """Display form, e.g. "Videos \u203a High School \u203a Senior Film Project"."""
    return DISPLAY_SEPARATOR.join(tags)


def input_text(tags: tuple[str, ...] | list[str]) -> str:
    """Editable form used to pre-fill the input: 'Videos, High School'."""
    return ", ".join(tags)
