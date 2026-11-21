import pytest
import asyncio
import websockets
import json
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
    
    # We want to know where the player is to verify movement
    initial_room_name = engine._player_location.name
    
    server = MudServer(engine, port=8766)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.5)
    
    uri = f"ws://localhost:8766"
    
    try:
        async with websockets.connect(uri) as websocket:
            # 1. Handshake
            handshake_raw = await websocket.recv()
            assert json.loads(handshake_raw)["type"] == "hello"

            # 2. Look at initial room
            await websocket.send(json.dumps({
                "v": 1, "type": "input", "id": 1, "data": {"text": "look"}
            }))
            resp = json.loads(await websocket.recv())
            assert resp["type"] == "command_reply"
            assert len(resp["data"]["text"]) > 0

            # 3. Move to Marketplace
            await websocket.send(json.dumps({
                "v": 1, "type": "input", "id": 2, "data": {"text": "go north"}
            }))
            resp = json.loads(await websocket.recv())
            assert "marketplace" in resp["data"]["text"].lower()

            # 4. Pick up Gold Coin
            await websocket.send(json.dumps({
                "v": 1, "type": "input", "id": 3, "data": {"text": "take gold coin"}
            }))
            resp = json.loads(await websocket.recv())
            assert "picked up" in resp["data"]["text"].lower()
            
            # Assert internal engine state after networking calls
            assert any(item.name == "Gold Coin" for item in engine.player.contents)

            # 5. Drop the coin in the market
            await websocket.send(json.dumps({
                "v": 1, "type": "input", "id": 4, "data": {"text": "drop gold coin"}
            }))
            resp = json.loads(await websocket.recv())
            assert "dropped" in resp["data"]["text"].lower()

            # 6. Final checks: Player has nothing, Room has the coin
            assert len(engine.player.contents) == 0
            market_room = next(r for r in engine.rooms.values() if r.name == "Marketplace")
            assert any(i.name == "Gold Coin" for i in market_room.contents)

    finally:
        server.stop()
        await task
