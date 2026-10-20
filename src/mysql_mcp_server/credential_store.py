"""Secret providers that never expose credential values in diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Hashable, Sequence

MAX_CREDENTIAL_BYTES = 16_384


class CredentialProviderError(ValueError):
    """Raised when a configured secret provider cannot return a credential."""


class CredentialCache:
    """Opt-in, process-local TTL cache for slow secret providers.

    Values never leave process memory and are dropped on expiry, explicit
    invalidation (authentication failure, forced profile reload) or shutdown.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, str]] = {}

    def get_or_load(
        self, key: Hashable, ttl_seconds: float, loader: Callable[[], str]
    ) -> str:
        if ttl_seconds <= 0:
            return loader()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > self._clock():
                return entry[1]
        # Load outside the lock so one slow provider cannot block other profiles.
        value = loader()
        with self._lock:
            self._entries[key] = (self._clock() + ttl_seconds, value)
        return value

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


credential_cache = CredentialCache()


def _keyring_module():
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError as exc:
        raise CredentialProviderError(
            "The keyring credential provider is unavailable"
        ) from exc
    return keyring, KeyringError


def get_keyring_password(service: str, reference: str) -> str:
    keyring, keyring_error = _keyring_module()
    try:
        value = keyring.get_password(service, reference)
    except keyring_error as exc:
        raise CredentialProviderError(
            "The system credential store is unavailable"
        ) from exc
    if value is None:
        raise CredentialProviderError(
            "No credential is stored for the configured reference"
        )
    return value


def set_keyring_password(service: str, reference: str, password: str) -> None:
    keyring, keyring_error = _keyring_module()
    try:
        keyring.set_password(service, reference, password)
    except keyring_error as exc:
        raise CredentialProviderError(
            "The system credential store rejected the credential"
        ) from exc


def delete_keyring_password(service: str, reference: str) -> None:
    keyring, keyring_error = _keyring_module()
    try:
        keyring.delete_password(service, reference)
    except keyring_error as exc:
        raise CredentialProviderError(
            "The system credential store could not delete the credential"
        ) from exc


def run_credential_command(command: Sequence[str], timeout_seconds: float) -> str:
    """Run one trusted argv vector without a shell and return one secret line."""
    if not command or not command[0]:
        raise CredentialProviderError("Credential command is empty")
    executable = command[0]
    if not os.path.isabs(executable) and shutil.which(executable) is None:
        raise CredentialProviderError("Credential command executable was not found")
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise CredentialProviderError("Credential command timed out") from exc
        except OSError as exc:
            raise CredentialProviderError(
                "Credential command could not be started"
            ) from exc
        output.seek(0, os.SEEK_END)
        if output.tell() > MAX_CREDENTIAL_BYTES:
            raise CredentialProviderError(
                "Credential command output exceeds safety limit"
            )
        output.seek(0)
        raw_output = output.read(MAX_CREDENTIAL_BYTES + 1)
    if result.returncode != 0:
        raise CredentialProviderError(
            f"Credential command failed with exit status {result.returncode}"
        )
    try:
        value = raw_output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialProviderError(
            "Credential command output is not valid UTF-8"
        ) from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith(("\n", "\r")):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise CredentialProviderError(
            "Credential command must return exactly one non-empty line"
        )
    return value
