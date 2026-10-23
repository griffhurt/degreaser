"""Background preparation of remote files: lookahead prefetch, deferred skips and Open
(docs/previews.md §3, product-spec §5).

A job detects the file's type (reading its first few KB) and, when the preview will need the
whole file, downloads it into the PreviewCache. The result is a `Prepared`: showing that file
later needs no round trips for detection, and full-file previews (images, PDFs, short videos)
open from the local copy.

At most `max_concurrent` jobs run at once, highest priority first. The file on screen doesn't
go through the queue: the preview pane fetches it directly, and the cache makes sure a file is
only ever downloaded once.
"""

import heapq
import itertools
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from degreaser.core.preview.cache import PreviewCache
from degreaser.core.preview.detect import Detection, Kind, detect
from degreaser.core.preview.loaders import Cancelled, CancelToken
from degreaser.core.preview.plan import plan_preview
from degreaser.core.settings import Downloads
from degreaser.core.sources.base import Entry, FileSource

log = logging.getLogger(__name__)

MEMO_SIZE = 64  # prepared results kept for files not yet shown


class Priority(IntEnum):
    OPEN = 1  # the user is waiting for another app to open it
    DEFERRED = 2  # skipped while loading; comes back once ready (jump-back)
    PREFETCH = 3  # the next few files in traversal order


class NotDeferrable(Exception):
    """The file can't usefully load in the background (it would need confirming first)."""


@dataclass(frozen=True, slots=True)
class Prepared:
    rel: str
    detection: Detection | None
    head: bytes
    path: Path | None  # the full local copy, when the preview (or Open) needs one


@dataclass
class _Job:
    rel: str
    entry: Entry
    priority: Priority
    seq: int
    cancel: CancelToken = field(default_factory=CancelToken)
    running: bool = False

    @property
    def want_full(self) -> bool:
        return self.priority is Priority.OPEN


DoneFn = Callable[[str, Priority, Prepared], None]
FailedFn = Callable[[str, Priority, BaseException], None]
ProgressFn = Callable[[str, int, int], None]


