"""Ticket #36: the minimal reference client."""

import asyncio
import builtins
import io
import queue as _queue
import time
from contextlib import redirect_stdout

import pytest
import websockets

from examples.reference_client import client_handler, render_message
from tools.mock_v2_server import MockServer


def _wait_for(out, needle: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in out.getvalue():
            return True
        time.sleep(0.005)
    return False


def _room_state():
    return {
        "v": 2, "type": "room.state", "event_id": "e", "seq": 1,
        "data": {
            "room": {
                "id": "gate", "name": "Village Gate",
                "description": "A heavy stone gate stands tall.",
                "dark": False,
                "exits": [{"direction": "north", "to": "market",
                           "locked": False}],
                "objects": [{"id": "sign", "kind": "sign",
                             "name": "Village Sign", "portable": True}],
                "players": [{"name": "Ben"}],
                "npcs": [],
            },
            "text": "Village Gate\nA heavy stone gate stands tall.",
        },
    }


def test_render_room_state():
    out = render_message(_room_state(), "Ada")

    assert "Village Gate" in out
    assert "A heavy stone gate stands tall." in out
    assert "Here you see: Village Sign." in out
    assert "Others in the room: Ben." in out
    assert "North" in out


def test_render_error():
    out = render_message({
        "v": 2, "type": "error", "data": {
            "code": "locked", "message": "The Chest is locked.",
            "text": "The Chest is locked.",
        },
    }, "Ada")

    assert "locked" in out.lower()


def test_render_object_lit():
    out = render_message({
        "v": 2, "type": "object.lit", "data": {
            "text": "You light the Torch.", "self": True,
        },
    }, "Ada")

    assert "light" in out.lower()


def test_render_player_said():
    out = render_message({
        "v": 2, "type": "player.said", "data": {
            "from": "Ben", "text": 'Ben says, "hello".', "self": False,
        },
    }, "Ada")

    assert 'Ben says, "hello".' in out


def test_unknown_event_is_ignored():
    assert render_message({"v": 2, "type": "mystery", "data": {}}, "Ada") == ""


@pytest.mark.asyncio
async def test_reference_client_against_mock():
    mock = MockServer()
    server = await websockets.serve(mock.handler, "localhost", 0)
    port = server.sockets[0].getsockname()[1]

    lines = _queue.Queue()
    for line in ("look", "go north", "say hello", "quit"):
        lines.put(line)

    out = io.StringIO()
    waited = [False]

    def scripted_input(prompt=""):
        if not waited[0]:
            _wait_for(out, "Village Gate")
            waited[0] = True
        return lines.get()

    original_input = builtins.input
    builtins.input = scripted_input
    try:
        with redirect_stdout(out):
            await asyncio.wait_for(
                client_handler(f"ws://localhost:{port}", "Ada"), timeout=5.0)
    finally:
        builtins.input = original_input
        server.close()
        await server.wait_closed()

    text = out.getvalue()
    assert "Village Gate" in text
    assert "Marketplace" in text
    assert 'You say, "hello"' in text
    assert "Goodbye!" in text
