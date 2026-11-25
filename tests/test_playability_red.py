import asyncio
import json

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.server import MudServer
from src.mud.world.loader import load_demo_village


@pytest.mark.asyncio
async def test_automated_play_session_red():
    """[TASK-017] RED PHASE: Simulate a player connecting, moving, and interacting with items."""
    engine = Engine()
    await load_demo_village(engine)
    
    server = MudServer(engine, port=8766) # Using alternative port for safety
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.5)
    
    uri = "ws://localhost:8766"
    
    try:
        async with websockets.connect(uri) as websocket:
            # 1. Handle Handshake
            handshake_raw = await websocket.recv()
            assert json.loads(handshake_raw)["type"] == "hello"

            # Register the actor this session commands as.
            await websocket.send(json.dumps({
                "v": 1, "type": "join", "id": 0, "data": {"name": "Hero"}
            }))
            while json.loads(await websocket.recv())["type"] != "room_state":
                pass

            # 2. Send 'look' command
            await websocket.send(json.dumps({
                "v": 1,
                "type": "input",
                "id": 1,
                "data": {"text": "look"}
            }))
            resp = await websocket.recv()
            assert json.loads(resp)["data"]["text"]

            # 3. Move to another room
            await websocket.send(json.dumps({
                "v": 1,
                "type": "input",
                "id": 2,
                "data": {"text": "go north"}
            }))
            resp = await websocket.recv()
            # We expect a description of the marketplace or similar
            assert "marketplace" in json.loads(resp)["data"]["text"].lower()

            # 4. Try to take something (Gold Coin from Market as per loader)
            await websocket.send(json.dumps({
                "v": 1,
                "type": "input",
                "id": 3,
                "data": {"text": "take gold coin"}
            }))
            resp = await websocket.recv()
            assert "picked up" in json.loads(resp)["data"]["text"].lower()

    finally:
        server.stop()
        await task
