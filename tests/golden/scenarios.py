"""Golden scenarios for v2.1.3 (#83).

Every scenario starts its own fresh server and returns a `GoldenRun` holding
the rooms it visited and the event types that really arrived on its clients.
The coverage test in `tests/golden/test_golden_coverage.py` unions those runs
and compares them with `docs/PROTOCOL.md` and the product world.

Design notes:

* The auth-required server is the product default; `guest_session` uses the
  documented `auth_required=False` opt-out because `session.ok` and
  `not_joined` only exist on the v2.0 guest path.
* The 1 Hz NPC task is parked in the harness. `npc_wander` calls
  `wander_tick()` itself, so the path is seeded and repeatable.
* Commands use display names, like a real client, never room or object ids.
"""

from __future__ import annotations

from pathlib import Path

from tests.golden.harness import (
    GoldenRun,
    command,
    golden_client,
    golden_server,
    wait_until,
    walk,
)


async def session(directory: Path) -> GoldenRun:
    """The smallest auth-required session: hello, register, login, room.state."""
    run = GoldenRun("session")
    async with golden_server(directory / "session") as server:
        async with golden_client(server) as ada:
            ok, state = await ada.register("Ada")
            assert ok["data"]["mode"] == "register"
            assert ok["data"]["key"].startswith("mud1-")
            run.note_room(state["data"]["room"]["id"])
            run.collect(ada)
            key = ok["data"]["key"]

        # The register connection is gone, so the character is asleep and a
        # key login may wake it. A login response never repeats the key.
        await wait_until(
            lambda: bool(server.engine.get_player("Ada") is not None
                         and server.engine.get_player("Ada").asleep),
            description="Ada to sleep on disconnect",
        )
        async with golden_client(server) as probe:
            ok, state = await probe.login(key)
            assert ok["data"]["mode"] == "login"
            assert "key" not in ok["data"]
            run.note_room(state["data"]["room"]["id"])
            run.collect(probe)
    return run


