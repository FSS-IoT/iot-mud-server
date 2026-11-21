import pytest
import websockets
import json
import asyncio
from src.mud.net.server import MudServer
from src.mud.engine.core import Engine
from src.mud.world.loader import load_demo_village


@pytest.fixture
async def server_context():
    """Provides a clean server/client pair with a fresh, populated engine."""
    engine = Engine()
    await load_demo_village(engine)
    server = MudServer(engine, host="localhost", port=8765)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(1)  # Wait for the server to start
    yield server, task
    server.stop()
    await task


@pytest.mark.asyncio
async def test_server_command_routing_look(server_context):
    """[TASK-005] GREEN PHASE: Verify that 'look' command returns the room description."""
    server, _ = server_context
    uri = "ws://localhost:8765"

    async with websockets.connect(uri) as websocket:
        # 1. Handle Handshake
        handshake = await websocket.recv()
        assert json.loads(handshake)["type"] == "hello"

        # 2. Send the 'look' command
        await websocket.send(json.dumps({
            "v": 1,
            "type": "input",
            "id": "test-req-005-a",
            "data": {"text": "look"}
        }))

        # 3. Expect the processed response from the engine (command_reply)
        response = await websocket.recv()
        assert json.loads(response)["type"] == "command_reply"
        assert "A heavy stone gate stands tall" in json.loads(response)["data"]["text"]


@pytest.mark.asyncio
async def test_server_command_routing_say(server_context):
    """[TASK-005] GREEN PHASE: Verify 'say' command returns processed text."""
    server, _ = server_context
    uri = "ws://localhost:8765"

    async with websockets.connect(uri) as websocket:
        # 1. Handle Handshake
        await websocket.recv()

        # 2. Send the 'say' command
        await websocket.send(json.dumps({
            "v": 1,
            "type": "input",
            "id": "test-req-005-b",
            "data": {"text": "say hello world"}
        }))

        # 3. Expect response
        response = await websocket.recv()
        assert json.loads(response)["type"] == "command_reply"
        assert 'You say, "hello world"' in json.loads(response)["data"]["text"]
