"""Effective state of nodes (docs/triage-engine.md §2).

Precedence for a file F (first match wins):
  1. F or an ancestor folder has a `delete` decision      -> DELETE (direct / folder)
  2. F has a direct decision (keep / skip / deferred)      -> that   (direct)
  3. always-delete rule matches F or an ancestor           -> DELETE (rule)
  3b. with the duplicates rule on, F is identical to a file the user kept (directly or by
      folder) and F isn't in a kept folder                 -> DELETE (duplicate)
  4. always-keep rule matches F or an ancestor             -> KEEP   (rule)
  5. nearest ancestor folder decision is `keep`            -> KEEP   (folder)
  6. otherwise                                             -> UNTRIAGED
AppleDouble `._x` files follow their companion `x` after steps 1-2; orphans are junk (D20).
A kept file's category is its own, else its nearest categorised ancestor's.
"""

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from degreaser.core.categories.store import CategoryStore
from degreaser.core.model import Category, Effective, EffectiveState, Origin
from degreaser.core.rules.ruleset import ORPHAN_APPLEDOUBLE, RuleMatch, RuleSet
from degreaser.core.sources.base import basename, parent, subtree_range
from degreaser.core.triage.order import companion_of, is_appledouble, is_excluded

if TYPE_CHECKING:
    from degreaser.core.duplicates.index import DuplicateIndex

EXCLUDED_STATE = EffectiveState(Effective.KEEP, Origin.EXCLUDED)
UNTRIAGED_STATE = EffectiveState(Effective.UNTRIAGED, Origin.NONE)

_DIRECT = {
    "keep": Effective.KEEP,
    "skip": Effective.SKIP,
    "deferred": Effective.DEFERRED,
}


@dataclass(frozen=True, slots=True)
class DirCtx:
    """What a folder passes down to everything beneath it."""

    path: str
    excluded: bool = False
    deleted_by: str | None = None  # folder (self or ancestor) with a delete decision
    rule_delete: RuleMatch | None = None  # always-delete match on self or an ancestor
    rule_keep: RuleMatch | None = None
    kept_by: str | None = None  # nearest folder (self or ancestor) with a keep decision
    own_action: str | None = None  # this folder's own decision
    category: Category | None = None  # own or inherited
    category_from: str | None = None
    # The whole subtree goes in one delete operation: deleted by a decision, or rule-deleted
    # with no file inside holding a non-delete decision.
    whole_delete: bool = False

    @property
    def prunes(self) -> bool:
        """No file beneath can be UNTRIAGED (§1.2)."""
        return bool(
            self.excluded or self.deleted_by or self.rule_delete or self.rule_keep or self.kept_by
        )

    def state(self) -> EffectiveState:
        """The folder's own effective state."""
        if self.excluded:
            return EXCLUDED_STATE
        if self.deleted_by is not None:
            origin = Origin.DIRECT if self.deleted_by == self.path else Origin.FOLDER
            return EffectiveState(Effective.DELETE, origin, self.deleted_by)
        cat, cat_from = self.category, self.category_from
        if self.own_action == "keep":
            return EffectiveState(Effective.KEEP, Origin.DIRECT, self.path, cat, cat_from)
        if self.rule_delete is not None:
            return EffectiveState(Effective.DELETE, Origin.RULE, self.rule_delete.pattern)
        if self.rule_keep is not None:
            return EffectiveState(
                Effective.KEEP, Origin.RULE, self.rule_keep.pattern, cat, cat_from
            )
        if self.kept_by is not None:
            return EffectiveState(Effective.KEEP, Origin.FOLDER, self.kept_by, cat, cat_from)
        return UNTRIAGED_STATE


ROOT_CTX = DirCtx(path="")


def has_protected_files(conn: sqlite3.Connection, folder: str) -> bool:
    """Any file beneath `folder` with a keep/skip/deferred decision (blocks whole-folder delete)."""
    lo, hi = subtree_range(folder)
    return (
        conn.execute(
            "SELECT 1 FROM decisions WHERE path >= ? AND path < ? AND is_dir = 0"
            " AND action <> 'delete' LIMIT 1",
            (lo, hi),
        ).fetchone()
        is not None
    )


