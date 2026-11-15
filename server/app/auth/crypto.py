"""At-rest encryption for third-party refresh tokens (MultiFernet with key ids).

Keys come from `AuthSettings.fernet_keys`; the first is the primary key used to encrypt,
all keys can decrypt, so rotation is: prepend a new key, run `rotate()` over stored rows,
then drop the old key. `primary_key_id` is what a `key_id` column (e.g.
google_connections.key_id) should record.
"""

from __future__ import annotations

from collections.abc import Sequence

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class DecryptionError(Exception):
    """Ciphertext could not be decrypted with any configured key."""


class TokenCipher:
    def __init__(self, keys: Sequence[tuple[int, str]]) -> None:
        if not keys:
            raise ValueError("at least one Fernet key is required")
        self._key_ids = tuple(k for k, _ in keys)
        self._fernet = MultiFernet([Fernet(key.encode()) for _, key in keys])

    @property
    def primary_key_id(self) -> int:
        return self._key_ids[0]

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise DecryptionError("ciphertext not decryptable with configured keys") from exc

    def rotate(self, ciphertext: bytes) -> bytes:
        """Re-encrypt under the primary key."""
        try:
            return self._fernet.rotate(ciphertext)
        except InvalidToken as exc:
            raise DecryptionError("ciphertext not decryptable with configured keys") from exc
