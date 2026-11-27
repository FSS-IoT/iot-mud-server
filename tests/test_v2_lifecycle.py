"""Ticket #136: one awaitable v2 lifecycle and one protocol harness.

The lifecycle is the seam the rest of the v2 tests were re-implementing:

* `start()` binds an ephemeral endpoint and returns it without blocking, so a
  test never polls `server.port`;
* `close()`/`aclose()` stop accepting, close every client, and return only
  after the connection handlers have finished their cleanup;
* `run()` remains the CLI wrapper (`start()`, wait for `stop()`, `close()`).

The harness test drives the same server through observable client queries —
`state(actor)`, `who(actor)`, and `events(actor)` — instead of reading
`_sessions` or reaching into the engine for a lifecycle outcome.

Nothing here binds the product ports. Every server is `port=0` with a
temporary database; the last test keeps the v2 helpers that way. The v1
fixed-8765 tests are listed as out of scope here because #137 deletes them.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_world
from tests.golden.harness import (
    GoldenClient,
    golden_client,
    golden_server,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLD_PATH = REPO_ROOT / "data" / "worlds" / "starting_village.yaml"

# The v2 helpers this ticket owns. Each one must stay on port 0; the v1
# fixed-8765 files and tests/test_server_terminal.py are deliberately absent
# because #137 removes them.
V2_HELPER_PATHS = (
    "tests/golden/harness.py",
    "tests/test_v2_lifecycle.py",
    "tests/test_protocol_v2.py",
    "tests/test_limits.py",
    "tests/test_sleep_wake.py",
    "tests/test_social.py",
    "tests/test_effects.py",
    "tests/test_character_persistence.py",
    "tools/load_smoke.py",
)


async def _engine() -> Engine:
    engine = Engine()
    await load_world(engine, WORLD_PATH)
    return engine


@pytest.mark.asyncio
async def test_start_returns_a_bound_endpoint_without_polling(tmp_path):
    server = V2Server(
        await _engine(), port=0, db_path=tmp_path / "start.db",
        auth_required=True,
    )

    port = await server.start()
    try:
        # The return value is the bound endpoint, not the configured 0.
        assert isinstance(port, int)
        assert port > 0
        assert server.port == port
        assert server._server is not None
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_close_awaits_quiescence(tmp_path):
    server = V2Server(
        await _engine(), port=0, db_path=tmp_path / "close.db",
        auth_required=True,
    )
    port = await server.start()
    client = await GoldenClient(f"ws://localhost:{port}").connect()
    try:
        await client.register("Ada")
        assert server.sessions.active == 1

        await server.close()

        # The handler's cleanup (sleep + room.leave + persistence) finished
        # before close returned, and every owned resource is released.
        assert server.sessions.is_quiescent
        assert server.sessions.active == 0
        assert server._idle_task is None
        assert server._wander_task is None
        assert server._server is None
        assert server.db is None
        assert server.store is None
        # The client was closed by the server, not left reading a dead socket.
        assert await client.close_code(timeout=2.0) == 1001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_run_remains_a_start_and_stop_wrapper(tmp_path):
    server = V2Server(
        await _engine(), port=0, db_path=tmp_path / "run.db",
        auth_required=True,
    )

    task = asyncio.create_task(server.run())
    # Stop before the first await in run() is reached: the wrapper must still
    # start, observe the stop, and close without a liveness race.
    server.stop()
    await asyncio.wait_for(task, timeout=5.0)

    assert server._server is None
    assert server.db is None


@pytest.mark.asyncio
async def test_harness_reports_observable_state_who_and_events(tmp_path):
    async with golden_server(
        tmp_path / "harness",
    ) as server, golden_client(server) as ada:
        await ada.register("Ada")

        state = await server.state("Ada")
        assert state["room"]["id"] == "gate"

        roster = await server.who("Ada")
        players = {row["name"]: row for row in roster["players"]}
        assert players["Ada"] == {
            "name": "Ada", "room_id": "gate",
            "asleep": False, "stone": False,
        }

        types = {message["type"] for message in server.events("Ada")}
        assert {"auth.ok", "room.state"} <= types


def test_v2_helpers_do_not_bind_the_product_ports():
    """No helper added by this ticket may claim 8765 or the dev admin port."""
    fixed_bind = re.compile(r"port\s*=\s*(8765|8771)\b")
    fixed_uri = re.compile(r"ws://[^\"']*:(8765|8771)\b")
    for relative in V2_HELPER_PATHS:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert not fixed_bind.search(source), (
            f"{relative} binds a fixed product port")
        assert not fixed_uri.search(source), (
            f"{relative} points at a fixed product port")
