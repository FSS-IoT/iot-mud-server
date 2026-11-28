"""Ticket #138: one character-admission owner.

The transport should bind the avatar that admission returns. It should not
read `PlayerStore`, call `restore_player`/`wake_player` itself, or decide how a
character sleeps. These tests use the admission interface directly and pin the
server's import surface.

Red on the parent: `src/mud.admission` does not exist and v2_server imports
PlayerStore, WriteThroughStore, and StorageWriteError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.mud.auth import AuthError
from src.mud.content import Entity, Room
from src.mud.engine.core import Engine
from src.mud.storage.db import open_database
from src.mud.storage.store import ChangeSet, PlayerStore, StorageWriteError
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]

WORLD = """
name: Admission Test
start_room: hall
rng_seed: 7
rooms:
  - id: hall
    name: Hall
    description: A stone hall.
    exits:
      north: vault
    objects:
      - id: coin
        name: Coin
        description: A gold coin.
        weight: 0.1
      - id: key
        name: Iron Key
        description: A small iron key.
        weight: 0.1
        dropped_on_sleep: true
  - id: vault
    name: Vault
    description: A dusty vault.
"""


def _world_file(tmp_path: Path) -> Path:
    path = tmp_path / "world.yaml"
    path.write_text(WORLD, encoding="utf-8")
    return path


async def _engine(world_path: Path) -> Engine:
    engine = Engine()
    await load_world(engine, world_path)
    return engine


async def _admission(tmp_path: Path, *, fail=None):
    from src.mud.admission import CharacterAdmission

    world_path = _world_file(tmp_path)
    engine = await _engine(world_path)
    conn = open_database(tmp_path / "mud.db")
    admission = CharacterAdmission(engine, conn, fail=fail)
    return engine, conn, admission, world_path


async def test_register_returns_the_live_avatar_and_key(tmp_path):
    engine, conn, admission, _world_path = await _admission(tmp_path)
    try:
        result = admission.register("Ada")

        assert result.player is engine.get_player("Ada")
        assert result.player.location is engine.get_room("Hall")
        assert result.registration.key.startswith("mud1-")
        row = conn.execute(
            "SELECT name, room_id FROM players WHERE name = 'Ada'"
        ).fetchone()
        assert (row["name"], row["room_id"]) == ("Ada", "hall")

        with pytest.raises(AuthError) as taken:
            admission.register("Ada")
        assert taken.value.code == "name_taken"
    finally:
        conn.close()


async def test_login_uses_the_auth_room_id_without_reloading(
        tmp_path, monkeypatch):
    engine, conn, admission, world_path = await _admission(tmp_path)
    key = admission.register("Ada").registration.key
    await engine.process_command("take coin", "Ada")
    await engine.process_command("go north", "Ada")
    admission.suspend("Ada")
    conn.close()

    # A restart: a new engine and a new admission over the same database.
    conn = open_database(tmp_path / "mud.db")
    engine = await _engine(world_path)
    from src.mud.admission import CharacterAdmission

    admission = CharacterAdmission(engine, conn)

    def no_reload(self):
        raise AssertionError("admission must not re-read PlayerStore.load()")

    monkeypatch.setattr(PlayerStore, "load", no_reload)
    result = admission.resume(key)

    assert result.login.room_id == "vault"
    assert result.login.items == ("coin",)
    assert result.player.location is engine.get_room("Vault")
    assert [item.item_id for item in result.player.contents] == ["coin"]
    assert result.player.asleep is False
    conn.close()


async def test_resume_after_restart_returns_the_same_room_and_inventory(
    tmp_path,
):
    engine, conn, admission, world_path = await _admission(tmp_path)
    key = admission.register("Ada").registration.key
    await engine.process_command("take coin", "Ada")
    await engine.process_command("take key", "Ada")
    await engine.process_command("go north", "Ada")
    dropped = admission.suspend("Ada")
    assert [item.item_id for item in dropped.dropped] == ["key"]
    conn.close()

    conn = open_database(tmp_path / "mud.db")
    engine = await _engine(world_path)
    from src.mud.admission import CharacterAdmission

    result = CharacterAdmission(engine, conn).resume(key)

    assert result.player.location is engine.get_room("Vault")
    assert [item.item_id for item in result.player.contents] == ["coin"]
    assert result.player.asleep is False
    assert "key" not in [item.item_id for item in result.player.contents]
    vault = engine.get_room("Vault")
    assert "key" in [item.item_id for item in vault.contents]
    conn.close()


async def test_suspend_drops_only_dropped_on_sleep_items(tmp_path):
    engine, conn, admission, _world_path = await _admission(tmp_path)
    try:
        admission.register("Ada")
        await engine.process_command("take coin", "Ada")
        await engine.process_command("take key", "Ada")

        result = admission.suspend("Ada")

        assert result.persisted is True
        assert [item.item_id for item in result.dropped] == ["key"]
        ada = engine.get_player("Ada")
        assert ada.asleep is True
        assert [item.item_id for item in ada.contents] == ["coin"]
        hall = engine.get_room("Hall")
        assert "key" in [item.item_id for item in hall.contents]
        assert "coin" not in [item.item_id for item in hall.contents]
    finally:
        conn.close()


async def test_suspend_reports_a_failed_commit_and_still_stones_the_character(
    tmp_path,
):
    def fail(op: str) -> None:
        if op == "sleep":
            raise StorageWriteError("disk full")

    engine, conn, admission, _world_path = await _admission(tmp_path, fail=fail)
    try:
        admission.register("Ada")
        await engine.process_command("take coin", "Ada")

        result = admission.suspend("Ada")

        assert result.persisted is False
        assert engine.get_player("Ada").asleep is True
        assert [item.item_id for item in engine.get_player("Ada").contents] == [
            "coin",
        ]
    finally:
        conn.close()


async def test_roster_is_the_durable_half_who_reads(tmp_path):
    engine, conn, admission, _world_path = await _admission(tmp_path)
    try:
        admission.register("Ada")
        admission.register("Ben")
        admission.suspend("Ada")

        entries = {entry.name: entry for entry in admission.roster()}
        assert entries["Ada"].asleep is True
        assert entries["Ada"].room_id == "hall"
        assert entries["Ben"].asleep is False

        who = {entry["name"]: entry for entry in engine.who_entries()}
        assert who["Ada"]["asleep"] is True
        assert who["Ada"]["room_id"] == "hall"
    finally:
        conn.close()


def test_v2_server_does_not_read_character_rows_directly():
    source = (REPO_ROOT / "src/mud/net/v2_server.py").read_text(encoding="utf-8")
    assert "PlayerStore" not in source
    assert "WriteThroughStore" not in source
    assert "StorageWriteError" not in source


async def test_guest_admission_spills_carried_items_through_one_interface(
    tmp_path,
):
    engine, conn, admission, _world_path = await _admission(tmp_path)
    try:
        guest = admission.admit_guest("Guest").player
        coin = Entity("Coin", "A coin.", item_id="guest_coin")
        key = Entity("Key", "A key.", item_id="guest_key")
        guest.add_content(coin)
        guest.add_content(key)
        room = guest.location

        spill = admission.spill_guest("Guest")

        assert spill.room is room
        assert [item.item_id for item in spill.dropped] == [
            "guest_coin", "guest_key"]
        assert coin in room.contents and key in room.contents
        assert engine.get_player("Guest") is None
    finally:
        conn.close()


async def test_guest_admission_works_without_a_database():
    from src.mud.admission import CharacterAdmission

    room = Room("Hall", "A stone hall.")
    room.item_id = "hall"
    engine = Engine()
    engine._rooms[room.name] = room
    engine.start_room = room
    admission = CharacterAdmission(engine, None)

    guest = admission.admit_guest("Guest").player
    coin = Entity("Coin", "A coin.", item_id="guest_coin")
    guest.add_content(coin)
    spill = admission.spill_guest("Guest")

    assert spill.room is room
    assert spill.dropped == (coin,)
    assert coin in room.contents
    assert engine.get_player("Guest") is None


def test_a_guest_admission_does_not_replace_an_engine_store():
    from src.mud.admission import CharacterAdmission

    engine = Engine()
    store = object()
    engine.attach_store(store)

    CharacterAdmission(engine, None)

    assert engine.store is store
    assert engine.roster_provider is None


def test_a_no_database_register_still_validates_the_name():
    from src.mud.admission import CharacterAdmission

    engine = Engine()
    with pytest.raises(AuthError) as bad:
        CharacterAdmission(engine, None).register("!")
    assert bad.value.code == "invalid_name"


async def test_guest_spill_clears_an_equipped_slot_in_the_store(tmp_path):
    _engine, conn, admission, _world_path = await _admission(tmp_path)
    try:
        guest = admission.admit_guest("Guest").player
        torch = Entity(
            "Torch", "A wooden torch.", light_source=True,
            equip_slots=("hand",), hands=1, item_id="guest_torch")
        guest.add_content(torch)
        admission.store.commit(ChangeSet("take").place(torch, at=guest))
        guest.equip(torch, "hand_1")
        admission.store.commit(
            ChangeSet("equip").set(torch, "slot", "hand_1"))

        spill = admission.spill_guest("Guest")

        assert torch.equipped_slot is None
        assert torch in spill.room.contents
        state = json.loads(conn.execute(
            "SELECT state FROM objects WHERE id = 'guest_torch'"
        ).fetchone()["state"])
        assert state["slot"] is None
    finally:
        conn.close()
