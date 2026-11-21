import pytest
import asyncio
import websockets
import json
from src.mud.engine.core import Engine
from src.mud.net.server import MudServer

@pytest.fixture
async def running_server():
    """Fixture to start and stop the server automatically."""
    engine = Engine()
    server = MudServer(engine, port=8765)
    task = asyncio.create_task(server.run())
    
    # Give the server a moment to bind to the socket
    await asyncio.sleep(0.5)
    
    yield server, task
    
    server.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
    finally:
        if not task.done():
            task.cancel()

@pytest.mark.asyncio
async def test_websocket_handshake_green(running_server):
    """[TASK-016] GREEN PHASE: Verify that a client receives the correct greeting upon connection."""
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"
    
    async with websockets.connect(uri) as websocket:
        # 1. Expect the handshake
        handshake_raw = await websocket.recv()
        handshake = json.loads(handshake_raw)
        
        # 2. Validate fields
        assert handshake["type"] == "hello"
        assert "data" in handshake
        assert handshake["data"]["server"] == "iot-mud-dev"
        assert "protocol" in handshake["data"]
        assert isinstance(handshake["seq"], int)
