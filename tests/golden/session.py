"""The `docs/SESSION.md` acceptance route, shared by the docs and newcomer tests.

The route is deliberately written against the protocol, not the engine: every
observation comes from a client message. That lets the same flow run against
the in-process `golden_server` and against a server started from the documented
CLI in a subprocess.
"""

from __future__ import annotations

import asyncio

from tests.golden.harness import GoldenClient, command, golden_client


def _messages_since(
    client: GoldenClient, marker: int, type_: str
) -> list[dict]:
    return [
        message for message in client.log[marker:]
        if message.get("type") == type_
    ]


async def wait_until_asleep(client: GoldenClient, name: str) -> dict:
    """Wait for a character to appear asleep in `who.list`.

    Closing a socket is not instantaneous: the server's cleanup sleeps the
    character in its own task. Polling the documented roster keeps this helper
    usable when the server runs in another process.
    """
    for _ in range(200):
        roster = await command(client, "who", "who.list")
        row = next(
            row for row in roster["data"]["players"] if row["name"] == name
        )
        if row["asleep"]:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {name} to sleep")


async def run_acceptance_session(uri: str) -> None:
    """Run the full SESSION.md route against a fresh, seeded server at `uri`."""
    async with (
        golden_client(uri) as ada,
        golden_client(uri) as ben,
    ):
        ok, _ = await ada.register("Ada")
        key = ok["data"]["key"]
        await ben.register("Ben")

        # Session steps 4-5: move and arrive.
        await command(ada, "go north", "room.state")
        await ben.wait(
            "room.leave",
            lambda m: m["data"]["actor"]["name"] == "Ada",
        )
        await command(ben, "go north", "room.state")
        await ada.wait(
            "room.enter", lambda m: m["data"]["actor"]["name"] == "Ben"
        )

        # Steps 6-8: take and drop send no inventory follow-up.
        marker = len(ada.log)
        await command(ada, "take gold coin", "object.taken")
        assert not _messages_since(ada, marker, "player.inventory")
        await ben.wait(
            "object.taken", lambda m: m["data"]["by"] == "Ada"
        )
        inventory = await command(ada, "inventory", "player.inventory")
        assert any(
            item["name"] == "Gold Coin"
            for item in inventory["data"]["items"]
        )
        marker = len(ada.log)
        await command(ada, "drop gold coin", "object.dropped")
        assert not _messages_since(ada, marker, "player.inventory")

        # Steps 8 and the social section: say, emote, tell, who.
        await command(ada, "say hello", "player.said")
        await ben.wait("player.said")
        await command(ada, "emote waves at the sky", "player.emoted")
        await ben.wait("player.emoted")
        await command(ada, "tell Ben meet me at the gate", "player.told")
        await ben.wait("player.told")
        who = await command(ada, "who", "who.list")
        assert {"Ada", "Ben"} <= {
            row["name"] for row in who["data"]["players"]
        }

        # Step 11: a malformed frame does not close the socket.
        await ada.ws.send("{")
        malformed = await ada.wait("error")
        assert malformed["data"]["code"] == "malformed_message"
        await command(ada, "look", "room.state")

        # Exit locks: the market's east door needs the gold key.
        locked = await command(ada, "go east", "error")
        assert locked["data"]["code"] == "locked"
        await ben.wait("exit.locked")
        await command(ada, "go west", "room.state")
        await command(ada, "take torch", "object.taken")
        await command(ada, "take gold key", "object.taken")
        await command(ada, "light torch", "object.lit")
        npc = await command(ada, "talk Ysolde", "npc.say")
        assert npc["data"]["npc"]["id"] == "npc_alchemist"
        asked = await command(
            ada, "ask Ysolde about chest", "npc.say",
            lambda m: m["data"].get("topic") == "chest",
        )
        assert asked["data"]["self"] is True
        await command(ada, "go east", "room.state")
        unlocked = await command(ada, "use gold key on east", "exit.unlocked")
        assert unlocked["data"]["exit"]["to"] == "tavern"
        await ben.wait("exit.unlocked")
        await command(ada, "go east", "room.state")
        await command(ada, "go west", "room.state")

        # Containers: open, get, put, close, and typed errors.
        await command(ada, "go north", "room.state")
        await command(ada, "use gold key on chest", "object.unlocked")
        await command(ada, "open chest", "object.opened")
        await command(ada, "get brass lantern from chest", "object.got")
        await ada.wait("player.inventory")
        await command(ada, "put brass lantern in chest", "object.put")
        await ada.wait("player.inventory")
        await command(ada, "close chest", "object.closed")
        closed = await command(ada, "get brass lantern from chest", "error")
        assert closed["data"]["code"] == "container_closed"
        await command(ada, "open chest", "object.opened")
        marker = len(ada.log)
        await command(ada, "get brass lantern from chest", "object.got")
        await ada.wait("player.inventory")
        assert _messages_since(ada, marker, "player.inventory")
        await command(ada, "go south", "room.state")

        # Sleep and wake: the gold key drops in the room; no queue.
        await ada.quit()
        leave = await ben.wait(
            "room.leave",
            lambda m: m["data"]["actor"]["name"] == "Ada"
            and m["data"].get("reason") == "disconnect",
        )
        assert leave["data"]["asleep"] is True
        assert leave["data"]["stone"] is True
        assert any(
            item["id"] == "key_gold_1"
            for item in leave["data"].get("dropped", [])
        )
        roster = await command(ben, "who", "who.list")
        ada_row = next(
            row for row in roster["data"]["players"] if row["name"] == "Ada"
        )
        assert ada_row["asleep"] is True and ada_row["stone"] is True
        asleep_tell = await command(ben, "tell Ada hello", "error")
        assert asleep_tell["data"]["code"] == "not_awake"

        await wait_until_asleep(ben, "Ada")
        async with golden_client(uri) as woken:
            login, state = await woken.login(key)
            assert login["data"]["mode"] == "login"
            assert state["data"]["room"]["id"] == "market"
            names = {
                item["name"]
                for item in (
                    await command(woken, "inventory", "player.inventory")
                )["data"]["items"]
            }
            assert "Torch" in names
            assert "Gold Key" not in names
            assert not _messages_since(woken, 0, "player.told")
            await ben.wait(
                "room.enter",
                lambda m: m["data"]["actor"]["name"] == "Ada",
            )
            await command(ben, "take gold key", "object.taken")
