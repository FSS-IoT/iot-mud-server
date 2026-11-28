"""Protocol-v2 websocket server.

This server speaks the merged v2 contract and delegates all game logic to
`Engine` through the adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import websockets

from src.mud.auth import (
    AuthError,
    login_character,
    normalize_name,
    register_character,
)
from src.mud.config import (
    AuthConfig,
    ConfigError,
    FeaturesConfig,
    LimitsConfig,
    load_config,
)
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import (
    _object_data,
    _room_id,
    build_room_state,
    execute,
)
from src.mud.protocol.v2 import (
    AUTH_MODES,
    CAPABILITIES,
    FEATURE_LEVEL,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    UNKNOWN_SERVER_COMMIT,
    envelope,
    is_server_commit,
    utc_now,
)
from src.mud.storage.db import DatabaseError, open_database
from src.mud.storage.keys import object_key
from src.mud.storage.store import (
    PlayerStore,
    StorageWriteError,
    WriteThroughStore,
)
from src.mud.world.wander import NpcMove

logger = logging.getLogger(__name__)

SERVER_COMMIT_ENV = "MUD_SERVER_COMMIT"

# Every typed failure of an auth exchange counts toward the per-connection
# budget. `internal`, `already_joined`, and `rate_limited` do not: those are a
# server fault, a session-state error, and the limiter itself.
AUTH_FAILURE_CODES = frozenset(
    {"invalid_key", "invalid_name", "name_taken", "bad_args"})
RATE_LIMITED_MESSAGE = (
    "Too many authentication attempts; wait and try again.")
COMMAND_RATE_LIMITED_MESSAGE = "Too many commands; wait and try again."
# The protocol reserves 4004 for an idle timeout (docs/PROTOCOL.md). The
# character is asleep before this close is sent.
IDLE_TIMEOUT_CLOSE = 4004

# Repository root: <root>/src/mud/net/v2_server.py
REPO_ROOT = Path(__file__).resolve().parents[3]


def read_git_head(root: Path) -> str | None:
    """Return the 40-character git HEAD for `root`, or None.

    A released copy without `.git` (a Nix store path, an exported tarball)
    returns None without running git at all.
    """
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = result.stdout.strip()
    return head if is_server_commit(head) else None


@lru_cache(maxsize=1)
def _git_commit() -> str:
    """The checkout's commit, resolved at most once per process."""
    return read_git_head(REPO_ROOT) or UNKNOWN_SERVER_COMMIT


def resolve_server_commit(explicit: str | None = None) -> str:
    """Resolve the commit reported in `hello.data.server_commit`.

    Order: an explicit value (tests and embedders), `MUD_SERVER_COMMIT`, then
    `git rev-parse HEAD` at the repository root. Only a 40-character lowercase
    hex commit counts; anything else is passed over for the next source.
    `"unknown"` when nothing resolves.
    """
    if is_server_commit(explicit):
        return explicit
    from_env = os.environ.get(SERVER_COMMIT_ENV)
    if is_server_commit(from_env):
        return from_env
    return _git_commit()


