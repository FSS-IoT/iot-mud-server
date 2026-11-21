import asyncio
import json
import logging
import websockets
from typing import Any, Callable, Dict, List
from src.mud.content import Player, Room
from src.mud.engine.core import Engine

# Setup logging
logging.basicConfig(level=logging.ERROR) # Set to ERROR to ensure NO INFO cluttering stdout
logger = logging.getLogger(__name__)

class MudServer:
    def __init__(self, engine: Engine, host: str = "localhost", port: int = 8765):
        """Initializes the MUD server."""
        self.engine = engine
        self.host = host
        self.port = port
        self._stop_event = asyncio.Event()
        self._server = None
        # Maps a live websocket to the name its client announced on `join`.
        # Used to correlate players with sockets for join/leave broadcasts.
        self._ws_to_player: Dict[Any, str] = {}

    async def handler(self, websocket) -> None:
        """Handles the connection lifecycle and command routing."""
        try:
            # 1. The Handshake (Task-002 Requirement)
            handshake = {
                "v": 1,
                "type": "hello",
                "event_id": f"init-{int(asyncio.get_event_loop().time())}",
                "seq": 1,
                "ts": "2026-11-14T00:00:00Z",
                "data": {
                    "server": "iot-mud-dev",
                    "protocol": [1],
                    "auth_required": False,
                    "motd": "Welcome to the IoT MUD!"
                }
            }
            await websocket.send(json.dumps(handshake))

            # 2. Communication Loop
            while not self._stop_event.is_set():
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    data = json.loads(raw_message)

                    if data.get("type") == "join":
                        await self._handle_join(websocket, data)
                    elif data.get("type") == "input":
                        # Render from this socket's registered player so `look`
                        # can name the other occupants of the room.
                        player_name = self._ws_to_player.get(websocket)
                        result_text = await self.engine.process_command(
                            data["data"]["text"], player_name
                        )

                        response = {
                            "v": 1,
                            "type": "command_reply",
                            "event_id": f"resp-{int(asyncio.get_event_loop().time()*1000)}",
                            "seq": data.get("id", 0) if isinstance(data.get("id"), int) else 0,
                            "data": {"text": result_text}
                        }
                        await websocket.send(json.dumps(response))

                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    break
        except websockets.exceptions.ConnectionClosedOK:
            pass
        except Exception as e:
            logger.error(f"Error in handler loop: {e}")
        finally:
            # A socket closed: if it announced a player, tell the room they left.
            name = self._ws_to_player.pop(websocket, None)
            if name is not None and self.engine.get_player(name) is not None:
                # Broadcast first (while the player still has a location so we
                # can compute who else is in the room), then remove them.
                await self._broadcast_to_room(name, "player_left")
                self.engine.remove_player(name)
            await websocket.close()

    async def _handle_join(self, websocket, data: Dict[str, Any]) -> None:
        """Register the caller as a named player and notify roommates."""
        name = data.get("data", {}).get("name")
        if not name or not isinstance(name, str):
            return
        try:
            self.engine.register_player(name)
        except ValueError:
            # Name already in use; the protocol says reject duplicates. Ignore a
            # duplicate `join` rather than crash the connection over it.
            return

        self._ws_to_player[websocket] = name

        # Tell everyone already in the room that a newcomer has arrived.
        await self._broadcast_to_room(name, "player_joined")

        # And tell the newcomer who is already here — otherwise they see an
        # empty room even though occupants are standing right there. Send each
        # existing occupant's name back to the newcomer.
        room = self.engine.get_player(name).location
        for other in self.engine.players_in_room(room) if room else []:
            if other.name == name:
                continue
            await self._send_to_player(name, "player_joined", other.name)

        # Give the newcomer a full picture of their room on join — description,
        # items, and occupants — so they see the world without typing `look`.
        await self._send_room_state(websocket, name)

    async def _send_room_state(self, websocket, name: str) -> None:
        """Send a `room_state` event describing the player's current room.

        The engine is authoritative over what a player can see, so the view is
        built by `engine.visible_room_state` (which encodes the darkness rules)
        rather than reconstructed here from raw room contents. This keeps the
        server a dumb terminal: it never re-derives visibility.
        """
        player = self.engine.get_player(name)
        if player is None or player.location is None:
            return

        view = self.engine.visible_room_state(player)

        message = {
            "v": 1,
            "type": "room_state",
            "event_id": f"room_state-{int(asyncio.get_event_loop().time()*1000)}",
            "seq": 0,
            "data": {
                "name": view["name"],
                "description": view["description"],
                "items": view["items"],
                "occupants": view["occupants"],
            },
        }
        try:
            await websocket.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _send_to_player(self, name: str, event_type: str, actor_name: str) -> None:
        """Send an event to a single named player's socket (or nothing if offline)."""
        target = next((ws for ws, ws_name in self._ws_to_player.items()
                       if ws_name == name), None)
        if target is None:
            return
        message = {
            "v": 1,
            "type": event_type,
            "event_id": f"{event_type}-{int(asyncio.get_event_loop().time()*1000)}",
            "seq": 0,
            "data": {"name": actor_name},
        }
        try:
            await target.send(json.dumps(message))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _broadcast_to_room(self, name: str, event_type: str) -> None:
        """Send an event to every other player currently in the same room."""
        player = self.engine.get_player(name)
        if player is None or player.location is None:
            return
        await self._notify_room(player.location, event_type, name)

    async def _notify_room(self, room, event_type: str, actor_name: str) -> None:
        """Send an event to every live player in `room` except the actor.

        Takes an explicit room so callers can notify a room even after the actor's
        location has already changed (e.g. right after moving).
        """
        if room is None:
            return
        for other in self.engine.players_in_room(room):
            if other.name == actor_name:
                continue
            target = next((ws for ws, ws_name in self._ws_to_player.items()
                           if ws_name == other.name), None)
            if target is None:
                continue
            message = {
                "v": 1,
                "type": event_type,
                "event_id": f"{event_type}-{int(asyncio.get_event_loop().time()*1000)}",
                "seq": 0,
                "data": {"name": actor_name},
            }
            try:
                await target.send(json.dumps(message))
            except websockets.exceptions.ConnectionClosed:
                pass

    async def _handle_player_moved(self, event_type: str, data) -> None:
        """React to a `player_moved` semantic event by broadcasting position change.

        The departing room loses the player (`player_left`) and the arriving room
        gains them (`player_joined`). We must notify the *arriving* room before or
        independent of removing the player from the old room, so we compute both
        rooms up front.
        """
        name = data["name"]
        player = self.engine.get_player(name)
        if player is None:
            return

        # The engine is the authoritative brain: it has already moved the player
        # (updated `.location` and the room contents) before emitting. The server
        # is just a terminal here — it reacts by broadcasting, never by mutating.
        # The event carries the old room by name; the new room is wherever the
        # player's location points now (after a move, that's already updated).
        old_room = self.engine.get_room(data["from"])
        new_room = player.location

        # Notify everyone still in the old room that the player left, then the
        # arriving room that they have joined. No content mutation on our side.
        await self._notify_room(old_room, "player_left", name)
        await self._notify_room(new_room, "player_joined", name)

    async def _handle_player_said(self, event_type: str, data) -> None:
        """React to a `player_said` semantic event by broadcasting to roommates.

        The engine owns the speaker's location; we only relay the message to
        everyone else currently in the same room (no content mutation).
        """
        name = data["name"]
        player = self.engine.get_player(name)
        if player is None or player.location is None:
            return
        message = {
            "v": 1,
            "type": "player_said",
            "event_id": f"player_said-{int(asyncio.get_event_loop().time()*1000)}",
            "seq": 0,
            "data": {"name": name, "text": data.get("text", "")},
        }
        for other in self.engine.players_in_room(player.location):
            if other.name == name:
                continue
            target = next((ws for ws, ws_name in self._ws_to_player.items()
                           if ws_name == other.name), None)
            if target is None:
                continue
            try:
                await target.send(json.dumps(message))
            except websockets.exceptions.ConnectionClosed:
                pass

    async def run(self) -> None:
        """Starts the WebSocket server."""
        # React to registered players moving so position changes reach the rooms
        # affected by each move (see _handle_player_moved).
        await self.engine.subscribe("player_moved", self._handle_player_moved)
        # React to a player speaking so the message reaches everyone else in the
        # room (see _handle_player_said).
        await self.engine.subscribe("player_said", self._handle_player_said)
        self._server = await websockets.serve(self.handler, self.host, self.port)
        await self._stop_event.wait()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def stop(self) -> None:
        """Triggers an orderly shutdown."""
        self._stop_event.set()

if __name__ == "__main__":
    import sys
    try:
        from src.mud.engine.core import Engine
        from src.mud.world.loader import load_demo_village
        
        engine_instance = Engine()
        async def main():
            await load_demo_village(engine_instance)
            server_instance = MudServer(engine_instance)
            await server_instance.run()
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception as e:
        print(f"Failed to start server: {e}")
