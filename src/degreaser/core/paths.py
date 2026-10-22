"""App data directory layout (docs/architecture.md §8)."""

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "Degreaser"


def app_data_dir() -> Path:
    # Tests and portable setups can redirect everything with DEGREASER_DATA_DIR.
    override = os.environ.get("DEGREASER_DATA_DIR")
    return Path(override) if override else Path(user_data_dir(APP_NAME, appauthor=False))


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def projects_dir() -> Path:
    return app_data_dir() / "projects"


def project_dir(project_uuid: str) -> Path:
    return projects_dir() / project_uuid


def project_db_path(project_uuid: str) -> Path:
    return project_dir(project_uuid) / "project.sqlite"


def project_cache_dir(project_uuid: str) -> Path:
    return project_dir(project_uuid) / "cache"


def logs_dir() -> Path:
    return app_data_dir() / "logs"
