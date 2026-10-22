"""In-memory trie of categories for hierarchical autocomplete (docs/triage-engine.md §7.4)."""

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from degreaser.core.categories.store import CategoryStore


@dataclass
class _TrieNode:
    weight: int = 0
    children: dict[int, _TrieNode] = field(default_factory=dict)


class CategoryIndex:
    """Usage-weighted trie of own categories.

    A trie node's weight is how many nodes' own categories pass through it; per-tag usage is
    how many own categories contain the tag. Keep it current with add()/remove() as own
    categories change (the triage session does this for actions, undo and redo).
    """

    def __init__(self, store: CategoryStore) -> None:
        self.store = store
        self._root = _TrieNode()
        self._usage: Counter[int] = Counter()

    @classmethod
    def build(cls, conn: sqlite3.Connection, store: CategoryStore) -> CategoryIndex:
        index = cls(store)
        for cid, count in conn.execute(
            "SELECT category_id, count(*) FROM node_categories GROUP BY category_id"
        ):
            index._adjust(store.names(cid), count)
        return index

    def add(self, category_id: int, count: int = 1) -> None:
        self._adjust(self.store.names(category_id), count)

    def remove(self, category_id: int, count: int = 1) -> None:
        self._adjust(self.store.names(category_id), -count)

    def _adjust(self, names: Sequence[str], delta: int) -> None:
        node = self._root
        for name in names:
            tid = self.store.tag_id(name)
            assert tid is not None
            node = node.children.setdefault(tid, _TrieNode())
            node.weight += delta
            self._usage[tid] += delta

    def usage(self, name: str) -> int:
        tid = self.store.tag_id(name)
        return 0 if tid is None else self._usage[tid]

    def candidates(self, preceding: Sequence[str], fragment: str) -> list[str]:
        """Completions for the tag being typed, best first (canonical capitalisation).

        Contextual candidates (tags that followed the same preceding tags before) come first,
        then every other tag, each group by usage then alphabetically. Tags already used
        earlier in this category are left out, since a category can't repeat a tag.
        """
        prefix = fragment.strip().casefold()
        exclude = {tid for name in preceding if (tid := self.store.tag_id(name)) is not None}
        tags = self.store.all_tags()

        def wanted(tid: int) -> bool:
            return tid not in exclude and tags[tid].casefold().startswith(prefix)

        contextual: list[int] = []
        node: _TrieNode | None = self._root
        for name in preceding:
            tid = self.store.tag_id(name)
            node = None if tid is None or node is None else node.children.get(tid)
            if node is None:
                break
        if node is not None:
            contextual = sorted(
                (tid for tid, child in node.children.items() if child.weight > 0 and wanted(tid)),
                key=lambda tid: (-node.children[tid].weight, tags[tid].casefold()),
            )
        seen = set(contextual)
        global_ = sorted(
            (tid for tid in tags if tid not in seen and wanted(tid)),
            key=lambda tid: (-self._usage[tid], tags[tid].casefold()),
        )
        return [tags[tid] for tid in contextual + global_]
