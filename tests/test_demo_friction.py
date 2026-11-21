"""Red-green fixes for three demo-friction bugs reported while playing.

Bug 1: On join, a newcomer receives only player_joined/player_left events and no
room description — they must type `look` before seeing the room, its items, or
who is in it. The wire protocol already documents a `room_state` event as "not
yet implemented"; this ticket implements it so a newcomer sees their room on join.

Bug 2: Registered players cannot take items even in the same room they stand in
(`take gold coin`, `get coin`). `take`/`drop` only used the legacy single-player
slot and matched names exactly, so "coin" never matched "Gold Coin". This ticket
routes take/drop through the registered player's location and makes item-name
matching forgiving.

Bug 3: Players cannot look at another player (`look at Ada`, `examine Ada`). The
`look` verb only ever described the room; there was no way to focus on an entity.
This ticket adds an `examine` verb (and `look at`) that describes a target.

These tests encode behavior that does not exist yet, so they fail now (RED).
"""

import asyncio
import json

import pytest
import websockets

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.net.server import MudServer
from src.mud.world.loader import load_demo_village

# ---------------------------------------------------------------------------
# Bug 2: registered players can take items (pure engine)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registered_player_can_take_item():
    """`take gold coin` as a registered player moves the item into their inventory."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    room.add_content(coin)
    engine._rooms["Marketplace"] = room
    engine._player_location = room

    player = engine.register_player("Ada")
    assert coin in room.contents  # sanity: it is here before the command

    response = await engine.process_command("take gold coin", "Ada")

    assert coin in player.contents
    assert coin not in room.contents
    assert "picked up" in response.lower()


@pytest.mark.asyncio
async def test_registered_player_take_is_forgiving_on_name():
    """`get coin` matches the item whose display name is 'Gold Coin'."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    room.add_content(coin)
    engine._rooms["Marketplace"] = room
    engine._player_location = room

    engine.register_player("Ada")

    response = await engine.process_command("get coin", "Ada")

    assert "picked up" in response.lower()


@pytest.mark.asyncio
async def test_registered_player_can_drop_item():
    """A registered player can drop an item they are holding back into the room."""
    engine = Engine()
    room = Room("Marketplace", "Lively vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    engine._rooms["Marketplace"] = room
    engine._player_location = room

    player = engine.register_player("Ada")
    player.add_content(coin)  # pretend it is already in their inventory

    response = await engine.process_command("drop gold coin", "Ada")

    assert coin in room.contents
    assert coin not in player.contents
    assert "dropped" in response.lower()


@pytest.mark.asyncio
async def test_legacy_take_still_works_without_player_name():
    """Backward compat: no player_name still uses the legacy slot."""
    engine = Engine()
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    legacy = Entity("Hero", "A brave adventurer.")
    room.add_content(sword)
    engine.player = legacy
    engine._player_location = room

    await engine.process_command("take sword")

    assert sword in legacy.contents
    assert sword not in room.contents


# ---------------------------------------------------------------------------
# Bug 3: examine / look at a target (pure engine)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_examine_player():
    """`exmine Ada` describes the named player."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    engine._rooms["Marketplace"] = room
    engine._player_location = room
    engine.register_player("Ada")

    response = await engine.process_command("examine Ada", "Ben")

    assert "Ada" in response


@pytest.mark.asyncio
async def test_look_at_player():
    """`look at Ada` is an alias for examining the named player."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    engine._rooms["Marketplace"] = room
    engine._player_location = room
    engine.register_player("Ada")

    response = await engine.process_command("look at Ada", "Ben")

    assert "Ada" in response


@pytest.mark.asyncio
async def test_examine_item():
    """`examine gold coin` describes the item if the player is holding it."""
    engine = Engine()
    room = Room("Marketplace", "Lightly vendors cry out their wares.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    engine._rooms["Marketplace"] = room
    engine._player_location = room

    player = engine.register_player("Ada")
    player.add_content(coin)

    response = await engine.process_command("examine gold coin", "Ada")

    assert "shiny" in response.lower()  # from the item's description


# ---------------------------------------------------------------------------
# Bug 1: newcomer is told about their room on join (server, end-to-end)
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
        try:
            while True:
                raw = await self.ws.recv()
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def join(self, name, idx=1):
        await self.ws.send(json.dumps({
            "v": 1, "type": "join", "id": idx, "data": {"name": name},
        }))

    async def input(self, text, idx):
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
    engine = Engine()
    await load_demo_village(engine)  # start room = Village Gate (no items)
    server = MudServer(engine, port=8770)
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
async def test_newcomer_receives_room_state_on_join(running_server):
    """On join the newcomer receives a `room_state` describing their room."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    await ada.join("Ada")

    event = await ada.next_event("room_state")

    assert event["data"]["name"] == "Village Gate"


@pytest.mark.asyncio
async def test_newcomer_room_state_lists_occupants(running_server):
    """The newcomer's `room_state` names other players already in the room."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    ben = await FakeClient(uri).connect()

    await ada.join("Ada")
    await asyncio.sleep(0.2)  # let Ada register before Ben joins
    await ben.join("Ben")     # Ben must register to receive a room_state

    event = await ben.next_event("room_state")
    names = event["data"].get("occupants", [])
    assert "Ada" in names


@pytest.mark.asyncio
async def test_registered_player_can_take_item_end_to_end(running_server):
    """End-to-end: a registered client moves to the Marketplace and picks up the coin."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    await ada.join("Ada")

    # Ada starts at the Village Gate; the gold coin is in the Marketplace one step north.
    await ada.input("go north", 2)
    text = None
    while text is None:
        ev = await ada.next_event("command_reply")
        if ev["seq"] == 2:
            text = ev["data"]["text"]

    await ada.input("take gold coin", 3)
    # Drain until the matching command_reply for id 3.
    while True:
        ev = await ada.next_event("command_reply")
        if ev["seq"] == 3:
            text = ev["data"]["text"]
            break

    assert "picked up" in text.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
