"""Ticket #75: `who`, `emote`, and `tell` (SPEC v2.1.2 criterion 1).

* `who` is the server-wide roster, including the requester and characters that
  are asleep/stone, keeping the #72 `{name, room_id, asleep, stone}` shape.
* `emote` is room-local: the actor and everyone in its room see `player.emoted`;
  nobody outside the room does.
* `tell <player> <text>` is private to the sender and an awake target. An
  asleep/stone target is a typed `not_awake` error, and nothing is queued for
  it: `tell` never becomes offline mail.

Three real websocket clients drive the auth-required server against a temporary
database on an ephemeral port. The tests are the acceptance evidence for the
comment in `docs/PROTOCOL.md` and the social steps in `docs/SESSION.md`.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_world

WORLD_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "worlds" / "starting_village.yaml"
)


class Client:
    """A tiny v2 client with a background reader and a type-filtered drain."""

    def __init__(self, uri: str):
        self.uri = uri
        self.ws = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.hello = None

    async def connect(self) -> Self:
        self.ws = await websockets.connect(self.uri)
        self.hello = json.loads(await self.ws.recv())
        self.task = asyncio.create_task(self._reader())
        return self

    async def __aenter__(self) -> Self:
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                self.q.put_nowait(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, message: dict) -> None:
        await self.ws.send(json.dumps(message))

    async def register(self, name: str, msg_id="register") -> str:
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "register", "name": name},
        })
        ok = await self.next("auth.ok", msg_id=msg_id)
        await self.next("room.state")
        return ok["data"]["key"]

    async def login(self, key: str, msg_id="login"):
        await self.send({
            "v": 2, "type": "auth", "id": msg_id,
            "data": {"mode": "login", "key": key},
        })
        ok = await self.next("auth.ok", msg_id=msg_id)
        state = await self.next("room.state")
        return ok, state

    async def input(self, text: str, msg_id=1) -> None:
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def next(self, type_: str, *, msg_id=None, timeout: float = 5.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout)
            if message.get("type") != type_:
                continue
            if msg_id is not None and message.get("id") != msg_id:
                continue
            return message

    async def next_any(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.q.get(), timeout)

    async def drain(self, timeout: float = 0.2) -> list[dict]:
        """Read everything queued, stopping after `timeout` of silence."""
        seen: list[dict] = []
        while True:
            try:
                seen.append(await self.next_any(timeout))
            except TimeoutError:
                return seen

    async def assert_silent(self, timeout: float = 0.25) -> None:
        with pytest.raises(TimeoutError):
            await self.next_any(timeout)

    async def who(self, msg_id="who") -> dict:
        await self.input("who", msg_id)
        return await self.next("who.list", msg_id=msg_id)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


async def _wait_for_bind(server: V2Server) -> None:
    for _ in range(200):
        if server.port:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("v2 server did not bind")


async def _start(db_path):
    engine = Engine()
    await load_world(engine, WORLD_PATH)
    instance = V2Server(
        engine, port=0, db_path=db_path, auth_required=True)
    task = asyncio.create_task(instance.run())
    await _wait_for_bind(instance)
    return instance, engine, task


async def _stop(instance: V2Server, task) -> None:
    instance.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        task.cancel()


@asynccontextmanager
async def auth_server(db_path):
    instance, engine, task = await _start(db_path)
    try:
        yield instance, engine
    finally:
        await _stop(instance, task)


@pytest.mark.asyncio
async def test_who_emote_and_tell_across_rooms_and_sleep(tmp_path):
    db_path = tmp_path / "social.db"

    async with auth_server(db_path) as (instance, engine):
        uri = f"ws://localhost:{instance.port}"

        ada = await Client(uri).connect()
        key_cara = None
        ben = await Client(uri).connect()
        cara = await Client(uri).connect()
        try:
            await ada.register("Ada", "c-1")
            await ben.register("Ben", "c-2")
            key_cara = await cara.register("Cara", "c-3")
            for client in (ada, ben, cara):
                await client.drain()

            # `who` is server-wide and includes the requester, awake.
            who = await ada.who("who-1")
            players = {entry["name"]: entry for entry in who["data"]["players"]}
            assert players["Ada"] == {
                "name": "Ada", "room_id": "gate",
                "asleep": False, "stone": False,
            }
            assert set(players) >= {"Ada", "Ben", "Cara"}
            assert "Ada (gate, awake)" in who["data"]["text"]
            await ben.assert_silent()

            # Ben steps out of the room so only Cara shares it with Ada.
            await ben.input("go north", 2)
            await ben.next("room.state", msg_id=2)
            await ada.drain()
            await cara.drain()

            # `emote` is room-local: Ada and Cara see it, Ben does not.
            await ada.input("emote waves at the sky", 3)
            own = await ada.next("player.emoted", msg_id=3)
            seen = await cara.next("player.emoted")
            assert own["data"] == {
                "from": "Ada", "text": "Ada waves at the sky.", "self": True,
            }
            assert seen["data"] == {
                "from": "Ada", "text": "Ada waves at the sky.", "self": False,
            }
            await ben.assert_silent()

            # Ben is in the Marketplace: `tell` is server-wide, so it reaches
            # him privately. Cara shares Ada's room and sees nothing.
            await ada.input("tell Ben hello there", 5)
            told = await ada.next("player.told", msg_id=5)
            heard = await ben.next("player.told")
            assert told["data"] == {
                "from": "Ada", "to": "Ben",
                "text": 'You tell Ben, "hello there".', "self": True,
            }
            assert heard["data"] == {
                "from": "Ada", "to": "Ben",
                "text": 'Ada tells you, "hello there".', "self": False,
            }
            await cara.assert_silent()

            # An unknown character is `not_found`; nobody hears it.
            await ada.input("tell Nobody hello", 6)
            error = await ada.next("error", msg_id=6)
            assert error["data"]["code"] == "not_found"
            await ben.assert_silent()
            await cara.assert_silent()

            # Ben comes back to the gate, where the sleep broadcast happens.
            await ben.input("go south", 7)
            await ben.next("room.state", msg_id=7)
            await ada.drain()
            await cara.drain()

            # Cara disconnects: she sleeps in place (#72), still on the roster.
            await cara.close()
            left = await ada.next("room.leave")
            assert left["data"]["actor"]["name"] == "Cara"
            assert left["data"]["asleep"] is True
            assert left["data"]["stone"] is True
            await ben.next("room.leave")
            await ada.drain()
            await ben.drain()

            who = await ada.who("who-2")
            players = {entry["name"]: entry for entry in who["data"]["players"]}
            assert players["Cara"] == {
                "name": "Cara", "room_id": "gate",
                "asleep": True, "stone": True,
            }
            assert players["Ada"]["asleep"] is False
            assert "asleep" in who["data"]["text"].lower()
            assert engine.get_player("Cara").asleep is True

            # `tell` to an asleep/stone player is a typed error. It is not
            # queued: neither Ada nor Ben sees a `player.told`, and Cara cannot
            # receive one later.
            await ada.input("tell Cara are you there", 7)
            error = await ada.next("error", msg_id=7)
            assert error["data"]["code"] == "not_awake"
            assert "asleep" in error["data"]["message"].lower()
            await ada.assert_silent()
            await ben.assert_silent()

            # Login wakes Cara in place. No offline tell is replayed: she gets
            # her auth and room state, then silence.
            cara = await Client(uri).connect()
            await cara.login(key_cara, "c-4")
            await ada.next("room.enter")
            queued = await cara.drain()
            assert [message["type"] for message in queued] == []
            await cara.assert_silent()

            who = await ada.who("who-3")
            players = {entry["name"]: entry for entry in who["data"]["players"]}
            assert players["Cara"]["asleep"] is False
            assert players["Cara"]["stone"] is False
        finally:
            await ada.close()
            await ben.close()
            await cara.close()
