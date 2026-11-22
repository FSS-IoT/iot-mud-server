"""Protocol-v2 websocket server.

The v1 `MudServer` remains untouched. This server speaks the merged v2
contract and delegates all game logic to `Engine` through the adapter.
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
from src.mud.config import AuthConfig, ConfigError, LimitsConfig, load_config
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import _room_id, build_room_state, execute
from src.mud.protocol.v2 import (
    AUTH_MODES,
    PROTOCOL_VERSION,
    UNKNOWN_SERVER_COMMIT,
    envelope,
    is_server_commit,
    utc_now,
)
from src.mud.storage.db import DatabaseError, open_database
from src.mud.storage.keys import object_key, room_key
from src.mud.storage.store import (
    PlayerStore,
    StorageWriteError,
    WriteThroughStore,
)

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
    ws: Any
    seq: int = 0
    player_name: str | None = None
    joined: bool = False
    # True for the durable character paths (`auth` register/login). A guest
    # `join` leaves it False and keeps the v2.0 remove-on-disconnect behavior.
    authenticated: bool = False
    # Auth failure times on the server's clock. The limiter lives here, on the
    # connection: there is no shared table and no entry keyed by a name, so a
    # throttled connection can never lock a character name.
    auth_failures: list[float] = field(default_factory=list)
    # Command-rate bucket, also per connection. It is lazily built on the
    # first `input`, so auth traffic never spends a command token.
    command_bucket: TokenBucket | None = None
    # The last client message, on the server's clock. Server pushes do not
    # refresh this; only a message from the client proves it is not idle.
    last_activity: float = 0.0
    # Set once the #72 sleep path has run for this session, so an idle close
    # followed by `_cleanup` cannot sleep and broadcast the leave twice.
    slept: bool = False


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
        auth_limits: AuthConfig | None = None,
        limits: LimitsConfig | None = None,
        clock: Callable[[], float] | None = None,
        idle_poll_seconds: float = 1.0,
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.world_name = world_name
        self.epoch = epoch or utc_now()
        self.db_path = Path(db_path) if db_path is not None else None
        self.server_commit = resolve_server_commit(server_commit)
        # v2.1 is the product: a session authenticates before it can join or
        # send input. The v2.0 guest path is an explicit opt-out for legacy
        # tests and a deliberate guest server.
        self.auth_required = auth_required
        # The limiter is per connection and in memory, and the window is read
        # from this clock, so tests advance time instead of sleeping.
        self.auth_limits = auth_limits if auth_limits is not None else AuthConfig()
        # Command limits and the idle timeout are independent of the auth
        # limiter. Both read the same injected clock, so tests advance time
        # instead of sleeping.
        self.limits = limits if limits is not None else LimitsConfig()
        self.clock = clock if clock is not None else time.monotonic
        self.idle_poll_seconds = idle_poll_seconds
        self.db: sqlite3.Connection | None = None
        self.store: WriteThroughStore | None = None
        self._stop_event = asyncio.Event()
        self._server = None
        self._idle_task: asyncio.Task | None = None
        self._sessions: dict[Any, Session] = {}
        self._ws_by_name: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def _send(self, ws, session: Session, type_: str, data: dict,
                    *, direct: bool = False, msg_id: Any = None) -> None:
        session.seq += 1
        message = envelope(
            type_, data, seq=session.seq,
            msg_id=msg_id if direct else None,
        )
        await ws.send(json.dumps(message))

    async def _send_error(self, ws, session: Session, code: str, message: str,
                          msg_id: Any = None) -> None:
        await self._send(
            ws, session, "error",
            {"code": code, "message": message, "text": message},
            direct=msg_id is not None, msg_id=msg_id,
        )

    async def _send_auth_error(self, ws, session: Session, code: str,
                               message: str, msg_id: Any = None) -> None:
        """A typed auth failure: direct, echoes the request id, never the key."""
        if code in AUTH_FAILURE_CODES:
            session.auth_failures.append(self.clock())
        await self._send(
            ws, session, "auth.error",
            {"code": code, "message": message, "text": message},
            direct=True, msg_id=msg_id,
        )

    def _auth_is_limited(self, session: Session) -> bool:
        """True when this connection spent its failure budget in the window.

        Expired failures are pruned here, which is the whole recovery path:
        once the clock is a window past them, the budget is empty again.
        """
        now = self.clock()
        window = self.auth_limits.window_seconds
        recent = [at for at in session.auth_failures if now - at < window]
        session.auth_failures[:] = recent
        return len(recent) >= self.auth_limits.max_attempts

    def _command_bucket(self, session: Session) -> TokenBucket:
        """This connection's command bucket, created full on first use."""
        if session.command_bucket is None:
            session.command_bucket = TokenBucket.full(
                self.limits.command_burst,
                self.limits.commands_per_second,
                self.clock(),
            )
        return session.command_bucket

    async def _announce_connect(self, player) -> None:
        """Tell the character's room that it arrived."""
        room = player.location
        if room is None:
            return
        await self._broadcast_room(room.name, "room.enter", {
            "room": {"id": _room_id(room), "name": room.name},
            "actor": {"name": player.name},
            "reason": "connect",
            "text": f"{player.name} has arrived.",
        }, exclude_name=player.name)

    async def _broadcast_room(self, room_name: str | None, type_: str,
                              data: dict, exclude_name: str) -> None:
        if room_name is None:
            return
        room = self.engine.get_room(room_name)
        if room is None:
            return
        for player in self.engine.players_in_room(room):
            if player.name == exclude_name:
                continue
            target = self._ws_by_name.get(player.name)
            session = self._sessions.get(target)
            if target is None or session is None:
                continue
            await self._send(target, session, type_, data, direct=False)

    def _room_by_key(self, room_id: str | None):
        """The loaded room stored under `room_id`, or None if it is unknown.

        Room ids on disk and on the wire come from `room_key`, while the engine
        indexes rooms by display name; this is the one translation login needs.
        """
        if room_id is None:
            return None
        for room in self.engine.rooms.values():
            if room_key(room) == room_id:
                return room
        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_join(self, ws, session: Session, msg: dict) -> None:
        msg_id = msg.get("id")
        if session.joined:
            await self._send_error(ws, session, "already_joined",
                                   "This connection has already joined.",
                                   msg_id)
            return

        if self.auth_required:
            await self._send_error(ws, session, "not_authenticated",
                                   "Authenticate before joining.", msg_id)
            return

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await self._send_error(ws, session, "malformed_message",
                                   "The data field must be an object.",
                                   msg_id)
            return
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            await self._send_error(ws, session, "bad_args",
                                   "Join needs a non-empty name.", msg_id)
            return
        name = name.strip()

        try:
            player = self.engine.register_player(name)
        except ValueError:
            await self._send_error(ws, session, "name_taken",
                                   f"The name {name!r} is already in use.",
                                   msg_id)
            return

        session.player_name = name
        session.joined = True
        self._ws_by_name[name] = ws

        await self._send(ws, session, "session.ok", {
            "player": {"name": name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "guest",
            "text": f"Welcome, {name}.",
        }, direct=True, msg_id=msg_id)

        await self._send(ws, session, "room.state",
                         build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _handle_auth(self, ws, session: Session, msg: dict) -> None:
        """Route one `auth` message, enforcing the per-connection budget first.

        A connection over its budget gets `auth.error{rate_limited}` before its
        data is read, so a flood cannot keep probing shapes or keys. The socket
        stays open: a normal bad key is never a close, and the limit is not
        policy abuse.
        """
        msg_id = msg.get("id")
        if session.joined:
            await self._send_auth_error(
                ws, session, "already_joined",
                "This connection has already joined.", msg_id)
            return

        if self._auth_is_limited(session):
            await self._send_auth_error(
                ws, session, "rate_limited", RATE_LIMITED_MESSAGE, msg_id)
            return

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await self._send_error(ws, session, "malformed_message",
                                   "The data field must be an object.",
                                   msg_id)
            return

        mode = data.get("mode")
        if mode == "register":
            await self._auth_register(ws, session, data, msg_id)
            return
        if mode == "login":
            await self._auth_login(ws, session, data, msg_id)
            return
        await self._send_auth_error(
            ws, session, "bad_args",
            "Auth mode must be 'register' or 'login'.", msg_id)

    async def _auth_register(self, ws, session: Session, data: dict,
                             msg_id: Any) -> None:
        """Register a name, issue its first key, and enter the world."""
        try:
            name = normalize_name(data.get("name"))
        except AuthError as exc:
            await self._send_auth_error(
                ws, session, exc.code, str(exc), msg_id)
            return

        if self.engine.get_player(name) is not None:
            await self._send_auth_error(
                ws, session, "name_taken",
                f"The name {name!r} is already in use.", msg_id)
            return

        start_room = self.engine.current_location
        if self.db is None or start_room is None:
            await self._send_auth_error(
                ws, session, "internal",
                "The server cannot create characters right now.", msg_id)
            return

        try:
            registration = register_character(
                self.db, name, start_room=start_room)
        except AuthError as exc:
            await self._send_auth_error(
                ws, session, exc.code, str(exc), msg_id)
            return

        player = self.engine.register_player(registration.name)
        session.player_name = registration.name
        session.joined = True
        session.authenticated = True
        self._ws_by_name[registration.name] = ws

        # The key appears here and nowhere else: not in the session, the room
        # state, a broadcast, a log line, or the database.
        await self._send(ws, session, "auth.ok", {
            "player": {"name": registration.name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "register",
            "key": registration.key,
            "text": (f"Welcome, {registration.name}. Store this key; it is "
                     "shown only once."),
        }, direct=True, msg_id=msg_id)

        await self._send(ws, session, "room.state",
                         build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _auth_login(self, ws, session: Session, data: dict,
                          msg_id: Any) -> None:
        """Verify a key and bind the session to its durable character.

        The request has no name: `login_character` finds the row by key id and
        verifies the secret. A bad secret, an unknown id, and a revoked key all
        arrive here as one `invalid_key`; the key itself is never echoed or
        logged.
        """
        if self.db is None:
            await self._send_auth_error(
                ws, session, "internal",
                "The server cannot check keys right now.", msg_id)
            return

        try:
            login = login_character(self.db, data.get("key"))
        except AuthError as exc:
            await self._send_auth_error(
                ws, session, exc.code, str(exc), msg_id)
            return

        existing = self.engine.get_player(login.name)
        if existing is not None and not existing.asleep:
            await self._send_auth_error(
                ws, session, "name_taken",
                "That character is already in play.", msg_id)
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
                room=self._room_by_key(character.room_id),
                item_keys=character.items,
            )

        try:
            self.engine.wake_player(login.name)
        except StorageWriteError:
            logger.exception("could not wake %s", login.name)
            if existing is None:
                self.engine.remove_player(login.name)
            await self._send_auth_error(
                ws, session, "internal",
                "The character could not be woken right now.", msg_id)
            return

        session.player_name = login.name
        session.joined = True
        session.authenticated = True
        self._ws_by_name[login.name] = ws

        # No key field: a login response never repeats the credential.
        await self._send(ws, session, "auth.ok", {
            "player": {"name": login.name},
            "world": {"name": self.world_name, "epoch": self.epoch},
            "mode": "login",
            "text": f"Welcome back, {login.name}.",
        }, direct=True, msg_id=msg_id)

        await self._send(ws, session, "room.state",
                         build_room_state(self.engine, player))

        await self._announce_connect(player)

    async def _handle_input(self, ws, session: Session, msg: dict) -> int | None:
        msg_id = msg.get("id")
        if not session.joined or session.player_name is None:
            if self.auth_required:
                await self._send_error(ws, session, "not_authenticated",
                                       "Authenticate before sending commands.",
                                       msg_id)
            else:
                await self._send_error(ws, session, "not_joined",
                                       "Join before sending commands.", msg_id)
            return None
        if session.slept:
            # An idle frame that was already in flight must not run a command
            # for a stone character. The idle path is closing with this code.
            return IDLE_TIMEOUT_CLOSE
        if msg_id is None:
            await self._send_error(ws, session, "bad_args",
                                   "Input needs a request id.")
            return None

        data = msg.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            await self._send_error(ws, session, "malformed_message",
                                   "The data field must be an object.",
                                   msg_id)
            return None
        text = data.get("text")
        if not isinstance(text, str):
            await self._send_error(ws, session, "bad_args",
                                   "Input needs a text string.", msg_id)
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
        if not self._command_bucket(session).take(self.clock()):
            await self._send_error(
                ws, session, "rate_limited",
                COMMAND_RATE_LIMITED_MESSAGE, msg_id)
            return None

        result = await execute(self.engine, session.player_name, text)
        player = self.engine.get_player(session.player_name)
        current_room = player.location if player is not None else None

        for outbound in result.events:
            if outbound.target == "actor":
                await self._send(
                    ws, session, outbound.type, outbound.data,
                    direct=outbound.direct,
                    msg_id=msg_id if outbound.direct else None,
                )
            elif outbound.target == "player":
                # A private push to one named character (#75). It is not a
                # reply to the actor's request id and it never reaches the
                # room. A target that left between resolution and delivery
                # gets nothing; there is no offline queue.
                target_name = outbound.player_name
                target_ws = (
                    self._ws_by_name.get(target_name)
                    if target_name is not None else None
                )
                target_session = (
                    self._sessions.get(target_ws)
                    if target_ws is not None else None
                )
                if target_ws is None or target_session is None:
                    continue
                try:
                    await self._send(
                        target_ws, target_session, outbound.type,
                        outbound.data, direct=outbound.direct,
                    )
                except websockets.exceptions.ConnectionClosed:
                    continue
            else:
                room_name = outbound.room_name or (
                    current_room.name if current_room is not None else None)
                await self._broadcast_room(
                    room_name, outbound.type, outbound.data,
                    exclude_name=session.player_name,
                )
        return None

    async def _route(self, ws, session: Session, msg: Any) -> int | None:
        """Route one decoded message. Returns a close code to stop, else None."""
        if not isinstance(msg, dict):
            await self._send_error(ws, session, "malformed_message",
                                   "A message must be a JSON object.")
            return None

        if "v" not in msg or msg.get("v") is None:
            await self._send_error(ws, session, "malformed_message",
                                   "Missing protocol version.")
            return None
        if msg.get("v") != PROTOCOL_VERSION:
            await self._send_error(
                ws, session, "unsupported_version",
                f"Protocol {msg.get('v')!r} is not supported; this server "
                f"speaks {PROTOCOL_VERSION}.",
            )
            return 4003

        kind = msg.get("type")
        if kind == "auth":
            await self._handle_auth(ws, session, msg)
            return None
        if kind == "join":
            await self._handle_join(ws, session, msg)
            return None
        if kind == "input":
            return await self._handle_input(ws, session, msg)
        if kind == "quit":
            return 1000

        await self._send_error(ws, session, "unknown_message",
                               f"Unknown message type: {kind!r}.",
                               msg.get("id"))
        return None

    async def _sleep_authenticated(self, session: Session) -> None:
        """Run the #72 sleep path once for an authenticated session.

        Works for both a disconnect (`_cleanup`) and an idle timeout
        (`check_idle_timeouts`). `session.slept` makes it idempotent, so the
        idle close followed by the handler's cleanup cannot sleep twice or
        broadcast two `room.leave` events.
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
        await self._broadcast_room(room.name, "room.leave", {
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

    async def _cleanup(self, session: Session) -> None:
        name = session.player_name
        if name is None:
            return
        try:
            player = self.engine.get_player(name)
            if player is None:
                return
            room = player.location
            if session.authenticated:
                await self._sleep_authenticated(session)
            else:
                # The explicit guest path keeps the v2.0 remove-on-disconnect
                # behavior: there is no durable character to sleep.
                if room is not None:
                    await self._broadcast_room(room.name, "room.leave", {
                        "room": {"id": _room_id(room), "name": room.name},
                        "actor": {"name": name},
                        "reason": "disconnect",
                        "text": f"{name} has left.",
                    }, exclude_name=name)
                self.engine.remove_player(name)
        finally:
            # An idle close may already have let a fresh login bind the same
            # character to a new socket; only this session's mapping is ours
            # to remove.
            if self._ws_by_name.get(name) is session.ws:
                self._ws_by_name.pop(name, None)

    async def check_idle_timeouts(self) -> None:
        """Sleep authenticated sessions whose last client message is too old.

        The monitor below calls this on `idle_poll_seconds`; tests advance the
        injected clock and call it directly, so no test waits for a timeout.
        The character sleeps first (#72), then the socket closes with `4004`.
        Guest sessions have no durable character and are left alone.
        """
        now = self.clock()
        timeout = self.limits.idle_timeout_seconds
        for ws, session in list(self._sessions.items()):
            if not session.authenticated or session.player_name is None:
                continue
            if session.slept:
                continue
            if now - session.last_activity < timeout:
                continue
            await self._sleep_authenticated(session)
            if self._ws_by_name.get(session.player_name) is ws:
                self._ws_by_name.pop(session.player_name, None)
            try:
                await ws.close(code=IDLE_TIMEOUT_CLOSE)
            except websockets.exceptions.ConnectionClosed:
                pass

    async def _idle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.idle_poll_seconds)
                return
            except TimeoutError:
                pass
            try:
                await self.check_idle_timeouts()
            except Exception:
                logger.exception("idle sweep failed")

    async def handler(self, websocket) -> None:
        session = Session(ws=websocket, last_activity=self.clock())
        self._sessions[websocket] = session
        try:
            hello = {
                "server": "iot-mud-server",
                "protocol": [PROTOCOL_VERSION],
                "world": {"name": self.world_name, "epoch": self.epoch},
                "server_commit": self.server_commit,
                "auth_required": self.auth_required,
                "motd": "Welcome to the IoT MUD!",
                "text": "Welcome to the IoT MUD!",
            }
            if self.auth_required:
                hello["auth_modes"] = list(AUTH_MODES)
            await self._send(websocket, session, "hello", hello)

            async for raw in websocket:
                # Only a client message proves the client is not idle; server
                # pushes deliberately do not refresh this.
                session.last_activity = self.clock()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(
                        websocket, session, "malformed_message",
                        "That message was not valid JSON.")
                    continue

                try:
                    close_code = await self._route(websocket, session, message)
                except Exception:
                    logger.exception("v2 handler failed on a message")
                    await self._send_error(
                        websocket, session, "internal",
                        "The server could not process that command.")
                    continue

                if close_code is not None:
                    await websocket.close(code=close_code)
                    break
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._cleanup(session)
            self._sessions.pop(websocket, None)
            try:
                await websocket.close()
            except websockets.exceptions.ConnectionClosed:
                pass

    async def run(self) -> None:
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
        try:
            self._server = await websockets.serve(
                self.handler, self.host, self.port, max_size=65536)
            self.port = self._server.sockets[0].getsockname()[1]
            self._idle_task = asyncio.create_task(self._idle_loop())
            await self._stop_event.wait()
            self._server.close()
            await self._server.wait_closed()
        finally:
            if self._idle_task is not None:
                self._idle_task.cancel()
                try:
                    await self._idle_task
                except asyncio.CancelledError:
                    pass
                self._idle_task = None
            if self.store is not None:
                self.engine.attach_store(None)
                self.store = None
            if self.db is not None:
                self.db.close()
                self.db = None

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
                          limits=config.limits)
        print(f"Protocol-v2 server on ws://{args.host}:{args.port} "
              f"(db: {db_path})")
        await server.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
    except DatabaseError as exc:
        raise SystemExit(f"error: {exc}") from exc