class DownloadQueue:
    """Callbacks run on the queue's worker threads; the UI layer marshals them."""

    def __init__(
        self,
        source: FileSource,
        cache: PreviewCache,
        limits: Downloads,
        *,
        on_done: DoneFn,
        on_failed: FailedFn,
        on_progress: ProgressFn | None = None,
    ) -> None:
        self.source = source
        self.cache = cache
        self.limits = limits
        self._on_done, self._on_failed, self._on_progress = on_done, on_failed, on_progress
        self._jobs: dict[str, _Job] = {}
        self._heap: list[tuple[int, int, str]] = []
        self._seq = itertools.count()
        self._memo: OrderedDict[str, Prepared] = OrderedDict()
        self._cond = threading.Condition()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker, name=f"download-{i}", daemon=True)
            for i in range(max(1, limits.max_concurrent))
        ]
        for thread in self._threads:
            thread.start()

    # Requests ----------------------------------------------------------------------------------

    def request(self, rel: str, entry: Entry, priority: Priority) -> None:
        """Queue a file, or raise the priority of one already queued or running."""
        with self._cond:
            if self._closed:
                return
            job = self._jobs.get(rel)
            if job is not None:
                if priority < job.priority:
                    job.priority = priority
                    if not job.running:
                        heapq.heappush(self._heap, (priority, job.seq, rel))
                return
            memo = self._memo.get(rel)
            if memo is not None and (memo.path is not None or priority is not Priority.OPEN):
                done = memo
            else:
                job = _Job(rel, entry, priority, next(self._seq))
                self._jobs[rel] = job
                heapq.heappush(self._heap, (priority, job.seq, rel))
                self._cond.notify()
                return
        self._on_done(rel, priority, done)  # already prepared: report straight away

    def cancel(self, rel: str) -> None:
        with self._cond:
            job = self._jobs.pop(rel, None)
        if job is not None:
            job.cancel.cancel()

    def retain_only(self, priority: Priority, rels: set[str]) -> None:
        """Cancel jobs of exactly this priority that aren't in `rels` (stale prefetch)."""
        with self._cond:
            stale = [j for j in self._jobs.values() if j.priority is priority and j.rel not in rels]
            for job in stale:
                del self._jobs[job.rel]
        for job in stale:
            job.cancel.cancel()

    def prepared(self, rel: str) -> Prepared | None:
        with self._cond:
            return self._memo.get(rel)

    def forget(self, rel: str) -> None:
        with self._cond:
            self._memo.pop(rel, None)

    def pending(self, minimum: Priority = Priority.PREFETCH) -> list[str]:
        """Files queued or running at `minimum` priority or more urgent."""
        with self._cond:
            return [j.rel for j in self._jobs.values() if j.priority <= minimum]

    def close(self) -> None:
        with self._cond:
            self._closed = True
            jobs = list(self._jobs.values())
            self._jobs.clear()
            self._cond.notify_all()
        for job in jobs:
            job.cancel.cancel()

    # Workers -----------------------------------------------------------------------------------

    def _next(self) -> _Job | None:
        with self._cond:
            while True:
                if self._closed:
                    return None
                while self._heap:
                    priority, _seq, rel = heapq.heappop(self._heap)
                    job = self._jobs.get(rel)
                    if job is not None and not job.running and job.priority == priority:
                        job.running = True
                        return job
                self._cond.wait()

    def _worker(self) -> None:
        while (job := self._next()) is not None:
            try:
                prepared = self._prepare(job)
            except Cancelled:
                continue
            except BaseException as exc:
                with self._cond:
                    current = self._jobs.get(job.rel)
                    if current is job:
                        del self._jobs[job.rel]
                if current is job:
                    log.info("download of %s failed: %s", job.rel, exc)
                    self._on_failed(job.rel, job.priority, exc)
                continue
            with self._cond:
                current = self._jobs.get(job.rel)
                if current is job:
                    del self._jobs[job.rel]
                    self._memo[job.rel] = prepared
                    self._memo.move_to_end(job.rel)
                    while len(self._memo) > MEMO_SIZE:
                        self._memo.popitem(last=False)
            if current is job:
                self._on_done(job.rel, job.priority, prepared)

    def _prepare(self, job: _Job) -> Prepared:
        rel, entry, cancel = job.rel, job.entry, job.cancel
        size = entry.size or 0
        detection: Detection | None = None
        head = b""
        if not job.want_full:
            detection, head = detect(self.source, rel, entry)
            cancel.check()
        full = job.want_full or self._needs_full(job, detection, size)
        path = None
        if full or job.want_full:  # an Open may have arrived while detecting
            progress = self._progress_for(rel)
            path = self.cache.fetch(self.source, rel, entry, progress, cancel)
        return Prepared(rel, detection, head, path)

    def _needs_full(self, job: _Job, detection: Detection | None, size: int) -> bool:
        """Whether this preview wants the whole file now. Deferrals that would need asking
        first (big images or PDFs, long videos) can't load in the background."""
        if detection is None:
            return False
        plan = plan_preview(detection, job.entry, self.limits, local=False)
        media = detection.kind in (Kind.VIDEO, Kind.AUDIO)
        wants = (plan.needs_full_file and plan.confirm is None) or (
            media and size <= self.limits.auto_load_bytes
        )
        if job.priority is Priority.DEFERRED:
            if (plan.needs_full_file and plan.confirm is not None) or (
                media and size > self.limits.auto_load_bytes
            ):
                raise NotDeferrable("too large to load in the background")
            return wants
        # Prefetch never grows the cache past its cap.
        return wants and self.cache.size_bytes() + size <= self.limits.cache_cap_bytes

    def _progress_for(self, rel: str) -> Callable[[int, int], None] | None:
        report = self._on_progress
        if report is None:
            return None
        return lambda done, total: report(rel, done, total)
