import pytest

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_take_item_red():
    """[TASK-011] RED PHASE: Verify that 'take item' moves an item from the room to the player's inventory."""
    engine = Engine()
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    
    # Setup state
    engine.start_room = room
    player = engine.register_player("Hero")
    room.add_content(sword)
    
    # Action
    await engine.process_command("take sword", player)
    
    # Assertions
    assert sword in player.contents
    assert sword not in room.contents
    assert len(player.contents) == 1
    # The registered player stands in the room, so count only non-player items.
    assert [c for c in room.contents if not isinstance(c, Player)] == []

@pytest.mark.asyncio
async def test_drop_item_red():
    """[TASK-011] RED PHASE: Verify that 'drop item' moves the item from the player to the room."""
    engine = Engine()
    room = Room("Forest", "A dark forest.")
    sword = Entity("Sword", "A sharp blade.", weight=2.0)
    
    # Setup state
    engine.start_room = room
    player = engine.register_player("Hero")
    player.add_content(sword)
    
    # Action
    await engine.process_command("drop sword", player)
    
    # Assertions
    assert sword in room.contents
    assert sword not in player.contents
    assert len([c for c in room.contents if not isinstance(c, Player)]) == 1
    assert len(player.contents) == 0


@pytest.mark.asyncio
async def test_inventory_names_the_weight_unit_on_the_wire():
    """`player.inventory` names its weight unit so a client can label it (#146)."""
    from src.mud.protocol.adapter import execute

    engine = Engine()
    room = Room("Forest", "A dark forest.")
    engine.start_room = room
    engine.register_player("Hero")

    result = await execute(engine, "Hero", "inventory")
    payload = result.events[0].data

    assert payload["unit"] == "stones"
