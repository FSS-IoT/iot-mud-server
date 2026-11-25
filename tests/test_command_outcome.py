"""Typed command outcomes (#130, Phase 1).

The engine used to return only a prose string; the v2 adapter re-derived the
event from that string and a state diff. These tests pin the new typed seam:
`Engine.execute(actor, text) -> CommandOutcome` carries the reply, a typed
success/error, and the semantic events the command produced. The v2 adapter is
not rewritten yet, so nothing here asserts wire output.
"""

import pytest

from src.mud.content import Entity, ExitLock, Room
from src.mud.engine.core import Engine
from src.mud.protocol.v2 import CommandOutcome, SemanticEvent


@pytest.fixture
def vault():
    engine = Engine()
    room = Room("Vault", "A stone vault.")
    room.item_id = "vault"

    chest = Entity("Chest", "An oak chest.", item_id="chest")
    coin = Entity("Coin", "A gold coin.", item_id="coin")
    chest.add_content(coin)
    room.add_content(chest)

    engine._rooms[room.name] = room
    engine._player_location = room
    player = engine.register_player("Ada")
    return engine, player, room, chest, coin


def _kinds(outcome: CommandOutcome) -> list[str]:
    return [event.kind for event in outcome.events]


@pytest.mark.asyncio
async def test_take_returns_a_typed_outcome(vault):
    engine, player, room, chest, coin = vault
    chest.remove_content(coin)
    room.add_content(coin)

    outcome = await engine.execute(player, "take coin")

    assert isinstance(outcome, CommandOutcome)
    assert outcome.status == "success"
    assert outcome.error_code is None
    assert outcome.reply == "You picked up the Coin."
    assert _kinds(outcome) == ["object.taken"]
    event = outcome.events[0]
    assert isinstance(event, SemanticEvent)
    assert event.scope == "room"
    assert event.data["object"]["id"] == "coin"
    assert event.data["by"] == "Ada"
    # The move actually happened in the authoritative engine.
    assert coin in player.contents
    assert coin not in room.contents


@pytest.mark.asyncio
async def test_take_unknown_item_is_a_typed_error(vault):
    engine, player, _room, _chest, _coin = vault

    outcome = await engine.execute(player, "take dragon")

    assert outcome.status == "error"
    assert outcome.error_code == "not_found"
    assert outcome.events == []


@pytest.mark.asyncio
async def test_drop_returns_a_typed_outcome(vault):
    engine, player, room, chest, coin = vault
    chest.remove_content(coin)
    player.add_content(coin)

    outcome = await engine.execute(player, "drop coin")

    assert outcome.status == "success"
    assert outcome.reply == "You dropped the Coin."
    assert _kinds(outcome) == ["object.dropped"]
    assert outcome.events[0].scope == "room"
    assert coin in room.contents
    assert coin not in player.contents


@pytest.mark.asyncio
async def test_open_returns_a_typed_outcome(vault):
    engine, player, _room, chest, coin = vault

    outcome = await engine.execute(player, "open chest")

    assert outcome.status == "success"
    assert _kinds(outcome) == ["object.opened"]
    assert outcome.events[0].data["object"]["id"] == "chest"
    assert chest.open is True
    assert coin in chest.contents


@pytest.mark.asyncio
async def test_open_locked_container_is_a_typed_locked_error(vault):
    engine, player, _room, chest, _coin = vault
    chest.locked = True

    outcome = await engine.execute(player, "open chest")

    assert outcome.status == "error"
    assert outcome.error_code == "locked"
    assert outcome.events == []


@pytest.mark.asyncio
async def test_go_through_a_locked_exit_is_a_typed_locked_error(vault):
    engine, player, room, _chest, _coin = vault
    hall = Room("Hall", "A bare hall.")
    hall.item_id = "hall"
    room.exits["north"] = hall
    room.exit_locks["north"] = ExitLock(key="gold_key", label="iron door")
    engine._rooms["Hall"] = hall

    outcome = await engine.execute(player, "go north")

    assert outcome.status == "error"
    assert outcome.error_code == "locked"
    assert "locked" in outcome.reply.lower()
    # A refused move changes nothing.
    assert player.location is room


@pytest.mark.asyncio
async def test_go_through_an_open_exit_is_a_typed_success(vault):
    engine, player, room, _chest, _coin = vault
    hall = Room("Hall", "A bare hall.")
    hall.item_id = "hall"
    room.exits["north"] = hall
    engine._rooms["Hall"] = hall

    outcome = await engine.execute(player, "go north")

    assert outcome.status == "success"
    assert _kinds(outcome) == ["room.state", "room.leave", "room.enter"]
    assert player.location is hall


@pytest.mark.asyncio
async def test_look_returns_a_room_state_outcome(vault):
    """`look` produces a room.state event and the engine's reply."""
    engine, player, _room, _chest, _coin = vault

    outcome = await engine.execute(player, "look")

    assert outcome.status == "success"
    assert outcome.reply
    assert _kinds(outcome) == ["room.state"]


def test_adapter_is_a_pure_shaping_layer():
    """#130: the adapter no longer owns grammar, prose classification, or a diff."""
    import inspect

    from src.mud.protocol import adapter

    assert not hasattr(adapter, "_ALIASES")
    assert not hasattr(adapter, "_classify")
    source = inspect.getsource(adapter)
    assert "before_room_contents" not in source
    assert "before_inventory" not in source
