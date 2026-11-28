"""Red-green: carry limit and target resolution (SPEC v2.1.2 criterion 4, #78).

Taking more than the actor's capacity is a typed `error{too_heavy}` and
changes nothing.  A target that does not resolve is `error{not_found}`; a name
that matches several visible objects is `error{ambiguous}` with a structured
`candidates[]` list.  None of these closes the socket.

The resolution rules are the documented ones: leading filler (`the`, `a`, `an`)
is ignored, an exact display name wins, then a case-insensitive prefix, then
the v2.0 substring match for compatibility.  Matches are sorted by display
name.

These tests are written before the behavior exists, so they fail now (RED).
"""

import asyncio
import json

import pytest
import websockets

from src.mud.content import Entity, Player, Room
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.protocol.adapter import execute
from src.mud.protocol.v2 import ERROR_CODES


def _engine(*items, capacity=2.0):
    """Ada in a market, with `items` on the floor and a small pack."""
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    engine._rooms[room.name] = room
    engine.start_room = room
    player = engine.register_player("Ada")
    player.capacity = capacity
    for item in items:
        room.add_content(item)
    return engine, player, room


def test_new_codes_are_in_the_shared_error_vocabulary():
    """The adapter's new codes are part of `ERROR_CODES`, not just the docs."""
    assert {"too_heavy", "ambiguous", "no_slot", "not_equipped",
            "still_lit", "no_skill", "no_ignition", "no_fuel"} <= ERROR_CODES
    # The v2.0 spelling stays reserved for clients that pinned an old server.
    assert "ambiguous_target" in ERROR_CODES


def _events(result, type_=None, target=None):
    return [
        event for event in result.events
        if (type_ is None or event.type == type_)
        and (target is None or event.target == target)
    ]


# --- carry limit -----------------------------------------------------------


@pytest.mark.asyncio
async def test_take_over_capacity_is_too_heavy_and_changes_nothing():
    """A boulder over the actor's capacity is refused before any mutation."""
    boulder = Entity("Boulder", "A granite boulder.", weight=5.0,
                     item_id="boulder")
    engine, player, room = _engine(boulder, capacity=2.0)

    result = await execute(engine, "Ada", "take boulder")

    assert len(result.events) == 1
    error = result.events[0]
    assert error.type == "error"
    assert error.target == "actor"
    assert error.data["code"] == "too_heavy"
    assert "too heavy" in error.data["message"].lower()
    # The item is still on the floor and the inventory is untouched.
    assert boulder in room.contents
    assert boulder not in player.contents
    # The refusal carries the numbers a client needs to explain it.
    assert error.data["capacity"] == 2.0
    assert error.data["used"] == 0.0
    assert error.data["object"]["id"] == "boulder"
    assert error.data["weight"] == 5.0


@pytest.mark.asyncio
async def test_too_heavy_reports_the_weight_already_carried():
    """`used` in the refusal is the current load, not the proposed one."""
    rock = Entity("Rock", "A small rock.", weight=1.5, item_id="rock")
    boulder = Entity("Boulder", "A granite boulder.", weight=1.0,
                     item_id="boulder")
    engine, player, room = _engine(rock, boulder, capacity=2.0)
    player.add_content(rock)
    room.remove_content(rock)

    result = await execute(engine, "Ada", "take boulder")

    error = result.events[0]
    assert error.data["code"] == "too_heavy"
    assert error.data["used"] == 1.5
    assert error.data["capacity"] == 2.0
    assert boulder in room.contents


@pytest.mark.asyncio
async def test_get_from_over_capacity_stays_in_the_container():
    """`get ... from` is a take: the same limit applies before it moves."""
    chest = Entity("Chest", "A sturdy chest.", item_id="chest",
                   container=True, open=True)
    anvil = Entity("Anvil", "A heavy anvil.", weight=5.0, item_id="anvil")
    chest.add_content(anvil)
    engine, player, room = _engine(chest, capacity=2.0)

    result = await execute(engine, "Ada", "get anvil from chest")

    error = result.events[0]
    assert error.type == "error"
    assert error.data["code"] == "too_heavy"
    assert error.data["object"]["id"] == "anvil"
    assert error.data["weight"] == 5.0
    assert anvil in chest.contents
    assert anvil not in player.contents
    assert chest in room.contents


