"""SmbSource: a folder on an SMB share, via smbprotocol with no OS mount (D2, D28).

One connection and session per source, shared by every worker thread: smbprotocol multiplexes
concurrent requests by message id, and the spike found per-thread connections gained nothing.

Each operation is a single *related compound* request where possible, so it costs one round
trip: CREATE + (QUERY_DIRECTORY | READ… | WRITE… | SET_INFO…) + CLOSE. Directory listings are
parsed with `struct` because smbprotocol's generic parser was half the CPU time
(spikes/smb_listing).

Every wait on the server has a timeout. A lost connection becomes TransientSourceError and
the next call reconnects; idempotent reads retry once on a fresh connection first.
"""

import contextlib
import io
import statistics
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from smbprotocol.connection import Connection
from smbprotocol.exceptions import (
    AccessDenied,
    BadNetworkName,
    CannotDelete,
    DeletePending,
    DirectoryNotEmpty,
    EndOfFile,
    FileIsADirectory,
    InsufficientResources,
    InsuffServerResources,
    IOTimeout,
    LogonFailure,
    NetworkNameDelegated,
    NoMoreFiles,
    NoSuchFile,
    ObjectNameCollision,
    ObjectNameInvalid,
    ObjectNameNotFound,
    ObjectPathInvalid,
    ObjectPathNotFound,
    PasswordExpired,
    PrivilegeNotHeld,
    ServerUnavailable,
    SharingViolation,
    SMBAuthenticationError,
    SMBConnectionClosed,
    SMBException,
    SMBResponseException,
    StatusNotADirectory,
    UserSessionDeleted,
    WrongPassword,
)
from smbprotocol.exceptions import NotFound as SmbNotFound
from smbprotocol.file_info import (
    FileAttributes,
    FileBasicInformation,
    FileDispositionInformation,
    FileInformationClass,
    FileRenameInformation,
)
from smbprotocol.open import (
    CloseFlags,
    CreateDisposition,
    CreateOptions,
    FilePipePrinterAccessMask,
    ImpersonationLevel,
    Open,
    QueryDirectoryFlags,
    ShareAccess,
    SMB2CloseRequest,
    SMB2SetInfoRequest,
)
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect

from degreaser.core.sources.base import Capability, Entry, Kind, basename, check_rel, parent
from degreaser.core.sources.errors import (
    AlreadyExists,
    AuthenticationFailed,
    NotADirectory,
    NotFound,
    PermissionDenied,
    SourceError,
    TransientSourceError,
)

CONNECT_TIMEOUT = 10  # TCP connect + negotiate
REQUEST_TIMEOUT = 30.0  # any single request; generous because NAS disks may spin up
CREDITS = 2048  # requested up front so many compounds can be in flight (D28)
LIST_BUFFER = 1024 * 1024  # QUERY_DIRECTORY output; about 8,000 entries
IO_CHUNK = 1024 * 1024  # bytes per READ/WRITE in a compound
MAX_CHUNKS = 8  # READs/WRITEs per compound (8 MiB)
SLOW_RTT = 0.010  # above this, use 32 workers instead of 16 (D28)
RECONNECT_COOLDOWN = 2.0  # a failed connect is reported to other callers for this long

_SHARE_RWD = (
    ShareAccess.FILE_SHARE_READ | ShareAccess.FILE_SHARE_WRITE | ShareAccess.FILE_SHARE_DELETE
)
_ACCESS = FilePipePrinterAccessMask
_READ = _ACCESS.FILE_READ_DATA | _ACCESS.FILE_READ_ATTRIBUTES
_DIR = FileAttributes.FILE_ATTRIBUTE_DIRECTORY
_REPARSE = FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT
_SMB2_HEADER = 64
_EPOCH_DIFF = 116444736000000000  # 100 ns ticks between 1601-01-01 and 1970-01-01

