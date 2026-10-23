"""Whole-tree and subtree effective-state walks, and incremental summaries (§8).

The walk streams `nodes` in id order with decisions and categories pre-loaded into dicts
(spikes/resolver: ~5 s for 2M files). Ids normally put parents before children; when they
don't (e.g. after path rewrites), children are buffered until their parent has been seen.
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, fields

from degreaser.core.categories.store import CategoryStore
from degreaser.core.model import (
    Category,
    DeleteTarget,
    Effective,
    EffectiveState,
    Origin,
    WalkResult,
    WalkSummary,
)
from degreaser.core.rules.ruleset import RuleSet
from degreaser.core.triage.order import companion_of, is_appledouble
from degreaser.core.triage.resolver import (
    ROOT_CTX,
    DirCtx,
    Resolver,
    appledouble_state,
    child_ctx,
    file_state,
)

_Row = tuple[int, int | None, str, str, int, str, int | None, float | None]


def count_state(
    summary: WalkSummary, state: EffectiveState, size: int | None, sign: int = 1
) -> None:
    """Add (sign=1) or remove (sign=-1) one file's contribution to a summary."""
    if state.origin is Origin.EXCLUDED:
        return
    summary.files_known += sign
    match state.action:
        case Effective.UNTRIAGED:
            summary.files_untriaged += sign
        case Effective.KEEP:
            summary.files_keep += sign
            if state.category is not None:
                summary.files_categorised += sign
        case Effective.DELETE:
            summary.files_delete += sign
            summary.bytes_delete += sign * (size or 0)
        case Effective.SKIP:
            summary.files_skip += sign
        case Effective.DEFERRED:
            summary.files_deferred += sign


def add_summary(target: WalkSummary, other: WalkSummary, sign: int = 1) -> None:
    for f in fields(WalkSummary):
        setattr(target, f.name, getattr(target, f.name) + sign * getattr(other, f.name))


@dataclass(slots=True)
class _Pending:
    """An AppleDouble file resolved after the walk, once its companion's state is known."""

    path: str
    companion: str
    ctx: DirCtx
    action: str | None
    size: int | None
    mtime: float | None


