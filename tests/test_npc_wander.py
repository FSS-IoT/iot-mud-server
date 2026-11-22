"""Ticket #82: deterministic NPC wandering on the 1 Hz tick.

SPEC v2.1.3 criterion 3: "NPC wandering is deterministic for a fixed seed, runs
on the 1 Hz tick, and never places an NPC in an invalid room."

The world file carries the seed (`World.rng_seed`, 20261121 for the product
world) and each NPC carries `wander`/`wander_every`. This module names the
behavior the engine and server must provide:

* the same seed replays the same paths, tick for tick;
* one tick is one second, and an NPC moves on its own `wander_every` schedule;
* a locked exit and an exit that does not lead to a loaded room are never
  taken;
* `wander: false` never moves;
* moving an NPC changes only `Room.npcs` (never `Room.contents`), so the item
  vocabulary is untouched;
* the room sees the movement as `npc.leave`/`npc.enter`, and `look` refreshes
  `room.state` with the NPC in its new room;
* the server starts the 1 Hz task and stops it with the server, without
  binding the product port.

Nothing here reads a wall clock or an unseeded RNG: ticks are counted, and the
test world is built in memory.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from src.mud.content import NPC, ExitLock, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import Session, V2Server
from src.mud.protocol.adapter import execute
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLD_DIR = REPO_ROOT / "data" / "worlds" / "club_world"
PRODUCT_SEED = 20261121
PRODUCT_WANDERERS = {
    "npc_merchant": 20,
    "npc_carter": 12,
    "npc_miner": 15,
}


# ---------------------------------------------------------------------------
# Small in-memory worlds, so a tick's effect is one visible step
# ---------------------------------------------------------------------------


def _room(room_id: str, name: str) -> Room:
    room = Room(name=name, description=f"{name}, a test room.")
    room.item_id = room_id
    return room


def _npc(npc_id: str, name: str, *, wander: bool = True,
         wander_every: int = 1) -> NPC:
    npc = NPC(
        name=name, description=f"{name}, a test wanderer.",
        item_id=npc_id, wander=wander, wander_every=wander_every,
    )
    npc.instance_id = npc_id
    return npc


def _engine(*rooms: Room, seed: int = 7) -> Engine:
    engine = Engine()
    engine.rng_seed = seed
    for room in rooms:
        engine.rooms[room.name] = room
    return engine


def _corridor() -> tuple[Engine, Room, Room, Room]:
    """alpha <-> beta <-> gamma, no locks, no other exits."""
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    gamma = _room("gamma", "Gamma")
    alpha.exits["east"] = beta
    beta.exits["west"] = alpha
    beta.exits["east"] = gamma
    return _engine(alpha, beta, gamma), alpha, beta, gamma


def _place(engine: Engine, name: str, room: Room):
    player = engine.get_player(name)
    if player is None:
        engine._player_location = room
        player = engine.register_player(name)
        return player
    if player.location is not None:
        player.location.remove_content(player)
    player.location = room
    room.add_content(player)
    return player


async def _advance(engine: Engine, ticks: int) -> None:
    for _ in range(ticks):
        await engine.tick_world()


def _npc_rooms(engine: Engine) -> dict[str, str]:
    """Stable id -> room name for every placed NPC."""
    return {
        npc.item_id: room.name
        for room in engine.rooms.values()
        for npc in room.npcs
    }


# ---------------------------------------------------------------------------
# Determinism and the product seed
# ---------------------------------------------------------------------------


async def _product_engine() -> Engine:
    engine = Engine()
    await load_world(engine, WORLD_DIR)
    return engine


async def test_product_world_carries_the_seed_and_the_three_wanderers():
    engine = await _product_engine()

    assert engine.rng_seed == PRODUCT_SEED
    schedule = {
        npc.item_id: npc.wander_every
        for room in engine.rooms.values()
        for npc in room.npcs
        if npc.wander
    }
    assert schedule == PRODUCT_WANDERERS


async def test_same_seed_replays_the_same_paths_tick_for_tick():
    first = await _product_engine()
    second = await _product_engine()

    first_trail = []
    second_trail = []
    for _ in range(120):
        await first.tick_world()
        await second.tick_world()
        first_trail.append(_npc_rooms(first))
        second_trail.append(_npc_rooms(second))

    assert first_trail == second_trail
    # A fixed seed that never moves would also compare equal; prove the
    # wanderers actually walk.
    assert first_trail[0] != first_trail[-1]


# ---------------------------------------------------------------------------
# The tick and the per-NPC schedule
# ---------------------------------------------------------------------------


async def test_one_tick_advances_one_step():
    engine, _alpha, beta, _gamma = _corridor()
    walker = _npc("npc_walker", "Walker", wander_every=1)
    beta.npcs.append(walker)

    await engine.tick_world()

    assert engine.wanderer.tick == 1
    assert _npc_rooms(engine)["npc_walker"] != "Beta"
    assert len([npc for room in engine.rooms.values() for npc in room.npcs]) == 1


async def test_wander_every_delays_the_move_until_its_tick():
    engine, _alpha, beta, _gamma = _corridor()
    walker = _npc("npc_slow", "Slow Walker", wander_every=3)
    beta.npcs.append(walker)

    await _advance(engine, 2)
    assert _npc_rooms(engine)["npc_slow"] == "Beta"

    await engine.tick_world()
    assert engine.wanderer.tick == 3
    assert _npc_rooms(engine)["npc_slow"] != "Beta"


async def test_wander_false_stays_put_forever():
    engine, _alpha, beta, _gamma = _corridor()
    sitter = _npc("npc_sitter", "Sitter", wander=False, wander_every=1)
    beta.npcs.append(sitter)

    await _advance(engine, 50)

    assert _npc_rooms(engine)["npc_sitter"] == "Beta"
    assert sitter in beta.npcs
    assert engine.wanderer.tick == 50


# ---------------------------------------------------------------------------
# Invalid and locked destinations are never chosen
# ---------------------------------------------------------------------------


async def test_a_locked_exit_is_never_taken():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    gamma = _room("gamma", "Gamma")
    vault = _room("vault", "Vault")
    beta.exits["west"] = alpha
    beta.exits["east"] = gamma
    beta.exits["north"] = vault
    beta.exit_locks["north"] = ExitLock(key="key_vault", label="vault door")
    walker = _npc("npc_walker", "Walker", wander_every=1)
    beta.npcs.append(walker)
    engine = _engine(alpha, beta, gamma, vault)

    await _advance(engine, 200)

    assert "Vault" not in _npc_rooms(engine).values()
    assert beta.exit_is_locked("north")
    assert _npc_rooms(engine) != {}


async def test_an_exit_to_an_unloaded_room_is_never_taken():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    lost = _room("lost", "Lost Room")
    beta.exits["west"] = alpha
    beta.exits["east"] = lost          # a Room the engine never loaded
    beta.exits["north"] = object()     # not a Room at all
    walker = _npc("npc_walker", "Walker", wander_every=1)
    beta.npcs.append(walker)
    engine = _engine(alpha, beta)

    await _advance(engine, 200)

    assert set(_npc_rooms(engine).values()) <= {"Alpha", "Beta"}
    assert lost.npcs == []
    assert "Lost Room" not in _npc_rooms(engine).values()


# ---------------------------------------------------------------------------
# Movement is a `Room.npcs` move, never a `Room.contents` move
# ---------------------------------------------------------------------------


async def test_wandering_npcs_never_enter_room_contents():
    engine, _alpha, beta, _gamma = _corridor()
    beta.npcs.append(_npc("npc_walker", "Walker", wander_every=1))

    await _advance(engine, 20)

    for room in engine.rooms.values():
        assert all(not isinstance(item, NPC) for item in room.contents)
    assert len([npc for room in engine.rooms.values() for npc in room.npcs]) == 1


async def test_look_refreshes_room_state_with_the_npcs_new_room():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    alpha.exits["east"] = beta
    walker = _npc("npc_walker", "Walker", wander_every=1)
    alpha.npcs.append(walker)
    engine = _engine(alpha, beta)
    _place(engine, "Ada", beta)

    await engine.tick_world()
    result = await execute(engine, "Ada", "look")

    assert [event.type for event in result.events] == ["room.state"]
    room = result.events[0].data["room"]
    assert room["npcs"] == [{"id": "npc_walker", "name": "Walker"}]
    assert "Walker" in result.events[0].data["text"]


# ---------------------------------------------------------------------------
# Additive movement events
# ---------------------------------------------------------------------------


async def test_engine_emits_npc_moved_with_the_stable_id():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    alpha.exits["east"] = beta
    alpha.npcs.append(_npc("npc_walker", "Walker", wander_every=1))
    engine = _engine(alpha, beta)
    seen = []

    async def collect(_type, data):
        seen.append(data)

    await engine.subscribe("npc_moved", collect)
    await engine.tick_world()

    assert seen == [{
        "npc": {"id": "npc_walker", "name": "Walker"},
        "from": {"id": "alpha", "name": "Alpha"},
        "to": {"id": "beta", "name": "Beta"},
        "direction": "east",
    }]


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _bind(server: V2Server, name: str) -> _FakeSocket:
    ws = _FakeSocket()
    session = Session(ws=ws, player_name=name, joined=True)
    server._sessions[ws] = session
    server._ws_by_name[name] = ws
    return ws


async def test_server_broadcasts_npc_leave_and_npc_enter():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    alpha.exits["east"] = beta
    alpha.npcs.append(_npc("npc_walker", "Walker", wander_every=1))
    engine = _engine(alpha, beta)
    _place(engine, "Ada", alpha)
    _place(engine, "Ben", beta)
    server = V2Server(engine, port=0)
    ada = _bind(server, "Ada")
    ben = _bind(server, "Ben")

    moves = await server.wander_tick()

    assert len(moves) == 1
    assert [message["type"] for message in ada.sent] == ["npc.leave"]
    assert [message["type"] for message in ben.sent] == ["npc.enter"]
    for message in (ada.sent[0], ben.sent[0]):
        assert message["data"]["npc"] == {
            "id": "npc_walker", "name": "Walker"}
        assert message["data"]["from"] == {"id": "alpha", "name": "Alpha"}
        assert message["data"]["to"] == {"id": "beta", "name": "Beta"}
        assert message["data"]["direction"] == "east"
    assert ada.sent[0]["data"]["text"] == "Walker leaves."
    assert ben.sent[0]["data"]["text"] == "Walker arrives."


# ---------------------------------------------------------------------------
# The real server starts and can stop the 1 Hz task, without port 8765
# ---------------------------------------------------------------------------


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition did not become true")


async def test_server_runs_and_stops_the_wander_tick_without_the_product_port():
    alpha = _room("alpha", "Alpha")
    beta = _room("beta", "Beta")
    alpha.exits["east"] = beta
    beta.exits["west"] = alpha
    alpha.npcs.append(_npc("npc_walker", "Walker", wander_every=1))
    engine = _engine(alpha, beta)
    server = V2Server(
        engine, port=0, tick_seconds=0.01, idle_poll_seconds=3600.0)
    task = asyncio.create_task(server.run())
    try:
        await _wait_for(lambda: server.port != 0)
        assert server.port != 8765
        await _wait_for(lambda: getattr(engine.wanderer, "tick", 0) >= 1)
        assert server._wander_task is not None
        assert server._wander_task is not server._idle_task
        assert not server._wander_task.done()
    finally:
        server.stop()
        await asyncio.wait_for(task, timeout=3.0)

    stopped_at = engine.wanderer.tick
    await asyncio.sleep(0.05)
    assert engine.wanderer.tick == stopped_at
    assert server._wander_task is None
