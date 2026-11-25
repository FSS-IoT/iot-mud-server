
import pytest

from src.mud.content import Room
from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_automatic_room_description_with_exits():
    """[TASK-020 REFINED] Verify movement shows desc + exits."""
    engine = Engine()
    r1 = Room("Hallway", "A narrow corridor.")
    r2 = Room("Garden", "A beautiful garden.")
    r3 = Room("Forest", "A dark forest.")
    r1.exits["north"] = r2
    r2.exits["east"] = r3
    engine.start_room = r1
    player = engine.register_player("Adventurer")

    # Action: Move North to r2
    response = await engine.process_command("go north", player)

    assert "A beautiful garden." in response
    assert "Exits lead East." in response  # r2 has east exit

@pytest.mark.asyncio
async def test_exits_command():
    """Verify 'exits' command shows directions."""
    engine = Engine()
    r1 = Room("Hallway", "Desc")
    r1.exits["north"] = Room("N", "D")
    r1.exits["south"] = Room("S", "D")
    engine.start_room = r1
    player = engine.register_player("Player")

    response = await engine.process_command("exits", player)
    assert "Available exits: North, South." in response

@pytest.mark.asyncio
async def test_exit_emits_signal():
    """Verify 'exit' triggers player_quit signal instead of gameplay."""
    engine = Engine()
    quit_received = False

    async def mock_callback(event_type, data):
        nonlocal quit_received
        if event_type == "player_quit":
            quit_received = True

    await engine.subscribe("player_quit", mock_callback)
    player = engine.register_player("Adventurer")
    
    # We expect 'exit' to trigger the signal
    response = await engine.process_command("exit", player)
    assert response == "Goodbye!"
    assert quit_received is True
