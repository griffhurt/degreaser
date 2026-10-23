"""Read-only check of a real SMB server before trusting it with a commit (roadmap M2).

`degreaser --smb-probe smb://nas/share/folder --username griff` connects, lists folders
breadth-first for a while, reads a sample, and reports listing speed, latency, throughput
and server quirks that matter to Degreaser: case sensitivity, names Windows can't hold,
decomposed (NFD) Unicode names, very large folders. It never writes anything.
"""

import contextlib
import statistics
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from degreaser.core.sources.base import join
from degreaser.core.sources.errors import NotFound, PermissionDenied, SourceError
from degreaser.core.sources.smb import SmbConfig, SmbSource

WINDOWS_FORBIDDEN = set('<>:"\\|?*')
# Macs send ':' '?' etc. as private-use characters (Services for Macintosh uses U+F001–F029,
# Samba's catia U+F000 + the ASCII code). They stand for characters Windows can't hold.
MAC_MAPPED = range(0xF000, 0xF100)
LARGE_FOLDER = 8000  # entries; beyond this a listing needs paging (D28)
LONG_PATH = 250  # characters; Windows tools struggle past MAX_PATH


@dataclass
class _Findings:
    folders: int = 0
    files: int = 0
    bytes: int = 0
    symlinks: int = 0
    denied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    largest_folder: tuple[str, int] = ("", 0)
    case_twins: list[str] = field(default_factory=list)  # names equal ignoring case
    windows_unsafe: list[str] = field(default_factory=list)
    nfd_names: list[str] = field(default_factory=list)
    long_paths: list[str] = field(default_factory=list)
    biggest_file: tuple[str, int] = ("", 0)
    sample_file: tuple[str, int] = ("", 0)  # a mid-sized file to measure reads with


def _note(items: list[str], value: str, limit: int = 20) -> None:
    if len(items) < limit:
        items.append(value)


def _inspect(path: str, entries: list[Any], found: _Findings) -> None:
    if len(entries) > found.largest_folder[1]:
        found.largest_folder = (path or "/", len(entries))
    seen: dict[str, str] = {}
    for e in entries:
        rel = join(path, e.name)
        folded = e.name.casefold()
        if folded in seen:
            _note(found.case_twins, f"{rel}  ↔  {seen[folded]}")
        seen[folded] = e.name
        mapped = any(ord(ch) in MAC_MAPPED for ch in e.name)
        if mapped or WINDOWS_FORBIDDEN & set(e.name) or e.name.endswith((".", " ")):
            _note(found.windows_unsafe, rel)
        if unicodedata.normalize("NFC", e.name) != e.name:
            _note(found.nfd_names, rel)
        if len(rel) > LONG_PATH:
            _note(found.long_paths, rel)
        if e.kind == "symlink":
            found.symlinks += 1
        elif e.kind == "file":
            found.files += 1
            size = e.size or 0
            found.bytes += size
            if size > found.biggest_file[1]:
                found.biggest_file = (rel, size)
            if 8 << 20 <= size <= 512 << 20 and size > found.sample_file[1]:
                found.sample_file = (rel, size)


def probe(
    config: SmbConfig,
    password: str,
    *,
    seconds: float = 60.0,
    sample_bytes: int = 256 << 20,
    report: Callable[[str], None] = print,
) -> dict[str, Any]:
    result: dict[str, Any] = {"server": config.describe(), "read_only": True}
    source = SmbSource(config, password)
    start = time.perf_counter()
    source.connect()
    result["connect_seconds"] = round(time.perf_counter() - start, 3)
    result["connection"] = source.connection_info()
    result["rtt_ms"] = round((source.rtt or 0) * 1000, 2)
    result["workers"] = source.parallelism
    report(
        f"Connected in {result['connect_seconds']} s · {result['connection']['dialect']} · "
        f"RTT {result['rtt_ms']} ms · {source.parallelism} workers"
    )
    try:
        found = _list(source, seconds, report, result)
        result["case_sensitive"] = _case_sensitive(source, found)
        result["reads"] = _reads(source, found, sample_bytes, report)
        state_exists = True
        try:
            source.stat(".degreaser/state/state.json")
        except NotFound:
            state_exists = False
        result["degreaser_state_present"] = state_exists
        result["findings"] = {
            "large_folder": {"path": found.largest_folder[0], "entries": found.largest_folder[1]},
            "needs_paging": found.largest_folder[1] > LARGE_FOLDER,
            "names_differing_only_in_case": found.case_twins,
            "names_windows_cant_hold": found.windows_unsafe,
            "decomposed_unicode_names": found.nfd_names,
            "paths_over_250_chars": found.long_paths,
            "symlinks": found.symlinks,
            "permission_denied": found.denied,
            "other_errors": found.errors,
        }
    finally:
        source.close()
    return result