async def full_tour(directory: Path) -> GoldenRun:
    """Walk all 35 rooms and exercise every object, lock, and dialogue event.

    Ada carries the key progression; Ben is the room observer for the two
    lock events and the room copies of speech, then leaves. The route is
    explicit so a new room or area fails the area assertion until it is added.
    """
    run = GoldenRun("full_tour")
    async with golden_server(directory / "full_tour") as server:
        engine = server.engine
        async with golden_client(server) as ada, golden_client(server) as ben:
            _, ada_state = await ada.register("Ada")
            _, ben_state = await ben.register("Ben")
            run.note_room(ada_state["data"]["room"]["id"])
            run.note_room(ben_state["data"]["room"]["id"])

            # --- starting village: gate -> market -> shop ---
            await command(ada, "read sign", "object.read")
            await command(ada, "exits", "notice")
            await command(ada, "look at sign", "notice")
            await command(ada, "dance", "error")  # unknown_command
            await command(ada, "take nothing", "error")  # not_found

            await walk(run, ada, engine, "gate", "market")
            await walk(run, ben, engine, "gate", "market")

            await command(ada, "talk Mira", "npc.say")
            said = await ben.wait(
                "npc.say", lambda m: m["data"]["npc"]["id"] == "npc_merchant"
            )
            assert said["data"]["self"] is False
            await command(ben, "look", "room.state")  # resync path
            asked = await command(
                ada, "ask Mira about gold", "npc.say",
                lambda m: m["data"]["npc"]["id"] == "npc_merchant",
            )
            assert asked["data"]["topic"] == "gold"
            fallback = await command(
                ada, "ask Mira about weather", "npc.say",
                lambda m: m["data"]["npc"]["id"] == "npc_merchant",
            )
            assert "topic" not in fallback["data"]

            # The tavern door is locked until the gold key is used on it.
            locked = await command(ada, "go east", "error")
            assert locked["data"]["code"] == "locked"
            room_locked = await ben.wait("exit.locked")
            assert room_locked["data"]["exit"]["to"] == "tavern"

            await walk(run, ada, engine, "market", "shop")
            await walk(run, ben, engine, "market", "shop")
            await command(ada, "take torch", "object.taken")
            await command(ada, "take gold key", "object.taken")
            await command(ada, "light torch", "object.lit")
            await command(ada, "read journal", "object.read")
            # The explicit verb is the golden coverage path for
            # `player.inventory`; no step waits for the take/drop follow-up
            # that #54/#56 remove.
            inventory = await command(ada, "inventory", "player.inventory")
            assert inventory["data"]["capacity"] == 12.0

            await walk(run, ada, engine, "shop", "market")
            await walk(run, ben, engine, "shop", "market")
            unlocked = await command(ada, "use gold key on east", "exit.unlocked")
            assert unlocked["data"]["exit"]["to"] == "tavern"
            room_unlocked = await ben.wait("exit.unlocked")
            assert room_unlocked["data"]["self"] is False

            await walk(run, ada, engine, "market", "tavern")
            await command(ada, "read tavern ledger", "object.read")
            await walk(run, ada, engine, "tavern", "market")

            # --- forest: the container cycle and the dark room ---
            await walk(run, ada, engine, "market", "forest")
            await walk(run, ben, engine, "market", "forest")
            await command(ada, "use gold key on chest", "object.unlocked")
            await command(ada, "open chest", "object.opened")
            await command(ada, "get brass lantern from chest", "object.got")
            await command(ada, "put brass lantern in chest", "object.put")
            await command(ada, "close chest", "object.closed")
            await walk(run, ada, engine, "forest", "market")
            await walk(run, ben, engine, "forest", "market")

            # --- market: take/drop, speech, and the social events ---
            await command(ada, "take gold coin", "object.taken")
            await command(ada, "drop gold coin", "object.dropped")
            await command(ada, "emote waves at the sky", "player.emoted")
            await ben.wait("player.emoted")
            await command(ada, "say hello", "player.said")
            await ben.wait("player.said")
            await command(ada, "tell Ben meet me at the gate", "player.told")
            told = await ben.wait("player.told")
            assert told["data"]["self"] is False
            who = await command(ada, "who", "who.list")
            assert {row["name"] for row in who["data"]["players"]} >= {
                "Ada", "Ben"
            }

            await ben.quit()
            left = await ada.wait(
                "room.leave", lambda m: m["data"]["actor"]["name"] == "Ben"
            )
            assert left["data"]["reason"] == "disconnect"

            # --- south road and the mill ---
            await walk(run, ada, engine, "market", "gate", "south_road")
            await command(ada, "read milestone", "object.read")

            await walk(run, ada, engine, "south_road", "orchard")
            await command(ada, "read orchard sign", "object.read")
            await command(
                ada, "get red apple from apple basket", "object.got"
            )
            await command(ada, "put red apple in apple basket", "object.put")

            await walk(run, ada, engine, "orchard", "beehives")
            await command(ada, "open honey jar", "object.opened")
            await command(ada, "get honeycomb from honey jar", "object.got")
            await command(ada, "close honey jar", "object.closed")
            await command(ada, "read apiary sign", "object.read")

            await walk(
                run, ada, engine,
                "beehives", "orchard", "south_road", "mill_bridge",
            )
            await command(ada, "read bridge plaque", "object.read")

            await walk(run, ada, engine, "mill_bridge", "mill")
            await command(ada, "read miller's ledger", "object.read")
            await walk(run, ada, engine, "mill", "granary")
            await command(ada, "take mill key", "object.taken")
            await command(ada, "read granary tally", "object.read")
            await walk(run, ada, engine, "granary", "mill")
            await walk(run, ada, engine, "mill", "millpond")
            await command(ada, "read pond notice", "object.read")
            await walk(run, ada, engine, "millpond", "mill", "mill_bridge")

            gate_locked = await command(ada, "go east", "error")
            assert gate_locked["data"]["code"] == "locked"
            await command(ada, "use mill key on east", "exit.unlocked")
            await walk(run, ada, engine, "mill_bridge", "millrace")
            await command(ada, "use mill key on race chest", "object.unlocked")
            await command(ada, "open race chest", "object.opened")
            await command(ada, "get mill token from race chest", "object.got")
            await command(ada, "close race chest", "object.closed")
            await walk(run, ada, engine, "millrace", "mill_bridge")

            # --- riverside to the north bank ---
            await walk(run, ada, engine, "mill_bridge", "river_path")
            await command(ada, "read river marker", "object.read")
            await walk(run, ada, engine, "river_path", "riverbank")
            await command(ada, "read river notice", "object.read")
            await walk(run, ada, engine, "riverbank", "ford")
            await command(ada, "read ford stone", "object.read")
            await walk(run, ada, engine, "ford", "river_shallows")
            await walk(run, ada, engine, "river_shallows", "heron_rock")
            await command(ada, "open heron cache", "object.opened")
            await command(ada, "get brass button from heron cache", "object.got")
            await command(ada, "close heron cache", "object.closed")
            await walk(run, ada, engine, "heron_rock", "reeds")
            await walk(run, ada, engine, "reeds", "ferry_landing")
            await command(ada, "read ferry schedule", "object.read")
            await walk(run, ada, engine, "ferry_landing", "ferry")
            await command(ada, "open ferry locker", "object.opened")
            await command(ada, "get iron key from ferry locker", "object.got")
            await command(ada, "close ferry locker", "object.closed")
            await walk(run, ada, engine, "ferry", "north_bank")
            await command(ada, "read boundary stone", "object.read")

            # --- toll house: iron key opens the strongbox and the stair ---
            await walk(run, ada, engine, "north_bank", "toll_house")
            stair_locked = await command(ada, "go north", "error")
            assert stair_locked["data"]["code"] == "locked"
            await command(ada, "use iron key on strongbox", "object.unlocked")
            await command(ada, "open strongbox", "object.opened")
            await command(ada, "get bone key from strongbox", "object.got")
            await command(ada, "close strongbox", "object.closed")
            await command(ada, "use iron key on north", "exit.unlocked")
            await command(ada, "read toll ledger", "object.read")

            await walk(run, ada, engine, "toll_house", "watchtower")
            await command(ada, "read watch log", "object.read")
            await walk(run, ada, engine, "watchtower", "tower_top")
            await command(ada, "read tower map", "object.read")
            await walk(
                run, ada, engine,
                "tower_top", "watchtower", "toll_house", "north_bank",
            )

            # --- ruins ---
            await walk(run, ada, engine, "north_bank", "shrine")
            await command(ada, "read altar tablet", "object.read")
            await walk(run, ada, engine, "shrine", "ruins_gate")
            await command(ada, "read pillar carving", "object.read")
            await walk(run, ada, engine, "ruins_gate", "ruins_court")
            await command(ada, "read court notice", "object.read")

            arch_locked = await command(ada, "go west", "error")
            assert arch_locked["data"]["code"] == "locked"
            await command(ada, "use bone key on west", "exit.unlocked")
            await walk(run, ada, engine, "ruins_court", "fallen_tower")
            await command(ada, "open tower rubble", "object.opened")
            await command(ada, "get crumpled note from tower rubble", "object.got")
            await command(ada, "read crumpled note", "object.read")
            await command(ada, "put crumpled note in tower rubble", "object.put")
            await command(ada, "close tower rubble", "object.closed")
            await command(ada, "take silver key", "object.taken")
            await walk(run, ada, engine, "fallen_tower", "ruins_court")

            await walk(run, ada, engine, "ruins_court", "chapel")
            await command(ada, "read chapel altar", "object.read")
            await walk(run, ada, engine, "chapel", "crypt")
            await command(ada, "use silver key on sarcophagus", "object.unlocked")
            await command(ada, "open sarcophagus", "object.opened")
            await command(ada, "get ancient scroll from sarcophagus", "object.got")
            await command(ada, "close sarcophagus", "object.closed")

            # --- caves and the underground pool ---
            await walk(run, ada, engine, "crypt", "cave_mouth")
            await command(ada, "read cave warning", "object.read")
            await walk(run, ada, engine, "cave_mouth", "cave_tunnel")
            await walk(run, ada, engine, "cave_tunnel", "crystal_grotto")
            await command(ada, "read grotto carving", "object.read")
            await walk(run, ada, engine, "crystal_grotto", "underground_pool")
            await command(ada, "read still pool", "object.read")
            await command(ada, "open pool deposit", "object.opened")
            await command(ada, "get cave pearl from pool deposit", "object.got")
            await command(ada, "close pool deposit", "object.closed")

            run.collect(ada, ben)
    return run


