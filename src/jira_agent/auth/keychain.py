import json
from typing import Protocol

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "jira-agent"
CLIENT_KEY = "oauth-client"


class SecretStoreError(Exception):
    pass


class SecretStore(Protocol):
    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...
    def delete(self, key: str) -> None: ...


class KeychainStore:
    def get(self, key: str) -> dict | None:
        try:
            raw = keyring.get_password(SERVICE, key)
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}") from e
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict) -> None:
        try:
            keyring.set_password(SERVICE, key, json.dumps(value))
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}") from e

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(SERVICE, key)
        except PasswordDeleteError:
            pass
        except KeyringError as e:
            raise SecretStoreError(f"keychain unavailable: {e}") from e


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
