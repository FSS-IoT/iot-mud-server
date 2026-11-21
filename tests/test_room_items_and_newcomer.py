"""Red-green fixes for two visibility bugs in the multiplayer slice.

Bug 1 (core): `look` never lists items that are in a room, so an item such as
the Gold Coin sits invisible until you happen to guess `take gold coin`.
Bug 2 (server): when a player joins a room that already has occupants, those
existing players are told about the newcomer (`player_joined`) but the newcomer
is never told who is already in the room. The newly-joined player sees an empty
room even though someone is standing there.

These tests encode the desired behavior; they fail until the code is fixed.
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
# Bug 1: `look` shows items in the room
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_look_lists_items_in_room():
    """A room holding an item should mention that item when you look."""
    engine = Engine()
    gate = Room("Village Gate", "A heavy stone gate stands tall.")
    coin = Entity("Gold Coin", "A shiny, minted copper coin.", weight=0.01)
    gate.add_content(coin)
    engine._rooms["Village Gate"] = gate
    engine._player_location = gate

    response = await engine.process_command("look")

    assert "A heavy stone gate stands tall." in response
    # The item is visible without guessing the command.
    assert "Gold Coin" in response


@pytest.mark.asyncio
async def test_look_omits_players_from_item_line():
    """Other players are named on their own line, not lumped into items."""
    engine = Engine()
    room = Room("Market", "Lively vendors cry out their wares.")
    torch = Entity("Torch", "A flickering wooden torch.", weight=0.5)
    room.add_content(torch)
    engine._rooms["Market"] = room
    engine._player_location = room

    # Register two players both standing in the market.
    p1 = engine.register_player("Ada")
    p2 = engine.register_player("Ben")
    p1.location = room
    p2.location = room

    response = await engine.process_command("look", "Ada")

    # Torch is listed as an item...
    assert "Torch" in response
    # ...and Ben is named on the occupants line, not as an item.
    assert "Others in the room: Ben." in response


@pytest.mark.asyncio
async def test_look_without_items_has_no_item_line():
    """A bare room should not invent an items line."""
    engine = Engine()
    room = Room("Empty", "A hollow chamber.")
    engine._rooms["Empty"] = room
    engine._player_location = room

    response = await engine.process_command("look")

    assert "A hollow chamber." in response


# ---------------------------------------------------------------------------
# Bug 2: newcomer learns who is already in the room
# ---------------------------------------------------------------------------

@pytest.fixture
async def running_server():
    """Start a server backed by the starting-village world, stop it after."""
    engine = Engine()
    await load_demo_village(engine)  # start room = Village Gate (no items)
    server = MudServer(engine, port=8768)
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


async def _handshake(uri):
    ws = await websockets.connect(uri)
    first = json.loads(await ws.recv())  # hello
    assert first["type"] == "hello"
    return ws


async def _read_until(websocket, event_type):
    """Read messages until one of `event_type` arrives, returning that message.

    A player now receives a `room_state` on join (so even a solo player sees their
    room immediately), so the first post-join message is not always the broadcast we
    are waiting for; skip any earlier messages rather than assuming order.
    """
    while True:
        raw = await asyncio.wait_for(websocket.recv(), timeout=3.0)
        msg = json.loads(raw)
        if msg["type"] == event_type:
            return msg


@pytest.mark.asyncio
async def test_newcomer_is_told_about_existing_occupant(running_server):
    """Ada joins first; when Ben joins, Ben must learn that Ada is present."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await _handshake(uri)
    await ada.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ada"}}))
    # Let the server register Ada and settle.
    await asyncio.sleep(0.2)

    ben = await _handshake(uri)
    await ben.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ben"}}))
    # Ben should receive an event naming the pre-existing occupant Ada.
    event = await _read_until(ben, "player_joined")

    assert event["type"] == "player_joined"
    assert event["data"]["name"] == "Ada"


@pytest.mark.asyncio
async def test_existing_occupant_is_told_about_newcomer(running_server):
    """Ada joins first; when Ben joins, Ada must learn that Ben is present."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await _handshake(uri)
    await ada.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ada"}}))
    await asyncio.sleep(0.2)

    ben = await _handshake(uri)
    await ben.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ben"}}))

    # Ada should receive a player_joined naming Ben (skip any buffered room_state).
    event = await _read_until(ada, "player_joined")

    assert event["type"] == "player_joined"
    assert event["data"]["name"] == "Ben"


@pytest.mark.asyncio
async def test_look_shows_occupants_to_newcomer(running_server):
    """End-to-end: after both are present, a look lists the other player."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await _handshake(uri)
    await ada.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ada"}}))
    await asyncio.sleep(0.2)

    ben = await _handshake(uri)
    await ben.send(json.dumps({"v": 1, "type": "join", "id": 1,
                               "data": {"name": "Ben"}}))

    # Ben sends a look (id 10). A player_joined about Ada may arrive before the
    # reply; drain everything until we get the matching command_reply.
    await ben.send(json.dumps({"v": 1, "type": "input", "id": 10,
                               "data": {"text": "look"}}))
    reply_text = None
    while reply_text is None:
        raw = await asyncio.wait_for(ben.recv(), timeout=3.0)
        event = json.loads(raw)
        if event["type"] == "command_reply" and event["seq"] == 10:
            reply_text = event["data"]["text"]

    assert "Ada" in reply_text  # Ben's look now names Ada


if __name__ == "__main__":
    asyncio.run(main := pytest.main([__file__, "-v"]))