async def social(directory: Path) -> GoldenRun:
    """Who/emote/tell, the stone sleep, typed auth errors, and key login."""
    run = GoldenRun("social")
    async with golden_server(directory / "social") as server, \
            golden_client(server) as ada, \
            golden_client(server) as ben, \
            golden_client(server) as cara:
        ok, ada_state = await ada.register("Ada")
        await ben.register("Ben")
        _, cara_state = await cara.register("Cara")
        run.note_room(ada_state["data"]["room"]["id"])
        run.note_room(cara_state["data"]["room"]["id"])
        key = ok["data"]["key"]

        # Cara moves out so `emote` can be shown to stay room-local.
        await walk(run, cara, server.engine, "gate", "market")
        await command(ada, "emote waves", "player.emoted")
        await ben.wait("player.emoted")
        # `tell` is private and server-wide: the next frame Cara receives
        # is the tell, so no emote reached her in between.
        await command(ada, "tell Cara meet at the mill", "player.told")
        cara_told = await cara.wait("player.told")
        assert cara_told["data"]["self"] is False
        assert "player.emoted" not in {m["type"] for m in cara.log}

        await command(ada, "say hello", "player.said")
        await ben.wait("player.said")
        who = await command(ben, "who", "who.list")
        rows = {row["name"]: row for row in who["data"]["players"]}
        assert {"Ada", "Ben", "Cara"} <= set(rows)
        assert rows["Cara"]["room_id"] == "market"

        await command(ada, "tell Nobody hello", "error")
        await ada.quit()
        left = await ben.wait(
            "room.leave", lambda m: m["data"]["actor"]["name"] == "Ada"
        )
        assert left["data"]["asleep"] is True
        assert left["data"]["stone"] is True
        assert left["data"]["dropped"] == []

        who = await command(ben, "who", "who.list")
        rows = {row["name"]: row for row in who["data"]["players"]}
        assert rows["Ada"]["asleep"] is True
        assert rows["Ada"]["stone"] is True
        asleep_tell = await command(ben, "tell Ada hello", "error")
        assert asleep_tell["data"]["code"] == "not_awake"

        # Typed auth failures: a bad key, then a duplicate name.
        async with golden_client(server) as probe:
            bad = await probe.auth_failure(
                {"mode": "login", "key": "mud1-k0000000000000000-bad"},
                "bad-key",
            )
            assert bad["data"]["code"] == "invalid_key"
            duplicate = await probe.auth_failure(
                {"mode": "register", "name": "Ada"}, "duplicate",
            )
            assert duplicate["data"]["code"] == "name_taken"
            run.collect(probe)

        # Login wakes the stone character in place.
        async with golden_client(server) as wake:
            ok, state = await wake.login(key)
            assert ok["data"]["mode"] == "login"
            assert "key" not in ok["data"]
            assert state["data"]["room"]["id"] == "gate"
            run.collect(wake)
        await wait_until(
            lambda: not server.engine.get_player("Ada").asleep,
            description="Ada to wake on login",
        )

        await ben.quit()
        await cara.quit()
        run.collect(ada, ben, cara)
    return run