def _read_world_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """One non-empty `world_meta` value, or None when it is absent."""
    row = conn.execute(
        "SELECT value FROM world_meta WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) and value.strip() else None


def read_world_epoch(conn: sqlite3.Connection) -> str | None:
    """The seeded world's epoch, or None when the database has no world yet.

    `admin seed` owns the value; the server only reports it. A client compares
    `hello.data.world.epoch` across connections to notice a world reset and
    drop cached maps (docs/PROTOCOL.md).
    """
    return _read_world_meta(conn, "epoch")


def read_world_name(conn: sqlite3.Connection) -> str | None:
    """The seeded world's name, or None when the database has no world yet.

    `admin seed` writes `world_meta.name`; `hello`, `session.ok`, and `auth.ok`
    report it beside the epoch. A character-only database has no name row, so
    the server keeps its constructor default for legacy and no-DB tests.
    """
    return _read_world_meta(conn, "name")


@dataclass
class TokenBucket:
    """A monotonic-clock token bucket for one connection (#85).

    The bucket starts full, which makes `command_burst` the number of commands
    a client may spend back to back. The auth-failure limiter in `Session` is
    a separate list and is never touched by commands or vice versa.
    """

    capacity: float
    rate: float
    tokens: float
    updated_at: float

    @classmethod
    def full(cls, capacity: float, rate: float, now: float) -> TokenBucket:
        return cls(
            capacity=float(capacity), rate=rate,
            tokens=float(capacity), updated_at=now,
        )

    def take(self, now: float) -> bool:
        """Spend one command token, refilling first on the injected clock."""
        if now > self.updated_at:
            elapsed = now - self.updated_at
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.updated_at = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


@dataclass
class Session:
    """One websocket connection and the policy that belongs to it (#134).

    The session owns what is true only of this connection: its socket, the
    outbound sequence, the identity phase, the auth-failure budget, the command
    bucket, the last client message, and the idle deadline. `SessionTable`
    owns presence and delivery; `V2Server` owns accept, hello, and bootstrap.
    """

    ws: Any
    clock: Callable[[], float] = time.monotonic
    # Both limiters read this connection's clock and config. They stay
    # separate: auth failures never spend command tokens and vice versa.
    auth_limits: AuthConfig = field(default_factory=AuthConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    seq: int = 0
    player_name: str | None = None
    joined: bool = False
    # True for the durable character paths (`auth` register/login). A guest
    # `join` leaves it False and keeps the v2.0 remove-on-disconnect behavior.
    authenticated: bool = False
    # Auth failure times on this connection's clock. There is no shared table
    # and no entry keyed by a name, so a throttled connection can never lock a
    # character name.
    auth_failures: list[float] = field(default_factory=list)
    # Command-rate bucket, also per connection. It is lazily built on the
    # first `input`, so auth traffic never spends a command token.
    command_bucket: TokenBucket | None = None
    # The last client message, on this connection's clock. Server pushes do
    # not refresh this; only a message from the client proves it is not idle.
    last_activity: float = field(init=False)
    # Set once the #72 sleep path has run for this session, so an idle close
    # followed by the disconnect path cannot sleep and broadcast leave twice.
    slept: bool = False

    def __post_init__(self) -> None:
        self.last_activity = self.clock()

    @property
    def phase(self) -> str:
        """The identity phase: `anonymous`, `guest`, or `authenticated`."""
        if not self.joined:
            return "anonymous"
        return "authenticated" if self.authenticated else "guest"

    def bind(self, name: str, *, authenticated: bool) -> None:
        """Bind this connection to its one character identity."""
        self.player_name = name
        self.joined = True
        self.authenticated = authenticated

    def touch(self) -> None:
        """Record a client message; server pushes do not refresh idle."""
        self.last_activity = self.clock()

    @property
    def idle_deadline(self) -> float:
        """When this session's idle timeout would fire."""
        return self.last_activity + self.limits.idle_timeout_seconds

    def idle_expired(self) -> bool:
        """True once the clock reaches this session's idle deadline."""
        return self.clock() >= self.idle_deadline

    def note_auth_failure(self) -> None:
        """Count one typed auth failure against this connection."""
        self.auth_failures.append(self.clock())

    def auth_is_limited(self) -> bool:
        """True when this connection spent its failure budget in the window.

        Expired failures are pruned here, which is the whole recovery path:
        once the clock is a window past them, the budget is empty again.
        """
        now = self.clock()
        window = self.auth_limits.window_seconds
        recent = [at for at in self.auth_failures if now - at < window]
        self.auth_failures[:] = recent
        return len(recent) >= self.auth_limits.max_attempts

    def take_command(self) -> bool:
        """Spend one command token, building the bucket full on first use."""
        if self.command_bucket is None:
            self.command_bucket = TokenBucket.full(
                self.limits.command_burst,
                self.limits.commands_per_second,
                self.clock(),
            )
        return self.command_bucket.take(self.clock())

    async def send(self, type_: str, data: dict, *, direct: bool = False,
                   msg_id: Any = None) -> None:
        self.seq += 1
        message = envelope(
            type_, data, seq=self.seq,
            msg_id=msg_id if direct else None,
        )
        await self.ws.send(json.dumps(message))

    async def send_error(self, code: str, message: str,
                         msg_id: Any = None) -> None:
        await self.send(
            "error",
            {"code": code, "message": message, "text": message},
            direct=msg_id is not None, msg_id=msg_id,
        )

    async def send_auth_error(self, code: str, message: str,
                              msg_id: Any = None) -> None:
        """A typed auth failure: direct, echoes the request id, never the key."""
        if code in AUTH_FAILURE_CODES:
            self.note_auth_failure()
        await self.send(
            "auth.error",
            {"code": code, "message": message, "text": message},
            direct=True, msg_id=msg_id,
        )

    async def close(self, code: int = 1000) -> None:
        """Close this session's socket; an already-closed peer is not an error."""
        try:
            await self.ws.close(code=code)
        except websockets.exceptions.ConnectionClosed:
            pass


class SessionTable:
    """Presence and delivery for every live connection (#134).

    This is the one owner of both socket maps. A session is in `_sessions`
    exactly while its handler is live, and `_ws_by_name` never names a socket
    absent from `_sessions`; `bind` and `forget` are the only writers.
    """

    def __init__(self, engine: Engine):
        self.engine = engine
        self._sessions: dict[Any, Session] = {}
        self._ws_by_name: dict[str, Any] = {}

    @property
    def active(self) -> int:
        """The number of live session handlers."""
        return len(self._sessions)

    @property
    def is_quiescent(self) -> bool:
        """True when no connection handler has a session left."""
        return not self._sessions

    def active_sessions(self) -> tuple[Session, ...]:
        """A snapshot for the server's sweep; callers never mutate it."""
        return tuple(self._sessions.values())

    def bind(self, session: Session) -> None:
        """Index a session by socket and, once known, by character name."""
        self._sessions[session.ws] = session
        if session.player_name is not None:
            self._ws_by_name[session.player_name] = session.ws

    def session_for(self, ws) -> Session | None:
        """The live session for a socket, or None."""
        return self._sessions.get(ws)

    def session_for_name(self, name: str) -> Session | None:
        """The live session for `name`, pruning a stale name mapping."""
        ws = self._ws_by_name.get(name)
        if ws is None:
            return None
        session = self._sessions.get(ws)
        if session is None or session.player_name != name:
            if self._ws_by_name.get(name) is ws:
                self._ws_by_name.pop(name, None)
            return None
        return session

    def socket_for_name(self, name: str) -> Any | None:
        """The socket bound to `name`, or None. The supported presence query."""
        session = self.session_for_name(name)
        return session.ws if session is not None else None

    def forget(self, session: Session) -> None:
        """Drop this session from both maps; safe to call twice."""
        self._sessions.pop(session.ws, None)
        name = session.player_name
        if name is not None and self._ws_by_name.get(name) is session.ws:
            self._ws_by_name.pop(name, None)

    async def send_to_player(self, name: str, type_: str, data: dict, *,
                             direct: bool = False,
                             msg_id: Any = None) -> bool:
        """Deliver one event to `name`; False when they have no live session."""
        session = self.session_for_name(name)
        if session is None:
            return False
        try:
            await session.send(type_, data, direct=direct, msg_id=msg_id)
        except websockets.exceptions.ConnectionClosed:
            # A peer can close between the lookup and the send. Drop both
            # mappings so later pushes skip the socket; its own handler still
            # owns the leave/sleep transition.
            self.forget(session)
            return False
        return True

    async def broadcast_room(self, room_name: str | None, type_: str,
                             data: dict,
                             exclude_name: str | None = None) -> None:
        """Send one event to every other live session in a room."""
        if room_name is None:
            return
        room = self.engine.get_room(room_name)
        if room is None:
            return
        for player in self.engine.players_in_room(room):
            if player.name == exclude_name:
                continue
            await self.send_to_player(player.name, type_, data)

    async def disconnect(self, session: Session) -> None:
        """Run the leave/sleep policy, then forget this connection.

        The idle sweep and the handler's `finally` share this path: an
        authenticated character sleeps in place (#72), a guest is removed, and
        both maps drop the socket. `Session.slept` and `forget` make it
        idempotent.
        """
        name = session.player_name
        if name is not None:
            player = self.engine.get_player(name)
            if player is not None:
                room = player.location
                if session.authenticated:
                    await self._sleep(session)
                else:
                    # The explicit guest path keeps the v2.0
                    # remove-on-disconnect behavior: no durable character.
                    # Spill the carried items first, so a guest cannot
                    # permanently remove a world item.
                    if room is not None:
                        for item in list(player.contents):
                            player.remove_content(item)
                            room.add_content(item)
                            await self.broadcast_room(
                                room.name, "object.dropped", {
                                    "object": _object_data(item,
                                                           room.contents),
                                    "by": name,
                                    "self": False,
                                    "text": f"{name} drops the {item.name}.",
                                }, exclude_name=name)
                        await self.broadcast_room(room.name, "room.leave", {
                            "room": {"id": _room_id(room), "name": room.name},
                            "actor": {"name": name},
                            "reason": "disconnect",
                            "text": f"{name} has left.",
                        }, exclude_name=name)
                    self.engine.remove_player(name)
        self.forget(session)

    async def expire_idle(self) -> list[Session]:
        """Sleep and close every authenticated session past its deadline.

        `Session` owns the deadline; the table owns the sweep. Guest sessions
        have no durable character and are left alone, matching #85.
        """
        expired: list[Session] = []
        for session in self.active_sessions():
            if not session.authenticated or session.player_name is None:
                continue
            if session.slept or not session.idle_expired():
                continue
            await self.disconnect(session)
            await session.close(IDLE_TIMEOUT_CLOSE)
            expired.append(session)
        return expired

    async def _sleep(self, session: Session) -> None:
        """Run the #72 sleep path once for an authenticated session.

        Works for both a disconnect (`disconnect`) and an idle timeout
        (`expire_idle`). `session.slept` makes it idempotent, so the idle close
        followed by the handler's disconnect cannot sleep twice or broadcast
        two `room.leave` events.
        """
        name = session.player_name
        if name is None or session.slept:
            return
        session.slept = True
        player = self.engine.get_player(name)
        if player is None:
            return
        room = player.location
        # The durable character sleeps in place (#72). Only items marked
        # `dropped_on_sleep` drop into the room; the rest of the inventory
        # stays. The store commits each drop before the engine moves it.
        dropped = []
        try:
            dropped = self.engine.sleep_player(name)
        except StorageWriteError:
            logger.exception("could not persist sleep for %s", name)
            player.asleep = True
        if room is None:
            return
        await self.broadcast_room(room.name, "room.leave", {
            "room": {"id": _room_id(room), "name": room.name},
            "actor": {"name": name},
            "reason": "disconnect",
            "asleep": True,
            "stone": True,
            "dropped": [
                {"id": object_key(item), "name": item.name}
                for item in dropped
            ],
            "text": f"{name} turns to stone.",
        }, exclude_name=name)


class V2Server:
    """A v2 terminal over the authoritative engine."""

    def __init__(
        self,
        engine: Engine,
        host: str = "localhost",
        port: int = 0,
        world_name: str = "Starting Village",
        epoch: str | None = None,
        db_path: str | Path | None = None,
        server_commit: str | None = None,
        auth_required: bool = True,
        guest_ok: bool = True,
        auth_limits: AuthConfig | None = None,
        limits: LimitsConfig | None = None,
        features: FeaturesConfig | None = None,
        clock: Callable[[], float] | None = None,
        idle_poll_seconds: float = 1.0,
        tick_seconds: float = 1.0,
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.world_name = world_name
        self.epoch = epoch or utc_now()
        self.db_path = Path(db_path) if db_path is not None else None
        self.server_commit = resolve_server_commit(server_commit)
        # Auth is the persistent path. `guest_ok` keeps the v2.0 guest join as
        # a fallback for testing and old clients; `auth_required` controls
        # whether `hello` advertises the key modes. A deployment that sets
        # `guest_ok=False` refuses `join` with `not_authenticated`.
        self.auth_required = auth_required
        self.guest_ok = guest_ok
        # The limiter is per connection and in memory, and the window is read
        # from this clock, so tests advance time instead of sleeping.
        self.auth_limits = auth_limits if auth_limits is not None else AuthConfig()
        # Command limits and the idle timeout are independent of the auth
        # limiter. Both read the same injected clock, so tests advance time
        # instead of sleeping.
        self.limits = limits if limits is not None else LimitsConfig()
        # Optional behavior switches (#84). `effects=false` (the default)
        # keeps authored effects on the bus and in `event_log` while sending
        # no `effect` event to any client.
        self.features = (
            features if features is not None else FeaturesConfig())
        self.clock = clock if clock is not None else time.monotonic
        self.idle_poll_seconds = idle_poll_seconds
        # The world tick is wall-clock paced by the server, but game logic
        # never reads the clock: one tick is one step of the seeded wanderer.
        # Tests shorten it or call `wander_tick` directly.
        self.tick_seconds = tick_seconds
        self.db: sqlite3.Connection | None = None
        self.store: WriteThroughStore | None = None
        self._stop_event = asyncio.Event()
        self._server = None
        self._idle_task: asyncio.Task | None = None
        self._wander_task: asyncio.Task | None = None
        # Presence, delivery, and idle expiry live in one collaborator; the
        # server never owns either socket map (#134).
        self.sessions = SessionTable(engine)
        # Serializes close(): a second caller waits for the first cleanup
        # instead of racing it, and a repeated close is a no-op.
        self._close_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Room delivery
    # ------------------------------------------------------------------

    async def _announce_connect(self, player) -> None:
        """Tell the character's room that it arrived."""
        room = player.location
        if room is None:
            return
        await self.sessions.broadcast_room(room.name, "room.enter", {
            "room": {"id": _room_id(room), "name": room.name},
            "actor": {"name": player.name},
            "reason": "connect",
            "text": f"{player.name} has arrived.",
        }, exclude_name=player.name)

    async def _relay_effect(self, event_type: str, data: Any) -> None:
        """Send one triggered effect to its room when the flag allows (#84).

        The engine publishes `effect` for every authored trigger whether or
        not this feature is on; the log sink has already appended the row by
        the time this runs. With `features.effects=false` this is a deliberate
        no-op, which is why the disable switch cannot change gameplay.
        """
        if not self.features.effects or not isinstance(data, dict):
            return
        room = data.get("room")
        room_name = room.get("name") if isinstance(room, dict) else None
        await self.sessions.broadcast_room(
            room_name, "effect", data, exclude_name=None)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_join(self, session: Session, msg: dict) -> None:
        msg_id = msg.get("id")
        if session.joined:
            await session.send_error("already_joined",
                                     "This connection has already joined.",
                                     msg_id)
            return

        if not self.guest_ok:
            await session.send_error(
                "not_authenticated",
                "This server requires authentication before joining.",
                msg_id)
            return

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await session.send_error("malformed_message",
                                     "The data field must be an object.",
                                     msg_id)
            return
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            await session.send_error("bad_args",
                                     "Join needs a non-empty name.", msg_id)
            return
        name = name.strip()

        try:
            player = self.engine.register_player(name)
        except ValueError:
            await session.send_error(
                "name_taken", f"The name {name!r} is already in use.", msg_id)
            return

        session.bind(name, authenticated=False)
        self.sessions.bind(session)

        await session.send("session.ok", {
            "player": {"name": name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "guest",
            "text": f"Welcome, {name}.",
        }, direct=True, msg_id=msg_id)

        await session.send("room.state",
                           build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _handle_auth(self, session: Session, msg: dict) -> None:
        """Route one `auth` message, enforcing the per-connection budget first.

        A connection over its budget gets `auth.error{rate_limited}` before its
        data is read, so a flood cannot keep probing shapes or keys. The socket
        stays open: a normal bad key is never a close, and the limit is not
        policy abuse.
        """
        msg_id = msg.get("id")
        if session.joined:
            await session.send_auth_error(
                "already_joined",
                "This connection has already joined.", msg_id)
            return

        if session.auth_is_limited():
            await session.send_auth_error(
                "rate_limited", RATE_LIMITED_MESSAGE, msg_id)
            return

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await session.send_error("malformed_message",
                                     "The data field must be an object.",
                                     msg_id)
            return

        mode = data.get("mode")
        if mode == "register":
            await self._auth_register(session, data, msg_id)
            return
        if mode == "login":
            await self._auth_login(session, data, msg_id)
            return
        await session.send_auth_error(
            "bad_args", "Auth mode must be 'register' or 'login'.", msg_id)

    async def _auth_register(self, session: Session, data: dict,
                             msg_id: Any) -> None:
        """Register a name, issue its first key, and enter the world."""
        try:
            name = normalize_name(data.get("name"))
        except AuthError as exc:
            await session.send_auth_error(exc.code, str(exc), msg_id)
            return

        if self.engine.get_player(name) is not None:
            await session.send_auth_error(
                "name_taken", f"The name {name!r} is already in use.", msg_id)
            return

        start_room = self.engine.current_location
        if self.db is None or start_room is None:
            await session.send_auth_error(
                "internal",
                "The server cannot create characters right now.", msg_id)
            return

        try:
            registration = register_character(
                self.db, name, start_room=start_room)
        except AuthError as exc:
            await session.send_auth_error(exc.code, str(exc), msg_id)
            return

        player = self.engine.register_player(registration.name)
        session.bind(registration.name, authenticated=True)
        self.sessions.bind(session)

        # The key appears here and nowhere else: not in the session, the room
        # state, a broadcast, a log line, or the database.
        await session.send("auth.ok", {
            "player": {"name": registration.name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "register",
            "key": registration.key,
            "text": (f"Welcome, {registration.name}. Store this key; it is "
                     "shown only once."),
        }, direct=True, msg_id=msg_id)

        await session.send("room.state",
                           build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _auth_login(self, session: Session, data: dict,
                          msg_id: Any) -> None:
        """Verify a key and bind the session to its durable character.

        The request has no name: `login_character` finds the row by key id and
        verifies the secret. A bad secret, an unknown id, and a revoked key all
        arrive here as one `invalid_key`; the key itself is never echoed or
        logged.
        """
        if self.db is None:
            await session.send_auth_error(
                "internal", "The server cannot check keys right now.", msg_id)
            return

        try:
            login = login_character(self.db, data.get("key"))
        except AuthError as exc:
            await session.send_auth_error(exc.code, str(exc), msg_id)
            return

        existing = self.engine.get_player(login.name)
        if existing is not None and not existing.asleep:
            await session.send_auth_error(
                "name_taken", "That character is already in play.", msg_id)
            return

        # Login wakes the character where the store left it, carrying what the
        # store says it owns (#71). The same-process case already has the room
        # and the remaining inventory in memory because sleep kept them (#72);
        # after a restart the world was reloaded, so the engine has to move
        # those objects out of the room or container the loader authored them
        # into.
        if existing is not None:
            player = existing
        else:
            character = PlayerStore(self.db, login.name).load()
            player = self.engine.register_player(login.name)
            self.engine.restore_player(
                player,
                room=self.engine.get_room_by_id(character.room_id),
                item_keys=character.items,
            )

        try:
            self.engine.wake_player(login.name)
        except StorageWriteError:
            logger.exception("could not wake %s", login.name)
            if existing is None:
                self.engine.remove_player(login.name)
            await session.send_auth_error(
                "internal", "The character could not be woken right now.",
                msg_id)
            return

        session.bind(login.name, authenticated=True)
        self.sessions.bind(session)

        # No key field: a login response never repeats the credential.
        await session.send("auth.ok", {
            "player": {"name": login.name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "login",
            "text": f"Welcome back, {login.name}.",
        }, direct=True, msg_id=msg_id)

        await session.send("room.state",
                           build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _handle_input(self, session: Session, msg: dict) -> int | None:
        msg_id = msg.get("id")
        if not session.joined or session.player_name is None:
            await session.send_error(
                "not_joined", "Join before sending commands.", msg_id)
            return None
        if session.slept:
            # An idle frame that was already in flight must not run a command
            # for a stone character. The idle path is closing with this code.
            return IDLE_TIMEOUT_CLOSE
        if msg_id is None:
            await session.send_error("bad_args", "Input needs a request id.")
            return None

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await session.send_error("malformed_message",
                                     "The data field must be an object.",
                                     msg_id)
            return None
        text = data.get("text")
        if not isinstance(text, str):
            await session.send_error(
                "bad_args", "Input needs a text string.", msg_id)
            return None

        # `quit`/`exit` are commands in the v1 vocabulary and in the protocol's
        # command map. Honor them at the session layer; the cleanup path then
        # broadcasts room.leave and closes 1000. A quit is never rate-limited:
        # a client must always be able to leave.
        if text.strip().lower() in ("quit", "exit"):
            return 1000

        # The command bucket is separate from the auth-failure budget. A
        # limited command is a direct typed error that echoes the request id;
        # the socket stays open and the bucket refills on the clock.
        if not session.take_command():
            await session.send_error(
                "rate_limited", COMMAND_RATE_LIMITED_MESSAGE, msg_id)
            return None

        result = await execute(self.engine, session.player_name, text)
        player = self.engine.get_player(session.player_name)
        current_room = player.location if player is not None else None

        for outbound in result.events:
            if outbound.target == "actor":
                await session.send(
                    outbound.type, outbound.data,
                    direct=outbound.direct,
                    msg_id=msg_id if outbound.direct else None,
                )
            elif outbound.target == "player":
                # A private push to one named character (#75). It is not a
                # reply to the actor's request id and it never reaches the
                # room. A target that left between resolution and delivery
                # gets nothing; there is no offline queue.
                if outbound.player_name is not None:
                    await self.sessions.send_to_player(
                        outbound.player_name, outbound.type, outbound.data,
                        direct=outbound.direct,
                    )
            else:
                room_name = outbound.room_name or (
                    current_room.name if current_room is not None else None)
                await self.sessions.broadcast_room(
                    room_name, outbound.type, outbound.data,
                    exclude_name=session.player_name,
                )
        return None

    async def _route(self, session: Session, msg: Any) -> int | None:
        """Route one decoded message. Returns a close code to stop, else None."""
        if not isinstance(msg, dict):
            await session.send_error("malformed_message",
                                     "A message must be a JSON object.")
            return None

        if "v" not in msg or msg.get("v") is None:
            await session.send_error("malformed_message",
                                     "Missing protocol version.")
            return None
        if msg.get("v") != PROTOCOL_VERSION:
            await session.send_error(
                "unsupported_version",
                f"Protocol {msg.get('v')!r} is not supported; this server "
                f"speaks {PROTOCOL_VERSION}.",
            )
            return 4003

        kind = msg.get("type")
        if kind == "auth":
            await self._handle_auth(session, msg)
            return None
        if kind == "join":
            await self._handle_join(session, msg)
            return None
        if kind == "input":
            return await self._handle_input(session, msg)
        if kind == "quit":
            return 1000

        await session.send_error(
            "unknown_message", f"Unknown message type: {kind!r}.",
            msg.get("id"))
        return None

    async def _idle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.idle_poll_seconds)
                return
            except TimeoutError:
                pass
            try:
                await self.sessions.expire_idle()
            except Exception:
                logger.exception("idle sweep failed")

    async def wander_tick(self) -> list[NpcMove]:
        """Run one 1 Hz world tick and tell each affected room (#82).

        The engine owns the seed, schedule, and mutation; this method is only
        the transport fan-out. The old room receives `npc.leave`, the new room
        `npc.enter`; both carry the structured `npc`, `from`, `to`, and
        `direction` fields. `room.state` (on `look` or a move) remains the
        resynchronization path for clients that ignore the events.

        The broadcast reuses `SessionTable.broadcast_room` unchanged and
        passes an empty exclude name: an NPC move excludes nobody. The task
        that paces this method is separate from the idle monitor and from
        connection cleanup (#86).
        """
        moves = await self.engine.tick_world()
        for move in moves:
            data = move.event_data()
            await self.sessions.broadcast_room(
                move.from_room.name, "npc.leave",
                {**data, "text": f"{move.npc_name} leaves."},
                exclude_name="",
            )
            await self.sessions.broadcast_room(
                move.to_room.name, "npc.enter",
                {**data, "text": f"{move.npc_name} arrives."},
                exclude_name="",
            )
        return moves

    async def _wander_loop(self) -> None:
        """Pace `wander_tick` at one tick per `tick_seconds` (1 Hz default)."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.tick_seconds)
                return
            except TimeoutError:
                pass
            try:
                await self.wander_tick()
            except Exception:
                logger.exception("npc wander tick failed")

    async def handler(self, websocket) -> None:
        session = Session(
            ws=websocket,
            clock=self.clock,
            auth_limits=self.auth_limits,
            limits=self.limits,
        )
        self.sessions.bind(session)
        try:
            hello = {
                "server": "iot-mud-server",
                "protocol": [PROTOCOL_VERSION],
                "server_version": SERVER_VERSION,
                "feature_level": FEATURE_LEVEL,
                "capabilities": list(CAPABILITIES),
                "world": {"name": self.world_name, "epoch": self.epoch},
                "server_commit": self.server_commit,
                "auth_required": self.auth_required,
                "guest_ok": self.guest_ok,
                "motd": "Welcome to the IoT MUD!",
                "text": "Welcome to the IoT MUD!",
            }
            if self.auth_required:
                hello["auth_modes"] = list(AUTH_MODES)
            await session.send("hello", hello)

            async for raw in websocket:
                # Only a client message proves the client is not idle; server
                # pushes deliberately do not refresh this.
                session.touch()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await session.send_error(
                        "malformed_message",
                        "That message was not valid JSON.")
                    continue

                try:
                    close_code = await self._route(session, message)
                except Exception:
                    logger.exception("v2 handler failed on a message")
                    await session.send_error(
                        "internal",
                        "The server could not process that command.")
                    continue

                if close_code is not None:
                    await session.close(close_code)
                    break
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.sessions.disconnect(session)
            await session.close()

    async def start(self) -> int:
        """Bind the configured endpoint and start serving.

        Returns the actually bound port, so a caller with `port=0` never
        polls `server.port` to discover it. The method does not block: the
        caller decides when to `wait_closed()`/`close()`.
        """
        if self._server is not None:
            raise RuntimeError("v2 server is already started")
        if self.db_path is not None:
            self.db = open_database(self.db_path)
            # The seeded world owns its name and epoch; a database without a
            # seed keeps the constructor values (the world-name default and
            # utc_now for the epoch). `hello`, `session.ok`, and `auth.ok` all
            # read both fields.
            stored_name = read_world_name(self.db)
            if stored_name is not None:
                self.world_name = stored_name
            stored_epoch = read_world_epoch(self.db)
            if stored_epoch is not None:
                self.epoch = stored_epoch
            # Every mutation goes through the database before it reaches
            # memory or another client (SPEC v2.1.0 criterion 2).
            self.store = WriteThroughStore(self.db)
            self.engine.attach_store(self.store)
            # The mutable half of the world is durable (#131): overlay the
            # committed placements and lock/lid/light flags onto the world the
            # loader just built from YAML, so a restart restores them.
            self.store.apply_durable_state(self.engine)
        # Authored effects are room events (#84). The engine publishes them
        # for the log either way; this subscriber is the only client path and
        # it is a no-op while `features.effects` is false.
        await self.engine.subscribe("effect", self._relay_effect)
        try:
            self._server = await websockets.serve(
                self.handler, self.host, self.port, max_size=65536)
        except BaseException:
            await self._release_resources()
            raise
        self.port = self._server.sockets[0].getsockname()[1]
        self._idle_task = asyncio.create_task(self._idle_loop())
        self._wander_task = asyncio.create_task(self._wander_loop())
        return self.port

    async def close(self) -> None:
        """Stop accepting, close every client, and await quiescence.

        `websockets.Server.close()` closes the listening socket and every open
        connection, and `wait_closed()` returns after all connection handlers
        have run their `finally` path (sleep/leave/persistence). The idle and
        wander tasks are then cancelled, and the database and event
        subscription are released. A second call is a no-op.
        """
        async with self._close_lock:
            self._stop_event.set()
            server = self._server
            self._server = None
            if server is not None:
                server.close()
                await server.wait_closed()
            if self._idle_task is not None:
                self._idle_task.cancel()
                try:
                    await self._idle_task
                except asyncio.CancelledError:
                    pass
                self._idle_task = None
            if self._wander_task is not None:
                self._wander_task.cancel()
                try:
                    await self._wander_task
                except asyncio.CancelledError:
                    pass
                self._wander_task = None
            await self._release_resources()

    async def aclose(self) -> None:
        """Async-context alias for `close()`."""
        await self.close()

    async def _release_resources(self) -> None:
        """Undo what `start()` acquired; safe when only some of it exists."""
        await self.engine.unsubscribe("effect", self._relay_effect)
        if self.store is not None:
            self.engine.attach_store(None)
            self.store = None
        if self.db is not None:
            self.db.close()
            self.db = None

    async def run(self) -> None:
        """CLI wrapper: start, wait for `stop()`, then close."""
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.close()

    def stop(self) -> None:
        self._stop_event.set()


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _host(value: str) -> str:
    host = value.strip()
    if not host:
        raise argparse.ArgumentTypeError("host must not be empty")
    return host


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Protocol-v2 IoT MUD server")
    parser.add_argument(
        "--host", default="localhost", type=_host,
        help="interface to bind (default: localhost)")
    parser.add_argument(
        "--port", default=8765, type=_port,
        help="TCP port to bind (default: 8765)")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    parser.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


if __name__ == "__main__":
    from src.mud.world.loader import load_demo_village

    args = parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc
    db_path = args.db or config.storage.path

    async def main():
        engine = Engine()
        await load_demo_village(engine)
        server = V2Server(engine, host=args.host, port=args.port,
                          db_path=db_path, auth_limits=config.auth,
                          guest_ok=config.auth.guest_ok,
                          limits=config.limits, features=config.features)
        print(f"Protocol-v2 server on ws://{args.host}:{args.port} "
              f"(db: {db_path})")
        await server.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
    except DatabaseError as exc:
        raise SystemExit(f"error: {exc}") from exc
