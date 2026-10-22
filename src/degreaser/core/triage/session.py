"""TriageSession: the cursor, the queues and every triage action (docs/triage-engine.md).

UI-agnostic and synchronous. It uses the caller's connection (the UI thread's) and never
does file I/O. Two signals come back in Advance results instead of an item:
  * needs_listing: traversal reached an unlisted folder. List it (core/scan), call
    on_tree_changed(), then advance() again. Actions have already been applied by then.
  * needs_sweep: the main DFS hit the end. Run sweep_first() on a worker with its own
    connection (it can take ~5 s on a fully decided 2M-file tree) and pass the result to
    apply_sweep(); run_sweep() does the same synchronously.
"""

import bisect
import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from degreaser.core.categories.index import CategoryIndex
from degreaser.core.categories.store import CategoryStore, InvalidCategory
from degreaser.core.categories.validate import parse_category
from degreaser.core.db.connection import tx
from degreaser.core.model import Category, Effective, EffectiveState, Origin, WalkSummary
from degreaser.core.project import ProjectSettings
from degreaser.core.rules.ruleset import RuleSet
from degreaser.core.sources.base import basename, is_within, join, parent, subtree_range
from degreaser.core.triage import history
from degreaser.core.triage.order import DfsKey, dfs_key
from degreaser.core.triage.resolver import Resolver
from degreaser.core.triage.traversal import NeedsListing, Traversal
from degreaser.core.triage.walk import SummaryTracker, add_summary, count_state, subtree_summary


class Banner(StrEnum):
    NONE = "none"
    NEW = "new"  # NEWLY ADDED FILE
    CHANGED = "changed"  # CHANGED SINCE YOU TRIAGED IT
    LOADED_SKIP = "loaded_skip"  # SKIPPED EARLIER — NOW LOADED
    PREVIOUSLY_SKIPPED = "previously_skipped"  # review-skipped pass


class Mode(StrEnum):
    NORMAL = "normal"
    REVIEW_SKIPPED = "review_skipped"


class ItemSource(StrEnum):
    MAIN = "main"
    DEFERRED_READY = "deferred_ready"
    NEW = "new"
    CHANGED = "changed"
    REVIEW = "review"
    JUMP = "jump"
    UNDO = "undo"


@dataclass(frozen=True, slots=True)
class Item:
    path: str
    banner: Banner
    source: ItemSource
    previous: EffectiveState | None = None  # the decision being revisited (CHANGED)


@dataclass(frozen=True, slots=True)
class Advance:
    item: Item | None
    needs_sweep: bool = False
    needs_listing: str | None = None  # folder to list before calling advance() again

    @property
    def done(self) -> bool:
        """Nothing left to triage."""
        return self.item is None and not self.needs_sweep and self.needs_listing is None


@dataclass(frozen=True, slots=True)
class ActionResult:
    label: str
    history_id: int | None
    advance: Advance | None  # None for actions that don't move on (unmark)
    deferred: bool = False  # Skip became a deferral


@dataclass(frozen=True, slots=True)
class DeleteFolderCheck:
    folder: str
    kept: int  # files beneath that are currently kept (any origin)
    categorised: int

    @property
    def needs_confirmation(self) -> bool:
        return self.kept > 0 or self.categorised > 0


@dataclass(frozen=True, slots=True)
class TagFolderCheck:
    folder: str
    blocked_by: str | None  # ancestor marked delete: refuse, offer to unmark it
    delete_marks: int  # delete decisions beneath, cleared if confirmed
    conflicting: list[str] = field(default_factory=list)  # own categories that don't refine C

    @property
    def needs_confirmation(self) -> bool:
        return self.blocked_by is None and (self.delete_marks > 0 or bool(self.conflicting))


class NoCurrentItem(RuntimeError):
    pass


class FolderActionRefused(RuntimeError):
    def __init__(self, message: str, blocked_by: str | None = None) -> None:
        super().__init__(message)
        self.blocked_by = blocked_by


