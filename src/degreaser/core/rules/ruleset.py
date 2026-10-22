"""Per-project always-delete / always-keep rules (docs/triage-engine.md §4).

Patterns use gitignore (gitwildmatch) syntax against the project-relative path. Matching is
split for speed (spikes/resolver): patterns that only look at a name (no inner '/') become
one combined regex over the node's name; anything else goes through pathspec on the path.
A folder match covers its whole subtree, so walks test each node once and inherit the rest.
"""

import fnmatch
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import pathspec

from degreaser.core.sources.base import ancestors, basename

RuleKind = Literal["always_delete", "always_keep"]

# Pseudo-pattern reported for orphan AppleDouble files (D20).
ORPHAN_APPLEDOUBLE = "._* (orphan AppleDouble)"


@dataclass(frozen=True, slots=True)
class Rule:
    id: int | None
    kind: RuleKind
    pattern: str
    enabled: bool = True
    position: int = 0


@dataclass(frozen=True, slots=True)
class RuleMatch:
    kind: RuleKind
    pattern: str
    path: str  # the node the pattern matched: the node itself or an ancestor folder


def validate_pattern(pattern: str) -> str | None:
    """Return an error message, or None if the pattern is usable."""
    if not pattern.strip():
        return "Pattern is empty."
    if pattern.startswith("!"):
        return "Negated patterns (!) aren't supported; use the other list instead."
    if pattern.strip("/") == "":
        return "Pattern would match the whole project."
    try:
        pathspec.PathSpec.from_lines("gitignore", [pattern])
    except Exception as exc:
        return f"Invalid pattern: {exc}"
    return None


def _name_only(pattern: str) -> bool:
    body = pattern[:-1] if pattern.endswith("/") else pattern
    # Backslash escapes and '**' mean something different to fnmatch; let pathspec handle them.
    return "/" not in body and "\\" not in body and "**" not in body and body != ""


class _KindMatcher:
    """All enabled patterns of one kind."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self.patterns = list(patterns)
        any_parts: list[str] = []
        dir_parts: list[str] = []
        self._any_names: list[str] = []
        self._dir_names: list[str] = []
        self._path_specs: list[tuple[str, pathspec.PathSpec]] = []
        for pattern in self.patterns:
            if _name_only(pattern):
                dir_only = pattern.endswith("/")
                body = pattern[:-1] if dir_only else pattern
                target = dir_parts if dir_only else any_parts
                names = self._dir_names if dir_only else self._any_names
                target.append(f"(?P<p{len(names)}>{fnmatch.translate(body)})")
                names.append(pattern)
            else:
                self._path_specs.append(
                    (pattern, pathspec.PathSpec.from_lines("gitignore", [pattern]))
                )
        self._any_re = re.compile("|".join(any_parts)) if any_parts else None
        self._dir_re = re.compile("|".join(dir_parts)) if dir_parts else None

    def match(self, path: str, name: str, is_dir: bool) -> str | None:
        """The first pattern matching this node itself (not its ancestors), if any."""
        if self._any_re is not None and (m := self._any_re.match(name)):
            return self._any_names[int(m.lastgroup[1:])]  # type: ignore[index]
        if is_dir and self._dir_re is not None and (m := self._dir_re.match(name)):
            return self._dir_names[int(m.lastgroup[1:])]  # type: ignore[index]
        if self._path_specs:
            probe = path + "/" if is_dir else path
            for pattern, spec in self._path_specs:
                if spec.match_file(probe):
                    return pattern
        return None


class RuleSet:
    """Compiled enabled rules. Immutable; reload after edits."""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules = sorted(rules, key=lambda r: r.position)
        enabled = [r for r in self.rules if r.enabled]
        self._delete = _KindMatcher([r.pattern for r in enabled if r.kind == "always_delete"])
        self._keep = _KindMatcher([r.pattern for r in enabled if r.kind == "always_keep"])

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> RuleSet:
        return cls(load_rules(conn))

    @classmethod
    def empty(cls) -> RuleSet:
        return cls([])

    def match_delete(self, path: str, name: str, is_dir: bool) -> str | None:
        """Always-delete pattern matching this node itself (not ancestors)."""
        return self._delete.match(path, name, is_dir)

    def match_keep(self, path: str, name: str, is_dir: bool) -> str | None:
        return self._keep.match(path, name, is_dir)

    def match(self, path: str, is_dir: bool) -> RuleMatch | None:
        """Rule deciding a node, considering its ancestors too. Always-delete beats always-keep.

        Walks and traversal should not use this per node; they inherit folder matches instead.
        """
        if path == "":
            return None
        chain = [(a, True) for a in ancestors(path) if a != ""] + [(path, is_dir)]
        for p, d in chain:
            if pattern := self.match_delete(p, basename(p), d):
                return RuleMatch("always_delete", pattern, p)
        for p, d in chain:
            if pattern := self.match_keep(p, basename(p), d):
                return RuleMatch("always_keep", pattern, p)
        return None


# Persistence -------------------------------------------------------------------------------


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    return [
        Rule(row[0], row[1], row[2], bool(row[3]), row[4])
        for row in conn.execute(
            "SELECT id, kind, pattern, enabled, position FROM rules ORDER BY position, id"
        )
    ]


def replace_rules(conn: sqlite3.Connection, rules: Sequence[Rule]) -> None:
    """Replace the whole rules table (the rules editor saves its list at once).

    The caller owns the transaction and should bump the state revision.
    """
    conn.execute("DELETE FROM rules")
    conn.executemany(
        "INSERT INTO rules(kind, pattern, enabled, position) VALUES (?, ?, ?, ?)",
        [(r.kind, r.pattern, int(r.enabled), i) for i, r in enumerate(rules)],
    )


def count_matches(conn: sqlite3.Connection, pattern: str) -> tuple[int, int]:
    """(files, bytes) that one pattern covers, directly or through a matched folder.

    For the rules editor's live counts. O(nodes); run it off the UI thread.
    """
    matcher = _KindMatcher([pattern])
    matched_dirs: set[int] = set()
    files = total = 0
    for node_id, parent_id, path, name, is_dir, size in conn.execute(
        "SELECT id, parent_id, path, name, is_dir, size FROM nodes ORDER BY id"
    ):
        if parent_id is None:
            continue
        hit = parent_id in matched_dirs or matcher.match(path, name, bool(is_dir)) is not None
        if is_dir:
            if hit:
                matched_dirs.add(node_id)
        elif hit:
            files += 1
            total += size or 0
    return files, total
