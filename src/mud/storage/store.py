"""Write-through stores for player location, object contents, and flags.

SPEC v2.1.0 criterion 2: a take, drop, or move is committed to SQLite before
its event is published. The engine calls a store *before* it touches memory, so
a failed write raises `StorageWriteError` and leaves both the world and the wire
unchanged.

The store is location-shaped, not verb-shaped (#131): a command records the
object placements and flags it changed in one `ChangeSet`, and
`WorldStore.commit` writes the whole set in one transaction. The store never
learns the command vocabulary, so a new verb cannot forget the seam by
accident.

`WorldStore` reads and writes where any object is; `PlayerStore` keeps the
character-shaped conveniences (`load`, `move`, `take`, `drop`, `sleep`,
`wake`) as thin `ChangeSet` builders. Seeding the world from content lives in
`src/mud/world/seed.py`. This module is the only path between the engine and
those rows.

Every mutation is one SQLite transaction. A store also creates the rows it
references when they are missing, because a fresh database has no seed yet; the
insert records the state *before* the mutation and the update after it records
the mutation itself. Both happen in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.mud.content import DEFAULT_CARRY_CAPACITY, Entity, Player, Room
from src.mud.content.models import OPPOSITE_EXITS
from src.mud.storage.keys import object_key, room_key


class StorageWriteError(RuntimeError):
    """A mutation could not be committed; in-memory state must not change."""


# A test seam: called with the operation name ("take", "drop", "move") before
# the transaction runs. Anything it raises becomes a `StorageWriteError`.
FailHook = Callable[[str], None]

# Runtime equipment slots written by the engine. The authored ``equip_slots``
# vocabulary uses ``hand``; the durable state uses the concrete hand.
RUNTIME_SLOTS = frozenset(
    {"hand_1", "hand_2", "head", "body", "back", "belt", "feet"}
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ObjectLocation:
    """Where an object is: at most one of a room, an owner, or a container."""

    room_id: str | None = None
    owner_id: int | None = None
    container_id: str | None = None


@dataclass(frozen=True)
class CharacterState:
    """A character's durable half: room, inventory, skills, and base capacity."""

    room_id: str | None
    items: tuple[str, ...]
    skills: dict[str, int] = field(default_factory=dict)
    capacity_base: float = DEFAULT_CARRY_CAPACITY


@dataclass(frozen=True)
class RosterEntry:
    """One character's row for the server-wide `who` roster (#72)."""

    name: str
    room_id: str | None
    asleep: bool


@dataclass(frozen=True)
class PlayerRef:
    """A character addressed by name, for callers without a live `Player`."""

    name: str


@dataclass(frozen=True)
class ExitLockChange:
    """One exit-lock flag write; `target` adds the shared reciprocal row."""

    room: Room
    direction: str
    locked: bool
    target: Room | None = None
    key_item: Entity | None = None
    label: str | None = None


class ChangeSet:
    """The placements and flags one command changed, committed atomically.

    `place(item, at)` is the one location shape:
    a `Room` puts the item on the floor, a `Player` puts it in that character's
    inventory, and any other `Entity` puts it inside that container. A `Player`
    as the item moves the character into the room. `set(entity, flag, value)`
    writes an object flag (`locked`, `open`, `lit`) or a player flag
    (`asleep`). Exit locks use `set_exit` because an `ExitLock` has no entity
    row of its own.
    """

    def __init__(self, op: str = "commit"):
        self.op = op
        self.placements: list[tuple[Any, Any]] = []
        self.flags: list[tuple[Any, str, Any]] = []
        self.exit_locks: list[ExitLockChange] = []

    def place(self, item: Any, at: Any) -> ChangeSet:
        self.placements.append((item, at))
        return self

    def set(self, entity: Any, flag: str, value: Any = True) -> ChangeSet:
        self.flags.append((entity, flag, value))
        return self

    def set_exit(
        self,
        room: Room,
        direction: str,
        *,
        locked: bool,
        target: Room | None = None,
        key_item: Entity | None = None,
        label: str | None = None,
    ) -> ChangeSet:
        self.exit_locks.append(ExitLockChange(
            room, direction, locked, target, key_item, label))
        return self

    def __bool__(self) -> bool:
        return bool(self.placements or self.flags or self.exit_locks)


