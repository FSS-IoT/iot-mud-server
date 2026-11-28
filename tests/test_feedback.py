"""Issue #177: player feedback reports with context.

A player sends a top-level `feedback` message with `{text, category?}`. The
server validates it, captures a bounded in-memory context (last 10 commands,
inventory snapshot, session stats), stores one `feedback` row, and replies
`feedback.saved {id}`. An operator reviews and exports the rows offline.

These tests are written before the behavior exists, so they fail now (RED).
"""

from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from src.mud.admin import main
from src.mud.feedback import (
    FeedbackLimiter,
    FeedbackValidationError,
    validate_feedback_category,
    validate_feedback_text,
)
from src.mud.storage.db import SCHEMA_VERSION, connect, migrate, open_database
from tests.golden.harness import GoldenClient, golden_client, golden_server


async def _register(harness, name: str = "Ada") -> GoldenClient:
    client = await harness.connect()
    await client.register(name)
    return client


async def _feedback(client, text, category=None, msg_id="f1"):
    data = {"text": text}
    if category is not None:
        data["category"] = category
    await client.send({
        "v": 2, "type": "feedback", "id": msg_id, "data": data,
    })
    return await client.wait(
        "feedback.saved", lambda m: m.get("id") == msg_id)


async def _feedback_error(client, text, category=None, msg_id="f1"):
    data = {"text": text}
    if category is not None:
        data["category"] = category
    await client.send({
        "v": 2, "type": "feedback", "id": msg_id, "data": data,
    })
    return await client.wait("error", lambda m: m.get("id") == msg_id)


def _stored(harness) -> list[sqlite3.Row]:
    return harness.instance.db.execute(
        "SELECT * FROM feedback ORDER BY id").fetchall()


def _insert_feedback(conn, *, text, player="Ada", category="other",
                     created_at="2026-01-01T00:00:00Z") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback(player_name, text, category, room_id, "
            "room_name, world_epoch, server_commit, context, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (player, text, category, "market", "Marketplace", "epoch-1",
             "0" * 40, "{}", created_at),
        )
    return int(cur.lastrowid)


# --- validation --------------------------------------------------------------


def test_text_must_be_a_string():
    for value in (None, 123, ["a"], {"text": "x"}):
        with pytest.raises(FeedbackValidationError) as exc:
            validate_feedback_text(value)
        assert exc.value.code == "bad_args"


def test_text_is_stripped_and_empty_is_refused():
    assert validate_feedback_text("  hello  ") == "hello"
    for value in ("", "   ", "\n\t"):
        with pytest.raises(FeedbackValidationError) as exc:
            validate_feedback_text(value)
        assert exc.value.code == "bad_args"


def test_text_at_2000_chars_is_accepted():
    assert validate_feedback_text("a" * 2000) == "a" * 2000


def test_text_at_2001_chars_is_refused():
    with pytest.raises(FeedbackValidationError) as exc:
        validate_feedback_text("a" * 2001)
    assert exc.value.code == "too_long"


def test_newline_and_tab_are_allowed_but_other_controls_are_not():
    assert validate_feedback_text("line one\nline two\tend") == \
        "line one\nline two\tend"
    for bad in ("a\x00b", "a\x1bb", "a\x07b", "a\x7fb", "a\u0085b"):
        with pytest.raises(FeedbackValidationError) as exc:
            validate_feedback_text(bad)
        assert exc.value.code == "bad_args"


def test_category_is_an_allowlist():
    assert validate_feedback_category(None) == "other"
    assert validate_feedback_category("bug") == "bug"
    assert validate_feedback_category("idea") == "idea"
    assert validate_feedback_category("other") == "other"
    for bad in ("BUG", "feature", 123, ""):
        with pytest.raises(FeedbackValidationError) as exc:
            validate_feedback_category(bad)
        assert exc.value.code == "bad_args"


# --- rate limiter ------------------------------------------------------------


def test_limiter_allows_one_per_minute_and_five_per_hour():
    clock = [0.0]
    limiter = FeedbackLimiter(lambda: clock[0])

    # The first five, spaced a minute apart, pass.
    for i in range(5):
        assert limiter.allowed("Ada")
        limiter.record("Ada")
        clock[0] += 61.0

    # A sixth within the hour is refused.
    assert limiter.allowed("Ada") is False

    # A second send inside the same minute is refused.
    fresh = FeedbackLimiter(lambda: clock[0])
    assert fresh.allowed("Ben")
    fresh.record("Ben")
    clock[0] += 1.0
    assert fresh.allowed("Ben") is False

    # A different character is unaffected.
    assert fresh.allowed("Cara") is True


