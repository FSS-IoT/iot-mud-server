"""Ticket #81: scripted NPC dialogue, `talk` and `ask ... about ...`.

The v2.1.3 product world authors eleven NPCs with a greeting, a default line,
and a topic map. Before this ticket the loader discards them: `room.state.npcs`
is always empty and both verbs fall through to "I don't understand". These
tests name the behavior the engine and adapter must provide.

Dialogue is authored in YAML (`data/worlds/club_world`); there is no LLM in
this path. Unknown NPCs are `not_found`, unknown topics fall back to the NPC's
default line, and a successful answer reaches the actor directly and the rest
of the room as a broadcast.
"""

from __future__ import annotations

from pathlib import Path

from src.mud.content.models import load_world as load_content_world
from src.mud.engine.core import Engine
from src.mud.protocol.adapter import build_room_state, execute
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLD_DIR = REPO_ROOT / "data" / "worlds" / "club_world"

# `market` is one unlocked step north of the start room and holds the first
# authored NPC, so every dialogue test starts in the same known room.
START_ROOM = "gate"
NPC_ROOM = "market"

MIRA = "Mira the Merchant"
MIRA_ID = "npc_merchant"
MIRA_GREETING = "Fresh coin, fresh gossip. Which do you want?"
MIRA_DEFAULT = "I sell what the village makes and hear what it hides."
MIRA_GOLD = "The gold key? The alchemist keeps it. The tavern door answers to it."


async def _world() -> Engine:
    engine = Engine()
    await load_world(engine, WORLD_DIR)
    return engine


async def _at_market() -> tuple[Engine, object]:
    engine = await _world()
    player = engine.register_player("Ada")
    moved, result = await engine.move_player("Ada", "north")
    assert moved, result
    assert player.location is not None
    assert player.location.name == "Marketplace"
    return engine, player


# ---------------------------------------------------------------------------
# Placement: the authored NPCs reach the engine, and only the room's `npcs`
# ---------------------------------------------------------------------------


async def test_every_authored_npc_is_placed_in_its_room():
    loaded = load_content_world(WORLD_DIR)
    engine = await _world()

    authored = [
        (room.name, npc) for room in loaded.world.rooms for npc in room.npcs
    ]
    assert len(authored) == 11

    placed = []
    for room_name, authored_npc in authored:
        room = engine.get_room(room_name)
        assert room is not None, room_name
        matches = [
            npc for npc in room.npcs
            if getattr(npc, "item_id", None) == authored_npc.id
        ]
        assert len(matches) == 1, (room_name, authored_npc.id)
        placed.append(matches[0].name)

    assert sorted(placed) == sorted(npc.name for _, npc in authored)


async def test_npcs_are_not_room_objects_or_inventory():
    engine = await _world()
    for room in engine.rooms.values():
        for npc in room.npcs:
            assert npc not in room.contents

    _engine, player = await _at_market()
    assert not any(
        npc.name == MIRA for npc in player.contents
    )


async def test_room_state_lists_npcs_additively():
    engine, player = await _at_market()

    state = build_room_state(engine, player)

    assert state["room"]["npcs"] == [{"id": MIRA_ID, "name": MIRA}]
    # Objects and players keep their v2.0 shape; adding NPCs changes neither.
    assert "Gold Coin" in [
        obj["name"] for obj in state["room"]["objects"]
    ]
    assert state["room"]["players"] == []
    assert MIRA in state["text"]


# ---------------------------------------------------------------------------
# talk: greeting, default fallback, unknown NPC
# ---------------------------------------------------------------------------


async def test_talk_returns_the_greeting():
    engine, _player = await _at_market()

    reply = await engine.process_command("talk Mira", "Ada")

    assert "Mira the Merchant" in reply
    assert MIRA_GREETING in reply


async def test_talk_without_a_greeting_uses_default():
    engine, player = await _at_market()
    npc = next(n for n in player.location.npcs if n.name == MIRA)
    npc.greeting = ""

    reply = await engine.process_command("talk Mira", "Ada")

    assert MIRA_DEFAULT in reply


async def test_talk_unknown_npc_is_not_found():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "talk Nobody")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "not_found"


async def test_talk_without_a_target_is_bad_args():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "talk")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "bad_args"


# ---------------------------------------------------------------------------
# ask: known topic, unknown topic fallback, unknown NPC
# ---------------------------------------------------------------------------


async def test_ask_known_topic_returns_the_topic_line():
    engine, _player = await _at_market()

    reply = await engine.process_command("ask Mira about gold", "Ada")

    assert MIRA_GOLD in reply
    assert MIRA_DEFAULT not in reply


async def test_ask_unknown_topic_returns_the_default():
    engine, _player = await _at_market()

    reply = await engine.process_command("ask Mira about weather", "Ada")

    assert MIRA_DEFAULT in reply


async def test_ask_without_about_is_bad_args():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "ask Mira gold")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "bad_args"


async def test_ask_unknown_npc_is_not_found():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "ask Nobody about gold")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "not_found"


# ---------------------------------------------------------------------------
# The typed event: actor gets the direct reply, the room gets the broadcast
# ---------------------------------------------------------------------------


async def test_talk_emits_npc_say_to_actor_and_room():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "talk Mira")

    assert [event.type for event in result.events] == ["npc.say", "npc.say"]
    actor, room = result.events
    assert actor.target == "actor" and actor.direct is True
    assert actor.data["self"] is True
    assert actor.data["npc"] == {"id": MIRA_ID, "name": MIRA}
    assert MIRA_GREETING in actor.data["text"]
    assert room.target == "room" and room.direct is False
    assert room.data["self"] is False
    assert room.data["npc"] == actor.data["npc"]
    assert room.data["text"] == actor.data["text"]
    assert room.room_name == "Marketplace"


async def test_ask_event_names_the_matched_topic():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "ask Mira about gold")

    actor = result.events[0]
    assert actor.type == "npc.say"
    assert actor.data["topic"] == "gold"
    assert MIRA_GOLD in actor.data["text"]


async def test_unknown_topic_event_has_no_topic_field():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "ask Mira about weather")

    actor = result.events[0]
    assert actor.type == "npc.say"
    assert "topic" not in actor.data
    assert MIRA_DEFAULT in actor.data["text"]


async def test_unknown_npc_emits_no_speech_event():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "talk Nobody")

    assert result.events[0].data["code"] == "not_found"
    assert all(event.type != "npc.say" for event in result.events)


# ---------------------------------------------------------------------------
# NPC names do not collide with the object vocabulary
# ---------------------------------------------------------------------------


async def test_take_cannot_take_an_npc():
    engine, _player = await _at_market()

    result = await execute(engine, "Ada", "take Mira")

    assert [event.type for event in result.events] == ["error"]
    assert result.events[0].data["code"] == "not_found"
    room = engine.get_player("Ada").location
    assert any(npc.name == MIRA for npc in room.npcs)
