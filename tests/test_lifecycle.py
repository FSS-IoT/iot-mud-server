import pytest
import asyncio
from src.mud.engine.core import Engine
from src.mud.net.server import MudServer

@pytest.mark.asyncio
async def test_server_graceful_shutdown():
    """[TASK-003] GREEN PHASE: Verify the server can be stopped gracefully."""
    server = MudServer(Engine(), host="localhost", port=8765)
    
    # Start the server in a background task
    server_task = asyncio.create_task(server.run())
    
    # Give it a moment to start up
    await asyncio.sleep(1)
    
    # Check if running
    assert not server_task.done()
    
    # Trigger stop
    server.stop()
    
    # Wait for the task to finish (with a timeout just in case)
    try:
        await asyncio.wait_for(server_task, timeout=3)
    except asyncio.TimeoutError:
        pytest.fail("Server failed to shut down within 3 seconds.")

    assert server_task.done()
