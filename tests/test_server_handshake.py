import pytest
import websockets
import asyncio
import json

from src.mud.engine.core import Engine
from src.mud.net.server import MudServer


@pytest.fixture
async def run_server():
    """Fixture to start the server in a background task."""
    server = MudServer(Engine(), host="localhost", port=8765)
    # Start the server in a separate task
    task = asyncio.create_task(server.run())
    # Wait for it to be ready
    await asyncio.sleep(1)
    yield server
    # Stop the server
    server.stop()
    await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_server_handshake_success(run_server):
    """[TASK-002] GREEN PHASE: Verify the server sends a correctly formatted 'hello' message."""
    uri = "ws://localhost:8765"
    try:
        async with websockets.connect(uri) as websocket:
            # Wait for the handshake message
            message = await websocket.recv()
            data = json.loads(message)

            # Assertions based on our specification
            assert data["type"] == "hello"
            assert "server" in data["data"]
            assert "protocol" in data["data"]
            assert data["v"] == 1
    except Exception as e:
        pytest.fail(f"Handshake failed: {e}")
