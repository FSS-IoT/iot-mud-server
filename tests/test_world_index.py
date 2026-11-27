"""#139: one world index and one identity rule.

Identity is the stable id, not the display name. Two rooms may share a display
name; the id map holds both, and the name map keeps the first-loaded room as
the documented tie-break. Shaping a payload never assigns identity.
"""

from __future__ import annotations

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.protocol.adapter import build_room_state
from src.mud.storage.keys import room_key
from src.mud.world.loader import load_world

WORLD = """\
name: Twin Halls
start_room: hall_a
rng_seed: 1
rooms:
  - id: hall_a
    name: Hall
    description: The first hall.
    exits:
      east: hall_b
    objects:
      - id: coin
        name: Gold Coin
        description: A minted coin.
  - id: hall_b
    name: Hall
    description: The second hall.
"""


def _write(tmp_path):
    path = tmp_path / "twin.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_two_rooms_with_the_same_display_name_stay_distinct(tmp_path):
    engine = Engine()
    await load_world(engine, _write(tmp_path))

    first = engine.get_room_by_id("hall_a")
    second = engine.get_room_by_id("hall_b")

    assert first is not None and second is not None
    assert first is not second
    assert first.name == second.name == "Hall"
    # Name lookup keeps the first-loaded room as the deterministic tie-break.
    assert engine.get_room("Hall") is first


@pytest.mark.asyncio
async def test_login_finds_the_room_by_id_without_a_scan(tmp_path):
    engine = Engine()
    await load_world(engine, _write(tmp_path))

    room = engine.get_room_by_id("hall_b")

    assert room is not None and room_key(room) == "hall_b"
    assert not hasattr(V2Server, "_room_by_key"), (
        "login must use the index, not a scan over engine.rooms")


def _engine_with_coin():
    engine = Engine()
    hall = Room("Hall", "A hall.")
    hall.item_id = "hall"
    engine._rooms["Hall"] = hall
    engine.start_room = hall
    player = engine.register_player("Ada")
    coin = Entity("Coin", "A coin.", item_id="coin")
    hall.add_content(coin)
    return engine, player, coin


def test_room_state_shaping_does_not_assign_identity():
    engine, player, coin = _engine_with_coin()

    build_room_state(engine, player)

    assert coin.instance_id is None


@pytest.mark.asyncio
async def test_event_shaping_does_not_assign_identity():
    engine, player, coin = _engine_with_coin()

    await engine.execute(player, "take coin")

    assert coin.instance_id is None
