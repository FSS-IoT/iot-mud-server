"""Ticket #86: load smoke — 20 clients x 200 commands.

SPEC v2.1.4 criterion 2:

* 20 in-process clients, each sending 200 commands, complete with zero protocol
  errors and a command latency p95 of at most 100 ms.
* The run is a real `V2Server` on an ephemeral port with a temporary SQLite
  database and key registration, not a mock transport.
* The suite is marked `load` so the PR `test` job can exclude it; the nightly
  workflow runs `uv run pytest -m load`.

The command budget is injected high on purpose. The default `[limits]` bucket
(10 commands/s, burst 20) would throttle an unpaced 200-command burst and the
run would fail the zero-protocol-error gate with `rate_limited`, which measures
#85 rather than this criterion. The red evidence for this test was exactly that
failure before the injection; `tests/test_limits.py` owns the limiter itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.load_smoke import (
    DEFAULT_CLIENTS,
    DEFAULT_COMMANDS,
    P95_BUDGET_MS,
    format_report,
    load_limits,
    p95_within_budget,
    percentile,
    run_load,
    write_report,
)

LOAD_TIMEOUT_SECONDS = 180.0

pytestmark = pytest.mark.load


def _append_step_summary(path: str, report: str) -> None:
    """Sync helper so the async test does not block on file I/O (ASYNC230)."""
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(report + "\n")


def test_p95_budget_rejects_a_slow_tail():
    """The shared gate is a real threshold, not an unconditional pass.

    1890 fast samples and 110 slow ones is a 5.5% tail: the 95th percentile
    lands in the slow group and must be over budget.
    """
    samples = [0.5] * 1890 + [P95_BUDGET_MS + 1.0] * 110

    assert len(samples) == 2000
    assert percentile(samples, 95) > P95_BUDGET_MS
    assert p95_within_budget(samples) is False
    assert p95_within_budget([0.5] * 2000) is True


@pytest.mark.asyncio
async def test_20_clients_200_commands_zero_protocol_errors_and_p95(tmp_path):
    """SPEC v2.1.4 criterion 2 against the real server and a temp database."""
    result = await run_load(
        db_path=tmp_path / "load.db",
        clients=DEFAULT_CLIENTS,
        commands=DEFAULT_COMMANDS,
        limits=load_limits(),
        timeout=LOAD_TIMEOUT_SECONDS,
    )
    report = format_report(result)
    print(report)
    if path := os.environ.get("MUD_LOAD_REPORT"):
        write_report(result, path)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        _append_step_summary(summary, report)

    assert result.commands_sent == DEFAULT_CLIENTS * DEFAULT_COMMANDS
    assert result.commands_replied == DEFAULT_CLIENTS * DEFAULT_COMMANDS
    assert len(result.latencies_ms) == DEFAULT_CLIENTS * DEFAULT_COMMANDS
    assert result.per_client_counts == [DEFAULT_COMMANDS] * DEFAULT_CLIENTS
    # The mixed script includes say and movement; if no broadcast arrived, the
    # run did not exercise fan-out and the latency number is not the one this
    # criterion asks for.
    assert result.broadcasts > 0
    assert result.protocol_errors == []
    assert result.p95_ms <= P95_BUDGET_MS
