"""File sources: every byte of file I/O goes through a FileSource (docs/architecture.md §4)."""

from degreaser.core.sources.base import Capability, Entry, FileSource, Kind
from degreaser.core.sources.errors import (
    AlreadyExists,
    NotFound,
    PermissionDenied,
    SourceError,
    TransientSourceError,
    Unsupported,
)

__all__ = [
    "AlreadyExists",
    "Capability",
    "Entry",
    "FileSource",
    "Kind",
    "NotFound",
    "PermissionDenied",
    "SourceError",
    "TransientSourceError",
    "Unsupported",
]
