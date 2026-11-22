"""Ticket #37: the protocol-only mock server speaks the accepted session."""

import asyncio
import json

import pytest
import websockets

from tools.mock_v2_server import MockServer


async def _next(ws, type_):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        if message.get("type") == type_:
            return message


async def _send(ws, type_, data=None, msg_id=None):
    message = {"v": 2, "type": type_}
    if msg_id is not None:
        message["id"] = msg_id
    if data is not None:
        message["data"] = data
    await ws.send(json.dumps(message))


@pytest.mark.asyncio
async def test_mock_two_players_see_and_hear():
    mock = MockServer()
    server = await websockets.serve(mock.handler, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://localhost:{port}") as ada, \
                websockets.connect(f"ws://localhost:{port}") as ben:
            await ada.recv()
            await ben.recv()

            await _send(ada, "join", {"name": "Ada"}, "join")
            await _next(ada, "session.ok")
            await _next(ada, "room.state")

            await _send(ben, "join", {"name": "Ben"}, "join")
            await _next(ben, "session.ok")
            ben_state = await _next(ben, "room.state")
            assert {"name": "Ada"} in ben_state["data"]["room"]["players"]

            entered = await _next(ada, "room.enter")
            assert entered["data"]["actor"]["name"] == "Ben"

            await _send(ada, "input", {"text": "look"}, 1)
            state = await _next(ada, "room.state")
            assert {"name": "Ben"} in state["data"]["room"]["players"]

            await _send(ada, "input", {"text": "say hello"}, 2)
            own = await _next(ada, "player.said")
            heard = await _next(ben, "player.said")
            assert own["data"]["self"] is True
            assert heard["data"]["self"] is False
            assert heard["data"]["from"] == "Ada"

            await _send(ada, "input", {"text": "go north"}, 3)
            await _next(ada, "room.state")
            left = await _next(ben, "room.leave")
            assert left["data"]["actor"]["name"] == "Ada"

            await _send(ben, "input", {"text": "go north"}, 1)
            await _next(ben, "room.state")
            joined = await _next(ada, "room.enter")
            assert joined["data"]["actor"]["name"] == "Ben"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_mock_runs_the_session_and_container_flow():
    mock = MockServer()
    server = await websockets.serve(mock.handler, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(f"ws://localhost:{port}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"
            assert hello["v"] == 2
            assert hello["data"]["protocol"] == [2]

            await _send(ws, "join", {"name": "Ada"}, "join")
            assert (await _next(ws, "session.ok"))["data"]["player"]["name"] == "Ada"
            state = await _next(ws, "room.state")
            assert state["data"]["room"]["id"] == "gate"

            await _send(ws, "input", {"text": "go north"}, 1)
            assert (await _next(ws, "room.state"))["data"]["room"]["id"] == "market"
            await _send(ws, "input", {"text": "go west"}, 2)
            assert (await _next(ws, "room.state"))["data"]["room"]["id"] == "shop"

            await _send(ws, "input", {"text": "take torch"}, 3)
            assert (await _next(ws, "object.taken"))["data"]["object"]["name"] == "Torch"
            # `take` has no inventory follow-up; the explicit request answers
            # as the very next frame on this connection.
            await _send(ws, "input", {"text": "inventory"}, 4)
            inventory = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert inventory["type"] == "player.inventory"
            assert inventory["id"] == 4
            assert [item["name"] for item in inventory["data"]["items"]] == [
                "Torch"]
            await _send(ws, "input", {"text": "take gold key"}, 5)
            assert (await _next(ws, "object.taken"))["data"]["object"]["name"] == "Gold Key"

            await _send(ws, "input", {"text": "light torch"}, 6)
            lit = await _next(ws, "object.lit")
            assert lit["data"]["object"]["lit"] is True

            await _send(ws, "input", {"text": "go east"}, 7)
            await _next(ws, "room.state")
            await _send(ws, "input", {"text": "go north"}, 8)
            forest = await _next(ws, "room.state")
            assert forest["data"]["room"]["id"] == "forest"
            assert forest["data"]["room"]["dark"] is False

            await _send(ws, "input", {"text": "open chest"}, 9)
            assert (await _next(ws, "error"))["data"]["code"] == "locked"

            await _send(ws, "input", {"text": "use gold key on chest"}, 10)
            assert (await _next(ws, "object.unlocked"))["data"]["self"] is True

            await _send(ws, "input", {"text": "open chest"}, 11)
            opened = await _next(ws, "object.opened")
            assert "brass lantern" in opened["data"]["text"].lower()

            await _send(ws, "input", {"text": "get brass lantern from chest"}, 12)
            got = await _next(ws, "object.got")
            assert got["data"]["container"]["id"] == "chest_forest"

            await _send(ws, "input", {"text": "put brass lantern in chest"}, 13)
            put = await _next(ws, "object.put")
            assert put["data"]["container"]["id"] == "chest_forest"
            # `put` keeps the documented inventory push; hold it so the drop
            # assertion below starts from a known frame.
            put_inventory = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert put_inventory["type"] == "player.inventory"
            assert [item["name"] for item in put_inventory["data"]["items"]] == [
                "Gold Key", "Torch"]

            await _send(ws, "input", {"text": "drop gold key"}, 14)
            dropped = await _next(ws, "object.dropped")
            assert dropped["data"]["self"] is True
            assert dropped["data"]["object"]["id"] == "gold_key"
            # `drop` has no inventory follow-up; the explicit request answers
            # as the very next frame on this connection.
            await _send(ws, "input", {"text": "inventory"}, 15)
            inventory = json.loads(
                await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert inventory["type"] == "player.inventory"
            assert inventory["id"] == 15
            assert [item["name"] for item in inventory["data"]["items"]] == [
                "Torch"]

            await _send(ws, "input", {"text": "say hello"}, 16)
            said = await _next(ws, "player.said")
            assert said["data"]["self"] is True
            assert said["data"]["from"] == "Ada"

            await _send(ws, "quit")
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
            assert ws.close_code == 1000
    finally:
        server.close()
        await server.wait_closed()