def child_ctx(
    conn: sqlite3.Connection,
    rules: RuleSet,
    parent_ctx: DirCtx,
    path: str,
    name: str,
    action: str | None,
    own_category: Category | None,
) -> DirCtx:
    """Context of folder `path` given its parent's context and its own decision/category."""
    if parent_ctx.excluded or is_excluded(path):
        return DirCtx(path, excluded=True)
    if parent_ctx.deleted_by is not None or action == "delete":
        return DirCtx(
            path,
            deleted_by=parent_ctx.deleted_by or path,
            own_action=action,
            whole_delete=True,
        )
    rule_delete = parent_ctx.rule_delete
    if rule_delete is None and (pattern := rules.match_delete(path, name, True)) is not None:
        rule_delete = RuleMatch("always_delete", pattern, path)
    rule_keep = parent_ctx.rule_keep
    if rule_keep is None and (pattern := rules.match_keep(path, name, True)) is not None:
        rule_keep = RuleMatch("always_keep", pattern, path)
    if own_category is not None:
        category, category_from = own_category, path
    else:
        category, category_from = parent_ctx.category, parent_ctx.category_from
    whole = False
    if rule_delete is not None:
        whole = parent_ctx.whole_delete or not has_protected_files(conn, path)
    return DirCtx(
        path,
        rule_delete=rule_delete,
        rule_keep=rule_keep,
        kept_by=path if action == "keep" else parent_ctx.kept_by,
        own_action=action,
        category=category,
        category_from=category_from,
        whole_delete=whole,
    )


def file_state(
    rules: RuleSet,
    ctx: DirCtx,
    path: str,
    name: str,
    action: str | None,
    own_category: Category | None,
    kept_copy: str | None = None,
) -> EffectiveState:
    """Effective state of a non-AppleDouble file, given its folder's context. `kept_copy` is
    an identical file the user kept, when the delete-duplicates rule is on."""
    if ctx.excluded:
        return EXCLUDED_STATE
    if ctx.deleted_by is not None:
        return EffectiveState(Effective.DELETE, Origin.FOLDER, ctx.deleted_by)
    if action == "delete":
        return EffectiveState(Effective.DELETE, Origin.DIRECT, path)
    if own_category is not None:
        cat, cat_from = own_category, path
    else:
        cat, cat_from = ctx.category, ctx.category_from
    if action is not None:
        effective = _DIRECT[action]
        if effective is Effective.KEEP:
            return EffectiveState(effective, Origin.DIRECT, path, cat, cat_from)
        return EffectiveState(effective, Origin.DIRECT, path)
    if ctx.rule_delete is not None:
        return EffectiveState(Effective.DELETE, Origin.RULE, ctx.rule_delete.pattern)
    if (pattern := rules.match_delete(path, name, False)) is not None:
        return EffectiveState(Effective.DELETE, Origin.RULE, pattern)
    if kept_copy is not None and ctx.kept_by is None:
        # Beats always-keep rules (a second copy is still redundant), loses to kept folders.
        return EffectiveState(Effective.DELETE, Origin.DUPLICATE, kept_copy)
    if ctx.rule_keep is not None:
        return EffectiveState(Effective.KEEP, Origin.RULE, ctx.rule_keep.pattern, cat, cat_from)
    if (pattern := rules.match_keep(path, name, False)) is not None:
        return EffectiveState(Effective.KEEP, Origin.RULE, pattern, cat, cat_from)
    if ctx.kept_by is not None:
        return EffectiveState(Effective.KEEP, Origin.FOLDER, ctx.kept_by, cat, cat_from)
    return UNTRIAGED_STATE