# FILE_ID_FULL_DIR_INFORMATION fixed part (MS-FSCC 2.4.18): next, index, ctime, atime, mtime,
# change, eof, alloc, attrs, name_len, ea_size, (reserved), file_id.
_DIR_ENTRY = struct.Struct("<IIqqqqqqIII4xQ")
_QD_RESPONSE = struct.Struct("<HHI")  # structure size, buffer offset, buffer length
# CLOSE response with POSTQUERY_ATTRIB: size, flags, reserved, ctime, atime, mtime, change,
# alloc, eof, attrs.
_CLOSE_RESPONSE = struct.Struct("<HHIqqqqqqI")
_READ_RESPONSE = struct.Struct("<HBxII")  # structure size, data offset, data length, remaining


def _filetime(ticks: int) -> float | None:
    return (ticks - _EPOCH_DIFF) / 1e7 if ticks > 0 else None


def _kind(attrs: int) -> Kind:
    if attrs & _REPARSE:
        return "symlink"
    return "dir" if attrs & _DIR else "file"


def _entry(name: str, attrs: int, eof: int, mtime: int, ctime: int) -> Entry:
    kind = _kind(attrs)
    return Entry(name, kind, None if kind == "dir" else eof, _filetime(mtime), _filetime(ctime))


def parse_directory(buf: bytes) -> list[Entry]:
    """Entries of a FILE_ID_FULL_DIR_INFORMATION buffer, without '.' and '..'."""
    out = []
    off, n = 0, len(buf)
    while off < n:
        fields = _DIR_ENTRY.unpack_from(buf, off)
        nxt, ctime, mtime, eof, attrs, name_len = (fields[i] for i in (0, 2, 4, 6, 8, 9))
        start = off + _DIR_ENTRY.size
        name = buf[start : start + name_len].decode("utf-16-le", "surrogatepass")
        if name not in (".", ".."):
            out.append(_entry(name, attrs, eof, mtime, ctime))
        if not nxt:
            break
        off += nxt
    return out


# Errors -----------------------------------------------------------------------------------------

_NOT_FOUND = (NoSuchFile, ObjectNameNotFound, ObjectPathNotFound, SmbNotFound, DeletePending)
_DENIED = (AccessDenied, PrivilegeNotHeld, CannotDelete, SharingViolation)
_EXISTS = (ObjectNameCollision, DirectoryNotEmpty)
_AUTH = (LogonFailure, WrongPassword, PasswordExpired, SMBAuthenticationError)
_TRANSIENT = (
    SMBConnectionClosed,
    UserSessionDeleted,
    NetworkNameDelegated,
    ServerUnavailable,
    InsufficientResources,
    InsuffServerResources,
    IOTimeout,
)


class _ConnectionLost(Exception):
    """Internal: the shared connection is unusable; reconnect before the next request."""


def _translate(exc: BaseException, rel: str) -> SourceError:
    message = str(exc) or type(exc).__name__
    match exc:
        case SourceError():
            return exc
        case _ if isinstance(exc, _NOT_FOUND):
            return NotFound(f"not found: {rel or '(project folder)'}", rel)
        case _ if isinstance(exc, _DENIED):
            what = "in use by another program" if isinstance(exc, SharingViolation) else "denied"
            return PermissionDenied(f"access {what}: {rel or '(project folder)'}", rel)
        case _ if isinstance(exc, _EXISTS):
            return AlreadyExists(f"already exists or not empty: {rel}", rel)
        case StatusNotADirectory():
            return NotADirectory(f"not a folder: {rel}", rel)
        case FileIsADirectory():
            return SourceError(f"is a folder: {rel}", rel)
        case ObjectNameInvalid() | ObjectPathInvalid():
            return SourceError(f"name not allowed by the server: {rel}", rel)
        case _ if isinstance(exc, _AUTH):
            return AuthenticationFailed("the server rejected the username or password", rel)
        case BadNetworkName():
            return NotFound("share not found on the server", rel)
        case _ if isinstance(exc, (*_TRANSIENT, _ConnectionLost, OSError)):
            return TransientSourceError(f"connection lost: {message}", rel)
        case SMBException() if "timeout" in message.lower():
            return TransientSourceError(f"the server didn't respond: {message}", rel)
        case _:
            return SourceError(message, rel)


