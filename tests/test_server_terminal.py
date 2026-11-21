"""Red-green fixes for server-as-terminal refinements.

The engine is the authoritative brain of the MUD; the server is a dumb terminal
that only reacts to semantic events. It must not mutate world state, and every
player-facing action that affects roommates must be broadcast.

These tests encode behavior that does not exist yet, so they fail now (RED):

- `test_move_does_not_double_add_player`: the server must not re-add a moved
  player to the destination room's contents. The engine's `move_player` is the
  sole mutator of room contents; the server's `player_moved` handler must only
  broadcast, never mutate. Today the server re-adds the player, leaving a
  duplicate in `room.contents`.

- `test_say_broadcasts_to_roommates`: `say <text>` must emit an event the server
  broadcasts to everyone else in the room. Today `say` only echoes to the sender,
  so roommates never hear anything.
"""

import asyncio
import json

import pytest
import websockets

from src.mud.content import Room
from src.mud.engine.core import Engine
from src.mud.net.server import MudServer

# ---------------------------------------------------------------------------
# Server must not mutate world state on move
# ---------------------------------------------------------------------------

def _two_room_engine():
    """gate --north--> market, both rooms registered, legacy start = gate."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    market = Room("Marketplace", "Lightly vendors cry out their wares.")
    gate.exits["north"] = market
    engine._rooms["Village Gate"] = gate
    engine._rooms["Marketplace"] = market
    engine._player_location = gate
    return engine


@pytest.mark.asyncio
async def test_move_does_not_double_add_player():
    """Moving must not duplicate the player in the destination room's contents.

    The engine's `move_player` is the sole mutator of room contents. The server
    only reacts to the `player_moved` event; it must not add/remove anything.
    """
    engine = _two_room_engine()
    gate = engine._rooms["Village Gate"]
    market = engine._rooms["Marketplace"]
    player = engine.register_player("Ada")  # adds the player to the start room (gate)
    assert player.location is gate

    server = MudServer(engine, port=8770)
    await engine.subscribe("player_moved", server._handle_player_moved)

    await engine.process_command("go north", "Ada")

    # The player ends up in the marketplace exactly once — never duplicated.
    assert len([c for c in market.contents if c is player]) == 1
    # And the departing room is fully empty of the player.
    assert len([c for c in gate.contents if c is player]) == 0


# ---------------------------------------------------------------------------
# `say` must broadcast to roommates
# ---------------------------------------------------------------------------

class FakeClient:
    """A tiny websocket client with a background reader and type-filtered drain."""

    def __init__(self, uri):
        self.uri = uri
        self.ws = None
        self.q = asyncio.Queue()
        self._task = None

    async def connect(self):
        self.ws = await websockets.connect(self.uri)
        first = json.loads(await self.ws.recv())  # hello handshake
        assert first["type"] == "hello"
        self._task = asyncio.create_task(self._reader())
        return self

    async def _reader(self):
        while True:
            raw = await self.ws.recv()
            self.q.put_nowait(json.loads(raw))

    async def join(self, name):
        await self.ws.send(json.dumps({
            "v": 1, "type": "join", "id": 1, "data": {"name": name},
        }))

    async def input(self, text, idx=1):
        await self.ws.send(json.dumps({
            "v": 1, "type": "input", "id": idx, "data": {"text": text},
        }))

    async def next_event(self, etype):
        """Return the next event of `etype`, skipping any others already buffered."""
        while True:
            ev = await asyncio.wait_for(self.q.get(), timeout=3.0)
            if ev["type"] == etype:
                return ev

    async def close(self):
        self._task.cancel()
        await self.ws.close()


@pytest.fixture
async def running_server():
    """Start a server backed by the starting-village world, stop it after."""
    from src.mud.world.loader import load_demo_village
    engine = Engine()
    await load_demo_village(engine)
    server = MudServer(engine, port=8771)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)
    yield server, task
    server.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_say_broadcasts_to_roommates(running_server):
    """When Ada says something, everyone else in the room hears it."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    ben = await FakeClient(uri).connect()

    await ada.join("Ada")
    await ben.join("Ben")

    # Clear the initial player_joined events (both at the Village Gate).
    assert (await ada.next_event("player_joined"))["data"]["name"] == "Ben"
    assert (await ben.next_event("player_joined"))["data"]["name"] == "Ada"

    await ada.input('say hello there', 1)

    event = await ben.next_event("player_said")
    assert event["data"]["name"] == "Ada"
    assert "hello there" in event["data"]["text"]

    await ada.close()
    await ben.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