def appledouble_state(
    ctx: DirCtx,
    path: str,
    action: str | None,
    companion_path: str,
    companion: EffectiveState | None,
) -> EffectiveState:
    """Effective state of `._x` given its companion's state (None = orphan)."""
    if ctx.excluded:
        return EXCLUDED_STATE
    if ctx.deleted_by is not None:
        return EffectiveState(Effective.DELETE, Origin.FOLDER, ctx.deleted_by)
    if action == "delete":
        return EffectiveState(Effective.DELETE, Origin.DIRECT, path)
    if action is not None:
        return EffectiveState(_DIRECT[action], Origin.DIRECT, path)
    if companion is None:
        return EffectiveState(Effective.DELETE, Origin.RULE, ORPHAN_APPLEDOUBLE)
    return EffectiveState(companion.action, Origin.COMPANION, companion_path)


class Resolver:
    """Effective state of single nodes, memoising folder contexts.

    Call invalidate() after any folder decision, folder category or rule change; file-level
    changes don't affect folder contexts.
    """

    def __init__(self, conn: sqlite3.Connection, rules: RuleSet, store: CategoryStore) -> None:
        self.conn = conn
        self.rules = rules
        self.store = store
        self._ctx: dict[str, DirCtx] = {"": ROOT_CTX}
        # Set when the delete-duplicates rule is on (TriageSession keeps it current).
        self.duplicates: DuplicateIndex | None = None

    def invalidate(self, rules: RuleSet | None = None) -> None:
        if rules is not None:
            self.rules = rules
        self._ctx = {"": ROOT_CTX}

    def _decision_and_category(self, path: str) -> tuple[str | None, Category | None]:
        row = self.conn.execute(
            "SELECT (SELECT action FROM decisions WHERE path = ?1),"
            " (SELECT category_id FROM node_categories WHERE path = ?1)",
            (path,),
        ).fetchone()
        return row[0], None if row[1] is None else self.store.names(row[1])

    def dir_ctx(self, path: str) -> DirCtx:
        ctx = self._ctx.get(path)
        if ctx is not None:
            return ctx
        parent_ctx = self.dir_ctx(parent(path))
        action, category = self._decision_and_category(path)
        ctx = child_ctx(self.conn, self.rules, parent_ctx, path, basename(path), action, category)
        self._ctx[path] = ctx
        return ctx

    def kept_copy(self, path: str) -> str | None:
        """With the duplicates rule on: an identical file the user kept, directly or by
        folder. Those keeps never depend on duplicates, so this can't loop."""
        if self.duplicates is None:
            return None
        for other in self.duplicates.copies(path):
            try:
                state = self.resolve(other, duplicates=False)
            except KeyError:
                continue
            if state.action is Effective.KEEP and state.origin in (Origin.DIRECT, Origin.FOLDER):
                return other
        return None

    def resolve(self, path: str, *, duplicates: bool = True) -> EffectiveState:
        """Effective state of any known node. Raises KeyError if the path isn't in the tree."""
        row = self.conn.execute(
            "SELECT is_dir, kind, name FROM nodes WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            raise KeyError(path)
        is_dir, kind, name = row
        if is_dir:
            return self.dir_ctx(path).state() if path else UNTRIAGED_STATE
        ctx = self.dir_ctx(parent(path))
        action, category = self._decision_and_category(path)
        if is_appledouble(name, kind):
            companion_path = companion_of(path, name)
            exists = (
                self.conn.execute(
                    "SELECT 1 FROM nodes WHERE path = ?", (companion_path,)
                ).fetchone()
                is not None
            )
            companion = self.resolve(companion_path) if exists else None
            return appledouble_state(ctx, path, action, companion_path, companion)
        kept = self.kept_copy(path) if duplicates and action is None else None
        return file_state(self.rules, ctx, path, name, action, category, kept)

    def is_untriaged_candidate(self, ctx: DirCtx, path: str, name: str, kind: str) -> bool:
        """For a file with no direct decision in an unpruned folder: is it UNTRIAGED?"""
        if is_appledouble(name, kind):
            return False  # hidden (follows its companion) or orphan junk
        return (
            self.rules.match_delete(path, name, False) is None
            and self.rules.match_keep(path, name, False) is None
            and self.kept_copy(path) is None
        )
