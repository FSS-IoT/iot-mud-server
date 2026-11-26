"""#133: one required actor at the engine seam.

The engine no longer carries a legacy single-player slot beside the registered
players, and the command entry point requires an actor. These tests fail on the
parent commit, where `Engine.player` / `_player_location` still exist and
`process_command` accepts no actor.
"""

import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


def _engine_with_room() -> tuple[Engine, object, Room]:
    engine = Engine()
    room = Room("Hall", "A hall.")
    engine._rooms["Hall"] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    room.add_content(Entity("Sword", "A blade.", item_id="sword"))
    return engine, player, room


def test_the_engine_has_no_legacy_single_player_slot():
    engine = Engine()
    assert not hasattr(engine, "player")
    assert not hasattr(engine, "_player_location")
    assert engine.start_room is None


@pytest.mark.asyncio
async def test_process_command_requires_an_actor():
    """There is no single-player fallback: an actor is mandatory."""
    engine, _player, _room = _engine_with_room()

    with pytest.raises(TypeError):
        await engine.process_command("look")


@pytest.mark.asyncio
async def test_process_command_runs_as_the_registered_actor():
    """v1 (name) and v2 (Player) use the same actor path and see the same reply."""
    engine, player, _room = _engine_with_room()

    by_player = await engine.process_command("look", player)
    by_name = await engine.process_command("look", "Ada")

    assert by_player == by_name
    assert "A hall." in by_player
