import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_impossible_direction_reports_reason():
    """A handled-but-invalid move must not fall through to the generic fallback."""
    engine = Engine()
    engine.start_room = Room("Hallway", "A narrow corridor.")
    player = engine.register_player("Adventurer")

    result = await engine.process_command("go west", player)

    assert result == "You cannot go west."


@pytest.mark.asyncio
async def test_say_preserves_player_casing():
    """'say' echoes the player's own words instead of a lowercased copy."""
    engine = Engine()
    player = engine.register_player("Adventurer")

    result = await engine.process_command("say Hello There", player)

    assert result == 'You say, "Hello There".'
