"""AES-256-GCM encryption for stored session cookies."""

from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


class CookieVault:
    """Encrypts cookie dicts; `context` (platform:username) is bound in as associated data."""

    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    def encrypt(self, context: str, cookies: dict[str, str]) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, json.dumps(cookies).encode(), context.encode())
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, context: str, blob: str) -> dict[str, str]:
        raw = base64.b64decode(blob)
        return json.loads(self._aes.decrypt(raw[:12], raw[12:], context.encode()))