class WorldStore:
    """Read and commit the durable location, contents, and flags of the world."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], str] = _utc_now,
        fail: FailHook | None = None,
    ):
        self._conn = conn
        self._now = now
        self._fail = fail

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

    def commit(self, changes: ChangeSet) -> None:
        """Write one command's placements and flags in one transaction.

        The caller applies the same changes in memory only after this returns.
        A failure raises `StorageWriteError` with every row unchanged.
        """
        def body(conn: sqlite3.Connection) -> None:
            now = self._now()
            for item, at in changes.placements:
                _place(conn, item, at, now)
            for entity, flag, value in changes.flags:
                _set_flag(conn, entity, flag, value, now)
            for change in changes.exit_locks:
                _set_exit(conn, change)

        _transaction(self._conn, changes.op, body, self._fail)

    def apply_durable_state(self, engine: Any) -> None:
        """Overlay committed placements and flags onto a freshly loaded world.

        The engine still loads its rooms and prose from YAML. This reads only
        the mutable half — object placement, `locked`/`open`/`lit`, and exit
        locks — so a restart does not undo work that was committed. Items owned
        by a character are skipped: login's restore path moves those.

        The overlay is idempotent against a freshly seeded database, where the
        rows already match the authored world.
        """
        slots = _object_slots(engine)
        rows = self._conn.execute(
            "SELECT id, room_id, owner_id, container_id, state FROM objects"
        ).fetchall()
        for row in rows:
            key = row["id"]
            entity = _entity_by_id(engine, key)
            if entity is None:
                continue

            if row["owner_id"] is None:
                target = None
                if row["container_id"] is not None:
                    target = _entity_by_id(engine, row["container_id"])
                elif row["room_id"] is not None:
                    target = engine.get_room_by_id(row["room_id"])
                if target is not None and target is not entity:
                    holder = slots.get(id(entity))
                    if holder is not None and holder is not target:
                        holder.remove_content(entity)
                    if entity not in target.contents:
                        target.add_content(entity)
                    slots[id(entity)] = target

            state = json.loads(row["state"] or "{}")
            for flag in ("locked", "open", "lit"):
                if flag in state:
                    setattr(entity, flag, bool(state[flag]))
            slot = state.get("slot")
            if isinstance(slot, str) and slot in RUNTIME_SLOTS:
                entity.equipped_slot = slot

        for row in self._conn.execute(
            "SELECT room_id, direction, locked FROM exits"
        ).fetchall():
            room = engine.get_room_by_id(row["room_id"])
            lock = room.exit_locks.get(row["direction"]) if room else None
            if lock is not None:
                lock.locked = bool(row["locked"])


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
        self._world = WorldStore(conn, now=now, fail=fail)
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

    def skills(self) -> dict[str, int]:
        """The character's skill levels, missing means 0."""
        row = self._conn.execute(
            "SELECT skills FROM players WHERE name = ?", (self.name,)
        ).fetchone()
        if row is None:
            return {}
        try:
            loaded = json.loads(row["skills"] or "{}")
        except ValueError:
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {
            str(skill): int(level)
            for skill, level in loaded.items()
            if isinstance(level, (int, float))
            and not isinstance(level, bool)
            and int(level) >= 0
        }

    def set_skill(self, skill: str, level: int) -> None:
        """Write one skill level; the character row must already exist."""
        skill = skill.strip().lower()
        if not skill or level < 0:
            raise ValueError("a skill needs a name and a non-negative level")
        row = self._conn.execute(
            "SELECT skills FROM players WHERE name = ?", (self.name,)
        ).fetchone()
        if row is None:
            raise StorageWriteError(
                f"skill could not be saved: no character {self.name!r}")
        skills = self.skills()
        skills[skill] = level
        with self._conn:
            self._conn.execute(
                "UPDATE players SET skills = ? WHERE name = ?",
                (json.dumps(skills, sort_keys=True), self.name),
            )

    def capacity_base(self) -> float:
        """The character's durable base carry capacity, default when absent."""
        row = self._conn.execute(
            "SELECT capacity_base FROM players WHERE name = ?", (self.name,)
        ).fetchone()
        if row is None:
            return DEFAULT_CARRY_CAPACITY
        return float(row["capacity_base"])

    def set_capacity_base(self, value: float) -> None:
        """Write one base carry capacity; the character row must already exist."""
        row = self._conn.execute(
            "SELECT id FROM players WHERE name = ?", (self.name,)
        ).fetchone()
        if row is None:
            raise StorageWriteError(
                f"capacity could not be saved: no character {self.name!r}")
        with self._conn:
            self._conn.execute(
                "UPDATE players SET capacity_base = ? WHERE name = ?",
                (float(value), self.name),
            )

    def load(self) -> CharacterState:
        """Read the character's durable room, inventory, skills, and base in one
        call.

        Login restores exactly this: the room key to place the character in,
        the object keys to find in the freshly loaded world, and the base carry
        capacity. The reads are plain queries; nothing here writes or moves a
        row.
        """
        return CharacterState(
            room_id=self.room_id,
            items=self.contents(),
            skills=self.skills(),
            capacity_base=self.capacity_base(),
        )

    def move(self, *, room: Any) -> None:
        """Commit that the player is now in `room`."""
        self._world.commit(
            ChangeSet("move").place(PlayerRef(self.name), at=room))

    def take(self, item: Any, *, room: Any) -> None:
        """Commit that `item` left `room` and became the player's."""
        self._world.commit(
            ChangeSet("take")
            .place(PlayerRef(self.name), at=room)
            .place(item, at=PlayerRef(self.name)))

    def drop(self, item: Any, *, room: Any) -> None:
        """Commit that `item` left the player's hands and is on `room`'s floor."""
        self._world.commit(
            ChangeSet("drop")
            .place(PlayerRef(self.name), at=room)
            .place(item, at=room))

    def sleep(self, *, room: Any | None = None) -> None:
        """Commit that the character is asleep (stone) in `room` (#72).

        `asleep` and `last_seen` are the durable sleep state; the room id is
        refreshed when the caller has it, so a sleeping character is found in
        the room it last stood in. Item drops are separate placements in the
        same commit when the sleeping inventory needs them.
        """
        changes = ChangeSet("sleep")
        if room is not None:
            changes.place(PlayerRef(self.name), at=room)
        changes.set(PlayerRef(self.name), "asleep", True)
        self._world.commit(changes)

    def wake(self) -> None:
        """Commit that the character is awake again (#72).

        The row must already exist: wake follows a verified login. A missing
        row raises `StorageWriteError` rather than inventing a character.
        """
        self._world.commit(
            ChangeSet("wake").set(PlayerRef(self.name), "asleep", False))


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
        self.world = WorldStore(conn, now=now, fail=fail)
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

    def commit(self, changes: ChangeSet) -> None:
        """The engine's one mutation boundary (#131)."""
        self.world.commit(changes)

    def apply_durable_state(self, engine: Any) -> None:
        """Overlay committed placements and flags after the world loads."""
        self.world.apply_durable_state(engine)

    def roster(self) -> tuple[RosterEntry, ...]:
        """Every registered character, awake or asleep, sorted by name (#72).

        A `who` roster is server-wide, including characters whose session is
        gone. It reads the durable rows, so a restart does not hide a sleeping
        character; the engine overlays live socket state on top.
        """
        rows = self.conn.execute(
            "SELECT name, room_id, asleep FROM players "
            "ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return tuple(
            RosterEntry(
                name=row["name"],
                room_id=row["room_id"],
                asleep=bool(row["asleep"]),
            )
            for row in rows
        )

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
        room_id: str | None = None,
    ) -> None:
        """Append one semantic event to the existing `event_log` table (#84).

        The table shipped with the v2.1.0 schema and needs no migration. The
        payload is stored as canonical JSON (`sort_keys`, compact separators)
        so the same bus event always has the same durable text. `actor` and
        `room_id` are the queryable columns; the payload remains the complete
        record. A write failure raises `StorageWriteError`, and the engine's
        effect sink deliberately logs and drops it: effects are not
        authoritative state.
        """
        event_id = f"{event_type}-{uuid.uuid4().hex}"
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO event_log(event_id, ts, type, actor, "
                    "room_id, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, self._now(), event_type, actor, room_id, body),
                )
        except sqlite3.Error as exc:
            raise StorageWriteError(
                f"{event_type} event could not be logged: {exc}") from exc


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


