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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import websockets

from src.mud.auth import AuthError, normalize_name, register_character
from src.mud.config import ConfigError, load_config
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
from src.mud.storage.store import WriteThroughStore

logger = logging.getLogger(__name__)

SERVER_COMMIT_ENV = "MUD_SERVER_COMMIT"

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


@dataclass
class Session:
    ws: Any
    seq: int = 0
    player_name: str | None = None
    joined: bool = False


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
        auth_required: bool = False,
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.world_name = world_name
        self.epoch = epoch or utc_now()
        self.db_path = Path(db_path) if db_path is not None else None
        self.server_commit = resolve_server_commit(server_commit)
        # v2.1 auth is opt-in for now: the v2.0 conformance session and the
        # reference client stay on the guest path until #74 flips the default.
        self.auth_required = auth_required
        self.db: sqlite3.Connection | None = None
        self.store: WriteThroughStore | None = None
        self._stop_event = asyncio.Event()
        self._server = None
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
        await self._send(
            ws, session, "auth.error",
            {"code": code, "message": message, "text": message},
            direct=True, msg_id=msg_id,
        )

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
        """Route one `auth` message; registration is the only mode for now."""
        msg_id = msg.get("id")
        if session.joined:
            await self._send_auth_error(
                ws, session, "already_joined",
                "This connection has already joined.", msg_id)
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
            # Login is #69. No key can verify until that lands, so say so
            # rather than pretending every key is wrong.
            await self._send_auth_error(
                ws, session, "bad_args",
                "Login is not available in this build.", msg_id)
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

    async def _handle_input(self, ws, session: Session, msg: dict) -> int | None:
        msg_id = msg.get("id")
        if not session.joined or session.player_name is None:
            await self._send_error(ws, session, "not_joined",
                                   "Join before sending commands.", msg_id)
            return None
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
        # broadcasts room.leave and closes 1000.
        if text.strip().lower() in ("quit", "exit"):
            return 1000

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

    async def _cleanup(self, session: Session) -> None:
        name = session.player_name
        if name is None:
            return
        player = self.engine.get_player(name)
        if player is not None:
            room = player.location
            if room is not None:
                await self._broadcast_room(room.name, "room.leave", {
                    "room": {"id": _room_id(room), "name": room.name},
                    "actor": {"name": name},
                    "reason": "disconnect",
                    "text": f"{name} has left.",
                }, exclude_name=name)
            self.engine.remove_player(name)
        self._ws_by_name.pop(name, None)

    async def handler(self, websocket) -> None:
        session = Session(ws=websocket)
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
            # Every mutation goes through the database before it reaches
            # memory or another client (SPEC v2.1.0 criterion 2).
            self.store = WriteThroughStore(self.db)
            self.engine.attach_store(self.store)
        try:
            self._server = await websockets.serve(
                self.handler, self.host, self.port, max_size=65536)
            self.port = self._server.sockets[0].getsockname()[1]
            await self._stop_event.wait()
            self._server.close()
            await self._server.wait_closed()
        finally:
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
                          db_path=db_path)
        print(f"Protocol-v2 server on ws://{args.host}:{args.port} "
              f"(db: {db_path})")
        await server.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
    except DatabaseError as exc:
        raise SystemExit(f"error: {exc}") from exc
