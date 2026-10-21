import json
import os
import sys
from pathlib import Path
from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "jira-agent"
CLIENT_KEY = "oauth-client"


class SecretStoreError(Exception):
    pass


def _hint() -> str:
    if sys.platform.startswith("linux"):
        return (
            " (no desktop keyring found; install one such as gnome-keyring or KWallet, or set "
            "JIRA_AGENT_SECRETS=file to keep secrets in a private file instead)"
        )
    return ""


class SecretStore(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...
    def delete(self, key: str) -> None: ...


class KeychainStore:
    def get(self, key: str) -> dict | None:
        try:
            raw = keyring.get_password(SERVICE, key)
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}{_hint()}") from e
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict) -> None:
        try:
            keyring.set_password(SERVICE, key, json.dumps(value))
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}{_hint()}") from e

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(SERVICE, key)
        except PasswordDeleteError:
            pass
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}{_hint()}") from e


class FileStore:
    """Opt-in fallback for machines without a keyring (headless Linux, containers).

    Secrets sit in a JSON file readable only by the user (mode 0600; on Windows the user profile's
    access rules apply instead). Weaker than an OS keyring: enable it with JIRA_AGENT_SECRETS=file.
    """

    def __init__(self, path: Path | None = None):
        home = Path(os.environ.get("JIRA_AGENT_HOME", "~/.jira-agent")).expanduser()
        self.path = path or home / "secrets.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            raise SecretStoreError(f"secrets file unreadable: {e}") from e

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data))
            os.chmod(self.path, 0o600)
        except OSError as e:
            raise SecretStoreError(f"secrets file unwritable: {e}") from e

    def get(self, key: str) -> dict | None:
        return self._load().get(key)

    def set(self, key: str, value: dict) -> None:
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._save(data)


def default_store() -> SecretStore:
    if os.environ.get("JIRA_AGENT_SECRETS", "").lower() == "file":
        return FileStore()
    return KeychainStore()


class MemoryStore:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def token_key(site_url: str) -> str:
    return f"tokens:{site_url}"
