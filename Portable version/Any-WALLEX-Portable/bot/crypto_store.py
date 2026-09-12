"""Encrypted credential store — API keys live ONLY on the server, encrypted at rest.

Keys are encrypted with Fernet (AES-128-CBC + HMAC) using a key derived from
WALLEX_KEY_PASSWORD via PBKDF2. The plaintext key never touches logs:
`redact()` is applied to every log line that could contain it.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_SALT_FILE = "salt.bin"
_STORE_FILE = "secrets.enc"


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


class CryptoStore:
    def __init__(self, data_dir: str, password: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        salt_path = self.dir / _SALT_FILE
        if salt_path.exists():
            self.salt = salt_path.read_bytes()
        else:
            self.salt = os.urandom(16)
            salt_path.write_bytes(self.salt)
        self._fernet = Fernet(_derive_key(password, self.salt))
        self._path = self.dir / _STORE_FILE

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self._path.read_bytes()).decode("utf-8"))
        except InvalidToken:
            raise RuntimeError("WALLEX_KEY_PASSWORD is wrong — cannot decrypt secrets.")

    def _save(self, data: dict) -> None:
        self._path.write_bytes(self._fernet.encrypt(json.dumps(data).encode("utf-8")))

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        """Remove a key entirely from the encrypted store (e.g. API key)."""
        data = self._load()
        if name in data:
            del data[name]
            self._save(data)

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def redact(self, text: str) -> str:
        """Remove any secret value from a string before it is logged."""
        data = self._load()
        for v in data.values():
            if v and len(v) >= 6:
                text = text.replace(v, "***REDACTED***")
        return text
