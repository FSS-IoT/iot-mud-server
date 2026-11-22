"""The experimental admin console and world editor.

Every server binds port 0 for both the MUD and the admin HTTP listener, so the
fixed-8765 legacy suite is never disturbed. Worlds and databases are copies in
``tmp_path``; nothing here writes the repository's product world.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import websockets
import yaml

from src.mud.config import (
    AdminConfig,
    ConfigError,
    load_config,
)
from src.mud.engine.core import Engine
from src.mud.net.admin_console import AdminConsoleError
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
CLUB_WORLD = REPO_ROOT / "data" / "worlds" / "club_world"
TOKEN = "test-admin-token-0123456789"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_admin_defaults_to_disabled_loopback():
    config = load_config()

    assert config.admin.enabled is False
    assert config.admin.host == "127.0.0.1"
    assert config.admin.port == 8766
    assert config.admin.token is None
    assert "token" not in repr(config.admin)


def test_admin_section_is_parsed(tmp_path):
    path = tmp_path / "mud.toml"
    path.write_text(
        '[admin]\nenabled = true\nhost = "0.0.0.0"\nport = 9000\n'
        'token = "from-file"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.admin == AdminConfig(
        enabled=True, host="0.0.0.0", port=9000, token="from-file")


def test_admin_token_environment_overrides_the_file(tmp_path, monkeypatch):
    path = tmp_path / "mud.toml"
    path.write_text(
        '[admin]\nenabled = true\ntoken = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("MUD_ADMIN_TOKEN", "from-environment")

    assert load_config(path).admin.token == "from-environment"


def test_admin_port_must_be_in_range(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[admin]\nport = 70000\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="port"):
        load_config(path)


@pytest.mark.asyncio
async def test_enabled_admin_without_token_refuses_to_start(tmp_path):
    engine = Engine()
    await load_world(engine, CLUB_WORLD)
    server = V2Server(
        engine,
        port=0,
        db_path=tmp_path / "mud.db",
        admin=AdminConfig(enabled=True, port=0, token=None),
        world_path=CLUB_WORLD,
    )

    with pytest.raises(AdminConsoleError, match="token"):
        await server.run()

    assert server.port == 0
    assert server.admin_server is None


# ---------------------------------------------------------------------------
# HTTP and websocket helpers
# ---------------------------------------------------------------------------


class Client:
    """A tiny v2 client with a type-filtered queue."""

    def __init__(self, uri: str):
        self.uri = uri
        self.ws = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.hello = None

    async def connect(self):
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

    async def register(self, name: str) -> str:
        await self.send({
            "v": 2, "type": "auth", "id": "register",
            "data": {"mode": "register", "name": name},
        })
        ok = await self.next("auth.ok")
        await self.next("room.state")
        return ok["data"]["key"]

    async def login_reply(self, key: str) -> dict[str, Any]:
        await self.send({
            "v": 2, "type": "auth", "id": "login",
            "data": {"mode": "login", "key": key},
        })
        return await self.next_any()

    async def login(self, key: str):
        reply = await self.login_reply(key)
        assert reply["type"] == "auth.ok", reply["data"]
        state = await self.next("room.state")
        return reply, state

    async def input(self, text: str, msg_id: int = 1) -> None:
        await self.send({
            "v": 2, "type": "input", "id": msg_id,
            "data": {"text": text},
        })

    async def next(self, type_: str, timeout: float = 4.0):
        while True:
            message = await asyncio.wait_for(self.q.get(), timeout)
            if message.get("type") == type_:
                return message

    async def next_any(self, timeout: float = 4.0):
        return await asyncio.wait_for(self.q.get(), timeout)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = TOKEN,
    body: Any = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """One blocking HTTP request, called through `asyncio.to_thread`."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = json.loads(raw) if raw else {}
        return exc.code, payload, dict(exc.headers)


async def _http(
    port: int, method: str, path: str, *, token: str | None = TOKEN,
    body: Any = None,
):
    return await asyncio.to_thread(
        _request, port, method, path, token=token, body=body)


def _raw_request(port: int, path: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


async def _raw(port: int, path: str):
    return await asyncio.to_thread(_raw_request, port, path)


async def _wait_ready(server: V2Server) -> None:
    for _ in range(300):
        if server.port and server.admin_port:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"server did not bind (mud={server.port}, admin={server.admin_port})")