def _transaction(
    conn: sqlite3.Connection,
    op: str,
    body: Callable[[sqlite3.Connection], None],
    fail: FailHook | None,
) -> None:
    if fail is not None:
        try:
            fail(op)
        except StorageWriteError:
            raise
        except Exception as exc:
            raise StorageWriteError(
                f"{op} could not be saved: {exc}") from exc
    try:
        with conn:
            body(conn)
    except sqlite3.Error as exc:
        raise StorageWriteError(f"{op} could not be saved: {exc}") from exc


def _place(
    conn: sqlite3.Connection,
    item: Any,
    at: Any,
    now: str,
) -> None:
    """Write one placement: an object into a location, or a player into a room."""
    if isinstance(item, (Player, PlayerRef)):
        if isinstance(at, Entity) and not isinstance(at, Room):
            raise TypeError("a player can only be placed in a room")
        ensure_room(conn, at)
        player_id = _ensure_player(conn, item.name, room_key(at), now)
        conn.execute(
            "UPDATE players SET room_id = ? WHERE id = ?",
            (room_key(at), player_id),
        )
        return

    room_id: str | None = None
    owner_id: int | None = None
    container_id: str | None = None
    if isinstance(at, Room):
        ensure_room(conn, at)
        room_id = room_key(at)
    elif isinstance(at, Player):
        initial = room_key(at.location) if at.location is not None else None
        if at.location is not None:
            ensure_room(conn, at.location)
        owner_id = _ensure_player(conn, at.name, initial, now)
    elif isinstance(at, PlayerRef):
        owner_id = _ensure_player(conn, at.name, None, now)
    elif isinstance(at, Entity):
        _ensure_object(conn, at)
        container_id = object_key(at)
    else:
        # A room-shaped value from the content model (tests and `admin seed`
        # callers use it) is still a floor destination.
        ensure_room(conn, at)
        room_id = room_key(at)

    _ensure_object(
        conn, item, room_id=room_id, owner_id=owner_id,
        container_id=container_id)
    conn.execute(
        "UPDATE objects SET room_id = ?, owner_id = ?, container_id = ? "
        "WHERE id = ?",
        (room_id, owner_id, container_id, object_key(item)),
    )


