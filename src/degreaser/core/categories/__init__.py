"""Ordered categories: validation, storage and hierarchical autocomplete (§7)."""

from degreaser.core.categories.complete import Completer
from degreaser.core.categories.index import CategoryIndex
from degreaser.core.categories.store import CategoryStore, InvalidCategory
from degreaser.core.categories.validate import (
    MAX_DEPTH,
    MAX_TAG_LENGTH,
    ParsedCategory,
    TagIssue,
    format_category,
    input_text,
    parse_category,
    validate_tag,
)

__all__ = [
    "MAX_DEPTH",
    "MAX_TAG_LENGTH",
    "CategoryIndex",
    "CategoryStore",
    "Completer",
    "InvalidCategory",
    "ParsedCategory",
    "TagIssue",
    "format_category",
    "input_text",
    "parse_category",
    "validate_tag",
]