def _list(
    source: SmbSource, seconds: float, report: Callable[[str], None], result: dict[str, Any]
) -> _Findings:
    found = _Findings()
    queue: deque[str] = deque([""])
    workers = source.parallelism
    start = time.perf_counter()
    last = start
    with ThreadPoolExecutor(workers, thread_name_prefix="probe") as pool:
        in_flight: dict[Future[list[Any]], str] = {}
        while (queue or in_flight) and time.perf_counter() - start < seconds:
            while queue and len(in_flight) < workers * 2:
                path = queue.popleft()
                in_flight[pool.submit(source.list_dir, path)] = path
            done, _ = wait(in_flight, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                path = in_flight.pop(future)
                try:
                    entries = future.result()
                except PermissionDenied:
                    _note(found.denied, path or "/")
                    continue
                except SourceError as exc:
                    _note(found.errors, f"{path or '/'}: {exc}")
                    continue
                found.folders += 1
                _inspect(path, entries, found)
                for e in entries:
                    if e.kind == "dir" and not (path == "" and e.name == ".degreaser"):
                        queue.append(join(path, e.name))
            if time.perf_counter() - last > 5:
                last = time.perf_counter()
                report(f"  …{found.folders:,} folders, {found.files:,} files")
        for future in in_flight:
            future.cancel()
    elapsed = time.perf_counter() - start
    complete = not queue and not in_flight
    rate = found.folders / elapsed if elapsed else 0.0
    result["listing"] = {
        "seconds": round(elapsed, 2),
        "complete": complete,
        "folders": found.folders,
        "files": found.files,
        "bytes": found.bytes,
        "folders_per_second": round(rate, 1),
        "entries_per_second": round((found.folders + found.files) / elapsed if elapsed else 0),
        "folders_still_queued": len(queue),
    }
    scope = "the whole tree" if complete else f"{found.folders:,} folders (stopped at the limit)"
    report(f"Listed {scope} in {elapsed:.1f} s: {rate:,.0f} folders/s")
    if complete and rate:
        result["listing"]["estimated_rescan_seconds"] = round(found.folders / rate, 1)
    return found


def _case_sensitive(source: SmbSource, found: _Findings) -> bool | None:
    """Stat a known file with its name's case flipped: found → case-insensitive server."""
    rel = found.biggest_file[0]
    if not rel:
        return None
    head, _, name = rel.rpartition("/")
    flipped = name.swapcase()
    if flipped == name:
        return None
    with contextlib.suppress(SourceError):
        source.stat(f"{head}/{flipped}" if head else flipped)
        return False
    return True


def _reads(
    source: SmbSource, found: _Findings, sample_bytes: int, report: Callable[[str], None]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rel, size = found.sample_file if found.sample_file[0] else found.biggest_file
    if not rel:
        return out
    latencies = []
    for _ in range(10):
        start = time.perf_counter()
        source.read_range(rel, 0, 4096)
        latencies.append(time.perf_counter() - start)
    out["small_read_ms"] = round(statistics.median(latencies) * 1000, 2)
    want = min(size, sample_bytes)
    start = time.perf_counter()
    done = 0
    while done < want:
        data = source.read_range(rel, done, min(4 << 20, want - done))
        if not data:
            break
        done += len(data)
    elapsed = time.perf_counter() - start
    out["sequential_read"] = {
        "file": rel,
        "bytes": done,
        "seconds": round(elapsed, 2),
        "mb_per_second": round(done / elapsed / 1e6, 1) if elapsed else None,
    }
    report(
        f"4 KB read: {out['small_read_ms']} ms · sequential read of {done / 1e6:.0f} MB: "
        f"{out['sequential_read']['mb_per_second']} MB/s"
    )
    return out


def summarise(result: dict[str, Any]) -> list[str]:
    """Plain-language notes for the person running the probe."""
    notes = []
    f = result.get("findings", {})
    if result.get("case_sensitive"):
        notes.append(
            "The share is case-sensitive: files whose names differ only in case are distinct."
        )
    if f.get("names_differing_only_in_case"):
        notes.append(
            "Some folders hold names that differ only in case; a Windows or Mac client would "
            "see just one of each."
        )
    if f.get("names_windows_cant_hold"):
        notes.append(
            "Some names contain characters Windows can't use (e.g. ':' or '?', often from Macs)."
        )
    if f.get("decomposed_unicode_names"):
        notes.append("Some names use decomposed Unicode (typical of files copied from a Mac).")
    if f.get("needs_paging"):
        notes.append(
            "The largest folder has more than 8,000 entries; listing it takes extra round trips."
        )
    if f.get("permission_denied"):
        notes.append("Some folders couldn't be read (permission denied); triage skips them.")
    listing = result.get("listing", {})
    if listing.get("estimated_rescan_seconds"):
        notes.append(
            f"A startup check of this tree should take about "
            f"{listing['estimated_rescan_seconds']:.0f} s."
        )
    return notes
