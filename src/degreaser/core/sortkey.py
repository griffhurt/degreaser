"""Natural-sort key for names (docs/data-model.md §2.2)."""

import re
import unicodedata

_DIGITS = re.compile(r"\d+")


def _digit_run(match: re.Match[str]) -> str:
    digits = match.group().lstrip("0") or "0"
    return f"{len(digits):02d}{digits}"


def sort_key(name: str) -> str:
    """NFC + casefold, with each digit run replaced by its 2-digit length then the digits.

    "file2" < "file10" because "file012" < "file0210". Ties are broken by the raw name, which
    is the last column of the nodes_children index.
    """
    return _DIGITS.sub(_digit_run, unicodedata.normalize("NFC", name).casefold())