@pytest.mark.asyncio
async def test_take_at_capacity_still_succeeds():
    """Exactly at the limit is allowed; the comparison is not strictly less."""
    rock = Entity("Rock", "A small rock.", weight=2.0, item_id="rock")
    engine, player, _room = _engine(rock, capacity=2.0)

    result = await execute(engine, "Ada", "take rock")

    assert _events(result, "object.taken", "actor")
    assert rock in player.contents


@pytest.mark.asyncio
async def test_inventory_reports_capacity_and_used():
    """Every `player.inventory` carries the same capacity/used fields."""
    coin = Entity("Gold Coin", "A coin.", weight=0.25, item_id="gold_coin")
    engine, player, _room = _engine(coin, capacity=3.0)

    empty = await execute(engine, "Ada", "inventory")
    payload = empty.events[0].data
    assert payload["capacity"] == 3.0
    assert payload["used"] == 0.0

    player.add_content(coin)
    full = await execute(engine, "Ada", "inventory")
    payload = full.events[0].data
    assert payload["capacity"] == 3.0
    assert payload["used"] == 0.25
    assert payload["items"] == [
        {"id": "gold_coin", "name": "Gold Coin", "weight": 0.25}]


# --- target resolution: not found -----------------------------------------


@pytest.mark.asyncio
async def test_unknown_take_is_typed_not_found():
    engine, player, room = _engine()

    result = await execute(engine, "Ada", "take dragon")

    assert len(result.events) == 1
    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "not_found"
    assert not player.contents
    assert [item for item in room.contents
            if not isinstance(item, Player)] == []


@pytest.mark.asyncio
async def test_unknown_drop_is_typed_not_found():
    engine, _player, _room = _engine()

    result = await execute(engine, "Ada", "drop dragon")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "not_found"


# --- target resolution: ambiguous -----------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_take_lists_candidates_and_changes_nothing():
    gold = Entity("Gold Key", "A gold key.", weight=0.1, item_id="gold_key")
    silver = Entity("Silver Key", "A silver key.", weight=0.1,
                    item_id="silver_key")
    engine, player, room = _engine(gold, silver, capacity=12.0)

    result = await execute(engine, "Ada", "take key")

    assert len(result.events) == 1
    error = result.events[0]
    assert error.type == "error"
    assert error.data["code"] == "ambiguous"
    assert "several" in error.data["message"].lower()
    assert [(candidate["id"], candidate["name"])
            for candidate in error.data["candidates"]] == [
        ("gold_key", "Gold Key"),
        ("silver_key", "Silver Key"),
    ]
    assert gold in room.contents and silver in room.contents
    assert not player.contents


@pytest.mark.asyncio
async def test_ambiguous_drop_lists_candidates_and_changes_nothing():
    gold = Entity("Gold Key", "A gold key.", weight=0.1, item_id="gold_key")
    silver = Entity("Silver Key", "A silver key.", weight=0.1,
                    item_id="silver_key")
    engine, player, room = _engine(capacity=12.0)
    player.add_content(gold)
    player.add_content(silver)

    result = await execute(engine, "Ada", "drop key")

    error = result.events[0]
    assert error.type == "error"
    assert error.data["code"] == "ambiguous"
    assert [candidate["name"] for candidate in error.data["candidates"]] == [
        "Gold Key", "Silver Key"]
    assert gold in player.contents and silver in player.contents
    assert [item for item in room.contents
            if not isinstance(item, Player)] == []


@pytest.mark.asyncio
async def test_examine_ambiguous_is_typed_with_candidates():
    """Ambiguity is a typed error for every resolving verb, not just take."""
    engine, _player, _room = _engine(
        Entity("Gold Key", "A gold key.", item_id="gold_key"),
        Entity("Silver Key", "A silver key.", item_id="silver_key"),
        capacity=12.0,
    )

    result = await execute(engine, "Ada", "examine key")

    assert result.events[0].type == "error"
    assert result.events[0].data["code"] == "ambiguous"
    assert len(result.events[0].data["candidates"]) == 2


# --- target resolution: exact, prefix, filler -----------------------------


