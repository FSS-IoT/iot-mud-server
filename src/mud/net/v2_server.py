"""Protocol-v2 websocket server.

The v1 `MudServer` remains untouched. This server speaks the merged v2
contract and delegates all game logic to `Engine` through the adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from src.mud.config import ConfigError, load_config
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import _room_id, build_room_state, execute
from src.mud.protocol.v2 import PROTOCOL_VERSION, envelope, utc_now
from src.mud.storage.db import DatabaseError, open_database

logger = logging.getLogger(__name__)


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
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.world_name = world_name
        self.epoch = epoch or utc_now()
        self.db_path = Path(db_path) if db_path is not None else None
        self.db: sqlite3.Connection | None = None
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

        room = player.location
        if room is not None:
            await self._broadcast_room(room.name, "room.enter", {
                "room": {"id": _room_id(room), "name": room.name},
                "actor": {"name": name},
                "reason": "connect",
                "text": f"{name} has arrived.",
            }, exclude_name=name)

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
            await self._send(websocket, session, "hello", {
                "server": "iot-mud-server",
                "protocol": [PROTOCOL_VERSION],
                "world": {"name": self.world_name, "epoch": self.epoch},
                "auth_required": False,
                "motd": "Welcome to the IoT MUD!",
                "text": "Welcome to the IoT MUD!",
            })

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
        try:
            self._server = await websockets.serve(
                self.handler, self.host, self.port, max_size=65536)
            self.port = self._server.sockets[0].getsockname()[1]
            await self._stop_event.wait()
            self._server.close()
            await self._server.wait_closed()
        finally:
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
