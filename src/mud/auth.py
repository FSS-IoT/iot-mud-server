"""Character-key registration (SPEC v2.1.1 criterion 1).

A member registers a name and receives a one-time character key. Only the key's
id, salt, and one-way scrypt hash are stored; the plaintext key is returned in
the `auth.ok` response and never written down or logged.

**Name rule.** 2-24 characters, the first a letter, the rest letters, digits,
`_`, or `-` (`Ada`, `ada-2`, `Ada_Lovelace`). Surrounding whitespace is
trimmed. Uniqueness is case-insensitive, matching the `players.name` column's
`COLLATE NOCASE`; the stored casing is preserved.

**Key format.** `mud1-<key id>-<secret>`, where the key id is `k` plus 16 hex
characters (public, used to find the row) and the secret is
`secrets.token_urlsafe(32)` — 32 bytes (256 bits) of OS entropy in base64url.
The `mud1-` prefix makes a leaked key recognisable to secret scanners. Clients
treat the whole string as opaque.

**At rest.** `character_keys.salt` is 16 random bytes as hex.
`character_keys.hash` is `scrypt$<n>$<r>$<p>$<hex digest>`; the parameters live
in the value so a future parameter change needs no schema migration. Login
(#69) verifies against the same layout.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.mud.storage.keys import room_key
from src.mud.storage.store import ensure_room

KEY_PREFIX = "mud1-"
KEY_SECRET_BYTES = 32
KEY_ID_BYTES = 8
SALT_BYTES = 16

# RFC 7914's interactive-login recommendation: N=2^14, r=8, p=1.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

NAME_MIN_LENGTH = 2
NAME_MAX_LENGTH = 24
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,23}")


class AuthError(Exception):
    """A typed auth failure that is safe to put on the wire."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Registration:
    """What a successful registration produced."""

    name: str
    player_id: int
    key_id: str
    key: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def normalize_name(name: object) -> str:
    """Return the trimmed name, or raise `AuthError("invalid_name")`.

    The message states the rule instead of echoing the input, so a name that
    is a key by mistake is not reflected back into a log or an error.
    """
    if not isinstance(name, str):
        raise AuthError("invalid_name", "A character name must be a string.")
    name = name.strip()
    if _NAME_RE.fullmatch(name) is None:
        raise AuthError(
            "invalid_name",
            "Names are 2-24 characters, start with a letter, and use only "
            "letters, digits, '_' or '-'.",
        )
    return name


def generate_key() -> tuple[str, str]:
    """Return `(key, key id)` from OS security randomness.

    The key id is public and only locates the row; the secret carries the
    entropy. `secrets` is deliberately outside the deterministic-game rule
    (SPEC constraints).
    """
    key_id = f"k{secrets.token_hex(KEY_ID_BYTES)}"
    secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
    return f"{KEY_PREFIX}{key_id}-{secret}", key_id


def hash_key(key: str, salt: bytes) -> str:
    """The one-way scrypt value stored in `character_keys.hash`."""
    digest = hashlib.scrypt(
        key.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${digest.hex()}"


def register_character(
    conn: sqlite3.Connection,
    name: object,
    *,
    start_room: Any,
    now: Callable[[], str] = _utc_now,
) -> Registration:
    """Create the character and its first key in one transaction.

    The character starts at `start_room` (the world's `start_location`), which
    is created in the same transaction if the database has no seed yet. A
    duplicate name raises `AuthError("name_taken")` and writes nothing.
    """
    name = normalize_name(name)
    key, key_id = generate_key()
    salt = secrets.token_bytes(SALT_BYTES)
    key_hash = hash_key(key, salt)
    created_at = now()
    destination = room_key(start_room)

    try:
        with conn:
            ensure_room(conn, start_room)
            if conn.execute(
                "SELECT 1 FROM players WHERE name = ?", (name,)
            ).fetchone() is not None:
                raise AuthError(
                    "name_taken", f"The name {name!r} is already in use.")
            cursor = conn.execute(
                "INSERT INTO players(name, room_id, created_at) "
                "VALUES (?, ?, ?)",
                (name, destination, created_at),
            )
            player_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO character_keys"
                "(key_id, player_id, salt, hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, player_id, salt.hex(), key_hash, created_at),
            )
    except sqlite3.IntegrityError as exc:
        if "players.name" in str(exc):
            raise AuthError(
                "name_taken", f"The name {name!r} is already in use.") from exc
        raise

    return Registration(
        name=name, player_id=player_id, key_id=key_id, key=key)
