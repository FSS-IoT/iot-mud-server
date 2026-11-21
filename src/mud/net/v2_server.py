"""Protocol-v2 websocket server.

The v1 `MudServer` remains untouched. This server speaks the merged v2
contract and delegates all game logic to `Engine` through the adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import websockets

from src.mud.engine.core import Engine
from src.mud.protocol.adapter import _room_id, build_room_state, execute
from src.mud.protocol.v2 import PROTOCOL_VERSION, envelope, utc_now

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
    ):
        self.engine = engine
        self.host = host
        self.port = port
        self.world_name = world_name
        self.epoch = epoch or utc_now()
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
            event_id=f"{type_}-{session.seq}",
            msg_id=msg_id if direct else None,
        )
        await ws.send(json.dumps(message))

    async def _send_error(self, ws, session: Session, code: str, message: str,
                          msg_id: Any = None) -> None:
        await self._send(
            ws, session, "error",
            {"code": code, "message": message},
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

        data = msg.get("data") or {}
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

    async def _handle_input(self, ws, session: Session, msg: dict) -> None:
        msg_id = msg.get("id")
        if not session.joined or session.player_name is None:
            await self._send_error(ws, session, "not_joined",
                                   "Join before sending commands.", msg_id)
            return
        if msg_id is None:
            await self._send_error(ws, session, "bad_args",
                                   "Input needs a request id.")
            return

        data = msg.get("data") or {}
        text = data.get("text")
        if not isinstance(text, str):
            await self._send_error(ws, session, "bad_args",
                                   "Input needs a text string.", msg_id)
            return

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

    async def _route(self, ws, session: Session, msg: Any) -> int | None:
        """Route one decoded message. Returns a close code to stop, else None."""
        if not isinstance(msg, dict):
            await self._send_error(ws, session, "malformed_message",
                                   "A message must be a JSON object.")
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
            await self._handle_input(ws, session, msg)
            return None
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
                "server": "iot-mud-v2",
                "protocol": [PROTOCOL_VERSION],
                "world": {"name": self.world_name, "epoch": self.epoch},
                "auth_required": False,
                "motd": "Welcome to the IoT MUD!",
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
        self._server = await websockets.serve(
            self.handler, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        await self._stop_event.wait()
        self._server.close()
        await self._server.wait_closed()

    def stop(self) -> None:
        self._stop_event.set()


if __name__ == "__main__":
    from src.mud.engine.core import Engine
    from src.mud.world.loader import load_demo_village

    async def main():
        engine = Engine()
        await load_demo_village(engine)
        server = V2Server(engine, port=8765)
        print(f"Protocol-v2 server on ws://localhost:{server.port}")
        await server.run()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
