"""Contract tests for the engine player registry (issue #22).

The engine currently tracks a single player (`self.player`, `self._player_location`).
Multiplayer needs a registry of named players, each with its own location, plus
join and leave. These tests encode that behavior; they fail until the registry
exists, which is the RED phase for #22.

Scope note: this ticket is registry + join/leave only. Movement-driven presence
is issue #23 and is deliberately NOT asserted here.
"""

import pytest

from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village


@pytest.fixture
async def engine_with_world():
    """A fresh engine with the starting village loaded (start room = Village Gate)."""
    engine = Engine()
    await load_demo_village(engine)
    return engine


@pytest.mark.asyncio
async def test_register_two_named_players_at_start_room(engine_with_world):
    """Two players register with distinct names and both spawn at the start room."""
    engine = engine_with_world

    ada = engine.register_player("Ada")
    ben = engine.register_player("Ben")

    assert ada.name == "Ada"
    assert ben.name == "Ben"
    assert ada is not ben
    # Both spawn at the loaded start room.
    assert ada.location is not None
    assert ada.location.name == "Village Gate"
    assert ben.location.name == "Village Gate"


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected(engine_with_world):
    """A second player cannot register with a name already in use."""
    engine = engine_with_world

    engine.register_player("Ada")
    with pytest.raises(ValueError):
        engine.register_player("Ada")


@pytest.mark.asyncio
async def test_remove_player_leaves_registry(engine_with_world):
    """Joining then leaving removes the player from the registry and clears its room."""
    engine = engine_with_world

    assert engine.remove_player("Ada") is False  # never joined
    engine.register_player("Ada")
    assert engine.get_player("Ada") is not None

    assert engine.remove_player("Ada") is True
    assert engine.get_player("Ada") is None


@pytest.mark.asyncio
async def test_players_in_room(engine_with_world):
    """A room reports exactly the players currently in it."""
    engine = engine_with_world

    ada = engine.register_player("Ada")
    ben = engine.register_player("Ben")
    start_room = engine.get_room("Village Gate")

    assert set(engine.players_in_room(start_room)) == {ada, ben}

    # After Ada leaves, only Ben remains in the room.
    engine.remove_player("Ada")
    assert engine.players_in_room(start_room) == [ben]
