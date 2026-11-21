import pytest
import websockets
import json
import asyncio
from src.mud.net.server import MudServer
from src.mud.engine.core import Engine

@pytest.fixture
async def server_context(request):
    """Provides a clean server/client pair with a fresh engine."""
    # We need to get the run_server fixture which is also async
    # In pytest-asyncio, we can use dependency injection or just start it here
    from src.mud.net.server import MudServer
    engine = Engine()
    server = MudServer(host="localhost", port=8765, engine=engine)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.5) 
    yield server, task
    server.stop()
    try:
        await task
    except asyncio.CancelledError:
        pass

@pytest.mark.asyncio
async def test_command_processing_red(server_context):
    """[TASK-004] RED PHASE: Verify that 'say hello' triggers engine processing (not an echo)."""
    server, _ = server_context
    uri = "ws://localhost:8765"

    async with websockets.connect(uri) as websocket:
        # 1. Handle Handshake
        await websocket.recv()

        # 2. Send a command that should be processed
        payload = {
            "v": 1,
            "type": "input",
            "id": "test-004",
            "data": {"text": "say hello"}
        }
        await websocket.send(json.dumps(payload))

        # 3. The expected behavior (THE RED ASSERTION):
        response = await websocket.recv()
        resp_data = json.loads(response)
        
        # We expect it to be an 'echo' right now because we haven't implemented the engine loop yet.
        # This assertion SHOULD FAIL once we implement the real logic in TASK-005.
        assert resp_data["type"] != "echo", f"Expected engine processing, but got: {resp_data['type']}"