# --- schema ------------------------------------------------------------------


def test_schema_3_database_migrates_to_4_and_keeps_rows(tmp_path):
    conn = connect(tmp_path / "old.db")
    try:
        conn.execute(
            "CREATE TABLE players (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "room_id TEXT, created_at TEXT NOT NULL, last_seen TEXT, "
            "asleep INTEGER NOT NULL DEFAULT 0, "
            "skills TEXT NOT NULL DEFAULT '{}', "
            "capacity_base REAL NOT NULL DEFAULT 30.0)"
        )
        conn.execute(
            "INSERT INTO players(name, created_at) VALUES ('Ada', 'now')")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

        migrate(conn, tmp_path / "old.db")

        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            SCHEMA_VERSION
        row = conn.execute(
            "SELECT name FROM players WHERE name = 'Ada'").fetchone()
        assert row["name"] == "Ada"
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "feedback" in tables
    finally:
        conn.close()


# --- the wire ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_send_stores_a_row_and_returns_saved(tmp_path):
    async with golden_server(tmp_path / "send") as server:
        async with golden_client(server) as ada:
            await ada.register("Ada")
            reply = await _feedback(ada, "the torch is broken", "bug")

        assert isinstance(reply["data"]["id"], int)
        assert set(reply["data"]) == {"id"}

        rows = _stored(server)
        assert len(rows) == 1
        assert rows[0]["player_name"] == "Ada"
        assert rows[0]["text"] == "the torch is broken"
        assert rows[0]["category"] == "bug"


@pytest.mark.asyncio
async def test_guest_feedback_is_refused(tmp_path):
    async with golden_server(tmp_path / "guest") as server:
        async with golden_client(server) as visitor:
            await visitor.join("Visitor")
            await visitor.send({
                "v": 2, "type": "feedback", "id": "f1",
                "data": {"text": "hello"},
            })
            error = await visitor.wait(
                "error", lambda m: m.get("id") == "f1")
            assert error["data"]["code"] == "not_authenticated"

        assert _stored(server) == []


@pytest.mark.asyncio
async def test_empty_text_is_refused(tmp_path):
    async with (
        golden_server(tmp_path / "empty") as server,
        golden_client(server) as ada,
    ):
        await ada.register("Ada")
        error = await _feedback_error(ada, "   ")
        assert error["data"]["code"] == "bad_args"


@pytest.mark.asyncio
async def test_too_long_text_is_refused(tmp_path):
    async with (
        golden_server(tmp_path / "long") as server,
        golden_client(server) as ada,
    ):
        await ada.register("Ada")
        error = await _feedback_error(ada, "a" * 2001)
        assert error["data"]["code"] == "too_long"


@pytest.mark.asyncio
async def test_control_character_text_is_refused(tmp_path):
    async with (
        golden_server(tmp_path / "controls") as server,
        golden_client(server) as ada,
    ):
        await ada.register("Ada")
        for text in ("a\x00b", "a\x1bb"):
            error = await _feedback_error(ada, text)
            assert error["data"]["code"] == "bad_args"


@pytest.mark.asyncio
async def test_unknown_category_is_refused(tmp_path):
    async with (
        golden_server(tmp_path / "category") as server,
        golden_client(server) as ada,
    ):
        await ada.register("Ada")
        error = await _feedback_error(ada, "hello", "nope")
        assert error["data"]["code"] == "bad_args"


@pytest.mark.asyncio
async def test_missing_category_defaults_to_other(tmp_path):
    async with golden_server(tmp_path / "default") as server:
        async with golden_client(server) as ada:
            await ada.register("Ada")
            await _feedback(ada, "no category given")

        rows = _stored(server)
        assert rows[0]["category"] == "other"


@pytest.mark.asyncio
async def test_rate_limit_refuses_the_second_send_within_a_minute(tmp_path):
    async with golden_server(tmp_path / "rate") as server:
        async with golden_client(server) as ada:
            await ada.register("Ada")
            await _feedback(ada, "first report", msg_id="f1")
            await ada.send({
                "v": 2, "type": "feedback", "id": "f2",
                "data": {"text": "second report"},
            })
            error = await ada.wait("error", lambda m: m.get("id") == "f2")
            assert error["data"]["code"] == "rate_limited"

        # Only the first was stored, and the socket is still usable.
        rows = _stored(server)
        assert len(rows) == 1
        assert rows[0]["text"] == "first report"


