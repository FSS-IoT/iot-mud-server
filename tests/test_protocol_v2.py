"""Protocol-v2 conformance session.

Two real websocket clients drive the v2 server through the accepted session:
handshake, join, room state, movement broadcasts, take/drop, chat, malformed
input, a duplicate name, and a clean quit. This is the gate the common server
and every member client are measured against.
"""

import asyncio
import json

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_demo_village


class V2Client:
    """A tiny v2 client with a background reader and type-filtered drain."""

    def __init__(self, uri):
        self.uri = uri
        self.ws = None
        self.q = asyncio.Queue()
        self.task = None
        self.hello = None

    async def connect(self):
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._reader())
        return self

    async def _reader(self):
        try:
            async for raw in self.ws:
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def join(self, name):
        await self.send({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": name},
        })
        ok = await self.next("session.ok")
        state = await self.next("room.state")
        return ok, state

    async def input(self, text, msg_id=1):
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def next(self, type_, timeout=3.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout=timeout)
            if message.get("type") == type_:
                return message

    async def next_any(self, timeout=3.0):
        return await asyncio.wait_for(self.q.get(), timeout=timeout)

    async def close(self):
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


@pytest.fixture
async def server():
    engine = Engine()
    await load_demo_village(engine)
    instance = V2Server(engine, port=0)
    task = asyncio.create_task(instance.run())
    for _ in range(200):
        if instance.port:
            break
        await asyncio.sleep(0.01)
    assert instance.port, "v2 server did not bind"
    try:
        yield instance, engine
    finally:
        instance.stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()


async def _two_clients(server):
    """Connect Ada and Ben; drain the one cross-join event Ada receives."""
    uri = f"ws://localhost:{server.port}"
    ada = await V2Client(uri).connect()
    ben = await V2Client(uri).connect()
    await ada.join("Ada")
    await ben.join("Ben")
    # Ben's room_state already lists Ada; only the existing player is notified.
    await ada.next("room.enter")  # Ben arrived
    return ada, ben


