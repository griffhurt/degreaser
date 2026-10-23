"""Exact-duplicate detection (docs/duplicates.md, D34)."""

from degreaser.core.duplicates.finder import (
    DuplicateFinder,
    FindProgress,
    FindSummary,
    Pause,
    full_hash,
    partial_hash,
    purge_stale,
)
from degreaser.core.duplicates.index import DuplicateIndex

__all__ = [
    "DuplicateFinder",
    "DuplicateIndex",
    "FindProgress",
    "FindSummary",
    "Pause",
    "full_hash",
    "partial_hash",
    "purge_stale",
]
