"""Contract test for the documented wire protocol (docs/PROTOCOL.md).

A scripted client drives the server end-to-end without any human in the loop.
This is what lets club members write independent clients against the contract,
and it is what issue #21's definition of done requires: the scripted client
completes handshake + input round-trip and the replies pair correctly.

Red-green: this test encodes the protocol as documented, so it must pass before
#21 closes.
"""

import asyncio
import json

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.server import MudServer
from src.mud.world.loader import load_demo_village


@pytest.fixture
async def running_server():
    """Start a server backed by the starting-village world, stop it after."""
    engine = Engine()
    await load_demo_village(engine)
    server = MudServer(engine, port=8766)
    task = asyncio.create_task(server.run())

    # Give the server a moment to bind before any client connects.
    await asyncio.sleep(0.3)

    yield server, task

    server.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
    finally:
        if not task.done():
            task.cancel()


async def _read_handshake(websocket):
    """Read exactly one message and return the parsed JSON."""
    raw = await websocket.recv()
    return json.loads(raw)


@pytest.mark.asyncio
async def test_handshake_shape(running_server):
    """A client receives a well-formed `hello` handshake on connect.

    Mirrors docs/PROTOCOL.md: version 1, type hello, data.server present,
    protocol list present, seq is an integer.
    """
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    async with websockets.connect(uri) as websocket:
        handshake = await _read_handshake(websocket)

    assert handshake["v"] == 1
    assert handshake["type"] == "hello"
    assert isinstance(handshake["seq"], int)
    assert handshake["data"]["server"] == "iot-mud-dev"
    assert isinstance(handshake["data"]["protocol"], list)


@pytest.mark.asyncio
async def test_input_reply_pairs(running_server):
    """A client `input` returns a `command_reply` whose seq matches its id.

    This is the request/reply pairing invariant in docs/PROTOCOL.md: the reply
    echoes the request id so a client can correlate responses.
    """
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    async with websockets.connect(uri) as websocket:
        await _read_handshake(websocket)

        payload = {"v": 1, "type": "input", "id": 7, "data": {"text": "look"}}
        await websocket.send(json.dumps(payload))

        raw = await websocket.recv()
        reply = json.loads(raw)

    assert reply["type"] == "command_reply"
    assert reply["seq"] == payload["id"]
    assert isinstance(reply["data"]["text"], str)
    assert reply["data"]["text"]  # non-empty


@pytest.mark.asyncio
async def test_scripted_full_session(running_server):
    """Handshake -> look -> move -> take -> drop, all scripted.

    The world starts the player at the Village Gate; the Gold Coin sits in the
    Marketplace one step north. So a real session is: look, go north, take the
    coin, then drop it. Each command returns a reply whose seq matches its id,
    and the take/drop succeed (not the "no such item" text) — proving the
    protocol carries an actual inventory round-trip end-to-end with no human in
    the loop, which is the bar for a client authoring against this contract.
    """
    server, _ = running_server
    uri = f"ws://localhost:{server.port}"

    async with websockets.connect(uri) as websocket:
        await _read_handshake(websocket)

        commands = ["look", "go north", "take gold coin", "drop gold coin"]
        results = []
        for command in commands:
            payload = {"v": 1, "type": "input", "id": len(results), "data": {"text": command}}
            await websocket.send(json.dumps(payload))
            raw = await websocket.recv()
            reply = json.loads(raw)
            assert reply["seq"] == payload["id"]
            results.append(reply["data"]["text"])

    # Every response is a string.
    assert all(isinstance(r, str) for r in results)
    # The inventory round-trip actually happened (not "no such item").
    assert "picked up" in results[2].lower()
    assert "dropped" in results[3].lower()