def _lost(exc: BaseException) -> bool:
    """True if this error means the connection itself is gone (not a per-file failure)."""
    if isinstance(exc, (*_TRANSIENT, _ConnectionLost, OSError)):
        return True
    return isinstance(exc, SMBException) and "timeout" in str(exc).lower()


# Configuration -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SmbConfig:
    host: str
    share: str
    path: str = ""  # folder inside the share, POSIX-style, no leading/trailing slash
    username: str = ""
    domain: str = ""
    port: int = 445
    # Encrypt even if the server doesn't ask for it (needs SMB 3). Off: the server decides,
    # and unencrypted sessions are signed instead (D39).
    encrypt: bool = False

    @classmethod
    def from_source(cls, source: dict[str, Any]) -> SmbConfig:
        return cls(
            host=source["host"],
            share=source["share"],
            path=normalise_share_path(source.get("path", "")),
            username=source.get("username", ""),
            domain=source.get("domain", ""),
            port=int(source.get("port", 445)),
            encrypt=bool(source.get("encrypt", False)),
        )

    def to_source(self) -> dict[str, Any]:
        source: dict[str, Any] = {"type": "smb", "host": self.host, "share": self.share}
        source |= {"path": self.path, "username": self.username, "domain": self.domain}
        if self.port != 445:
            source["port"] = self.port
        if self.encrypt:
            source["encrypt"] = True
        return source

    @property
    def login(self) -> str:
        return f"{self.domain}\\{self.username}" if self.domain else self.username

    def describe(self) -> str:
        port = f":{self.port}" if self.port != 445 else ""
        return f"smb://{self.host}{port}/{self.share}" + (f"/{self.path}" if self.path else "")


def normalise_share_path(path: str) -> str:
    """'\\Backups\\Old/' or '/Backups/Old' → 'Backups/Old'."""
    return "/".join(p for p in path.replace("\\", "/").split("/") if p and p != ".")


def parse_smb_location(text: str) -> tuple[str, int | None, str, str] | None:
    """'smb://nas:1445/share/a/b' or '\\\\nas\\share\\a' → (host, port, share, path); None if it's
    a bare host name."""
    text = text.strip()
    if text.startswith("\\\\"):
        text = "smb://" + text[2:].replace("\\", "/")
    if not text.lower().startswith("smb://"):
        return None
    parts = urlsplit(text)
    segments = [unquote(p) for p in parts.path.split("/") if p]
    share = segments[0] if segments else ""
    return parts.hostname or "", parts.port, share, "/".join(segments[1:])


# The source --------------------------------------------------------------------------------------


@dataclass
class _Link:
    connection: Connection
    session: Session
    tree: TreeConnect


