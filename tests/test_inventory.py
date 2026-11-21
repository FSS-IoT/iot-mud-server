import pytest
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine

@pytest.mark.asyncio
async def test_take_item_red():
    """[TASK-011] RED PHASE: Verify that 'take item' moves an item from the room to the player's inventory."""
    engine = Engine()
    player = Entity("Hero", "A brave adventurer.")
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    
    # Setup state
    engine.player = player # Note: This will fail until Engine has 'player' attribute
    room.add_content(sword)
    engine._player_location = room
    
    # Action
    await engine.process_command("take sword")
    
    # Assertions
    assert sword in player.contents
    assert sword not in room.contents
    assert len(player.contents) == 1
    assert len(room.contents) == 0

@pytest.mark.asyncio
async def test_drop_item_red():
    """[TASK-011] RED PHASE: Verify that 'drop item' moves an item from the player to the room."""
    engine = Engine()
    player = Entity("Hero", "A brave adventurer.")
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    
    # Setup state
    engine.player = player
    player.add_content(sword)
    engine._player_location = room
    
    # Action
    await engine.process_command("drop sword")
    
    # Assertions
    assert sword in room.contents
    assert sword not in player.contents
    assert len(room.contents) == 1
    assert len(player.contents) == 0
