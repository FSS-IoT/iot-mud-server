"""Character-key registration and login (SPEC v2.1.1 criteria 1 and 2).

A member registers a name and receives a one-time character key. Only the key's
id, salt, and one-way scrypt hash are stored; the plaintext key is returned in
the `auth.ok` response and never written down or logged.

Login sends the key back and no name. The public key id finds the stored row and
the secret is verified against the hash with a constant-time comparison, so a
failed login never says whether a character name exists.

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

**Login.** An unknown key id, a revoked key, and a bad secret all raise one
`AuthError("invalid_key")` with the same message. The key is the only lookup;
no character name is consulted. A successful login returns the durable room id
and inventory with the verified key row, so admission does not need a second
player-store read (#138).

**Offline rekey (#73).** `rekey_character` issues a replacement key for an
existing character and revokes every key it still holds, in one transaction.
The `players` row is not touched, so the room and inventory are unchanged. It
looks the character up by name because an administrator already knows who the
member is; this is not a login path and never runs on the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
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

KEY_ID_RE = re.compile(rf"k[0-9a-f]{{{KEY_ID_BYTES * 2}}}")
INVALID_KEY_MESSAGE = "That character key is not valid."


class AuthError(Exception):
    """A typed auth failure that is safe to put on the wire."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RekeyError(Exception):
    """An offline rekey could not proceed; the message is safe to print."""


@dataclass(frozen=True)
class Registration:
    """What a successful registration produced."""

    name: str
    player_id: int
    key_id: str
    key: str


@dataclass(frozen=True)
class Login:
    """The durable character a verified key belongs to.

    `room_id`, `items`, and `skills` are read in the same login lookup, so
    admission does not need a second `PlayerStore.load()` (#138, #183).
    """

    name: str
    player_id: int
    key_id: str
    room_id: str | None
    items: tuple[str, ...] = ()
    skills: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Rekey:
    """What a successful offline rekey produced.

    `key` is the plaintext credential and exists only until the caller prints
    it. `revoked_key_ids` lists the live keys that died in the same
    transaction; the room and inventory were not touched.
    """

    name: str
    player_id: int
    key_id: str
    key: str
    revoked_key_ids: tuple[str, ...]


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


def parse_key(key: object) -> tuple[str, str]:
    """Return `(key id, secret)` for a well-formed key, else raise `AuthError`.

    Only the shape is checked here; whether the id exists is a lookup, and
    whether the secret is right is the hash comparison. Both failures carry the
    same message.
    """
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)
    parts = key.split("-", 2)
    if len(parts) != 3:
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)
    _prefix, key_id, secret = parts
    if KEY_ID_RE.fullmatch(key_id) is None or not secret:
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)
    return key_id, secret


def verify_key(key: str, salt_hex: str, stored_hash: str) -> bool:
    """Check `key` against the stored scrypt value, in constant time.

    The parameters and salt are read from the stored row, so a later change to
    the scrypt cost needs no migration. A malformed stored value compares as
    false rather than raising, which keeps a corrupt row from becoming an
    unhandled error on the login path.
    """
    try:
        salt = bytes.fromhex(salt_hex)
        algorithm, n_text, r_text, p_text, digest_hex = stored_hash.split("$")
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        if not expected:
            return False
    except ValueError:
        return False

    try:
        computed = hashlib.scrypt(
            key.encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected),
        )
    except (ValueError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(computed, expected)


def login_character(conn: sqlite3.Connection, key: object) -> Login:
    """Find the character a key belongs to, or raise `AuthError("invalid_key")`.

    The key id selects the row; no character name is accepted or looked up. An
    unknown id, a revoked key, and a bad secret are indistinguishable on the
    wire: one code, one message.
    """
    if not isinstance(key, str):
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)
    key_id, _secret = parse_key(key)

    row = conn.execute(
        "SELECT k.key_id, k.player_id, k.salt, k.hash, k.revoked_at, "
        "p.name, p.room_id, p.skills "
        "FROM character_keys k JOIN players p ON p.id = k.player_id "
        "WHERE k.key_id = ?",
        (key_id,),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)

    if not verify_key(key, row["salt"], row["hash"]):
        raise AuthError("invalid_key", INVALID_KEY_MESSAGE)

    player_id = int(row["player_id"])
    items = tuple(
        item["id"]
        for item in conn.execute(
            "SELECT id FROM objects WHERE owner_id = ? ORDER BY id",
            (player_id,),
        )
    )
    try:
        raw_skills = json.loads(row["skills"] or "{}")
    except ValueError:
        raw_skills = {}
    skills = {
        str(skill): int(level)
        for skill, level in raw_skills.items()
        if isinstance(level, (int, float)) and not isinstance(level, bool)
        and int(level) >= 0
    } if isinstance(raw_skills, dict) else {}
    return Login(
        name=row["name"],
        player_id=player_id,
        key_id=row["key_id"],
        room_id=row["room_id"],
        items=items,
        skills=skills,
    )


def rekey_character(
    conn: sqlite3.Connection,
    name: object,
    *,
    now: Callable[[], str] = _utc_now,
) -> Rekey:
    """Issue a replacement key and revoke every live key, in one transaction.

    The character is found by name (case-insensitively, as `players.name` is
    `COLLATE NOCASE`); the stored casing is kept. The `players` row is not
    written, so the character stays in its room with its inventory.

    An unknown name raises `RekeyError` naming only the request. The caller is
    an offline administrator, so there is no wire message to match and no
    reason to enumerate other characters.
    """
    if not isinstance(name, str) or not name.strip():
        raise RekeyError("A character name is required.")
    requested = name.strip()

    key, key_id = generate_key()
    salt = secrets.token_bytes(SALT_BYTES)
    key_hash = hash_key(key, salt)
    created_at = now()

    with conn:
        row = conn.execute(
            "SELECT id, name FROM players WHERE name = ?", (requested,)
        ).fetchone()
        if row is None:
            raise RekeyError(f"No character named {requested!r}.")
        player_id = int(row["id"])

        stale = conn.execute(
            "SELECT key_id FROM character_keys "
            "WHERE player_id = ? AND revoked_at IS NULL ORDER BY key_id",
            (player_id,),
        ).fetchall()
        conn.execute(
            "UPDATE character_keys SET revoked_at = ? "
            "WHERE player_id = ? AND revoked_at IS NULL",
            (created_at, player_id),
        )
        conn.execute(
            "INSERT INTO character_keys"
            "(key_id, player_id, salt, hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key_id, player_id, salt.hex(), key_hash, created_at),
        )

    return Rekey(
        name=row["name"],
        player_id=player_id,
        key_id=key_id,
        key=key,
        revoked_key_ids=tuple(key_row["key_id"] for key_row in stale),
    )


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