@pytest.mark.asyncio
async def test_tell_body_is_redacted_in_the_stored_context(tmp_path):
    async with golden_server(tmp_path / "tell") as server:
        async with golden_client(server) as ada, golden_client(server) as ben:
            await ada.register("Ada")
            await ben.register("Ben")
            await ada.input("tell Ben meet me at the gate")
            await ada.wait("player.told")
            await _feedback(ada, "a report")

        row = _stored(server)[0]
        context = json.loads(row["context"])
        texts = [entry["text"] for entry in context["commands"]]
        assert "tell Ben [redacted]" in texts
        assert not any("meet me at the gate" in t for t in texts)


@pytest.mark.asyncio
async def test_inventory_snapshot_and_session_stats_are_present(tmp_path):
    async with golden_server(tmp_path / "context") as server:
        async with golden_client(server) as ada:
            await ada.register("Ada")
            await ada.input("go north")
            await ada.wait("room.state")
            await ada.input("take gold coin")
            await ada.wait("object.taken")
            await ada.input("inventory")
            await ada.wait("player.inventory")
            await _feedback(ada, "context please")

        row = _stored(server)[0]
        context = json.loads(row["context"])
        inventory = context["inventory"]
        assert inventory["capacity"] >= 0.0
        assert inventory["used"] >= 0.0
        assert any(item["name"] == "Gold Coin" for item in inventory["items"])
        for item in inventory["items"]:
            assert set(item) == {"id", "name", "weight", "lit"}
        session = context["session"]
        assert session["age_seconds"] >= 0.0
        assert session["command_count"] >= 2
        assert session["asleep"] is False


# --- admin -------------------------------------------------------------------


def test_admin_list_shows_a_row(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="the torch is broken", category="bug")
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["feedback", "list", "--db", str(path)]) == 0

    out, _ = capsys.readouterr()
    assert "Ada" in out
    assert "bug" in out
    assert "the torch is broken" in out


def test_admin_show_escapes_control_characters(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="a\x1bb\x00c")
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["feedback", "show", "1", "--db", str(path)]) == 0

    out, _ = capsys.readouterr()
    assert "\x1b" not in out
    assert "\x00" not in out
    assert "\\x1b" in out
    assert "\\x00" in out


def test_admin_export_markdown_fences_and_is_0600(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="code ````` snippet")
    finally:
        conn.close()
    capsys.readouterr()

    out_file = tmp_path / "out.md"
    assert main([
        "feedback", "export", "--format", "markdown",
        "--output", str(out_file), "--db", str(path),
    ]) == 0

    assert out_file.is_file()
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    content = out_file.read_text(encoding="utf-8")
    assert "code ````` snippet" in content
    assert "`" * 6 in content


def test_admin_export_json_dumps_and_is_0600(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="hello\nworld")
    finally:
        conn.close()
    capsys.readouterr()

    out_file = tmp_path / "out.json"
    assert main([
        "feedback", "export", "--format", "json",
        "--output", str(out_file), "--db", str(path),
    ]) == 0

    assert out_file.is_file()
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data[0]["text"] == "hello\nworld"
    assert data[0]["player_name"] == "Ada"


def test_admin_delete_guards_and_logs_only_the_id(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="TOP_SECRET_TEXT", category="bug")
    finally:
        conn.close()

    assert main(["feedback", "delete", "1", "--db", str(path)]) == 2
    capsys.readouterr()

    assert main(["feedback", "delete", "1", "--yes", "--db", str(path)]) == 0
    out, _ = capsys.readouterr()
    assert "TOP_SECRET_TEXT" not in out
    assert "1" in out

    assert main(["feedback", "delete", "999", "--yes", "--db", str(path)]) == 1


def test_admin_prune_removes_rows_older_than_the_window(tmp_path, capsys):
    path = tmp_path / "mud.db"
    assert main(["init-db", "--db", str(path)]) == 0
    conn = open_database(path)
    try:
        _insert_feedback(conn, text="old report",
                         created_at="2020-01-01T00:00:00Z")
        _insert_feedback(conn, text="new report",
                         created_at="2099-01-01T00:00:00Z")
    finally:
        conn.close()
    capsys.readouterr()

    assert main(["feedback", "prune", "--older-than", "90d",
                 "--db", str(path)]) == 0

    out, _ = capsys.readouterr()
    assert "pruned" in out
    assert "1" in out

    conn = open_database(path)
    try:
        rows = conn.execute(
            "SELECT text FROM feedback ORDER BY id").fetchall()
    finally:
        conn.close()
    assert [row["text"] for row in rows] == ["new report"]