def _set_flag(
    conn: sqlite3.Connection,
    entity: Any,
    flag: str,
    value: Any,
    now: str,
) -> None:
    """Write one durable flag: a player's sleep state or an object's state JSON."""
    if isinstance(entity, (Player, PlayerRef)):
        if flag != "asleep":
            raise ValueError(f"unknown player flag {flag!r}")
        row = conn.execute(
            "SELECT id FROM players WHERE name = ?", (entity.name,)
        ).fetchone()
        if row is None:
            if not value:
                raise StorageWriteError(
                    f"wake could not be saved: no character {entity.name!r}")
            player_id = _ensure_player(conn, entity.name, None, now)
        else:
            player_id = int(row["id"])
        conn.execute(
            "UPDATE players SET asleep = ?, last_seen = ? WHERE id = ?",
            (1 if value else 0, now, player_id),
        )
        return

    if flag not in ("locked", "open", "lit", "slot"):
        raise ValueError(f"unknown object flag {flag!r}")
    _ensure_object(conn, entity)
    key = object_key(entity)
    row = conn.execute(
        "SELECT state FROM objects WHERE id = ?", (key,)
    ).fetchone()
    state = json.loads(row["state"] or "{}")
    state[flag] = value
    conn.execute(
        "UPDATE objects SET state = ? WHERE id = ?",
        (json.dumps(state, sort_keys=True), key),
    )


def _set_exit(conn: sqlite3.Connection, change: ExitLockChange) -> None:
    """Write an exit's locked flag, and the reciprocal row when it is shared."""
    room = change.room
    key_id = object_key(change.key_item) if change.key_item is not None else None
    if change.key_item is not None:
        _ensure_object(conn, change.key_item)
    _write_exit_row(
        conn, room, change.direction, change.target, key_id,
        change.label, change.locked)

    if change.target is None:
        return
    opposite = OPPOSITE_EXITS.get(change.direction)
    if opposite is None:
        return
    _write_exit_row(
        conn, change.target, opposite, room, key_id, change.label,
        change.locked)


def _write_exit_row(
    conn: sqlite3.Connection,
    room: Room,
    direction: str,
    target: Room | None,
    key_id: str | None,
    label: str | None,
    locked: bool,
) -> None:
    ensure_room(conn, room)
    if target is not None:
        ensure_room(conn, target)
        target_id = room_key(target)
    else:
        row = conn.execute(
            "SELECT target_id FROM exits WHERE room_id = ? AND direction = ?",
            (room_key(room), direction),
        ).fetchone()
        if row is None:
            return
        target_id = row["target_id"]
    conn.execute(
        "INSERT INTO exits(room_id, direction, target_id, locked, key_id, label) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(room_id, direction) DO UPDATE SET locked = excluded.locked",
        (
            room_key(room), direction, target_id, 1 if locked else 0,
            key_id, label,
        ),
    )


def _object_slots(engine: Any) -> dict[int, Entity]:
    """Every loaded object's current holder, keyed by the entity's identity."""
    slots: dict[int, Entity] = {}

    def walk(holder: Entity) -> None:
        for item in holder.contents:
            if isinstance(item, Player):
                continue
            slots[id(item)] = holder
            walk(item)

    rooms = (engine.index.rooms_by_id.values()
             if engine.index is not None else engine.rooms.values())
    for room in rooms:
        walk(room)
    return slots


def _entity_by_id(engine: Any, key: str) -> Entity | None:
    """Find a loaded object by durable id, indexed first and walked second."""
    if engine.index is not None:
        entity = engine.index.object_by_id(key)
        if entity is not None:
            return entity
    rooms = (engine.index.rooms_by_id.values()
             if engine.index is not None else engine.rooms.values())
    for room in rooms:
        found = _find_in(room, key)
        if found is not None:
            return found
    return None


def _find_in(holder: Entity, key: str) -> Entity | None:
    for item in holder.contents:
        if isinstance(item, Player):
            continue
        if object_key(item) == key:
            return item
        found = _find_in(item, key)
        if found is not None:
            return found
    return None


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