async def npc_wander(directory: Path) -> GoldenRun:
    """Drive the seeded 1 Hz tick and watch an NPC leave and return.

    Mira the Merchant starts in the marketplace and first moves at tick 20
    (her authored `wander_every`), returning at tick 40. Pinning those steps
    proves the seed path, and running the scenario twice proves re-runnability.
    """
    run = GoldenRun("npc_wander")
    trail: list[tuple[int, str, str, str, str]] = []
    async with golden_server(directory / "npc_wander") as server, \
            golden_client(server) as ada:
        _, state = await ada.register("Ada")
        run.note_room(state["data"]["room"]["id"])
        await walk(run, ada, server.engine, "gate", "market")

        for _ in range(20):
            for move in await server.instance.wander_tick():
                trail.append(
                    (
                        server.engine.wanderer.tick,
                        move.npc_id,
                        move.from_room.item_id,
                        move.to_room.item_id,
                        move.direction,
                    )
                )
        left = await ada.wait(
            "npc.leave", lambda m: m["data"]["npc"]["id"] == "npc_merchant"
        )
        assert left["data"]["from"]["id"] == "market"
        assert left["data"]["to"]["id"] == "forest"
        assert left["data"]["direction"] == "north"

        state = await command(ada, "look", "room.state")
        assert state["data"]["room"]["npcs"] == []

        for _ in range(20):
            for move in await server.instance.wander_tick():
                trail.append(
                    (
                        server.engine.wanderer.tick,
                        move.npc_id,
                        move.from_room.item_id,
                        move.to_room.item_id,
                        move.direction,
                    )
                )
        entered = await ada.wait(
            "npc.enter", lambda m: m["data"]["npc"]["id"] == "npc_merchant"
        )
        assert entered["data"]["from"]["id"] == "forest"
        assert entered["data"]["to"]["id"] == "market"
        assert entered["data"]["direction"] == "south"

        state = await command(ada, "look", "room.state")
        assert "npc_merchant" in {
            npc["id"] for npc in state["data"]["room"]["npcs"]
        }
        run.collect(ada)
    run.detail["trail"] = trail
    return run


