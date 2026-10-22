"""`.degreaser/catalog.json` (docs/data-model.md §4)."""

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from degreaser.core.model import Category
from degreaser.core.project import DEGREASER_DIR
from degreaser.core.sources.base import FileSource, is_within
from degreaser.core.sources.errors import NotFound

log = logging.getLogger(__name__)

CATALOG_PATH = f"{DEGREASER_DIR}/catalog.json"
FORMAT = "degreaser-catalog"
VERSION = 1


def read_catalog(source: FileSource) -> dict[str, Any] | None:
    """The catalog currently on the source, or None if missing or unreadable."""
    try:
        with source.open_read(CATALOG_PATH) as f:
            data = json.loads(f.read().decode("utf-8"))
    except NotFound:
        return None
    except ValueError, UnicodeDecodeError:
        log.warning("ignoring unreadable %s", CATALOG_PATH)
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        log.warning("ignoring %s with unexpected format", CATALOG_PATH)
        return None
    return data


def build_catalog(
    existing: Mapping[str, Any] | None,
    files: Mapping[str, Category],
    folders: Mapping[str, Category],
    *,
    project_name: str,
    is_known: Callable[[str], bool],
    removed: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge the project's current categories into the existing catalog.

    The catalog is cumulative: entries for paths this project no longer knows about are
    kept (they may predate this device's index), except under paths this commit deleted.
    Paths the project does know about are replaced by `files` / `folders`, so removing a
    category drops its entry.
    """
    removed = list(removed)

    def keep_old(path: str) -> bool:
        return not is_known(path) and not any(is_within(path, r) for r in removed)

    def merge(key: str, current: Mapping[str, Category]) -> dict[str, list[str]]:
        old = (existing or {}).get(key) or {}
        out = {p: list(c) for p, c in old.items() if isinstance(c, list) and keep_old(p)}
        out.update({p: list(c) for p, c in current.items()})
        return dict(sorted(out.items()))

    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "format": FORMAT,
        "version": VERSION,
        "project": project_name,
        "generated_at": stamp,
        "folders": merge("folders", folders),
        "files": merge("files", files),
    }


def write_catalog(source: FileSource, catalog: Mapping[str, Any]) -> None:
    source.makedirs(DEGREASER_DIR)
    data = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    source.write_file_atomic(CATALOG_PATH, data.encode("utf-8"))
