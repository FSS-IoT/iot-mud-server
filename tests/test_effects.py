"""Ticket #84: the effects bus and event-log seam.

SPEC v2.1.3 criterion 5: "Effects are parsed and emitted to the bus and event
log, but not sent to clients when `features.effects=false`; disabling effects
changes no gameplay."

The authored effects were validated by #65 and are part of the product world
(`data/worlds/club_world`: 7 room blocks, 2 object blocks, 3 NPC blocks). This
module names the behavior the loader, engine, storage, and server must provide:

* every authored block reaches the running room, object, or NPC;
* a triggered effect is published on the engine bus as one `effect` event;
* each `effect` event appends exactly one `event_log` row;
* `on_talk` (NPC), `on_enter` (room), and `on_open` (object) are driven by
  the existing command paths;
* `[features].effects` defaults to false, and false means bus/log only: no
  client receives an `effect` event;
* the payload is JSON-safe and identical on the bus and in the log;
* removing every authored effect changes no reply and no game state;
* `content check` still rejects a malformed effect, so there is no second
  schema at runtime.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import pytest
import websockets
import yaml

from src.mud.config import ConfigError, load_config
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.storage.db import open_database
from src.mud.storage.store import WriteThroughStore
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLD_DIR = REPO_ROOT / "data" / "worlds" / "club_world"

# The product world's authored effect owners (#80): 7 room, 2 object, 3 NPC.
AUTHORED_ROOM_EFFECTS = 7
AUTHORED_OBJECT_EFFECTS = 2
AUTHORED_NPC_EFFECTS = 3

MARKET_TALK_EFFECT = {
    "effect": {"type": "sound.play", "params": {"clip": "coins_clink"}},
    "trigger": "on_talk",
    "source": {
        "kind": "npc",
        "id": "npc_merchant",
        "name": "Mira the Merchant",
    },
    "room": {"id": "market", "name": "Marketplace"},
    "actor": {"name": "Ada"},
}

MARKET_ENTER_EFFECT = {
    "effect": {"type": "ambience.market", "params": {"volume": 0.4}},
    "trigger": "on_enter",
    "source": {"kind": "room", "id": "market", "name": "Marketplace"},
    "room": {"id": "market", "name": "Marketplace"},
    "actor": {"name": "Ada"},
}

FOREST_CHEST_EFFECT = {
    "effect": {"type": "sound.play", "params": {"clip": "iron_latch"}},
    "trigger": "on_open",
    "source": {"kind": "object", "id": "chest_forest", "name": "Chest"},
    "room": {"id": "forest", "name": "Mysterious Forest"},
    "actor": {"name": "Ada"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _engine() -> Engine:
    engine = Engine()
    await load_world(engine, WORLD_DIR)
    return engine


async def _stored_engine(tmp_path) -> tuple[Engine, object]:
    conn = open_database(tmp_path / "effects.db")
    engine = Engine(store=WriteThroughStore(conn))
    await load_world(engine, WORLD_DIR)
    return engine, conn


def _rows(conn) -> list:
    return conn.execute(
        "SELECT seq, event_id, type, actor, room_id, payload "
        "FROM event_log ORDER BY seq"
    ).fetchall()


def _walk(entity):
    yield entity
    for child in getattr(entity, "contents", []):
        yield from _walk(child)


def _strip_effects(engine: Engine) -> None:
    """Remove every authored effect, leaving the world otherwise identical."""
    for room in engine.rooms.values():
        room.effects = {}
        for entity in _walk(room):
            if getattr(entity, "effects", None):
                entity.effects = {}
        for npc in room.npcs:
            npc.effects = {}


def _snapshot(engine: Engine) -> dict:
    player = engine.get_player("Ada")
    return {
        "location": player.location.name if player.location else None,
        "inventory": sorted(item.name for item in player.contents),
        "rooms": {
            name: {
                "contents": sorted(item.name for item in room.contents),
                "npcs": sorted(npc.name for npc in room.npcs),
                "objects": {
                    item.name: {
                        "open": bool(getattr(item, "open", False)),
                        "locked": bool(getattr(item, "locked", False)),
                    }
                    for item in room.contents
                },
            }
            for name, room in sorted(engine.rooms.items())
        },
    }


def _write_config(tmp_path: Path, effects: bool) -> Path:
    path = tmp_path / "mud.toml"
    path.write_text(
        f"[features]\neffects = {'true' if effects else 'false'}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_features_effects_defaults_to_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.features.effects is False


def test_features_effects_is_read_from_mud_toml(tmp_path):
    path = _write_config(tmp_path, True)

    config = load_config(path)

    assert config.features.effects is True


@pytest.mark.parametrize("body", [
    "[features]\neffects = 1\n",
    '[features]\neffects = "true"\n',
    'features = "not a table"\n',
])
def test_features_reject_mistyped_values(tmp_path, body):
    path = tmp_path / "bad.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError, match="features"):
        load_config(path)


# ---------------------------------------------------------------------------
# The authored blocks reach the running world
# ---------------------------------------------------------------------------


async def test_authored_effect_blocks_reach_the_engine():
    engine = await _engine()

    rooms = objects = npcs = 0
    for room in engine.rooms.values():
        if room.effects:
            rooms += 1
        for entity in _walk(room):
            if entity is not room and getattr(entity, "effects", None):
                objects += 1
        for npc in room.npcs:
            if npc.effects:
                npcs += 1

    assert (rooms, objects, npcs) == (
        AUTHORED_ROOM_EFFECTS,
        AUTHORED_OBJECT_EFFECTS,
        AUTHORED_NPC_EFFECTS,
    )

    gate = engine.get_room("Village Gate")
    assert gate.effects["on_enter"][0].type == "sound.play"
    assert gate.effects["on_enter"][0].params == {"clip": "gate_wind"}


# ---------------------------------------------------------------------------
# The bus and the event log
# ---------------------------------------------------------------------------


async def test_npc_talk_effect_reaches_bus_and_event_log(tmp_path):
    engine, conn = await _stored_engine(tmp_path)
    events: list[dict] = []

    async def collect(event_type: str, data: dict) -> None:
        assert event_type == "effect"
        events.append(data)

    await engine.subscribe("effect", collect)
    engine.register_player("Ada")
    moved, _ = await engine.move_player("Ada", "north")
    assert moved
    before = len(_rows(conn))
    events.clear()

    reply = await engine.process_command("talk Mira", "Ada")

    assert reply.startswith('Mira the Merchant says, "')
    assert events == [MARKET_TALK_EFFECT]
    rows = _rows(conn)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["type"] == "effect"
    assert row["event_id"].startswith("effect-")
    assert row["actor"] == "Ada"
    assert row["room_id"] == "market"
    assert json.loads(row["payload"]) == MARKET_TALK_EFFECT


async def test_room_enter_effect_reaches_the_bus():
    engine = await _engine()
    events: list[dict] = []

    async def collect(event_type: str, data: dict) -> None:
        events.append(data)

    await engine.subscribe("effect", collect)
    engine.register_player("Ada")

    moved, _ = await engine.move_player("Ada", "north")

    assert moved
    assert events == [MARKET_ENTER_EFFECT]


async def test_object_open_effect_reaches_the_bus():
    engine = await _engine()
    events: list[dict] = []

    async def collect(event_type: str, data: dict) -> None:
        events.append(data)

    await engine.subscribe("effect", collect)
    engine.register_player("Ada")

    for direction in ("north", "west"):
        moved, result = await engine.move_player("Ada", direction)
        assert moved, result
    assert "You picked up" in await engine.process_command(
        "take gold key", "Ada")
    for direction in ("east", "north"):
        moved, result = await engine.move_player("Ada", direction)
        assert moved, result
    await engine.process_command("use gold key on chest", "Ada")
    events.clear()

    reply = await engine.process_command("open chest", "Ada")

    assert reply.startswith("You open the Chest.")
    assert events == [FOREST_CHEST_EFFECT]


async def test_effect_payload_is_json_safe():
    engine = await _engine()
    events: list[dict] = []

    async def collect(event_type: str, data: dict) -> None:
        events.append(data)

    await engine.subscribe("effect", collect)
    engine.register_player("Ada")
    await engine.move_player("Ada", "north")
    await engine.process_command("talk Mira", "Ada")

    assert events
    for payload in events:
        # Round-trips unchanged: no objects, dates, or tuples on the bus.
        assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# Disabling effects changes no gameplay
# ---------------------------------------------------------------------------


async def test_disabling_effects_changes_no_gameplay():
    with_effects = await _engine()
    without_effects = await _engine()
    _strip_effects(without_effects)
    with_events: list[dict] = []
    without_events: list[dict] = []

    async def collect_with(event_type: str, data: dict) -> None:
        with_events.append(data)

    async def collect_without(event_type: str, data: dict) -> None:
        without_events.append(data)

    await with_effects.subscribe("effect", collect_with)
    await without_effects.subscribe("effect", collect_without)
    with_effects.register_player("Ada")
    without_effects.register_player("Ada")

    commands = [
        "look",
        "go north",
        "talk Mira",
        "ask Mira about gold",
        "go west",
        "take gold key",
        "go east",
        "go north",
        "use gold key on chest",
        "open chest",
        "get brass lantern from chest",
        "inventory",
    ]
    replies_with = [
        await with_effects.process_command(command, "Ada")
        for command in commands
    ]
    replies_without = [
        await without_effects.process_command(command, "Ada")
        for command in commands
    ]

    assert with_events
    assert without_events == []
    assert replies_with == replies_without
    assert _snapshot(with_effects) == _snapshot(without_effects)


# ---------------------------------------------------------------------------
# Client delivery is gated by features.effects
# ---------------------------------------------------------------------------


class Client:
    """A tiny v2 guest client with a background reader and typed drain."""

    def __init__(self, uri: str):
        self.uri = uri
        self.ws = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def connect(self) -> Self:
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._reader())
        return self

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def join(self, name: str) -> None:
        await self.send({
            "v": 2, "type": "join", "id": "join",
            "data": {"name": name},
        })
        await self.next("session.ok")
        await self.next("room.state")

    async def input(self, text: str, msg_id=1) -> None:
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def next(self, type_: str, timeout: float = 5.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout)
            if message.get("type") == type_:
                return message

    async def absent(self, type_: str, timeout: float = 0.3) -> bool:
        """True when no `type_` message arrives before the timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return True
            try:
                message = await asyncio.wait_for(self.q.get(), remaining)
            except TimeoutError:
                return True
            if message.get("type") == type_:
                return False

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