class SmbSource:
    capabilities = frozenset({Capability.RANGE_READ, Capability.SERVER_RENAME, Capability.WRITE})

    def __init__(self, config: SmbConfig, password: str) -> None:
        self.config = config
        self._password = password
        self._prefix = [p for p in config.path.split("/") if p]
        self._lock = threading.Lock()
        self._link: _Link | None = None
        self._failed_at = 0.0
        self._failure: SourceError | None = None
        self.rtt: float | None = None  # median echo round trip at connect, seconds

    # Lifecycle -------------------------------------------------------------------------------

    @property
    def parallelism(self) -> int:
        """Requests worth keeping in flight: latency-bound above ~10 ms RTT (D28)."""
        return 32 if self.rtt is not None and self.rtt > SLOW_RTT else 16

    def connect(self) -> None:
        """Connect and check the project folder exists. Safe to call again."""
        self._ensure()
        entry = self.stat("")
        if entry.kind != "dir":
            raise NotADirectory("the project path is not a folder", "")

    def close(self) -> None:
        with self._lock:
            link, self._link = self._link, None
        if link is not None:
            _disconnect(link.connection)

    def describe(self) -> str:
        return self.config.describe()

    def connection_info(self) -> dict[str, Any]:
        """Negotiated protocol details (for diagnostics)."""
        link = self._ensure()
        conn, session = link.connection, link.session
        dialect = conn.dialect
        return {
            "dialect": f"SMB {dialect >> 8:x}.{dialect & 0xFF:x}" if dialect else "unknown",
            "signing": bool(session.signing_required),
            "encryption": bool(session.encrypt_data),
            "max_read_size": conn.max_read_size,
            "max_write_size": conn.max_write_size,
        }

    def _ensure(self) -> _Link:
        with self._lock:
            if self._link is not None and _alive(self._link.connection):
                return self._link
            recent = time.monotonic() - self._failed_at < RECONNECT_COOLDOWN
            if self._failure is not None and recent:
                raise self._failure
            if self._link is not None:
                _disconnect(self._link.connection)
                self._link = None
            try:
                self._link = self._open_link()
            except Exception as exc:
                self._failure = _connect_error(exc, self.config)
                self._failed_at = time.monotonic()
                raise self._failure from exc
            self._failure = None
            return self._link

    def _open_link(self) -> _Link:
        cfg = self.config
        connection = Connection(uuid.uuid4(), cfg.host, cfg.port, require_signing=True)
        try:
            connection.connect(timeout=CONNECT_TIMEOUT)
        except Exception as exc:
            # smbprotocol reports refused/unreachable/DNS failures as ValueError.
            _disconnect(connection)
            raise _ConnectionLost(str(exc)) from exc
        try:
            # smbprotocol defaults to requiring encryption, which also refuses SMB 2.x servers.
            session = Session(connection, cfg.login, self._password, require_encryption=cfg.encrypt)
            session.connect()
            tree = TreeConnect(session, rf"\\{cfg.host}\{cfg.share}")
            tree.connect()
            # Credits let many compounds be in flight at once; echoes also measure latency.
            samples = []
            for i in range(3):
                start = time.perf_counter()
                connection.echo(
                    sid=session.session_id or 0,
                    timeout=int(REQUEST_TIMEOUT),
                    credit_request=CREDITS if i == 0 else 1,
                )
                samples.append(time.perf_counter() - start)
            self.rtt = statistics.median(samples)
        except BaseException:
            _disconnect(connection)
            raise
        return _Link(connection, session, tree)

    def _invalidate(self, link: _Link) -> None:
        with self._lock:
            if self._link is link:
                self._link = None
        _disconnect(link.connection)

    def _run[T](self, rel: str, fn: Callable[[_Link], T], *, idempotent: bool = True) -> T:
        """Run fn on the shared connection, translating errors. Idempotent calls get one more
        try on a fresh connection if the existing one turns out to be dead (e.g. after sleep)."""
        for attempt in (0, 1):
            link = self._ensure()
            try:
                return fn(link)
            except SourceError:
                raise
            except Exception as exc:
                if not _lost(exc):
                    raise _translate(exc, rel) from exc
                self._invalidate(link)
                if attempt == 1 or not idempotent:
                    raise _translate(exc, rel) from exc
        raise AssertionError("unreachable")

    # Paths and compounds -----------------------------------------------------------------------

    def _smb_path(self, rel: str) -> str:
        """Share-relative path with backslashes; the project root may be a share subfolder."""
        check_rel(rel)
        return "\\".join(self._prefix + (rel.split("/") if rel else []))

    @staticmethod
    def _compound(
        link: _Link, messages: Sequence[Any], tolerate: tuple[type[Exception], ...] = ()
    ) -> list[Any]:
        """Send related messages as one compound; return each raw response header, or the
        exception for a tolerated failure (e.g. NO_MORE_FILES after the CREATE).

        Every response is received (the server answers all of them) before the first error is
        raised, so no request is left outstanding.
        """
        conn = link.connection
        requests = conn.send_compound(
            list(messages), link.session.session_id, link.tree.tree_connect_id, related=True
        )
        responses: list[Any] = []
        error: BaseException | None = None
        for request in requests:
            try:
                responses.append(conn.receive(request, timeout=REQUEST_TIMEOUT))
            except SMBResponseException as exc:
                responses.append(exc)
                if error is None and not (len(responses) > 1 and isinstance(exc, tolerate)):
                    error = exc
            except Exception as exc:  # connection-level: nothing more will arrive
                raise _ConnectionLost(str(exc)) from exc
        if error is not None:
            raise error
        return responses

    @staticmethod
    def _create(
        handle: Open,
        access: int,
        options: int,
        disposition: int = CreateDisposition.FILE_OPEN,
        attributes: int = 0,
    ) -> Any:
        message, _unpack = _deferred(
            handle.create(
                ImpersonationLevel.Impersonation,
                access,
                attributes,
                _SHARE_RWD,
                disposition,
                options,
                send=False,
            )
        )
        return message

    @staticmethod
    def _close(get_attributes: bool = False) -> SMB2CloseRequest:
        close = SMB2CloseRequest()
        close["file_id"] = Open._RELATED_COMPOUND_FILE_ID
        if get_attributes:
            close["flags"] = CloseFlags.SMB2_CLOSE_FLAG_POSTQUERY_ATTRIB
        return close

    @staticmethod
    def _set_info(info: Any) -> SMB2SetInfoRequest:
        request = SMB2SetInfoRequest()
        request["info_type"] = info.INFO_TYPE
        request["file_info_class"] = info.INFO_CLASS
        request["file_id"] = Open._RELATED_COMPOUND_FILE_ID
        request["buffer"] = info
        return request

    # Reading -----------------------------------------------------------------------------------

    def list_dir(self, rel: str) -> list[Entry]:
        return self._run(rel, lambda link: self._list(link, rel))

    def _list(self, link: _Link, rel: str) -> list[Entry]:
        handle = Open(link.tree, self._smb_path(rel))
        buf = min(link.connection.max_transact_size or 65536, LIST_BUFFER)
        info = FileInformationClass.FILE_ID_FULL_DIRECTORY_INFORMATION
        create = self._create(
            handle,
            _READ,
            CreateOptions.FILE_DIRECTORY_FILE,
        )
        first, _ = handle.query_directory(
            "*", info, flags=QueryDirectoryFlags.SMB2_RESTART_SCANS, max_output=buf, send=False
        )
        second, _ = handle.query_directory("*", info, max_output=buf, send=False)
        # Samba answers an empty folder's query with NO_SUCH_FILE rather than NO_MORE_FILES.
        responses = self._compound(
            link, [create, first, second, self._close()], tolerate=(NoMoreFiles, NoSuchFile)
        )
        entries = parse_directory(_qd_buffer(responses[1]))
        if _qd_buffer(responses[2]):
            # More than one buffer's worth: the compound already closed the handle, so page.
            return self._list_paged(link, rel, buf)
        return entries

    def _list_paged(self, link: _Link, rel: str, buf: int) -> list[Entry]:
        handle = Open(link.tree, self._smb_path(rel))
        conn = link.connection
        create, unpack = _deferred(
            handle.create(
                ImpersonationLevel.Impersonation,
                _READ,
                0,
                _SHARE_RWD,
                CreateDisposition.FILE_OPEN,
                CreateOptions.FILE_DIRECTORY_FILE,
                send=False,
            )
        )
        request = conn.send(create, link.session.session_id, link.tree.tree_connect_id)
        conn.receive(request, timeout=REQUEST_TIMEOUT)
        unpack(request)
        out: list[Entry] = []
        flags = QueryDirectoryFlags.SMB2_RESTART_SCANS
        try:
            while True:
                query, _ = handle.query_directory(
                    "*",
                    FileInformationClass.FILE_ID_FULL_DIRECTORY_INFORMATION,
                    flags=flags,
                    max_output=buf,
                    send=False,
                )
                request = conn.send(query, link.session.session_id, link.tree.tree_connect_id)
                try:
                    response = conn.receive(request, timeout=REQUEST_TIMEOUT)
                except NoMoreFiles, NoSuchFile:
                    break
                flags = 0
                out.extend(parse_directory(_qd_buffer(response)))
        finally:
            with contextlib.suppress(Exception):
                close, close_unpack = _deferred(handle.close(send=False))
                request = conn.send(close, link.session.session_id, link.tree.tree_connect_id)
                conn.receive(request, timeout=REQUEST_TIMEOUT)
                close_unpack(request)
        return out

    def stat(self, rel: str) -> Entry:
        def run(link: _Link) -> Entry:
            handle = Open(link.tree, self._smb_path(rel))
            create = self._create(
                handle,
                FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                CreateOptions.FILE_OPEN_REPARSE_POINT,
            )
            responses = self._compound(link, [create, self._close(get_attributes=True)])
            data = responses[1]["data"].get_value()
            _s, _f, _r, ctime, _a, mtime, _c, _al, eof, attrs = _CLOSE_RESPONSE.unpack_from(data)
            name = basename(rel) if rel else self.config.share
            return _entry(name, attrs, eof, mtime, ctime)

        return self._run(rel, run)

    def read_range(self, rel: str, offset: int, length: int) -> bytes:
        """Up to `length` bytes from `offset` (fewer at the end of the file)."""
        if length <= 0:
            return b""
        parts = []
        done = 0
        while done < length:
            want = min(length - done, IO_CHUNK * MAX_CHUNKS)
            data = self._run(rel, lambda link, o=offset + done, w=want: self._read(link, rel, o, w))
            parts.append(data)
            done += len(data)
            if len(data) < want:
                break
        return b"".join(parts)

    def _read(self, link: _Link, rel: str, offset: int, length: int) -> bytes:
        handle = Open(link.tree, self._smb_path(rel))
        chunk = min(IO_CHUNK, link.connection.max_read_size or 65536)
        create = self._create(
            handle,
            _READ,
            CreateOptions.FILE_NON_DIRECTORY_FILE,
        )
        reads = []
        pos = offset
        while pos < offset + length:
            n = min(chunk, offset + length - pos)
            message, _ = handle.read(pos, n, send=False)
            reads.append(message)
            pos += n
        responses = self._compound(link, [create, *reads, self._close()], tolerate=(EndOfFile,))
        out = []
        for response in responses[1:-1]:
            if isinstance(response, EndOfFile):
                break
            data = response["data"].get_value()
            _size, data_off, data_len, _rem = _READ_RESPONSE.unpack_from(data)
            start = data_off - _SMB2_HEADER
            piece = data[start : start + data_len]
            out.append(piece)
            if len(piece) < chunk:
                break
        return b"".join(out)

    def open_read(self, rel: str) -> BinaryIO:
        size = self.stat(rel).size or 0
        return io.BufferedReader(_RangeReader(self, rel, size), buffer_size=IO_CHUNK)  # type: ignore[return-value]

    def local_path(self, rel: str) -> Path | None:
        return None

    # Writing -----------------------------------------------------------------------------------

    def rename(self, src: str, dst: str) -> None:
        """Server-side rename. Never replaces an existing destination."""

        def run(link: _Link) -> None:
            handle = Open(link.tree, self._smb_path(src))
            create = self._create(
                handle,
                FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                CreateOptions.FILE_OPEN_REPARSE_POINT,
            )
            info = FileRenameInformation()
            info["replace_if_exists"] = False
            info["file_name"] = self._smb_path(dst)
            self._compound(link, [create, self._set_info(info), self._close()])

        try:
            self._run(src, run, idempotent=False)
        except AlreadyExists as exc:
            raise AlreadyExists(f"destination exists: {dst}", dst) from exc

    def makedirs(self, rel: str) -> None:
        if rel == "" and not self._prefix:
            return

        def run(link: _Link) -> None:
            handle = Open(link.tree, self._smb_path(rel))
            create = self._create(
                handle,
                FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                CreateOptions.FILE_DIRECTORY_FILE,
                CreateDisposition.FILE_OPEN_IF,
                _DIR,
            )
            self._compound(link, [create, self._close()])

        try:
            self._run(rel, run)
        except NotFound:
            if rel == "":
                raise
            self.makedirs(parent(rel))
            self._run(rel, run)

    def delete_file(self, rel: str) -> None:
        def run(link: _Link, clear_readonly: bool) -> None:
            handle = Open(link.tree, self._smb_path(rel))
            access = _ACCESS.DELETE | _ACCESS.FILE_WRITE_ATTRIBUTES
            create = self._create(
                handle,
                access,
                CreateOptions.FILE_NON_DIRECTORY_FILE | CreateOptions.FILE_OPEN_REPARSE_POINT,
            )
            messages = [create]
            if clear_readonly:
                basic = FileBasicInformation()
                basic["file_attributes"] = FileAttributes.FILE_ATTRIBUTE_NORMAL
                messages.append(self._set_info(basic))
            dispose = FileDispositionInformation()
            dispose["delete_pending"] = True
            messages += [self._set_info(dispose), self._close()]
            self._compound(link, messages)

        try:
            self._run(rel, lambda link: run(link, False))
        except PermissionDenied as exc:
            if not isinstance(exc.__cause__, CannotDelete):
                raise
            # Read-only attribute: Windows servers refuse to delete until it's cleared.
            self._run(rel, lambda link: run(link, True))

    def remove_dir(self, rel: str) -> None:
        """Remove an empty folder; raises AlreadyExists if it isn't empty."""
        if rel == "":
            raise ValueError("refusing to remove the project root")

        def run(link: _Link) -> None:
            handle = Open(link.tree, self._smb_path(rel))
            create = self._create(
                handle,
                FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                CreateOptions.FILE_DIRECTORY_FILE | CreateOptions.FILE_OPEN_REPARSE_POINT,
            )
            dispose = FileDispositionInformation()
            dispose["delete_pending"] = True
            self._compound(link, [create, self._set_info(dispose), self._close()])

        self._run(rel, run)

    def delete_tree(self, rel: str) -> None:
        """Delete a folder and everything in it, bottom-up; symlinks are removed, not followed.
        Tolerates a partial earlier run (anything already gone is fine)."""
        if rel == "":
            raise ValueError("refusing to delete the project root")
        if self.stat(rel).kind != "dir":
            self.delete_file(rel)
            return
        for entry in self.list_dir(rel):
            child = f"{rel}/{entry.name}"
            with contextlib.suppress(NotFound):
                if entry.kind == "dir":
                    self.delete_tree(child)
                else:
                    self.delete_file(child)
        with contextlib.suppress(NotFound):
            self.remove_dir(rel)

    def write_file_atomic(self, rel: str, data: bytes) -> None:
        """Write a temporary file next to rel, then rename it over rel."""
        name = basename(rel)
        tmp = (f"{parent(rel)}/" if parent(rel) else "") + f".{name}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            self._write_new(tmp, data)
            self._replace(tmp, rel)
        except BaseException:
            with contextlib.suppress(SourceError):
                self.delete_file(tmp)
            raise

    def _write_new(self, rel: str, data: bytes) -> None:
        def run(link: _Link) -> None:
            chunk = min(IO_CHUNK, link.connection.max_write_size or 65536)
            pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
            # The first compound creates the file; later ones reopen it to append.
            for batch_start in range(0, len(pieces), MAX_CHUNKS):
                handle = Open(link.tree, self._smb_path(rel))
                create = self._create(
                    handle,
                    FilePipePrinterAccessMask.FILE_WRITE_DATA
                    | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                    CreateOptions.FILE_NON_DIRECTORY_FILE,
                    CreateDisposition.FILE_OPEN if batch_start else CreateDisposition.FILE_CREATE,
                )
                writes = []
                for i in range(batch_start, min(batch_start + MAX_CHUNKS, len(pieces))):
                    if pieces[i]:
                        message, _ = handle.write(pieces[i], i * chunk, send=False)
                        writes.append(message)
                self._compound(link, [create, *writes, self._close()])

        self._run(rel, run, idempotent=False)

    def _replace(self, src: str, dst: str) -> None:
        def run(link: _Link) -> None:
            handle = Open(link.tree, self._smb_path(src))
            create = self._create(
                handle,
                FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                CreateOptions.FILE_NON_DIRECTORY_FILE,
            )
            info = FileRenameInformation()
            info["replace_if_exists"] = True
            info["file_name"] = self._smb_path(dst)
            self._compound(link, [create, self._set_info(info), self._close()])

        self._run(src, run, idempotent=False)


