import asyncio
import json

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.server import MudServer
from src.mud.world.loader import load_demo_village


@pytest.mark.asyncio
async def test_full_adventure_cycle():
    """[TASK-017] GREEN PHASE: Comprehensive end-to-end automation.
    Checks: Login -> Look -> Move -> Take -> Drop -> Location Check.
    """
    engine = Engine()
    await load_demo_village(engine)
    
    server = MudServer(engine, port=8766)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.5)
    
    uri = "ws://localhost:8766"
    
    try:
        async with websockets.connect(uri) as websocket:
            # 1. Handshake
            handshake_raw = await websocket.recv()
            assert json.loads(handshake_raw)["type"] == "hello"

            # Register the actor this session commands as.
            await websocket.send(json.dumps({
                "v": 1, "type": "join", "id": 0, "data": {"name": "Hero"}
            }))
            while True:
                joined = json.loads(await websocket.recv())
                if joined["type"] == "room_state":
                    break

            async def command(idx, text):
                await websocket.send(json.dumps({
                    "v": 1, "type": "input", "id": idx,
                    "data": {"text": text}
                }))
                while True:
                    message = json.loads(await websocket.recv())
                    if (message["type"] == "command_reply"
                            and message["seq"] == idx):
                        return message

            player = engine.get_player("Hero")

            # 2. Look at initial room
            resp = await command(1, "look")
            assert len(resp["data"]["text"]) > 0

            # 3. Move to Marketplace
            resp = await command(2, "go north")
            assert "marketplace" in resp["data"]["text"].lower()

            # 4. Pick up Gold Coin
            resp = await command(3, "take gold coin")
            assert "picked up" in resp["data"]["text"].lower()
            
            # Assert internal engine state after networking calls
            assert any(item.name == "Gold Coin" for item in player.contents)

            # 5. Drop the coin in the market
            resp = await command(4, "drop gold coin")
            assert "dropped" in resp["data"]["text"].lower()

            # 6. Final checks: Player has nothing, Room has the coin
            assert len(player.contents) == 0
            market_room = next(r for r in engine.rooms.values() if r.name == "Marketplace")
            assert any(i.name == "Gold Coin" for i in market_room.contents)

    finally:
        server.stop()
        await task