@asynccontextmanager
async def _v2_server(tmp_path: Path, name: str, *, effects: bool):
    config = load_config(_write_config(tmp_path, effects))
    engine = await _engine()
    server = V2Server(
        engine,
        port=0,
        db_path=tmp_path / name,
        auth_required=False,
        features=config.features,
        # Keep the 1 Hz NPC tick out of a test that never waits a second.
        tick_seconds=3600.0,
    )
    task = asyncio.create_task(server.run())
    for _ in range(200):
        if server.port:
            break
        await asyncio.sleep(0.01)
    assert server.port, "v2 server did not bind"
    client = await Client(f"ws://localhost:{server.port}").connect()
    try:
        yield server, client, tmp_path / name
    finally:
        await client.close()
        server.stop()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except TimeoutError:
            task.cancel()


async def test_disabled_effects_are_bus_and_log_only(tmp_path):
    async with _v2_server(tmp_path, "off.db", effects=False) as (
            _server, client, db_path):
        await client.join("Ada")
        await client.input("go north", 1)
        state = await client.next("room.state")
        assert state["data"]["room"]["id"] == "market"

        await client.input("talk Mira", 2)
        said = await client.next("npc.say")
        assert said["data"]["npc"]["id"] == "npc_merchant"
        assert await client.absent("effect")

    conn = open_database(db_path)
    try:
        rows = _rows(conn)
    finally:
        conn.close()
    talk_rows = [
        row for row in rows
        if json.loads(row["payload"]).get("trigger") == "on_talk"
    ]
    assert len(talk_rows) == 1
    assert json.loads(talk_rows[0]["payload"]) == MARKET_TALK_EFFECT
    assert any(
        json.loads(row["payload"]).get("trigger") == "on_enter"
        for row in rows
    )


async def test_enabled_effects_reach_the_room(tmp_path):
    async with _v2_server(tmp_path, "on.db", effects=True) as (
            _server, client, _db_path):
        await client.join("Ada")
        await client.input("go north", 1)
        await client.next("room.state")

        await client.input("talk Mira", 2)
        effect = await client.next("effect")
        said = await client.next("npc.say")

    assert effect["v"] == 2
    assert effect["data"] == MARKET_TALK_EFFECT
    assert said["data"]["npc"]["id"] == "npc_merchant"


# ---------------------------------------------------------------------------
# Content check still owns the effect schema
# ---------------------------------------------------------------------------


def test_malformed_effect_is_rejected_by_content_check(tmp_path):
    path = tmp_path / "world.yaml"
    path.write_text(yaml.safe_dump({
        "name": "Test World",
        "start_room": "gate",
        "rng_seed": 7,
        "rooms": [{
            "id": "gate",
            "name": "Gate",
            "description": "The gate.",
            "effects": {"on_enter": [{"type": "Light Dim"}]},
        }],
    }), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable, "-m", "src.mud.admin", "content", "check",
            "--world", str(path),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "'gate'" in output
    assert "effects" in output
    assert "type" in output
