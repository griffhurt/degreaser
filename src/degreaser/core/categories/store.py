"""Tags and categories in the project database (docs/data-model.md §2.3)."""

import sqlite3
from collections.abc import Sequence

from degreaser.core.categories.validate import parse_category
from degreaser.core.model import Category


class InvalidCategory(ValueError):
    pass


class CategoryStore:
    """Canonicalises tag names and maps category ids <-> ordered tag-name tuples.

    Categories are few, so they are cached in memory; the cache only grows (rows are never
    deleted), which keeps it valid across undo/redo.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._tag_names: dict[int, str] = {}
        self._tag_ids: dict[str, int] = {}  # casefolded name -> id
        self._categories: dict[int, Category] = {}
        self._category_ids: dict[tuple[int, ...], int] = {}
        self.reload()

    def reload(self) -> None:
        self._tag_names = dict(self.conn.execute("SELECT id, name FROM tags"))
        self._tag_ids = {name.casefold(): tid for tid, name in self._tag_names.items()}
        self._categories = {}
        self._category_ids = {}
        for cid, tag_ids in self.conn.execute("SELECT id, tag_ids FROM categories"):
            self._remember(cid, tuple(int(t) for t in tag_ids.split("/")))

    def _remember(self, cid: int, ids: tuple[int, ...]) -> None:
        self._category_ids[ids] = cid
        self._categories[cid] = tuple(self._tag_names[t] for t in ids)

    # Lookups --------------------------------------------------------------------------------

    def names(self, category_id: int) -> Category:
        return self._categories[category_id]

    def tag_id(self, name: str) -> int | None:
        return self._tag_ids.get(name.strip().casefold())

    def tag_name(self, tag_id: int) -> str:
        return self._tag_names[tag_id]

    def all_tags(self) -> dict[int, str]:
        return dict(self._tag_names)

    def canonical(self, names: Sequence[str]) -> Category:
        """Existing capitalisation for known tags; unknown tags as typed (trimmed)."""
        out = []
        for name in names:
            tid = self.tag_id(name)
            out.append(self._tag_names[tid] if tid is not None else name.strip())
        return tuple(out)

    def find(self, names: Sequence[str]) -> int | None:
        """Id of an existing category with exactly these tags (case-insensitive), if any."""
        ids = []
        for name in names:
            tid = self.tag_id(name)
            if tid is None:
                return None
            ids.append(tid)
        return self._category_ids.get(tuple(ids))

    def own_category(self, path: str) -> tuple[int, Category] | None:
        row = self.conn.execute(
            "SELECT category_id FROM node_categories WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else (row[0], self._categories[row[0]])

    # Writes ---------------------------------------------------------------------------------

    def get_or_create(self, names: Sequence[str]) -> tuple[int, Category]:
        """Validate, canonicalise and store a category. The caller owns the transaction."""
        parsed = parse_category(", ".join(names))
        if not parsed.ok or len(parsed.tags) != len(names) or not names:
            message = parsed.issues[0].message if parsed.issues else "Category is empty."
            raise InvalidCategory(message)
        ids = []
        for name in parsed.tags:
            tid = self.tag_id(name)
            if tid is None:
                tid = self.conn.execute("INSERT INTO tags(name) VALUES (?)", (name,)).lastrowid
                assert tid is not None
                self._tag_names[tid] = name
                self._tag_ids[name.casefold()] = tid
            ids.append(tid)
        key = tuple(ids)
        cid = self._category_ids.get(key)
        if cid is None:
            cid = self.conn.execute(
                "INSERT INTO categories(tag_ids) VALUES (?)", ("/".join(map(str, key)),)
            ).lastrowid
            assert cid is not None
            self._remember(cid, key)
        return cid, self._categories[cid]
