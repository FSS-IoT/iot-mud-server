import pytest

from src.mud.content import Entity, Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_deterministic_parsing():
    """[TASK-007] Verify that specific syntax is handled deterministically."""
    engine = Engine()
    player = engine.register_player("Adventurer")

    result = await engine.process_command("say hello world", player)
    assert result == 'You say, "hello world".'


@pytest.mark.asyncio
async def test_command_alias_deterministic():
    """[TASK-007] Verify that 'get' resolves to 'take' on the fast path."""
    engine = Engine()
    room = Room("Clearing", "A quiet clearing.")
    sword = Entity("Sword", "A blunt sword.")
    room.add_content(sword)
    engine.start_room = room
    player = engine.register_player("Adventurer")

    result = await engine.process_command("get sword", player)

    assert "picked up" in result.lower()
    assert sword in player.contents
    assert sword not in room.contents