@pytest.mark.asyncio
async def test_exact_name_beats_a_longer_name_that_contains_it():
    """`take gold coin` picks the coin, not the pouch whose name contains it."""
    coin = Entity("Gold Coin", "A coin.", weight=0.1, item_id="gold_coin")
    pouch = Entity("Gold Coin Pouch", "A pouch.", weight=0.1,
                   item_id="gold_coin_pouch")
    engine, player, room = _engine(coin, pouch, capacity=12.0)

    result = await execute(engine, "Ada", "take gold coin")

    assert _events(result, "object.taken", "actor")
    assert coin in player.contents
    assert pouch in room.contents


@pytest.mark.asyncio
async def test_unique_prefix_resolves_without_an_index():
    lantern = Entity("Brass Lantern", "A lantern.", weight=1.0,
                     item_id="brass_lantern")
    key = Entity("Brass Key", "A key.", weight=0.1, item_id="brass_key")
    engine, player, room = _engine(lantern, key, capacity=12.0)

    result = await execute(engine, "Ada", "take brass lant")

    assert _events(result, "object.taken", "actor")
    assert lantern in player.contents
    assert key in room.contents


@pytest.mark.asyncio
async def test_leading_filler_words_are_ignored():
    coin = Entity("Gold Coin", "A coin.", weight=0.1, item_id="gold_coin")
    engine, player, _room = _engine(coin, capacity=12.0)

    for command in ("take the gold coin", "take a gold coin",
                    "take an gold coin"):
        result = await execute(engine, "Ada", command)
        assert _events(result, "object.taken", "actor"), command
        assert coin in player.contents
        # Put it back for the next spelling.
        await execute(engine, "Ada", "drop gold coin")


# --------------------------------------------------------------------------
# The socket stays open after every command error
# --------------------------------------------------------------------------


class _Client:
    """A tiny v2 client: authenticate, send input, wait for one event type."""

    def __init__(self, uri):
        self.uri = uri
        self.ws = None
        self.queue = asyncio.Queue()
        self.reader = None

    async def connect(self):
        self.ws = await websockets.connect(self.uri)
        hello = await self.ws.recv()
        assert '"hello"' in hello
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for raw in self.ws:
                self.queue.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def auth(self, data, msg_id="auth"):
        await self.send({"v": 2, "type": "auth", "id": msg_id,
                         "data": data})

    async def register(self, name):
        await self.auth({"mode": "register", "name": name})
        await self.next("auth.ok")
        await self.next("room.state")

    async def input(self, text, msg_id):
        await self.send({"v": 2, "type": "input", "id": msg_id,
                         "data": {"text": text}})

    async def next(self, type_, timeout=3.0):
        while True:
            message = await asyncio.wait_for(self.queue.get(), timeout=timeout)
            if message.get("type") == type_:
                return message

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
        if self.ws is not None:
            await self.ws.close()


@pytest.fixture
async def target_server(tmp_path):
    engine = Engine()
    room = Room("Market", "A busy market.")
    room.item_id = "market"
    engine._rooms[room.name] = room
    engine.start_room = room
    room.add_content(Entity("Boulder", "A granite boulder.", weight=40.0,
                            item_id="boulder"))
    room.add_content(Entity("Gold Key", "A gold key.", weight=0.1,
                            item_id="gold_key"))
    room.add_content(Entity("Silver Key", "A silver key.", weight=0.1,
                            item_id="silver_key"))

    instance = V2Server(
        engine, port=0, db_path=tmp_path / "target-errors.db",
        auth_required=True)
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


@pytest.mark.asyncio
async def test_command_errors_never_drop_the_socket(target_server):
    instance, _engine = target_server
    client = await _Client(f"ws://localhost:{instance.port}").connect()
    try:
        await client.register("Ada")

        await client.input("take boulder", 2)
        heavy = await client.next("error")
        assert heavy["id"] == 2
        assert heavy["data"]["code"] == "too_heavy"
        assert heavy["data"]["capacity"] == 30.0
        assert heavy["data"]["used"] == 0.0

        await client.input("take key", 3)
        ambiguous = await client.next("error")
        assert ambiguous["data"]["code"] == "ambiguous"
        assert [candidate["id"] for candidate in
                ambiguous["data"]["candidates"]] == ["gold_key", "silver_key"]

        await client.input("take dragon", 4)
        missing = await client.next("error")
        assert missing["data"]["code"] == "not_found"

        # The same socket still answers a normal command after three errors.
        await client.input("look", 5)
        state = await client.next("room.state")
        assert state["id"] == 5
        assert client.ws.state is websockets.State.OPEN
    finally:
        await client.close()
