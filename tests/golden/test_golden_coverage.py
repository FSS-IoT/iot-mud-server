"""#83 red: every documented event and every area needs a golden scenario.

The test parses `docs/PROTOCOL.md` and the product world. It runs the whole
golden catalogue against fresh servers and fails when:

* a documented, client-delivered event type never appears in a client log;
* a room or area of the 34-room world is never visited;
* a scenario silently stops delivering something it used to;
* a documented-but-undelivered event is observed (the exception is stale).

The list of exceptions lives in `tests/golden/events.py` with a reason per
entry, so the only way past a new protocol row is a scenario or a recorded
decision.
"""

from __future__ import annotations

import pytest

from tests.golden import scenarios
from tests.golden.events import (
    NOT_CLIENT_DELIVERED,
    delivered_events,
    parse_protocol_events,
)
from tests.golden.harness import ALL_AREAS, ALL_ROOMS


async def _run_all(tmp_path):
    runs = {}
    for name, scenario in scenarios.SCENARIOS.items():
        runs[name] = await scenario(tmp_path / "runs" / name)
    return runs


@pytest.mark.asyncio
async def test_every_documented_client_event_has_a_scenario(tmp_path):
    runs = await _run_all(tmp_path)
    assert runs, "the golden catalogue is empty"

    documented = parse_protocol_events()
    # A single-word event (`effect`, #84) is easy for a table parser to miss;
    # pin the names that are not dotted so a regex change cannot hide one.
    assert {
        "hello", "error", "notice", "shutdown", "effect",
    } <= documented
    covered = set().union(*(run.events for run in runs.values()))
    missing = delivered_events(documented) - covered

    assert missing == set(), (
        "PROTOCOL.md documents client events no golden scenario delivers: "
        f"{sorted(missing)}. Add a scenario step, or record why the event is "
        "not client-delivered in tests/golden/events.py."
    )

    # An exception is a promise that the event is invisible to clients. If a
    # scenario starts seeing it, the exception (or the server behavior) is
    # stale.
    observed_exceptions = covered & set(NOT_CLIENT_DELIVERED)
    assert observed_exceptions == set(), (
        "these events are listed as not client-delivered but a scenario saw "
        f"them: {sorted(observed_exceptions)}"
    )


@pytest.mark.asyncio
async def test_every_room_and_area_has_a_golden_path(tmp_path):
    runs = await _run_all(tmp_path)
    covered_rooms = set().union(*(run.rooms for run in runs.values()))
    covered_areas = set().union(*(run.areas for run in runs.values()))

    assert ALL_ROOMS - covered_rooms == set(), (
        "golden scenarios never visit these rooms: "
        f"{sorted(ALL_ROOMS - covered_rooms)}"
    )
    assert ALL_AREAS - covered_areas == set(), (
        "golden scenarios never visit these areas: "
        f"{sorted(ALL_AREAS - covered_areas)}"
    )
    assert covered_rooms <= ALL_ROOMS
