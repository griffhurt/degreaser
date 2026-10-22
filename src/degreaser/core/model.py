"""Shared value types for decisions, effective state and walks (docs/triage-engine.md §2)."""

from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    """A stored decision (decisions.action)."""

    KEEP = "keep"
    DELETE = "delete"
    SKIP = "skip"
    DEFERRED = "deferred"


class Effective(StrEnum):
    """What will happen to a node at commit, after folder marks and rules."""

    UNTRIAGED = "untriaged"
    KEEP = "keep"
    DELETE = "delete"
    SKIP = "skip"
    DEFERRED = "deferred"


class Origin(StrEnum):
    """Why a node has its effective action."""

    NONE = "none"  # untriaged
    DIRECT = "direct"  # the node's own decision
    FOLDER = "folder"  # an ancestor folder's decision
    RULE = "rule"  # an always-delete / always-keep rule
    COMPANION = "companion"  # AppleDouble ._name following its companion (D20)
    EXCLUDED = "excluded"  # Degreaser's own .degreaser/ folder: never triaged or touched


Category = tuple[str, ...]  # ordered tag names, broad → specific


@dataclass(frozen=True, slots=True)
class EffectiveState:
    action: Effective
    origin: Origin
    decided_by: str | None = None  # path of the deciding node, or the rule pattern
    category: Category | None = None
    category_from: str | None = None  # path whose own category this is


@dataclass(slots=True)
class WalkSummary:
    """Status-bar and confirmation figures over known files."""

    files_known: int = 0
    files_untriaged: int = 0
    files_keep: int = 0
    files_categorised: int = 0
    files_delete: int = 0
    bytes_delete: int = 0
    files_skip: int = 0
    files_deferred: int = 0

    @property
    def files_triaged(self) -> int:
        return self.files_known - self.files_untriaged


@dataclass(frozen=True, slots=True)
class DeleteTarget:
    """A topmost node to delete at commit: a whole folder, or a file whose parent stays."""

    path: str
    is_dir: bool
    size: int | None
    mtime: float | None


@dataclass(slots=True)
class WalkResult:
    summary: WalkSummary = field(default_factory=WalkSummary)
    delete_targets: list[DeleteTarget] = field(default_factory=list)
    # Effective categories of kept files, and folders' own categories (catalog.json).
    catalog_files: dict[str, Category] = field(default_factory=dict)
    catalog_folders: dict[str, Category] = field(default_factory=dict)
