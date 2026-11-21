"""Red-green fixes for bug 3: movement and position awareness for registered players.

Bug 3: `go` (and the other interaction commands) only moved the legacy
single-player slot (`engine._player_location` / `engine.player`). A registered,
named player never actually moved — so `look`, which reads the registered
player's `.location`, reported a stale room. Movement and item handling were not
shared between the two slots.

This file covers movement + position broadcasting:

- Engine `move_player(name, direction)` moves a *registered* player's location
  and emits a `player_moved` semantic event (name / from / to). The `go` branch
  routes through it when a `player_name` is supplied; the legacy path is kept for
  backward compatibility.
- The server subscribes to `player_moved` and broadcasts `player_left` to the
  departing room and `player_joined` to the arriving room, so everyone in the
  affected rooms sees the change.

These tests encode behavior that does not exist yet, so they fail now (RED).
"""

import asyncio
import json

import pytest
import websockets

from src.mud.content import Room
from src.mud.engine.core import Engine
from src.mud.net.server import MudServer
from src.mud.world.loader import load_demo_village

# ---------------------------------------------------------------------------
# Pure engine: a registered player actually moves
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
async def test_registered_player_moves_to_destination():
    """`go north` as a registered player relocates that player's location."""
    engine = _two_room_engine()
    player = engine.register_player("Ada")
    assert player.location.name == "Village Gate"

    response = await engine.process_command("go north", "Ada")

    assert player.location.name == "Marketplace"
    assert "Marketplace" in response


@pytest.mark.asyncio
async def test_registered_player_cannot_go_without_exit():
    """No exit in a direction -> clear refusal, player stays put."""
    engine = _two_room_engine()  # gate only has a north exit
    engine.register_player("Ada")

    response = await engine.process_command("go south", "Ada")

    assert response == "You cannot go south."


@pytest.mark.asyncio
async def test_registered_move_emits_player_moved():
    """Moving emits a `player_moved` event carrying name / from / to."""
    engine = _two_room_engine()
    events = []
    await engine.subscribe(
        "player_moved", lambda et, d: events.append((et, dict(d)))
    )
    engine.register_player("Ada")

    await engine.process_command("go north", "Ada")

    assert ("player_moved", {
        "name": "Ada", "from": "Village Gate", "to": "Marketplace",
    }) in events


@pytest.mark.asyncio
async def test_legacy_move_still_works_without_player_name():
    """Backward compat: no player_name still moves the legacy slot."""
    engine = _two_room_engine()

    await engine.process_command("go north")

    assert engine._player_location.name == "Marketplace"


# ---------------------------------------------------------------------------
# Server: position changes are broadcast to affected rooms
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
    await load_demo_village(engine)  # start room = Village Gate; market one step north
    server = MudServer(engine, port=8769)
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
async def test_move_broadcasts_left_to_departing_room(running_server):
    """When Ada leaves a room, everyone still there hears player_left(Ada)."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    ben = await FakeClient(uri).connect()

    await ada.join("Ada")
    await ben.join("Ben")

    # Both are now at the Village Gate. Ada hears about Ben; Ben hears about Ada.
    assert (await ada.next_event("player_joined"))["data"]["name"] == "Ben"
    assert (await ben.next_event("player_joined"))["data"]["name"] == "Ada"

    # Ada walks north to the Marketplace. The only remaining occupant of the gate
    # is Ben, who should hear that Ada left.
    await ada.input("go north", 1)

    event = await ben.next_event("player_left")
    assert event["data"]["name"] == "Ada"

    await ada.close()
    await ben.close()


@pytest.mark.asyncio
async def test_move_broadcasts_joined_to_arriving_room(running_server):
    """When Ben arrives in a room, everyone already there hears player_joined(Ben)."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    ben = await FakeClient(uri).connect()

    await ada.join("Ada")
    await ben.join("Ben")

    # Clear the initial player_joined events (both at the gate).
    assert (await ada.next_event("player_joined"))["data"]["name"] == "Ben"
    assert (await ben.next_event("player_joined"))["data"]["name"] == "Ada"

    # Ada moves to the Marketplace first; Ben (still at the gate) hears her leave.
    await ada.input("go north", 1)
    assert (await ben.next_event("player_left"))["data"]["name"] == "Ada"

    # Now Ben follows to the Marketplace, where Ada is waiting. Ada should hear that
    # Ben has arrived.
    await ben.input("go north", 2)
    event = await ada.next_event("player_joined")
    assert event["data"]["name"] == "Ben"

    await ada.close()
    await ben.close()


@pytest.mark.asyncio
async def test_mover_receives_room_contents_via_reply(running_server):
    """Moving carries room contents in the `go` reply, with no redundant push.

    The `go` reply is the single source of truth for what a mover sees on arrival:
    travel prose, destination description, exits, and items. A separate `room_state`
    push from the server would duplicate that reply (both describing the same room),
    so it must not be sent. This test fails until (a) the reply carries contents AND
    (b) no redundant `room_state` arrives between the command and its reply.
    """
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    ada = await FakeClient(uri).connect()
    await ada.join("Ada")

    # Ada starts at the Village Gate (no items); she already got a room_state for it.
    assert (await ada.next_event("room_state"))["data"]["name"] == "Village Gate"

    # Ada moves north into the Marketplace, where a Gold Coin is present.
    await ada.input("go north", 1)

    # Drain messages until our command_reply for id 1 arrives, asserting no
    # redundant market room_state was pushed in between.
    reply = None
    while reply is None:
        ev = await asyncio.wait_for(ada.q.get(), timeout=3.0)
        if ev["type"] == "room_state" and ev["data"]["name"] == "Marketplace":
            raise AssertionError(
                "Server pushed a redundant room_state on move; the go reply is the "
                "single source of truth for what the mover sees."
            )
        if ev["type"] == "command_reply" and ev["seq"] == 1:
            reply = ev

    text = reply["data"]["text"].lower()
    assert "marketplace" in text
    # The mover sees the items via the reply itself.
    assert "gold coin" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