class _RangeReader(io.RawIOBase):
    """A seekable, read-only file object over read_range (each read is one round trip)."""

    def __init__(self, source: SmbSource, rel: str, size: int) -> None:
        self._source, self._rel, self._size, self._pos = source, rel, size, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        if self._pos >= self._size or len(view) == 0:
            return 0
        data = self._source.read_range(self._rel, self._pos, min(len(view), self._size - self._pos))
        view[: len(data)] = data
        self._pos += len(data)
        return len(data)


# Helpers -----------------------------------------------------------------------------------------


def _qd_buffer(response: Any) -> bytes:
    """The output buffer of a QUERY_DIRECTORY response ('' if it reported no more files)."""
    if isinstance(response, BaseException):
        return b""
    data = response["data"].get_value()
    _size, offset, length = _QD_RESPONSE.unpack_from(data, 0)
    start = offset - _SMB2_HEADER
    return data[start : start + length]


def _deferred(result: Any) -> tuple[Any, Callable[[Any], Any]]:
    """smbprotocol's `send=False` calls return (message, unpack); its typing says otherwise."""
    assert isinstance(result, tuple)
    return result[0], result[1]


def _alive(connection: Connection) -> bool:
    transport = connection.transport
    return transport is not None and bool(transport.connected)


def _disconnect(connection: Connection) -> None:
    with contextlib.suppress(Exception):
        connection.disconnect(close=False)


def _connect_error(exc: BaseException, cfg: SmbConfig) -> SourceError:
    if isinstance(exc, _AUTH):
        return AuthenticationFailed(
            f"{cfg.host} rejected the username or password for “{cfg.login or 'guest'}”", ""
        )
    if isinstance(exc, BadNetworkName):
        return NotFound(f"there's no share called “{cfg.share}” on {cfg.host}", "")
    if isinstance(exc, (OSError, SMBConnectionClosed, _ConnectionLost)) or (
        isinstance(exc, SMBException) and "timeout" in str(exc).lower()
    ):
        return TransientSourceError(f"can't reach {cfg.host}:{cfg.port} ({exc})", "")
    return _translate(exc, "")


@contextlib.contextmanager
def connected(config: SmbConfig, password: str) -> Iterator[SmbSource]:
    """A connected SmbSource that is closed afterwards (Test connection, one-off probes)."""
    source = SmbSource(config, password)
    try:
        source.connect()
        yield source
    finally:
        source.close()