async def guest_session(directory: Path) -> GoldenRun:
    """The documented v2.0 guest opt-out: `session.ok` and `not_joined`."""
    run = GoldenRun("guest_session")
    async with golden_server(
        directory / "guest_session", auth_required=False
    ) as server, golden_client(server) as ada, \
            golden_client(server) as ben:
        assert ada.hello["data"]["auth_required"] is False
        assert "auth_modes" not in ada.hello["data"]
        _, state = await ada.join("Ada")
        run.note_room(state["data"]["room"]["id"])

        # `input` before `join` is a typed error on the guest path.
        before = await command(ben, "look", "error")
        assert before["data"]["code"] == "not_joined"
        await ben.send({
            "v": 2, "type": "join", "id": "dup",
            "data": {"name": "Ada"},
        })
        duplicate = await ben.wait("error")
        assert duplicate["data"]["code"] == "name_taken"

        _, state = await ben.join("Ben")
        run.note_room(state["data"]["room"]["id"])
        await ada.wait(
            "room.enter", lambda m: m["data"]["actor"]["name"] == "Ben"
        )

        await walk(run, ada, server.engine, "gate", "market")
        await walk(run, ben, server.engine, "gate", "market")
        await ada.quit()
        left = await ben.wait(
            "room.leave", lambda m: (
                m["data"]["actor"]["name"] == "Ada"
                and m["data"]["reason"] == "disconnect"
            )
        )
        assert "asleep" not in left["data"]
        run.collect(ada, ben)
    return run


SCENARIOS = {
    "session": session,
    "full_tour": full_tour,
    "social": social,
    "npc_wander": npc_wander,
    "guest_session": guest_session,
}