@asynccontextmanager
async def admin_server(tmp_path: Path, *, token: str = TOKEN):
    world = tmp_path / "world"
    shutil.copytree(CLUB_WORLD, world)
    engine = Engine()
    await load_world(engine, world)
    server = V2Server(
        engine,
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "mud.db",
        auth_required=True,
        admin=AdminConfig(
            enabled=True, host="127.0.0.1", port=0, token=token),
        world_path=world,
    )
    task = asyncio.create_task(server.run())
    await _wait_ready(server)
    try:
        yield server, world
    finally:
        server.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _payload(world_doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "world": world_doc["world"],
        "layout": world_doc.get("layout", {}),
        "areas": world_doc["areas"],
    }


# ---------------------------------------------------------------------------
# Auth and read API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_requires_the_bearer_token(tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        missing = await _http(
            server.admin_port, "GET", "/api/status", token=None)
        wrong = await _http(
            server.admin_port, "GET", "/api/status", token="wrong")

        assert missing[0] == 401
        assert wrong[0] == 401
        assert missing[1]["error"]
        assert "WWW-Authenticate" in missing[2]


@pytest.mark.asyncio
async def test_static_login_is_public_and_assets_are_same_origin(tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        status, body, headers = await _raw(server.admin_port, "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        html = body.decode("utf-8")
        assert '<div id="app"></div>' in html
        assert TOKEN not in html
        assert "Content-Security-Policy" in headers

        match = re.search(r'src="(/assets/[^"]+\.js)"', html)
        assert match is not None
        asset_status, asset, asset_headers = await _raw(
            server.admin_port, match.group(1))
        assert asset_status == 200
        assert asset_headers["Content-Type"].startswith("text/javascript")
        assert len(asset) > 1000

        missing, _, _ = await _raw(
            server.admin_port, "/assets/does-not-exist.js")
        assert missing == 404

        # The unauthenticated static page never leaks into /api.
        api_status, api_body, _ = await _http(
            server.admin_port, "GET", "/api/world", token=None)
        assert api_status == 401
        assert "cover" not in json.dumps(api_body)


@pytest.mark.asyncio
async def test_status_reports_the_live_server(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        status = await _http(server.admin_port, "GET", "/api/status")

        assert status[0] == 200
        body = status[1]
        assert len(body["commit"]) == 40
        assert body["world"]["name"]
        assert body["world"]["epoch"]
        assert body["db_path"].endswith("mud.db")
        assert body["world_path"] == str(world)
        assert body["rooms"] == 34
        assert body["players"] == 0
        assert body["live_sessions"] == 0
        assert body["features"]["effects"] is False
        assert body["limits"]["command_burst"] >= 1


@pytest.mark.asyncio
async def test_players_lists_roster_and_live_session(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="src.mud")
    async with admin_server(tmp_path) as (server, _world):
        uri = f"ws://127.0.0.1:{server.port}"
        client = await Client(uri).connect()
        try:
            key = await client.register("Ada")
            status, body, _ = await _http(
                server.admin_port, "GET", "/api/players")
            assert status == 200
            assert body["live_sessions"] == 1
            ada = next(row for row in body["players"] if row["name"] == "Ada")
            assert ada["room_id"] == "gate"
            assert ada["room_name"] == "Village Gate"
            assert ada["asleep"] is False
            assert ada["key_count"] == 1
            assert ada["online"] is True
            assert key not in json.dumps(body)
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_rekey_shows_the_key_once_and_old_key_stops_working(
        tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="src.mud")
    async with admin_server(tmp_path) as (server, _world):
        uri = f"ws://127.0.0.1:{server.port}"
        first = await Client(uri).connect()
        old_key = await first.register("Ada")
        await first.close()

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/players/Ada/rekey",
            body={})
        assert status == 200
        new_key = body["key"]
        assert new_key.startswith("mud1-")
        assert new_key != old_key
        assert body["revoked"] == 1
        assert "shown once" in body["message"]

        stale = await Client(uri).connect()
        try:
            reply = await stale.login_reply(old_key)
            assert reply["type"] == "auth.error"
            assert reply["data"]["code"] == "invalid_key"
        finally:
            await stale.close()

        fresh = await Client(uri).connect()
        try:
            ok, state = await fresh.login(new_key)
            assert ok["data"]["player"]["name"] == "Ada"
            assert state["data"]["room"]["id"] == "gate"
        finally:
            await fresh.close()

        status, roster, _ = await _http(
            server.admin_port, "GET", "/api/players")
        assert status == 200
        row = next(item for item in roster["players"]
                   if item["name"] == "Ada")
        assert row["key_count"] == 1
        assert new_key not in caplog.text
        assert new_key not in json.dumps(roster)


@pytest.mark.asyncio
async def test_rekey_unknown_character_is_a_404(tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        status, body, _ = await _http(
            server.admin_port, "POST", "/api/players/Nobody/rekey",
            body={})

        assert status == 404
        assert "Nobody" in body["error"]


# ---------------------------------------------------------------------------
# World read, validate, save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_world_returns_metadata_areas_and_manifest(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        status, doc, _ = await _http(
            server.admin_port, "GET", "/api/world")

        assert status == 200
        assert doc["kind"] == "directory"
        assert doc["path"] == str(world)
        assert doc["world"]["name"] == "The Commons"
        assert doc["world"]["start_room"] == "gate"
        assert len(doc["areas"]) == 7
        assert doc["validation"] == {"ok": True, "errors": []}
        files = {entry["path"] for entry in doc["manifest"]["files"]}
        assert "world.yaml" in files
        assert "areas/starting_village.yaml" in files
        gate = next(
            room
            for area in doc["areas"]
            for room in area["rooms"]
            if room["id"] == "gate"
        )
        assert gate["geometry"]["cells"][0] == "###+###"


@pytest.mark.asyncio
async def test_world_validate_reports_model_errors(tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        _, doc, _ = await _http(server.admin_port, "GET", "/api/world")
        draft = _payload(doc)
        draft["areas"][0]["rooms"][0]["name"] = ""

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/validate", body=draft)

        assert status == 200
        assert body["ok"] is False
        assert any("name" in error for error in body["errors"])


@pytest.mark.asyncio
async def test_world_save_backs_up_and_writes_canonical_yaml(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        _, doc, _ = await _http(server.admin_port, "GET", "/api/world")
        draft = _payload(doc)
        area = next(
            area for area in draft["areas"]
            if area["file"] == "areas/starting_village.yaml")
        gate = next(room for room in area["rooms"] if room["id"] == "gate")
        gate["description"] = "Edited in the admin console."

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/save", body=draft)

        assert status == 200
        assert body["rooms"] == 34
        assert body["areas"] == 7
        assert Path(body["backup_dir"]).is_dir()
        assert Path(body["manifest"]).is_file()
        assert body["backups"]
        for saved in body["saved"]:
            assert Path(saved).is_file()

        saved_yaml = yaml.safe_load(
            (world / "areas" / "starting_village.yaml").read_text(
                encoding="utf-8"))
        saved_gate = next(
            room for room in saved_yaml["rooms"] if room["id"] == "gate")
        assert saved_gate["description"] == "Edited in the admin console."
        # Canonical output is still a valid world and the edit is visible.
        _, after, _ = await _http(server.admin_port, "GET", "/api/world")
        assert after["validation"]["ok"] is True
        gate_after = next(
            room
            for area in after["areas"]
            for room in area["rooms"]
            if room["id"] == "gate"
        )
        assert gate_after["description"] == "Edited in the admin console."


@pytest.mark.asyncio
async def test_bad_draft_does_not_replace_the_world(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        _, doc, _ = await _http(server.admin_port, "GET", "/api/world")
        draft = _payload(doc)
        draft["world"]["start_room"] = "does_not_exist"
        before = (world / "world.yaml").read_text(encoding="utf-8")

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/save", body=draft)

        assert status == 422
        assert body["errors"]
        assert (world / "world.yaml").read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_world_returns_an_empty_layout_when_none_exists(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        status, doc, _ = await _http(
            server.admin_port, "GET", "/api/world")

        assert status == 200
        assert doc["layout"] == {}
        assert not (world / "layout.json").exists()


@pytest.mark.asyncio
async def test_malformed_layout_is_ignored_and_sanitized(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        (world / "layout.json").write_text(
            json.dumps(
                {
                    "gate": {"x": "left", "y": 3},
                    "market": {"x": 5, "y": 6},
                    "unknown_room": {"x": 1, "y": 2},
                    "bad_point": 4,
                }
            ),
            encoding="utf-8",
        )

        status, doc, _ = await _http(
            server.admin_port, "GET", "/api/world")
        assert status == 200
        assert doc["layout"] == {"market": {"x": 5.0, "y": 6.0}}

        draft = _payload(doc)
        draft["layout"] = {
            "gate": {"x": "left", "y": 3},
            "market": {"x": 5, "y": 6},
            "unknown_room": {"x": 1, "y": 2},
            "south_road": [1, 2],
        }
        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/validate", body=draft)
        assert status == 200
        assert body == {"ok": True, "errors": []}

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/save", body=draft)
        assert status == 200
        assert body["layout"] == {"market": {"x": 5.0, "y": 6.0}}
        assert json.loads(
            (world / "layout.json").read_text(encoding="utf-8")
        ) == {"market": {"x": 5.0, "y": 6.0}}


@pytest.mark.asyncio
async def test_invalid_layout_json_is_an_empty_layout(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        (world / "layout.json").write_text("{not json", encoding="utf-8")

        status, doc, _ = await _http(
            server.admin_port, "GET", "/api/world")

        assert status == 200
        assert doc["layout"] == {}


@pytest.mark.asyncio
async def test_world_save_writes_layout_atomically_and_backs_it_up(tmp_path):
    async with admin_server(tmp_path) as (server, world):
        (world / "layout.json").write_text(
            json.dumps({"gate": {"x": 10, "y": 20}}),
            encoding="utf-8",
        )
        _, doc, _ = await _http(server.admin_port, "GET", "/api/world")
        assert doc["layout"] == {"gate": {"x": 10.0, "y": 20.0}}

        draft = _payload(doc)
        draft["layout"] = {
            "gate": {"x": 30, "y": 40},
            "market": {"x": -12.5, "y": 7},
        }
        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/save", body=draft)

        assert status == 200
        assert body["layout"] == {
            "gate": {"x": 30.0, "y": 40.0},
            "market": {"x": -12.5, "y": 7.0},
        }
        saved = json.loads(
            (world / "layout.json").read_text(encoding="utf-8"))
        assert saved == body["layout"]
        assert str(world / "layout.json") in body["saved"]
        # Layout is editor metadata, not YAML content.
        assert "layout" not in (
            world / "areas" / "starting_village.yaml"
        ).read_text(encoding="utf-8")

        backup = Path(body["backup_dir"])
        assert (backup / "layout.json").is_file()
        assert json.loads(
            (backup / "layout.json").read_text(encoding="utf-8")
        ) == {"gate": {"x": 10.0, "y": 20.0}}
        manifest = json.loads(
            (backup / "manifest.json").read_text(encoding="utf-8"))
        entries = {entry["path"]: entry for entry in manifest["files"]}
        assert "layout.json" in entries
        assert entries["layout.json"]["bytes"] > 0

        _, refreshed, _ = await _http(
            server.admin_port, "GET", "/api/world")
        assert refreshed["layout"] == body["layout"]


# ---------------------------------------------------------------------------
# Apply: close 1001, swap the engine, login restores the character
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_closes_clients_swaps_engine_and_restores_character(
        tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        old_engine = server.engine
        old_epoch = server.epoch
        uri = f"ws://127.0.0.1:{server.port}"

        client = await Client(uri).connect()
        key = await client.register("Ada")
        await client.input("go north", 2)
        market = await client.next("room.state")
        assert market["data"]["room"]["id"] == "market"
        await client.input("take gold coin", 3)
        taken = await client.next("object.taken")
        assert taken["data"]["object"]["id"] == "gold_coin"

        # Players online: no force means no reload and no close.
        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/apply", body={})
        assert status == 409
        assert body["requires_force"] is True
        assert body["live_players"]

        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/apply",
            body={"force": True})

        assert status == 200
        assert body["applied"] is True
        assert body["closed"] >= 1
        assert body["world"]["name"] == "The Commons"
        assert body["world"]["epoch"] != old_epoch
        await asyncio.wait_for(client.ws.wait_closed(), timeout=5.0)
        assert client.ws.close_code == 1001
        assert client.ws.close_reason == "World reload"
        await client.close()

        assert server.engine is not old_engine
        assert server.world_name == "The Commons"
        assert server.epoch != old_epoch

        # Characters live in SQLite: login restores the room and inventory.
        fresh = await Client(uri).connect()
        try:
            ok, state = await fresh.login(key)
            assert ok["data"]["player"]["name"] == "Ada"
            assert state["data"]["room"]["id"] == "market"
            await fresh.input("inventory", 2)
            inventory = await fresh.next("player.inventory")
            ids = {item["id"] for item in inventory["data"]["items"]}
            assert "gold_coin" in ids
        finally:
            await fresh.close()


@pytest.mark.asyncio
async def test_apply_without_players_needs_no_force(tmp_path):
    async with admin_server(tmp_path) as (server, _world):
        status, body, _ = await _http(
            server.admin_port, "POST", "/api/world/apply", body={})

        assert status == 200
        assert body["applied"] is True
        assert body["closed"] == 0
