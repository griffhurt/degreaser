"""File sources: every byte of file I/O goes through a FileSource (docs/architecture.md §4)."""

from degreaser.core.sources.base import Capability, Entry, FileSource, Kind
from degreaser.core.sources.errors import (
    AlreadyExists,
    AuthenticationFailed,
    NotADirectory,
    NotFound,
    PermissionDenied,
    SourceError,
    TransientSourceError,
    Unsupported,
)

__all__ = [
    "AlreadyExists",
    "AuthenticationFailed",
    "Capability",
    "Entry",
    "FileSource",
    "Kind",
    "NotADirectory",
    "NotFound",
    "PermissionDenied",
    "SourceError",
    "TransientSourceError",
    "Unsupported",
]
