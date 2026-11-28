"""One owner for character admission: register, resume, suspend, roster.

The transport binds the live avatar this module returns. It does not read
`PlayerStore`, call `restore_player`/`wake_player`, or decide how a character
sleeps (#138). Admission composes auth, the write-through store, and the
engine, so login uses the room id auth already resolved and the sleep path has
one owner.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from src.mud.auth import (
    AuthError,
    Login,
    Registration,
    login_character,
    normalize_name,
    register_character,
)
from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine
from src.mud.storage.store import (
    ChangeSet,
    FailHook,
    RosterEntry,
    StorageWriteError,
    WriteThroughStore,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RegistrationResult:
    """A new character's durable registration and its live avatar."""

    registration: Registration
    player: Player


@dataclass(frozen=True)
class ResumeResult:
    """A verified login and the live avatar the transport binds."""

    login: Login
    player: Player


@dataclass(frozen=True)
class SuspendResult:
    """What the sleep path did to one character.

    `persisted` is False when the sleep commit failed; the character is still
    marked asleep in memory, matching the pre-#138 server behavior. `dropped`
    lists only the `dropped_on_sleep` items that left the inventory.
    """

    name: str
    room: Room | None
    dropped: tuple[Entity, ...]
    persisted: bool


@dataclass(frozen=True)
class GuestResult:
    """A guest's live avatar; there is no durable registration."""

    player: Player


@dataclass(frozen=True)
class GuestSpill:
    """The room and carried items a departing guest left behind."""

    name: str
    room: Room | None
    dropped: tuple[Entity, ...]


class CharacterAdmission:
    """Compose auth, the store, and the engine for one character lifecycle."""

    def __init__(
        self,
        engine: Engine,
        conn: sqlite3.Connection | None,
        *,
        now: Callable[[], str] = _utc_now,
        fail: FailHook | None = None,
    ):
        self.engine = engine
        self.conn = conn
        self._now = now
        self.store = (
            WriteThroughStore(conn, now=now, fail=fail)
            if conn is not None else None
        )
        # `who` reads the durable roster through admission, not a second path.
        # A guest-only admission has no durable roster; do not replace an
        # engine's existing provider or store in that case.
        self._roster_provider = self.roster
        if self.store is not None:
            if (self.engine.store is not None
                    and self.engine.store is not self.store):
                logger.warning(
                    "admission is replacing an attached engine store")
            self.engine.roster_provider = self._roster_provider
            self.engine.attach_store(self.store)
            # The mutable half of the world is durable (#131): apply committed
            # placements and flags after the YAML world loads.
            self.store.apply_durable_state(self.engine)

    def register(self, name: object) -> RegistrationResult:
        """Create a character and return the key plus the live avatar."""
        normalized = normalize_name(name)
        if self.engine.get_player(normalized) is not None:
            raise AuthError(
                "name_taken", f"The name {normalized!r} is already in use.")
        if self.conn is None:
            raise AuthError(
                "internal", "The server cannot create characters right now.")
        start_room = self.engine.current_location
        if start_room is None:
            raise AuthError(
                "internal", "The server cannot create characters right now.")
        registration = register_character(
            self.conn, normalized, start_room=start_room, now=self._now)
        player = self.engine.register_player(registration.name)
        return RegistrationResult(registration=registration, player=player)

    def admit_guest(self, name: str) -> GuestResult:
        """Register a live guest with no durable character."""
        try:
            player = self.engine.register_player(name)
        except ValueError:
            raise AuthError(
                "name_taken", f"The name {name!r} is already in use."
            ) from None
        return GuestResult(player=player)

    def spill_guest(self, name: str) -> GuestSpill:
        """Drop a guest's carried items into the room, then remove the guest."""
        player = self.engine.get_player(name)
        if player is None:
            return GuestSpill(name=name, room=None, dropped=())
        room = player.location
        items = list(player.contents)
        if room is not None:
            changes = ChangeSet("spill")
            for item in items:
                changes.place(item, at=room).set(item, "slot", None)
            if self.store is not None:
                self.store.commit(changes)
            for item in items:
                player.unequip(item)
                player.remove_content(item)
                room.add_content(item)
        self.engine.remove_player(name)
        return GuestSpill(name=name, room=room, dropped=tuple(items))

    def resume(self, key: object) -> ResumeResult:
        """Verify a key and return the character in its durable room.

        The room id and inventory come from the same `Login` lookup. No
        `PlayerStore.load()` re-read happens here.
        """
        if self.conn is None:
            raise AuthError(
                "internal", "The server cannot check keys right now.")
        login = login_character(self.conn, key)
        existing = self.engine.get_player(login.name)
        if existing is not None and not existing.asleep:
            raise AuthError(
                "name_taken", "That character is already in play.")

        if existing is not None:
            player = existing
        else:
            player = self.engine.register_player(login.name)
            self.engine.restore_player(
                player,
                room=self.engine.get_room_by_id(login.room_id),
                item_keys=login.items,
            )

        try:
            self.engine.wake_player(login.name)
        except StorageWriteError:
            logger.exception("could not wake %s", login.name)
            if existing is None:
                self.engine.remove_player(login.name)
            raise AuthError(
                "internal", "The character could not be woken right now."
            ) from None

        return ResumeResult(login=login, player=player)

    def suspend(self, name: str) -> SuspendResult:
        """Run the one sleep path: drop flagged items and stone the character."""
        player = self.engine.get_player(name)
        if player is None:
            return SuspendResult(name, None, (), True)
        room = player.location
        try:
            dropped = self.engine.sleep_player(name)
            persisted = True
        except StorageWriteError:
            logger.exception("could not persist sleep for %s", name)
            player.asleep = True
            dropped = []
            persisted = False
        return SuspendResult(
            name=name, room=room, dropped=tuple(dropped), persisted=persisted)

    def roster(self) -> tuple[RosterEntry, ...]:
        """Every registered character, awake or asleep, sorted by name."""
        if self.store is None:
            return ()
        return self.store.roster()

    def detach(self) -> None:
        """Release the engine's store and roster provider; keep the connection."""
        if self.engine.roster_provider is self._roster_provider:
            self.engine.roster_provider = None
        if self.store is not None:
            self.engine.attach_store(None)
