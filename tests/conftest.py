import os
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

# Run Qt headless unless a platform was chosen explicitly.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own app data dir, so nothing touches the real one."""
    data_dir = tmp_path / "appdata"
    monkeypatch.setenv("DEGREASER_DATA_DIR", str(data_dir))
    return data_dir


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        if self.passwords.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    """Never touch the real OS credential store from tests."""
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    return backend
