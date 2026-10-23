"""SMB passwords in the OS credential store (macOS Keychain, Windows Credential Manager,
Secret Service). Never in project state, never synced (docs/data-model.md §1)."""

import contextlib

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "Degreaser"


class CredentialStoreError(Exception):
    """The OS credential store is unavailable or refused the request."""


def get_password(project_uuid: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, project_uuid)
    except KeyringError as exc:
        raise CredentialStoreError(str(exc)) from exc


def set_password(project_uuid: str, password: str) -> None:
    try:
        keyring.set_password(SERVICE, project_uuid, password)
    except KeyringError as exc:
        raise CredentialStoreError(str(exc)) from exc


def delete_password(project_uuid: str) -> None:
    with contextlib.suppress(PasswordDeleteError, KeyringError):
        keyring.delete_password(SERVICE, project_uuid)
