"""Typed source errors. Callers branch on transient vs permanent (retry/pause vs skip/report)."""


class SourceError(Exception):
    """Base class. `path` is the project-relative path involved, if any."""

    transient = False

    def __init__(self, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.path = path


class NotFound(SourceError):
    pass


class PermissionDenied(SourceError):
    pass


class AlreadyExists(SourceError):
    pass


class NotADirectory(SourceError):
    pass


class Unsupported(SourceError):
    pass


class AuthenticationFailed(SourceError):
    """The server rejected the credentials (permanent until the user fixes them)."""


class TransientSourceError(SourceError):
    """Timeouts and lost connections: worth retrying."""

    transient = True
