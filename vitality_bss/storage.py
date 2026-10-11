"""Document blob storage on the local filesystem with optional Fernet encryption.

Files are named by a random key (never by patient data). When ENCRYPTION_KEYS is
set, blobs are encrypted with the first key; any listed key can decrypt, which
allows key rotation (see ``rotate``).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_KEY_RE = re.compile(r"^[a-f0-9]{32}$")


class StorageError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


@dataclass
class BlobStore:
    root: Path
    keys: list[str]

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._fernet: MultiFernet | None = (
                MultiFernet([Fernet(k.encode("ascii")) for k in self.keys]) if self.keys else None
            )
        except (ValueError, TypeError) as exc:
            raise StorageError("ENCRYPTION_KEYS contains an invalid Fernet key") from exc

    @property
    def encrypts(self) -> bool:
        return self._fernet is not None

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key):
            raise StorageError("invalid storage key")
        return self.root / f"{key}.bin"

    def put(self, data: bytes) -> tuple[str, bool]:
        """Store ``data``; return (storage_key, encrypted)."""
        key = secrets.token_hex(16)
        payload = self._fernet.encrypt(data) if self._fernet else data
        _atomic_write(self._path(key), payload)
        return key, self._fernet is not None

    def get(self, key: str, encrypted: bool) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError("stored document is missing")
        raw = path.read_bytes()
        if not encrypted:
            return raw
        if self._fernet is None:
            raise StorageError("document is encrypted but ENCRYPTION_KEYS is not configured")
        try:
            return self._fernet.decrypt(raw)
        except InvalidToken as exc:
            raise StorageError("document cannot be decrypted with the configured keys") from exc

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def rotate(self, key: str, encrypted: bool) -> bool:
        """Re-encrypt one blob with the primary key (encrypting plaintext blobs too)."""
        if self._fernet is None:
            raise StorageError("rotation requires ENCRYPTION_KEYS")
        path = self._path(key)
        raw = path.read_bytes()
        try:
            new = self._fernet.rotate(raw) if encrypted else self._fernet.encrypt(raw)
        except InvalidToken as exc:
            raise StorageError("document cannot be decrypted with the configured keys") from exc
        _atomic_write(path, new)
        return True


def get_store() -> BlobStore:
    from vitality_bss.config import get_settings

    s = get_settings()
    return BlobStore(root=s.upload_dir, keys=s.encryption_keys)
