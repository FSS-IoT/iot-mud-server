"""Write-through stores for player location and object contents.

SPEC v2.1.0 criterion 2: a take, drop, or move is committed to SQLite before
its event is published. The engine calls a store *before* it touches memory, so
a failed write raises `StorageWriteError` and leaves both the world and the wire
unchanged.

`PlayerStore` writes the actor's half — which room the player is in and which
objects it owns. `WorldStore` reads where any object is. Seeding the world from
content lives in `src/mud/world/seed.py`; loading a seeded world back into the
engine is still open. This module only makes each mutation durable.

Every mutation is one SQLite transaction. A store also creates the rows it
references when they are missing, because a fresh database has no seed yet; the
insert records the state *before* the mutation and the update after it records
the mutation itself. Both happen in the same transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.mud.storage.keys import object_key, room_key


class StorageWriteError(RuntimeError):
    """A mutation could not be committed; in-memory state must not change."""


# A test seam: called with the operation name ("take", "drop", "move") before
# the transaction runs. Anything it raises becomes a `StorageWriteError`.
FailHook = Callable[[str], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ObjectLocation:
    """Where an object is: at most one of a room, an owner, or a container."""

    room_id: str | None = None
    owner_id: int | None = None
    container_id: str | None = None


class WorldStore:
    """Read the durable location and contents of the world's objects."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def object_location(self, key: str) -> ObjectLocation | None:
        """Where `key` is, or None when the object has no row yet."""
        row = self._conn.execute(
            "SELECT room_id, owner_id, container_id FROM objects WHERE id = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return ObjectLocation(row["room_id"], row["owner_id"], row["container_id"])

    def room_contents(self, room_id: str) -> tuple[str, ...]:
        """The keys of the objects lying in a room, sorted."""
        rows = self._conn.execute(
            "SELECT id FROM objects WHERE room_id = ? ORDER BY id", (room_id,)
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def container_contents(self, container_id: str) -> tuple[str, ...]:
        """The keys of the objects inside a container, sorted."""
        rows = self._conn.execute(
            "SELECT id FROM objects WHERE container_id = ? ORDER BY id",
            (container_id,),
        ).fetchall()
        return tuple(row["id"] for row in rows)


class PlayerStore:
    """One player's row: where it is and which objects it owns."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str,
        *,
        now: Callable[[], str] = _utc_now,
        fail: FailHook | None = None,
    ):
        self._conn = conn
        self.name = name
        self._now = now
        self._fail = fail
        self._id: int | None = None

    @property
    def id(self) -> int | None:
        """The player's row id, or None before the first write."""
        if self._id is None:
            row = self._conn.execute(
                "SELECT id FROM players WHERE name = ?", (self.name,)
            ).fetchone()
            if row is not None:
                self._id = int(row["id"])
        return self._id

    @property
    def room_id(self) -> str | None:
        row = self._conn.execute(
            "SELECT room_id FROM players WHERE name = ?", (self.name,)
        ).fetchone()
        return row["room_id"] if row is not None else None

    def contents(self) -> tuple[str, ...]:
        """The keys of the objects this player owns, sorted."""
        rows = self._conn.execute(
            "SELECT o.id FROM objects o JOIN players p ON p.id = o.owner_id "
            "WHERE p.name = ? ORDER BY o.id",
            (self.name,),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def move(self, *, room: Any) -> None:
        """Commit that the player is now in `room`."""
        destination = room_key(room)

        def body(conn: sqlite3.Connection) -> None:
            ensure_room(conn, room)
            player_id = _ensure_player(conn, self.name, destination, self._now())
            conn.execute(
                "UPDATE players SET room_id = ? WHERE id = ?",
                (destination, player_id),
            )
            self._id = player_id

        self._transaction("move", body)

    def take(self, item: Any, *, room: Any) -> None:
        """Commit that `item` left `room` and became the player's."""
        origin = room_key(room)

        def body(conn: sqlite3.Connection) -> None:
            ensure_room(conn, room)
            player_id = _ensure_player(conn, self.name, origin, self._now())
            _ensure_object(conn, item, room_id=origin)
            conn.execute(
                "UPDATE objects SET room_id = NULL, owner_id = ?, "
                "container_id = NULL WHERE id = ?",
                (player_id, object_key(item)),
            )
            self._id = player_id

        self._transaction("take", body)

    def drop(self, item: Any, *, room: Any) -> None:
        """Commit that `item` left the player's hands and is on `room`'s floor."""
        destination = room_key(room)

        def body(conn: sqlite3.Connection) -> None:
            ensure_room(conn, room)
            player_id = _ensure_player(conn, self.name, destination, self._now())
            _ensure_object(conn, item, owner_id=player_id)
            conn.execute(
                "UPDATE objects SET room_id = ?, owner_id = NULL, "
                "container_id = NULL WHERE id = ?",
                (destination, object_key(item)),
            )
            self._id = player_id

        self._transaction("drop", body)

    def _transaction(
        self, op: str, body: Callable[[sqlite3.Connection], None]
    ) -> None:
        if self._fail is not None:
            try:
                self._fail(op)
            except StorageWriteError:
                raise
            except Exception as exc:
                raise StorageWriteError(
                    f"{op} could not be saved: {exc}") from exc
        try:
            with self._conn:
                body(self._conn)
        except sqlite3.Error as exc:
            raise StorageWriteError(f"{op} could not be saved: {exc}") from exc


class WriteThroughStore:
    """The stores the engine holds: one connection, one store per player."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], str] = _utc_now,
        fail: FailHook | None = None,
    ):
        self.conn = conn
        self.world = WorldStore(conn)
        self._now = now
        self._fail = fail
        self._players: dict[str, PlayerStore] = {}

    def player(self, name: str) -> PlayerStore:
        store = self._players.get(name)
        if store is None:
            store = PlayerStore(
                self.conn, name, now=self._now, fail=self._fail)
            self._players[name] = store
        return store


def ensure_room(conn: sqlite3.Connection, room: Any) -> None:
    """Create the room's row, or refresh its text, without moving anything."""
    conn.execute(
        "INSERT INTO rooms(id, name, description, dark) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
        "description = excluded.description, dark = excluded.dark",
        (
            room_key(room),
            room.name,
            room.description,
            1 if getattr(room, "dark", False) else 0,
        ),
    )


def _ensure_player(
    conn: sqlite3.Connection, name: str, room_id: str | None, now: str
) -> int:
    conn.execute(
        "INSERT INTO players(name, room_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO NOTHING",
        (name, room_id, now),
    )
    row = conn.execute(
        "SELECT id FROM players WHERE name = ?", (name,)
    ).fetchone()
    return int(row["id"])


def _ensure_object(
    conn: sqlite3.Connection,
    item: Any,
    *,
    room_id: str | None = None,
    owner_id: int | None = None,
    container_id: str | None = None,
) -> None:
    """Create the object's row, or refresh its text, without moving it."""
    conn.execute(
        "INSERT INTO objects(id, name, description, room_id, owner_id, "
        "container_id) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, "
        "description = excluded.description",
        (
            object_key(item),
            item.name,
            getattr(item, "description", None),
            room_id,
            owner_id,
            container_id,
        ),
    )