def bump_revision(conn: sqlite3.Connection) -> None:
    """Every write to synced tables bumps meta.state_revision (docs/data-model.md §2.4)."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('state_revision', '1')"
        " ON CONFLICT(key) DO UPDATE SET value = CAST(meta.value AS INTEGER) + 1"
    )


def sweep_first(conn: sqlite3.Connection) -> str | None:
    """Wrap-around sweep with a fresh resolver; safe to run on a worker's own connection."""
    store = CategoryStore(conn)
    return Traversal(conn, Resolver(conn, RuleSet.load(conn), store)).first()


class TriageSession:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        rules: RuleSet | None = None,
        store: CategoryStore | None = None,
        index: CategoryIndex | None = None,
        settings: ProjectSettings | None = None,
        tracker: SummaryTracker | None = None,
    ) -> None:
        self.conn = conn
        self.store = store or CategoryStore(conn)
        self.index = index or CategoryIndex.build(conn, self.store)
        self.rules = rules or RuleSet.load(conn)
        self.settings = settings or ProjectSettings.from_dict(self._meta("settings", {}))
        self.tracker = tracker or SummaryTracker()
        self.resolver = Resolver(conn, self.rules, self.store)
        self.traversal = Traversal(conn, self.resolver)
        self.mode = Mode.NORMAL
        self.current: Item | None = None
        self._cursor: str | None = self._meta("cursor")
        self._ready: list[str] = []
        self._new: list[tuple[DfsKey, str]] = []
        self._changed: list[tuple[DfsKey, str]] = []
        self._review: list[str] = []
        self._review_pos = -1

    # Meta -----------------------------------------------------------------------------------

    def _meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def _set_cursor(self, path: str | None) -> None:
        self._cursor = path
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES ('cursor', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(path),),
        )

    @property
    def cursor(self) -> str | None:
        return self._cursor

    # Starting and advancing -----------------------------------------------------------------

    def start(self) -> Advance:
        """Begin (or resume) triage."""
        if not self.settings.jump_back:
            self.convert_deferred_to_skip()
        self.refresh_queues()
        self.mode = Mode.NORMAL
        return self.advance()

    def refresh_queues(self) -> None:
        """Reload the new/changed queues from node flags (after a rescan, undo or redo)."""
        self._new = sorted(
            (dfs_key(p, False), p)
            for (p,) in self.conn.execute(
                "SELECT path FROM nodes WHERE flag_new = 1 AND is_dir = 0"
            )
        )
        self._changed = (
            sorted(
                (dfs_key(p, False), p)
                for (p,) in self.conn.execute(
                    "SELECT path FROM nodes WHERE flag_changed = 1 AND is_dir = 0"
                )
            )
            if self.settings.retriage_changed
            else []
        )

    def on_tree_changed(self) -> None:
        """Call after folders were listed or a rescan changed the tree."""
        self.resolver.invalidate()
        self.refresh_queues()

    def reload_rules(self) -> None:
        """Call after the rules editor saved: effective states change retroactively."""
        self.rules = RuleSet.load(self.conn)
        self.resolver.invalidate(self.rules)
        self.tracker.mark_stale()

    def advance(self) -> Advance:
        """Move to the next item (§1.3)."""
        if self.mode is Mode.REVIEW_SKIPPED:
            return self._advance_review()
        item = self._next_queued()
        if item is None:
            try:
                path = self.traversal.next_from(self._cursor, inclusive=True)
            except NeedsListing as signal:
                self.current = None
                return Advance(None, needs_listing=signal.path)
            if path is None:
                self.current = None
                return Advance(None, needs_sweep=True)
            self._set_cursor(path)
            item = Item(path, self._banner(path), ItemSource.MAIN)
        self.current = item
        return Advance(item)

    def _next_queued(self) -> Item | None:
        while self._ready:
            path = self._ready[0]
            if self._decision(path) == "deferred":
                return Item(path, Banner.LOADED_SKIP, ItemSource.DEFERRED_READY)
            self._ready.pop(0)
        while self._new:
            path = self._new[0][1]
            flags = self._flags(path)
            if flags is not None and flags[0] and self.traversal.needs_decision(path):
                return Item(path, Banner.NEW, ItemSource.NEW)
            self._new.pop(0)
        while self._changed:
            path = self._changed[0][1]
            flags = self._flags(path)
            if flags is not None and flags[1] and self._decision(path) is not None:
                state = self.resolver.resolve(path)
                if state.origin is not Origin.FOLDER:
                    return Item(path, Banner.CHANGED, ItemSource.CHANGED, previous=state)
            self._changed.pop(0)
        return None

    def run_sweep(self) -> Advance:
        """Synchronous wrap-around sweep (small trees, tests)."""
        try:
            return self.apply_sweep(self.traversal.first())
        except NeedsListing as signal:
            return Advance(None, needs_listing=signal.path)

    def apply_sweep(self, path: str | None) -> Advance:
        """Result of sweep_first() run on a worker."""
        if path is None:
            self.current = None
            return Advance(None)
        self._set_cursor(path)
        self.current = Item(path, self._banner(path), ItemSource.MAIN)
        return Advance(self.current)

    def lookahead(self, n: int = 3) -> list[str]:
        """Files likely to come next, for preview prefetch (stops at unlisted folders)."""
        start = self.current.path if self.current else self._cursor
        return [] if start is None else self.traversal.lookahead(start, n).files

    # Review-skipped pass ----------------------------------------------------------------------

    def start_review_skipped(self) -> Advance:
        paths = [
            p
            for (p,) in self.conn.execute(
                "SELECT path FROM decisions WHERE action = 'skip' AND is_dir = 0"
            )
        ]
        self._review = sorted(paths, key=lambda p: dfs_key(p, False))
        self._review_pos = -1
        self.mode = Mode.REVIEW_SKIPPED
        return self._advance_review()

    def skipped_count(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM decisions WHERE action = 'skip' AND is_dir = 0"
        ).fetchone()[0]

    def end_review(self) -> None:
        self.mode = Mode.NORMAL
        self.current = None

    def _advance_review(self) -> Advance:
        while self._review_pos + 1 < len(self._review):
            self._review_pos += 1
            path = self._review[self._review_pos]
            try:
                state = self.resolver.resolve(path)
            except KeyError:
                continue
            if state.action is Effective.SKIP and state.origin is Origin.DIRECT:
                self.current = Item(path, Banner.PREVIOUSLY_SKIPPED, ItemSource.REVIEW)
                return Advance(self.current)
        self.mode = Mode.NORMAL
        self.current = None
        return Advance(None)

    # Deferred files -----------------------------------------------------------------------

    def deferred_paths(self) -> list[str]:
        return [
            p
            for (p,) in self.conn.execute(
                "SELECT path FROM decisions WHERE action = 'deferred' ORDER BY path"
            )
        ]

    def mark_deferred_ready(self, path: str) -> None:
        """The preview of a deferred file has loaded: show it at the next advance (jump-back)."""
        if (
            self.settings.jump_back
            and self._decision(path) == "deferred"
            and path not in self._ready
        ):
            self._ready.append(path)

    def convert_deferred_to_skip(self, path: str | None = None) -> None:
        """Deferral failed / declined / jump-back off: make it a normal skip (not undoable)."""
        paths = self.deferred_paths() if path is None else [path]
        changed = False
        with tx(self.conn):
            for p in paths:
                if self._decision(p) == "deferred":
                    self.conn.execute("UPDATE decisions SET action = 'skip' WHERE path = ?", (p,))
                    changed = True
            if changed:
                bump_revision(self.conn)
        for p in paths:
            if p in self._ready:
                self._ready.remove(p)
            size = self._size(p)
            self.tracker.apply_file(
                EffectiveState(Effective.DEFERRED, Origin.DIRECT, p),
                EffectiveState(Effective.SKIP, Origin.DIRECT, p),
                size,
            )

    # Navigation -----------------------------------------------------------------------------

    def jump_to(self, path: str) -> Item:
        """Path-bar jump: make `path` current; advancing continues after it in DFS order."""
        row = self.conn.execute("SELECT is_dir FROM nodes WHERE path = ?", (path,)).fetchone()
        if row is None or row[0]:
            raise ValueError(f"not a file in this project: {path!r}")
        if self.mode is Mode.NORMAL:
            self._set_cursor(path)
        self.current = Item(path, self._banner(path), ItemSource.JUMP)
        return self.current

    def state_of(self, path: str) -> EffectiveState:
        return self.resolver.resolve(path)

    # File actions ---------------------------------------------------------------------------

    def keep(self) -> ActionResult:
        return self._file_action("Keep", "keep")

    def delete(self) -> ActionResult:
        return self._file_action("Delete", "delete")

    def skip(self, *, defer: bool = False) -> ActionResult:
        """defer=True when the preview is still loading from a remote source (§5)."""
        if defer and self.settings.jump_back and self.mode is Mode.NORMAL:
            return self._file_action("Defer", "deferred", deferred=True)
        return self._file_action("Skip", "skip")

    def tag(self, category: str | Sequence[str]) -> ActionResult:
        """Set the file's own category (empty clears it) and keep it."""
        names = self._parse(category)
        return self._file_action("Tag", "keep", category=names)

    def _file_action(
        self,
        verb: str,
        action: str,
        *,
        category: list[str] | None = None,
        deferred: bool = False,
    ) -> ActionResult:
        item = self._require_current()
        path = item.path
        size, mtime = self._size_mtime(path)
        affected = [path, *self._companions(path)]
        before = {p: self.resolver.resolve(p) for p in affected}
        label = f"{verb} {basename(path)}"
        with tx(self.conn):
            rec = history.Recorder(self.conn, label, path)
            rec.set("decisions", path, _decision_row(False, action, size, mtime))
            if action != "keep":
                rec.set("node_categories", path, None)
            elif category is not None:
                self._set_own_category(rec, path, category)
            rec.set("nodes_flags", path, {"flag_new": 0, "flag_changed": 0})
            hid = rec.finish()
            if hid is not None:
                bump_revision(self.conn)
        self._after_changes(rec.changes, undoing=False)
        for p in affected:
            self.tracker.apply_file(before[p], self.resolver.resolve(p), self._size(p))
        if self.mode is Mode.REVIEW_SKIPPED:
            return ActionResult(label, hid, self._advance_review(), deferred)
        return ActionResult(label, hid, self.advance(), deferred)

    def review_set(self, paths: Sequence[str], action: str | None) -> ActionResult:
        """Review screen: set (or clear, with None) many nodes' own decisions at once.

        One undoable entry; the triage cursor doesn't move. Files take keep/delete/skip/None;
        folders take delete/None (clearing a tag-folder also drops the folder's category).
        """
        if action not in (None, "keep", "delete", "skip"):
            raise ValueError(f"unsupported review action {action!r}")
        files = [p for p in paths if self._node_is_dir(p) is False]
        dirs = [p for p in paths if self._node_is_dir(p)]
        if dirs and action not in (None, "delete"):
            raise ValueError("folders can only be marked for deletion or cleared here")
        affected = [p for f in files for p in (f, *self._companions(f))]
        before = {p: self.resolver.resolve(p) for p in affected}
        verb = "Clear" if action is None else action.capitalize()
        count = len(files) + len(dirs)
        label = f"{verb} {basename(paths[0])}" if count == 1 else f"{verb} {count:,} items"
        cursor_before = self.current.path if self.current else self._cursor
        with tx(self.conn):
            rec = history.Recorder(self.conn, label, cursor_before)
            for path in files:
                size, mtime = self._size_mtime(path)
                row = None if action is None else _decision_row(False, action, size, mtime)
                rec.set("decisions", path, row)
                if action != "keep":
                    rec.set("node_categories", path, None)
                rec.set("nodes_flags", path, {"flag_new": 0, "flag_changed": 0})
            for path in dirs:
                rec.set(
                    "decisions",
                    path,
                    None if action is None else _decision_row(True, action, None, None),
                )
                rec.set("node_categories", path, None)
            hid = rec.finish()
            if hid is not None:
                bump_revision(self.conn)
        self._after_changes(rec.changes, undoing=False)
        if dirs:
            self.tracker.mark_stale()  # subtree effects: the owner re-walks
        else:
            for p in affected:
                self.tracker.apply_file(before[p], self.resolver.resolve(p), self._size(p))
        return ActionResult(label, hid, None)

    # Folder actions -------------------------------------------------------------------------

    def default_folder(self) -> str:
        return parent(self._require_current().path)

    def check_delete_folder(self, folder: str | None = None) -> DeleteFolderCheck:
        folder = self._folder(folder)
        s = subtree_summary(self.conn, self.rules, self.store, folder)
        return DeleteFolderCheck(folder, s.files_keep, s.files_categorised)

    def delete_folder(self, folder: str | None = None) -> ActionResult:
        """Mark a folder delete (call check_delete_folder first and confirm if needed)."""
        folder = self._folder(folder)
        label = f"Delete folder {basename(folder)}"

        def write(rec: history.Recorder) -> None:
            rec.set("decisions", folder, _decision_row(True, "delete", None, None))
            rec.set("node_categories", folder, None)

        hid = self._folder_write(folder, label, write)
        self._set_cursor(folder)
        return ActionResult(label, hid, self._advance_after_folder())

    def check_tag_folder(
        self, category: str | Sequence[str], folder: str | None = None
    ) -> TagFolderCheck:
        folder = self._folder(folder)
        blocked = self.resolver.dir_ctx(parent(folder)).deleted_by
        names = self.store.canonical(self._parse(category))
        lo, hi = subtree_range(folder)
        delete_marks = self.conn.execute(
            "SELECT count(*) FROM decisions WHERE path >= ? AND path < ? AND action = 'delete'",
            (lo, hi),
        ).fetchone()[0]
        return TagFolderCheck(folder, blocked, delete_marks, self._conflicting(folder, names))

    def tag_folder(
        self,
        category: str | Sequence[str],
        folder: str | None = None,
        *,
        replace_conflicting: bool = False,
    ) -> ActionResult:
        """Keep a folder and set its category (empty = plain keep folder).

        Clears delete, skip and deferred marks beneath it. With replace_conflicting,
        descendants whose own category doesn't refine the new one lose it and inherit the
        folder's instead.
        """
        folder = self._folder(folder)
        blocked = self.resolver.dir_ctx(parent(folder)).deleted_by
        if blocked is not None:
            raise FolderActionRefused(
                f"Parent folder {blocked or '/'} is marked for deletion; unmark it first.",
                blocked_by=blocked,
            )
        names = self._parse(category)
        canonical = self.store.canonical(names)
        conflicting = self._conflicting(folder, canonical) if replace_conflicting else []
        label = f"Tag folder {basename(folder)}" if names else f"Keep folder {basename(folder)}"

        def write(rec: history.Recorder) -> None:
            rec.set("decisions", folder, _decision_row(True, "keep", None, None))
            self._set_own_category(rec, folder, names)
            lo, hi = subtree_range(folder)
            for (p,) in self.conn.execute(
                "SELECT path FROM decisions WHERE path >= ? AND path < ?"
                " AND action IN ('delete', 'skip', 'deferred')",
                (lo, hi),
            ).fetchall():
                # "Tag folder" keeps everything inside, including skipped/deferred files.
                rec.set("decisions", p, None)
            for p in conflicting:
                rec.set("node_categories", p, None)

        hid = self._folder_write(folder, label, write)
        self._set_cursor(folder)
        return ActionResult(label, hid, self._advance_after_folder())

    def unmark_folder(self, folder: str) -> ActionResult:
        """Remove a folder's delete mark (offered when tag-folder is blocked)."""
        label = f"Unmark folder {basename(folder)}"
        if self._decision(folder) != "delete":
            return ActionResult(label, None, None)

        def write(rec: history.Recorder) -> None:
            rec.set("decisions", folder, None)

        return ActionResult(label, self._folder_write(folder, label, write), None)

    def _advance_after_folder(self) -> Advance:
        if self.mode is Mode.REVIEW_SKIPPED:
            return self._advance_review()
        return self.advance()

    def _folder_write(self, folder: str, label: str, write: Any) -> int | None:
        companions = self._companions(folder)
        before_sum = subtree_summary(self.conn, self.rules, self.store, folder)
        before = {p: self.resolver.resolve(p) for p in companions}
        cursor_before = self.current.path if self.current else self._cursor
        with tx(self.conn):
            rec = history.Recorder(self.conn, label, cursor_before)
            write(rec)
            hid = rec.finish()
            if hid is not None:
                bump_revision(self.conn)
        self._after_changes(rec.changes, undoing=False)
        self.tracker.apply_subtree(
            before_sum, subtree_summary(self.conn, self.rules, self.store, folder)
        )
        for p in companions:
            self.tracker.apply_file(before[p], self.resolver.resolve(p), self._size(p))
        return hid

    # Undo / redo ----------------------------------------------------------------------------

    def undo_label(self) -> str | None:
        return history.undo_label(self.conn)

    def redo_label(self) -> str | None:
        return history.redo_label(self.conn)

    def undo(self) -> Item | None:
        """Undo the last action and show the file it was taken on (with its banner)."""
        entry = self._apply_history(undoing=True)
        if entry is None:
            return None
        target = entry.cursor_before
        if target is None or self._node_is_dir(target) is not False:
            self.current = None
            return None
        if self.mode is Mode.NORMAL:
            if self._cursor is None or dfs_key(target, False) < dfs_key(self._cursor, False):
                self._set_cursor(target)
        elif target in self._review:
            self._review_pos = self._review.index(target)
        banner = (
            Banner.PREVIOUSLY_SKIPPED if self.mode is Mode.REVIEW_SKIPPED else self._banner(target)
        )
        self.current = Item(target, banner, ItemSource.UNDO)
        return self.current

    def redo(self) -> Advance | None:
        """Re-apply the last undone action and move on."""
        entry = self._apply_history(undoing=False)
        if entry is None:
            return None
        if self.mode is Mode.REVIEW_SKIPPED:
            return self._advance_review()
        return self.advance()

    def clear_history(self) -> None:
        """Forget undo history (the commit runner calls this when a commit finishes)."""
        with tx(self.conn):
            history.clear(self.conn)
            bump_revision(self.conn)

    def _apply_history(self, *, undoing: bool) -> history.Entry | None:
        peek = self.conn.execute(
            "SELECT id FROM history WHERE state = ? ORDER BY id "
            + ("DESC" if undoing else "")
            + " LIMIT 1",
            ("done" if undoing else "undone",),
        ).fetchone()
        if peek is None:
            return None
        scope = self._history_scope(peek[0])
        before = self._scope_summary(scope)
        with tx(self.conn):
            entry = history.undo(self.conn) if undoing else history.redo(self.conn)
            if entry is not None:
                bump_revision(self.conn)
        if entry is None:
            return None
        self._after_changes(entry.changes, undoing=undoing)
        after = self._scope_summary(scope)
        if before is None or after is None:
            self.tracker.mark_stale()
        else:
            self.tracker.apply_subtree(before, after)
        self.refresh_queues()
        return entry

    def _history_scope(self, hid: int) -> tuple[list[str], list[str]]:
        """(folders, files) whose effective states an entry can change."""
        folders, files = [], []
        for tbl, key, is_dir in self.conn.execute(
            "SELECT c.tbl, c.key, n.is_dir FROM history_changes c"
            " LEFT JOIN nodes n ON n.path = c.key WHERE c.history_id = ?",
            (hid,),
        ):
            if is_dir:
                if tbl != "nodes_flags":
                    folders.append(key)
            elif is_dir is not None:
                files.append(key)
        # Changes beneath a changed folder are covered by that folder's subtree.
        folders = sorted(set(folders))
        top = [f for f in folders if not any(o != f and is_within(f, o) for o in folders)]
        files = [f for f in set(files) if not any(is_within(f, o) for o in top)]
        for f in [*top, *list(files)]:
            files.extend(c for c in self._companions(f) if c not in files)
        return top, files

    def _scope_summary(self, scope: tuple[list[str], list[str]]) -> WalkSummary | None:
        folders, files = scope
        if len(folders) > 1:
            return None  # unusual; let the owner re-walk
        total = WalkSummary()
        for folder in folders:
            add_summary(total, subtree_summary(self.conn, self.rules, self.store, folder))
        for f in files:
            try:
                count_state(total, self.resolver.resolve(f), self._size(f))
            except KeyError:
                continue
        return total

    # Helpers ----------------------------------------------------------------------------------

    def _require_current(self) -> Item:
        if self.current is None:
            raise NoCurrentItem("no file is being triaged")
        return self.current

    def _folder(self, folder: str | None) -> str:
        folder = self.default_folder() if folder is None else folder
        if folder == "":
            raise FolderActionRefused("Folder actions don't apply to the project root.")
        if self._node_is_dir(folder) is not True:
            raise ValueError(f"not a folder in this project: {folder!r}")
        return folder

    def _parse(self, category: str | Sequence[str]) -> list[str]:
        text = category if isinstance(category, str) else ", ".join(category)
        parsed = parse_category(text)
        if not parsed.ok:
            raise InvalidCategory(parsed.issues[0].message)
        if not isinstance(category, str) and len(parsed.tags) != len(category):
            raise InvalidCategory("Tags can't contain commas.")
        return parsed.tags

    def _set_own_category(self, rec: history.Recorder, path: str, names: list[str]) -> None:
        inherited = self.resolver.dir_ctx(parent(path)).category
        if not names or _same(self.store.canonical(names), inherited):
            rec.set("node_categories", path, None)
            return
        cid, _ = self.store.get_or_create(names)
        rec.set("node_categories", path, {"category_id": cid})

    def _conflicting(self, folder: str, category: Category) -> list[str]:
        """Descendants with an own category that doesn't start with `category`."""
        if not category:
            return []
        want = tuple(t.casefold() for t in category)
        lo, hi = subtree_range(folder)
        out = []
        for p, cid in self.conn.execute(
            "SELECT path, category_id FROM node_categories WHERE path >= ? AND path < ?"
            " ORDER BY path",
            (lo, hi),
        ):
            names = tuple(t.casefold() for t in self.store.names(cid))
            if names[: len(want)] != want:
                out.append(p)
        return out

    def _after_changes(self, changes: Iterable[history.Change], *, undoing: bool) -> None:
        touched_dir = False
        for change in changes:
            old, new = (change.after, change.before) if undoing else (change.before, change.after)
            if change.tbl == "node_categories":
                if old is not None:
                    self.index.remove(old["category_id"])
                if new is not None:
                    self.index.add(new["category_id"])
            if change.tbl != "nodes_flags" and self._node_is_dir(change.key):
                touched_dir = True
        if touched_dir:
            self.resolver.invalidate()

    def _companions(self, path: str) -> list[str]:
        """AppleDouble `._name` sibling of a node, if present (it follows the node)."""
        if not path:
            return []
        sidecar = join(parent(path), "._" + basename(path))
        row = self.conn.execute("SELECT 1 FROM nodes WHERE path = ?", (sidecar,)).fetchone()
        return [sidecar] if row else []

    def _decision(self, path: str) -> str | None:
        row = self.conn.execute("SELECT action FROM decisions WHERE path = ?", (path,)).fetchone()
        return None if row is None else row[0]

    def _flags(self, path: str) -> tuple[int, int] | None:
        row = self.conn.execute(
            "SELECT flag_new, flag_changed FROM nodes WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def _node_is_dir(self, path: str) -> bool | None:
        row = self.conn.execute("SELECT is_dir FROM nodes WHERE path = ?", (path,)).fetchone()
        return None if row is None else bool(row[0])

    def _size_mtime(self, path: str) -> tuple[int | None, float | None]:
        row = self.conn.execute("SELECT size, mtime FROM nodes WHERE path = ?", (path,)).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def _size(self, path: str) -> int | None:
        return self._size_mtime(path)[0]

    def _banner(self, path: str) -> Banner:
        flags = self._flags(path)
        if path in self._ready and self._decision(path) == "deferred":
            return Banner.LOADED_SKIP
        if flags is not None and flags[0]:
            return Banner.NEW
        if flags is not None and flags[1] and self.settings.retriage_changed:
            return Banner.CHANGED
        return Banner.NONE

    def insert_new(self, paths: Iterable[str]) -> None:
        """Add newly flagged files to the new queue without a full refresh."""
        for p in paths:
            bisect.insort(self._new, (dfs_key(p, False), p))


def _decision_row(is_dir: bool, action: str, size: int | None, mtime: float | None) -> dict:
    return {
        "is_dir": int(is_dir),
        "action": action,
        "decided_at": time.time(),
        "seen_size": size,
        "seen_mtime": mtime,
    }


def _same(a: Category, b: Category | None) -> bool:
    return b is not None and tuple(t.casefold() for t in a) == tuple(t.casefold() for t in b)