def duplicate_deletions(
    conn: sqlite3.Connection, rules: RuleSet, store: CategoryStore
) -> dict[str, str]:
    """With the project's delete-duplicates rule on: file → the kept copy it duplicates.
    Empty when the rule is off. (Whether the file itself is decided or in a kept folder is
    left to file_state.)"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
    if row is None or not json.loads(row[0]).get("delete_duplicates"):
        return {}
    from degreaser.core.duplicates.index import DuplicateIndex

    resolver = Resolver(conn, rules, store)
    resolver.duplicates = DuplicateIndex.load(conn)
    out = {}
    for path in resolver.duplicates.by_path:
        if (kept := resolver.kept_copy(path)) is not None:
            out[path] = kept
    return out


def walk(
    conn: sqlite3.Connection,
    rules: RuleSet,
    store: CategoryStore,
    subtree: str = "",
    *,
    collect: bool = True,
    on_progress: Callable[[int], None] | None = None,
) -> WalkResult:
    """Walk the whole tree (subtree="") or one folder's subtree.

    collect=False skips delete targets and catalog maps (summary only, for incremental
    updates). For a subtree walk, figures cover only nodes strictly beneath the folder.
    """
    result = WalkResult()
    kept_copies = duplicate_deletions(conn, rules, store)
    summary = result.summary
    if subtree:
        lo, hi = subtree + "/", subtree + "0"
        rng = " WHERE path >= ? AND path < ?"
        args: tuple[str, ...] = (lo, hi)
        base_ctx = Resolver(conn, rules, store).dir_ctx(subtree)
    else:
        rng, args, base_ctx = "", (), ROOT_CTX

    decisions: dict[str, str] = dict(conn.execute("SELECT path, action FROM decisions" + rng, args))
    own_categories: dict[str, Category] = {
        path: store.names(cid)
        for path, cid in conn.execute("SELECT path, category_id FROM node_categories" + rng, args)
    }
    # Companions of AppleDouble files: their states are recorded during the walk.
    wanted: set[str] = {
        companion_of(path, name)
        for path, name in conn.execute(
            "SELECT path, name FROM nodes"
            + (rng + " AND" if rng else " WHERE")
            + " is_dir = 0 AND kind = 'file' AND substr(name, 1, 2) = '._' AND length(name) > 2",
            args,
        )
    }
    companion_states: dict[str, EffectiveState] = {}
    pending: list[_Pending] = []

    ctxs: dict[int, DirCtx] = {}
    waiting: dict[int, list[_Row]] = defaultdict(list)
    if subtree:
        root_row = conn.execute("SELECT id FROM nodes WHERE path = ?", (subtree,)).fetchone()
        if root_row is None:
            return result
        ctxs[root_row[0]] = base_ctx

    def visit(row: _Row) -> None:
        node_id, parent_id, path, name, is_dir, kind, size, mtime = row
        if parent_id is None:
            ctxs[node_id] = ROOT_CTX
            return
        pctx = ctxs[parent_id]
        if pctx.excluded:
            if is_dir:
                ctxs[node_id] = DirCtx(path, excluded=True)
            return
        action = decisions.get(path)
        if is_dir:
            ctx = child_ctx(conn, rules, pctx, path, name, action, own_categories.get(path))
            ctxs[node_id] = ctx
            if path in wanted:
                companion_states[path] = ctx.state()
            if collect and not ctx.excluded:
                if ctx.whole_delete and not pctx.whole_delete:
                    result.delete_targets.append(DeleteTarget(path, True, None, mtime))
                if path in own_categories and ctx.state().action is Effective.KEEP:
                    result.catalog_folders[path] = own_categories[path]
            return
        if is_appledouble(name, kind):
            pending.append(_Pending(path, companion_of(path, name), pctx, action, size, mtime))
            return
        state = file_state(
            rules, pctx, path, name, action, own_categories.get(path), kept_copies.get(path)
        )
        if path in wanted:
            companion_states[path] = state
        count_state(summary, state, size)
        if collect:
            if state.action is Effective.DELETE and not pctx.whole_delete:
                keep = state.decided_by if state.origin is Origin.DUPLICATE else None
                result.delete_targets.append(DeleteTarget(path, False, size, mtime, keep))
            elif state.action is Effective.KEEP and state.category is not None:
                result.catalog_files[path] = state.category

    def visit_with_children(row: _Row) -> None:
        stack = [row]
        while stack:
            current = stack.pop()
            visit(current)
            if current[4] and current[0] in waiting:
                stack.extend(waiting.pop(current[0]))

    for seen, row in enumerate(
        conn.execute(
            "SELECT id, parent_id, path, name, is_dir, kind, size, mtime FROM nodes"
            + rng
            + " ORDER BY id",
            args,
        ),
        start=1,
    ):
        parent_id = row[1]
        if parent_id is not None and parent_id not in ctxs:
            waiting[parent_id].append(row)
        else:
            visit_with_children(row)
        if on_progress is not None and seen % 100_000 == 0:
            on_progress(seen)

    for item in pending:
        state = appledouble_state(
            item.ctx, item.path, item.action, item.companion, companion_states.get(item.companion)
        )
        if state.origin is Origin.EXCLUDED:
            continue
        count_state(summary, state, item.size)
        if collect and state.action is Effective.DELETE and not item.ctx.whole_delete:
            result.delete_targets.append(DeleteTarget(item.path, False, item.size, item.mtime))
    # Anything still waiting has no parent in the tree (shouldn't happen); it's ignored.
    return result


def full_walk(
    conn: sqlite3.Connection,
    rules: RuleSet,
    store: CategoryStore,
    on_progress: Callable[[int], None] | None = None,
) -> WalkResult:
    """Everything the status bar, commit planner and catalog need. Run it on a worker."""
    return walk(conn, rules, store, on_progress=on_progress)


def subtree_summary(
    conn: sqlite3.Connection, rules: RuleSet, store: CategoryStore, folder: str
) -> WalkSummary:
    """Summary of the files beneath `folder` (for before/after deltas of folder actions)."""
    return walk(conn, rules, store, folder, collect=False).summary


class SummaryTracker:
    """Status-bar figures: one full walk, then incremental deltas (§8).

    While no full walk has completed, `summary` is None and deltas are dropped; mark_stale()
    asks the owner to run another full walk (e.g. a delta arrived mid-walk).
    """

    def __init__(self) -> None:
        self.summary: WalkSummary | None = None
        self.stale = True
        self._generation = 0

    def begin_walk(self) -> int:
        """Call before starting a full walk; pass the token to finish_walk()."""
        self._generation += 1
        return self._generation

    def finish_walk(self, token: int, summary: WalkSummary) -> None:
        self.summary = summary
        # A delta that arrived during the walk may or may not be reflected in it.
        self.stale = token != self._generation

    def mark_stale(self) -> None:
        self._generation += 1
        self.stale = True

    def apply_file(self, before: EffectiveState, after: EffectiveState, size: int | None) -> None:
        if self.summary is None:
            self.mark_stale()
            return
        self._generation += 1
        count_state(self.summary, before, size, -1)
        count_state(self.summary, after, size, 1)

    def apply_subtree(self, before: WalkSummary, after: WalkSummary) -> None:
        if self.summary is None:
            self.mark_stale()
            return
        self._generation += 1
        add_summary(self.summary, before, -1)
        add_summary(self.summary, after, 1)
