"""Triage engine: effective state, traversal, actions and undo (docs/triage-engine.md)."""

from degreaser.core.triage.resolver import DirCtx, Resolver
from degreaser.core.triage.session import (
    ActionResult,
    Advance,
    Banner,
    DeleteFolderCheck,
    FolderActionRefused,
    Item,
    ItemSource,
    Mode,
    NoCurrentItem,
    TagFolderCheck,
    TriageSession,
    bump_revision,
    sweep_first,
)
from degreaser.core.triage.traversal import Lookahead, NeedsListing, Traversal
from degreaser.core.triage.walk import SummaryTracker, full_walk, subtree_summary

__all__ = [
    "ActionResult",
    "Advance",
    "Banner",
    "DeleteFolderCheck",
    "DirCtx",
    "FolderActionRefused",
    "Item",
    "ItemSource",
    "Lookahead",
    "Mode",
    "NeedsListing",
    "NoCurrentItem",
    "Resolver",
    "SummaryTracker",
    "TagFolderCheck",
    "Traversal",
    "TriageSession",
    "bump_revision",
    "full_walk",
    "subtree_summary",
    "sweep_first",
]
