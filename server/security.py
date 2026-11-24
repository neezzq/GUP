from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets

from argon2 import PasswordHasher
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PASSWORDS = PasswordHasher()


def random_token():
    return secrets.token_urlsafe(32)


def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Vault:
    active: str
    keys: dict[str, bytes]

    @classmethod
    def load(cls, path: Path):
        with path.open(encoding="utf-8") as source:
            content = json.load(source)
        keys = {
            key_id: base64.b64decode(key, validate=True)
            for key_id, key in content["keys"].items()
        }
        active = content["active"]
        if active not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError("Invalid vault keyring")
        if len(set(keys.values())) != len(keys):
            raise ValueError("Key IDs must refer to distinct keys")
        return cls(active, keys)

    def encrypt(self, launcher, username, password):
        nonce = os.urandom(12)
        content = json.dumps({"username": username, "password": password}).encode()
        ciphertext = AESGCM(self.keys[self.active]).encrypt(nonce, content, launcher.encode())
        return self.active, nonce, ciphertext

    def decrypt(self, row):
        content = AESGCM(self.keys[row["key_id"]]).decrypt(
            row["nonce"], row["ciphertext"], row["launcher"].encode()
        )
        return json.loads(content)


def accept_request(database, scope, subject, now, limit, ttl):
    with database.transaction(write=True) as connection:
        connection.execute("DELETE FROM rate WHERE expires <= ?", (now,))
        row = connection.execute(
            """
            INSERT INTO rate VALUES (?, ?, ?, 1)
            ON CONFLICT(scope, subject) DO UPDATE SET hits = hits + 1
            RETURNING hits
            """,
            (scope, subject, now + ttl),
        ).fetchone()
        return row["hits"] <= limit
