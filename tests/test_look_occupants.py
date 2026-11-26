"""Contract tests for `look` showing occupants (issue #24).

Issue #23 made the server broadcast player_joined/player_left. Issue #24's
SPEC criterion is: "A player can look at a room and its occupants, including
other players present." So a registered player's `look` must name the other
players sharing their room.

To know whose perspective to render from, process_command gains an optional
player_name. Existing callers pass nothing and keep the legacy behavior; only
the multiplayer path (which knows each socket's registered name) passes one.

Red-green: these tests encode behavior that does not exist yet, so they fail now.
"""

import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village


@pytest.fixture
async def engine_with_world():
    """A fresh engine with the starting village loaded (start room = Village Gate)."""
    engine = Engine()
    await load_demo_village(engine)
    return engine


@pytest.mark.asyncio
async def test_look_without_other_occupants_is_unchanged(engine_with_world):
    """A solo registered player sees the room, with no occupants line."""
    engine = engine_with_world
    room = Room("Hallway", "A narrow corridor.")
    engine.start_room = room
    player = engine.register_player("Adventurer")

    response = await engine.process_command("look", player)
    assert "A narrow corridor." in response


@pytest.mark.asyncio
async def test_look_lists_other_occupants(engine_with_world):
    """Two registered players spawn at the Village Gate; Ada's look names Ben."""
    engine = engine_with_world

    engine.register_player("Ada")
    engine.register_player("Ben")

    response = await engine.process_command("look", "Ada")

    assert "heavy stone gate" in response  # room description still present
    assert "Ben" in response  # the other occupant is named


@pytest.mark.asyncio
async def test_look_lists_no_one_when_alone(engine_with_world):
    """A player alone in the room should not see a phantom occupants line."""
    engine = engine_with_world

    engine.register_player("Ada")

    response = await engine.process_command("look", "Ada")

    assert "heavy stone gate" in response
    # No other players are present, so no "others:" indicator appears.
    assert "Others:" not in response


@pytest.mark.asyncio
async def test_look_omits_self(engine_with_world):
    """A player's own name must not appear as an occupant of their own look."""
    engine = engine_with_world

    engine.register_player("Ada")

    response = await engine.process_command("look", "Ada")

    # Only Ada is here, so there should be no occupants listing at all.
    assert "Others:" not in response
