
import pytest

from src.mud.engine.core import Engine


@pytest.mark.asyncio
async def test_engine_emits_command_event():
    """[TASK-006] RED PHASE: Verify that the engine emits an event when a command is processed."""
    engine = Engine()
    events_received = []

    # A simple listener to track events
    async def listener(event_type, data):
        events_received.append((event_type, data))

    # The 'subscribe' method does not exist yet in the current implementation of Engine.
    # This is intentional for the RED phase.
    await engine.subscribe("command_processed", listener)
    player = engine.register_player("Adventurer")

    await engine.process_command("look", player)

    assert len(events_received) > 0, "The engine did not emit any events for the command 'look'."
