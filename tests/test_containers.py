import pytest
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_nested_containers():
    """[TASK-012] RED PHASE: Verify that taking a container preserves its contents."""
    engine = Engine()
    player = Entity("Hero", "A brave adventurer.")
    room = Room("Forest", "A dark forest.")
    box = Entity("Box", "A wooden box.", weight=1.0)
    coin = Entity("Coin", "A shiny coin.", weight=0.01)
    
    # Setup state
    engine.player = player
    room.add_content(box)
    box.add_content(coin)
    engine._player_location = room
    
    # Action: Take the box
    await engine.process_command("take box")
    
    # Assertions
    assert box in player.contents
    assert coin in box.contents  # THE KEY PART: Nested item is still in the box!
    assert box not in room.contents
    assert coin not in room.contents
    assert len(player.contents) == 1
    assert len(box.contents) == 1