@pytest.mark.asyncio
async def test_hello_and_join(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        assert ada.hello["v"] == 2
        assert ada.hello["type"] == "hello"
        assert ada.hello["data"]["server"] == "iot-mud-server"
        assert ada.hello["data"]["protocol"] == [2]
        assert ada.hello["data"]["auth_required"] is False
        assert ada.hello["data"]["text"] == "Welcome to the IoT MUD!"
        assert ada.hello["data"]["world"]["epoch"]

        ok, state = await ada.join("Ada")

        assert ok["data"]["player"]["name"] == "Ada"
        assert ok["data"]["mode"] == "guest"
        assert ok["id"] == "join"
        room = state["data"]["room"]
        assert room["id"] == "gate"
        assert room["name"] == "Village Gate"
        assert room["dark"] is False
        assert any(obj["id"] == "sign" for obj in room["objects"])
        assert room["exits"] == [
            {"direction": "north", "to": "market", "locked": False}
        ]


@pytest.mark.asyncio
async def test_move_broadcasts_leave_and_enter(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")

        await ada.input("go north", 2)
        state = await ada.next("room.state")
        assert state["id"] == 2
        assert state["data"]["room"]["id"] == "market"

        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["room"]["id"] == "gate"
        assert left["data"]["reason"] == "move"

        await ben.input("go north", 2)
        state = await ben.next("room.state")
        assert state["data"]["room"]["id"] == "market"

        entered = await ada.next("room.enter")
        assert entered["data"]["actor"]["name"] == "Ben"
        assert entered["data"]["reason"] == "move"


@pytest.mark.asyncio
async def test_take_drop_and_inventory(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")
        await ada.input("go north", 1)
        await ada.next("room.state")
        await ben.input("go north", 1)
        await ben.next("room.state")
        await ada.next("room.enter")

        await ada.input("take gold coin", 2)
        taken = await ada.next("object.taken")
        inventory = await ada.next("player.inventory")
        other = await ben.next("object.taken")

        assert taken["id"] == 2
        assert taken["data"]["self"] is True
        assert taken["data"]["object"]["id"] == "gold_coin"
        assert inventory["data"]["items"][0]["id"] == "gold_coin"
        assert inventory.get("id") is None
        assert other["data"]["self"] is False
        assert "Ada picks up" in other["data"]["text"]

        await ada.input("drop gold coin", 3)
        dropped = await ada.next("object.dropped")
        empty = await ada.next("player.inventory")
        other_drop = await ben.next("object.dropped")

        assert dropped["data"]["self"] is True
        assert empty["data"]["items"] == []
        assert other_drop["data"]["self"] is False


@pytest.mark.asyncio
async def test_room_state_lists_other_players(server):
    """After both join, a look shows the other player in room.players."""
    instance, _ = server
    ada, ben = await _two_clients(instance)
    try:
        await ada.input("look", 2)
        state = await ada.next("room.state")
        assert state["data"]["room"]["players"] == [{"name": "Ben"}]
    finally:
        await ada.close()
        await ben.close()


@pytest.mark.asyncio
async def test_player_is_not_takeable(server):
    """Ben cannot pick up Ada or move her out of the room."""
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")

        await ben.input("take Ada", 2)
        error = await ben.next("error")
        assert error["data"]["code"] == "not_found"

        await ben.input("look", 3)
        state = await ben.next("room.state")
        assert {"name": "Ada"} in state["data"]["room"]["players"]

        await ben.input("inventory", 4)
        inventory = await ben.next("player.inventory")
        assert inventory["data"]["items"] == []


@pytest.mark.asyncio
async def test_say_reaches_roommates(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")

        await ada.input("say hello", 2)
        own = await ada.next("player.said")
        heard = await ben.next("player.said")

        assert own["data"]["self"] is True
        assert heard["data"]["self"] is False
        assert heard["data"]["from"] == "Ada"
        assert heard["data"]["text"] == 'Ada says, "hello".'


@pytest.mark.asyncio
async def test_malformed_message_keeps_the_socket(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.ws.send("this is not json")
        error = await ada.next("error")
        assert error["data"]["code"] == "malformed_message"

        await ada.input("look", 2)
        state = await ada.next("room.state")
        assert state["id"] == 2
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_unknown_command_keeps_the_socket(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.input("dance", 2)
        error = await ada.next("error")
        assert error["id"] == 2
        assert error["data"]["code"] == "unknown_command"

        await ada.input("look", 3)
        state = await ada.next("room.state")
        assert state["id"] == 3


@pytest.mark.parametrize("command", ["quit", "exit"])
@pytest.mark.asyncio
async def test_input_quit_closes_1000(server, command):
    """An `input quit`/`exit` closes cleanly and tells the room."""
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")

        await ada.input(command, 2)
        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["reason"] == "disconnect"

        await asyncio.wait_for(ada.ws.wait_closed(), 2.0)
        assert ada.ws.close_code == 1000


@pytest.mark.asyncio
async def test_missing_version_is_malformed_and_keeps_socket(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.send({"type": "input", "id": 2,
                        "data": {"text": "look"}})
        error = await ada.next("error")
        assert error["data"]["code"] == "malformed_message"

        await ada.input("look", 3)
        state = await ada.next("room.state")
        assert state["id"] == 3


@pytest.mark.asyncio
async def test_unsupported_version_closes_4003(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.send({"v": 1, "type": "join", "id": "join",
                        "data": {"name": "Ada"}})
        error = await ada.next("error")
        assert error["data"]["code"] == "unsupported_version"

        await asyncio.wait_for(ada.ws.wait_closed(), 2.0)
        assert ada.ws.close_code == 4003


@pytest.mark.asyncio
async def test_join_with_non_object_data_is_rejected(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.send({"v": 2, "type": "join", "id": "join",
                        "data": "Ada"})
        error = await ada.next("error")
        assert error["data"]["code"] == "malformed_message"

        ok, state = await ada.join("Ada")
        assert ok["data"]["player"]["name"] == "Ada"
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_input_with_non_object_data_is_rejected(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.send({"v": 2, "type": "input", "id": 2,
                        "data": "look"})
        error = await ada.next("error")
        assert error["data"]["code"] == "malformed_message"

        await ada.input("look", 3)
        state = await ada.next("room.state")
        assert state["id"] == 3


@pytest.mark.asyncio
async def test_input_without_id_is_rejected(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.send({"v": 2, "type": "input",
                        "data": {"text": "look"}})
        error = await ada.next("error")
        assert error["data"]["code"] == "bad_args"

        await ada.input("look", 2)
        state = await ada.next("room.state")
        assert state["id"] == 2


@pytest.mark.asyncio
async def test_read_emits_readable_content(server):
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.input("read sign", 2)
        event = await ada.next("object.read")
        assert event["data"]["object"]["id"] == "sign"
        assert "wooden signpost" in event["data"]["content"].lower()


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_and_can_retry(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")

        await ben.send({"v": 2, "type": "join", "id": "join",
                        "data": {"name": "Ada"}})
        error = await ben.next("error")
        assert error["data"]["code"] == "name_taken"

        ok, state = await ben.join("Ben")
        assert ok["data"]["player"]["name"] == "Ben"
        assert state["data"]["room"]["id"] == "gate"


@pytest.mark.asyncio
async def test_quit_notifies_the_room(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada, \
            V2Client(f"ws://localhost:{instance.port}") as ben:
        await ada.join("Ada")
        await ben.join("Ben")
        await ada.next("room.enter")

        await ada.send({"v": 2, "type": "quit"})
        left = await ben.next("room.leave")
        assert left["data"]["actor"]["name"] == "Ada"
        assert left["data"]["reason"] == "disconnect"


@pytest.mark.asyncio
async def test_dark_room_state_hides_contents(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")

        await ada.input("go north", 2)
        market = await ada.next("room.state")
        assert market["data"]["room"]["id"] == "market"

        await ada.input("go north", 3)
        forest = await ada.next("room.state")
        room = forest["data"]["room"]
        assert room["id"] == "forest"
        assert room["dark"] is True
        assert room["objects"] == []
        assert room["players"] == []
        assert "dark" in forest["data"]["text"].lower()


@pytest.mark.asyncio
async def test_container_sequence_over_websocket(server):
    """The v2.0 lock/open/take/put path works end-to-end over the socket."""
    instance, _ = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")
        await ada.input("go north", 2)
        await ada.next("room.state")
        await ada.input("go west", 3)
        await ada.next("room.state")
        await ada.input("take torch", 4)
        await ada.next("object.taken")
        await ada.input("take gold key", 5)
        await ada.next("object.taken")
        await ada.input("go east", 6)
        await ada.next("room.state")
        await ada.input("go north", 7)
        await ada.next("room.state")

        await ada.input("open chest", 8)
        locked = await ada.next("error")
        assert locked["data"]["code"] == "locked"

        await ada.input("use gold key on chest", 9)
        unlocked = await ada.next("object.unlocked")
        assert unlocked["data"]["self"] is True

        await ada.input("open chest", 10)
        opened = await ada.next("object.opened")
        assert "brass lantern" in opened["data"]["text"].lower()

        await ada.input("take brass lantern", 11)
        await ada.next("object.taken")

        await ada.input("put brass lantern in chest", 12)
        put = await ada.next("object.put")
        assert put["data"]["container"]["id"] == "chest_forest"
        assert put["data"]["self"] is True


@pytest.mark.asyncio
async def test_exits_uses_the_players_current_room(server):
    instance, _engine = server
    async with V2Client(f"ws://localhost:{instance.port}") as ada:
        await ada.join("Ada")
        await ada.input("go north", 2)
        await ada.next("room.state")

        await ada.input("exits", 3)
        notice = await ada.next("notice")
        assert "South" in notice["data"]["text"]
        assert "West" in notice["data"]["text"]
