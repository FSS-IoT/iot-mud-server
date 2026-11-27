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

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.mud.engine.core import Engine
from src.mud.world.loader import load_world
from tests.golden.harness import server_harness

WORLD_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "worlds" / "starting_village.yaml"
)


@asynccontextmanager
async def auth_server(db_path):
    engine = Engine()
    await load_world(engine, WORLD_PATH)
    async with server_harness(
        engine, db_path=db_path, auth_required=True,
    ) as server:
        yield server


@pytest.mark.asyncio
async def test_who_emote_and_tell_across_rooms_and_sleep(tmp_path):
    db_path = tmp_path / "social.db"

    async with auth_server(db_path) as server:
        ada = await server.connect()
        key_cara = None
        ben = await server.connect()
        cara = await server.connect()
        try:
            await ada.register("Ada", "c-1")
            await ben.register("Ben", "c-2")
            key_cara = await cara.register_key("Cara", "c-3")
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
            cara = await server.connect()
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
