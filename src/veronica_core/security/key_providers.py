"""Key provider abstractions for VERONICA policy verification.

Supports pluggable key material sourcing: file, environment variable,
or HashiCorp Vault transit engine.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

# Conditional import for Vault support
try:
    import hvac  # type: ignore[import-untyped]
    _VAULT_AVAILABLE = True
except ImportError:
    _VAULT_AVAILABLE = False


@runtime_checkable
class KeyProvider(Protocol):
    """Protocol for supplying key material to VERONICA policy verification."""

    def get_public_key_pem(self) -> bytes:
        """Return the PEM-encoded public key bytes."""
        ...


class FileKeyProvider:
    """Load public key from a PEM file on disk.

    Args:
        path: Path to the PEM-encoded public key file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_public_key_pem(self) -> bytes:
        """Read and return the PEM bytes from the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            OSError: If the file cannot be read.
        """
        return self._path.read_bytes()


class EnvKeyProvider:
    """Load public key PEM from an environment variable.

    Args:
        env_var: Name of the environment variable holding the PEM content.
                 Defaults to ``VERONICA_PUBLIC_KEY_PEM``.
    """

    def __init__(self, env_var: str = "VERONICA_PUBLIC_KEY_PEM") -> None:
        self._env_var = env_var

    def get_public_key_pem(self) -> bytes:
        """Return the PEM bytes from the environment variable.

        Raises:
            RuntimeError: If the environment variable is not set.
        """
        value = os.environ.get(self._env_var)
        if value is None:
            raise RuntimeError(
                f"Environment variable '{self._env_var}' is not set. "
                "Set it to the PEM-encoded public key content."
            )
        return value.encode("utf-8")


class VaultKeyProvider:
    """Fetch public key from HashiCorp Vault transit engine.

    Args:
        vault_url: Base URL of the Vault server (e.g. ``https://vault.example.com``).
        mount_point: Transit secrets engine mount point. Defaults to ``transit``.
        key_name: Name of the transit key. Defaults to ``veronica-policy``.
        token: Vault token. If None, falls back to the ``VAULT_TOKEN`` env var.
        namespace: Vault namespace (Enterprise only). Defaults to None.

    Raises:
        RuntimeError: If ``hvac`` is not installed.
    """

    def __init__(
        self,
        vault_url: str,
        mount_point: str = "transit",
        key_name: str = "veronica-policy",
        token: str | None = None,
        namespace: str | None = None,
    ) -> None:
        if not _VAULT_AVAILABLE:
            raise RuntimeError(
                "hvac is required for VaultKeyProvider. "
                "Install it with: pip install veronica-core[vault]"
            )
        self._vault_url = vault_url
        self._mount_point = mount_point
        self._key_name = key_name
        self._token = token or os.environ.get("VAULT_TOKEN")
        self._namespace = namespace

    def get_public_key_pem(self) -> bytes:
        """Fetch the latest public key PEM from Vault transit engine.

        Raises:
            RuntimeError: If the Vault connection fails or no key is found.
        """
        client = hvac.Client(
            url=self._vault_url,
            token=self._token,
            namespace=self._namespace,
        )
        try:
            response = client.secrets.transit.get_key(
                name=self._key_name,
                mount_point=self._mount_point,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch key '{self._key_name}' from Vault "
                f"at {self._vault_url}: {exc}"
            ) from exc

        keys = response.get("data", {}).get("keys", {})
        if not keys:
            raise RuntimeError(
                f"No keys found for '{self._key_name}' in Vault"
            )
        # Get the latest version's public key
        latest_version = max(keys.keys(), key=int)
        public_key = keys[latest_version].get("public_key", "")
        if not public_key:
            raise RuntimeError(
                f"No public key found for '{self._key_name}' "
                f"version {latest_version} in Vault"
            )
        return public_key.encode("utf-8")
