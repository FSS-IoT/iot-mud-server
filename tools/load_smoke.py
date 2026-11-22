"""Load-smoke harness for the v2 server (ticket #86).

Twenty clients, each sending a script of valid commands over a real websocket
to a real `V2Server` on an ephemeral port, with a temporary SQLite database and
key registration. The measured quantity is per-command latency: monotonic time
from `send` to the direct reply that echoes the request id, plus a protocol
check on every server message.

The transport and the server are in one process, so the run also exercises the
engine, SQLite write-through, room-state rendering, and broadcast fan-out under
20-way concurrency. It is a smoke test, not a benchmark: the pass gate is zero
protocol errors and p95 <= 100 ms (SPEC v2.1.4 criterion 2).

Run it directly for a report:

    uv run python -m tools.load_smoke --report load-report.json

The command budget is injected high (`load_limits()`). The production default
is 10 commands/s with a burst of 20 (#85); an unpaced 200-command burst would
spend the burst and then fail this test's zero-protocol-error gate with
`rate_limited`. That limiter has its own tests in `tests/test_limits.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

from src.mud.config import LimitsConfig
from src.mud.engine.core import Engine
from src.mud.net.v2_server import V2Server
from src.mud.world.loader import load_world

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORLD = REPO_ROOT / "data" / "worlds" / "starting_village.yaml"
DEFAULT_CLIENTS = 20
DEFAULT_COMMANDS = 200
P95_BUDGET_MS = 100.0
COMMAND_TIMEOUT_SECONDS = 10.0
DEFAULT_RUN_TIMEOUT_SECONDS = 120.0
# A deterministic hello value: the harness is not testing commit discovery.
LOAD_SERVER_COMMIT = "0" * 40
# The server's own hello carries the bundle name; keep the check in one place.
SERVER_NAME = "iot-mud-server"

ERROR_TYPES = frozenset({"error", "auth.error"})

# A ten-command cycle that stays valid from any prefix: the character starts at
# the gate, moves to the market at command 5, and is back at the gate before
# `read sign`. `look`, `who`, and `inventory` always succeed; `say` and the two
# moves add broadcast fan-out. Repeating the cycle keeps every command valid
# while exercising room-state rendering in both rooms.
_COMMAND_CYCLE = (
    "look",
    "who",
    "inventory",
    "examine sign",
    "go north",
    "look",
    "say load smoke",
    "go south",
    "read sign",
    "inventory",
)


class ProtocolViolation(Exception):
    """A server message broke the v2 envelope or request pairing."""


def command_script(count: int) -> list[str]:
    """`count` valid commands, cycling through the mixed script above."""
    if count < 0:
        raise ValueError("command count must not be negative")
    return [_COMMAND_CYCLE[index % len(_COMMAND_CYCLE)]
            for index in range(count)]


def load_limits() -> LimitsConfig:
    """A command budget high enough that #85's limiter never fires.

    The load criterion measures transport, engine, and persistence latency.
    The default bucket (10/s, burst 20) would answer most of an unpaced burst
    with `rate_limited` and make the zero-error gate fail for an unrelated
    reason. The idle timeout stays at its production default.
    """
    return LimitsConfig(
        commands_per_second=1000.0,
        command_burst=1000,
        idle_timeout_seconds=900.0,
    )


def percentile(samples: list[float] | tuple[float, ...], p: float) -> float:
    """Linear-interpolated percentile, with `p` in (0, 100]."""
    if not samples:
        raise ValueError("percentile of an empty sample")
    if not 0.0 < p <= 100.0:
        raise ValueError("percentile must be in (0, 100]")
    values = sorted(samples)
    rank = (len(values) - 1) * (p / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    weight = rank - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def p95_within_budget(
        samples: list[float] | tuple[float, ...],
        budget_ms: float = P95_BUDGET_MS) -> bool:
    """True when the sample's p95 is at most `budget_ms`; empty is not a pass."""
    if not samples:
        return False
    return percentile(samples, 95.0) <= budget_ms


@dataclass
class LoadClient:
    """One in-process client with a background reader and exact pairing.

    The reader validates every server message, resolves a direct reply's future
    by request id, and consumes broadcasts. Draining the socket matters: a
    client that read only its own replies would leave thousands of unread
    `say`/movement broadcasts to push back on the server, and the next reply
    would be measured behind that self-inflicted backlog.
    """

    uri: str
    name: str
    script: list[str]
    ws: Any = None
    hello: dict | None = None
    latencies_ms: list[float] = field(default_factory=list)
    broadcasts: int = 0
    sent: int = 0
    replied: int = 0
    errors: list[str] = field(default_factory=list)
    _next_id: int = 0
    _next_seq: int = 1
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _room_state_seen: asyncio.Event = field(default_factory=asyncio.Event)
    _reader_task: asyncio.Task | None = None
    _broken: Exception | None = None
    _closing: bool = False

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def connect(self) -> None:
        self.ws = await websockets.connect(
            self.uri, max_size=1 << 20, open_timeout=COMMAND_TIMEOUT_SECONDS)
        hello = await self._recv_json(COMMAND_TIMEOUT_SECONDS)
        if hello["type"] != "hello":
            raise ProtocolViolation(
                f"first message was {hello['type']!r}, expected 'hello'")
        self.hello = hello
        data = hello["data"]
        if data.get("server") != SERVER_NAME:
            raise ProtocolViolation(f"unexpected server {data.get('server')!r}")
        if 2 not in data.get("protocol", []):
            raise ProtocolViolation(
                f"server does not speak protocol 2: {data.get('protocol')!r}")
        if data.get("auth_required") is not True:
            # The product path is auth-first (SPEC v2.1.1); a load run that
            # silently fell back to guests would not exercise registration.
            raise ProtocolViolation("hello did not require authentication")
        self._reader_task = asyncio.create_task(self._reader())

    async def register(self) -> None:
        auth_id = self._new_id()
        reply_future = self._expect(auth_id)
        await self.ws.send(json.dumps({
            "v": 2, "type": "auth", "id": auth_id,
            "data": {"mode": "register", "name": self.name},
        }))
        try:
            reply = await asyncio.wait_for(
                reply_future, COMMAND_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                self._room_state_seen.wait(), COMMAND_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ProtocolViolation(
                f"registration for {self.name} timed out") from None
        if reply["type"] != "auth.ok":
            raise ProtocolViolation(
                f"registration for {self.name} returned {reply['type']!r}")

    async def run(self, gate: asyncio.Event) -> None:
        await gate.wait()
        for text in self.script:
            request_id = self._new_id()
            reply_future = self._expect(request_id)
            payload = {
                "v": 2, "type": "input", "id": request_id,
                "data": {"text": text},
            }
            started = time.perf_counter()
            await self.ws.send(json.dumps(payload))
            self.sent += 1
            try:
                await asyncio.wait_for(
                    reply_future, COMMAND_TIMEOUT_SECONDS)
            except TimeoutError:
                self.errors.append(
                    f"timeout after {COMMAND_TIMEOUT_SECONDS:g}s on {text!r}")
                raise
            self.replied += 1
            self.latencies_ms.append(
                (time.perf_counter() - started) * 1000.0)

    async def close(self) -> None:
        self._closing = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except websockets.exceptions.ConnectionClosed:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

    def _expect(self, request_id: int) -> asyncio.Future:
        if self._broken is not None:
            raise self._broken
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    async def _reader(self) -> None:
        try:
            while True:
                message = await self._recv_json()
                if message["type"] in ERROR_TYPES:
                    self._record_error(message)
                if message["type"] == "room.state" and "id" not in message:
                    self._room_state_seen.set()
                if "id" in message:
                    self._resolve(message)
                else:
                    self.broadcasts += 1
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            if not self._closing:
                self._fail(
                    ProtocolViolation(f"server closed the connection: {exc}"))
        except Exception as exc:  # noqa: BLE001 - reported in the result
            self._fail(exc)

    def _resolve(self, message: dict) -> None:
        request_id = message["id"]
        future = self._pending.pop(request_id, None)
        if future is None:
            self._fail(ProtocolViolation(
                f"direct reply id={request_id!r} has no pending request"))
            return
        if not future.done():
            future.set_result(message)

    def _fail(self, exc: Exception) -> None:
        if self._broken is None:
            self._broken = exc
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    async def _recv_json(self, timeout: float | None = None) -> dict:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProtocolViolation(
                f"server sent non-JSON: {raw[:80]!r}") from exc
        self._check_envelope(message)
        return message

    def _check_envelope(self, message: object) -> None:
        if not isinstance(message, dict):
            raise ProtocolViolation(
                f"server message is {type(message).__name__}, not an object")
        if message.get("v") != 2:
            raise ProtocolViolation(
                f"server message has v={message.get('v')!r}")
        type_ = message.get("type")
        if not isinstance(type_, str) or not type_:
            raise ProtocolViolation(f"server message has type={type_!r}")
        data = message.get("data")
        if not isinstance(data, dict):
            raise ProtocolViolation(
                f"{type_} has data={type(data).__name__}, not an object")
        seq = message.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ProtocolViolation(f"{type_} has seq={seq!r}")
        if seq != self._next_seq:
            raise ProtocolViolation(
                f"{type_} has seq={seq}, expected {self._next_seq}")
        self._next_seq += 1

    def _record_error(self, message: dict) -> None:
        data = message["data"]
        code = data.get("code")
        text = data.get("text") or data.get("message") or ""
        self.errors.append(f"{message['type']} {code}: {text}".strip())


@dataclass(frozen=True)
class LoadResult:
    """One completed load run and its latency distribution."""

    clients: int
    commands_per_client: int
    latencies_ms: list[float]
    per_client_counts: list[int]
    protocol_errors: list[str]
    commands_sent: int
    commands_replied: int
    broadcasts: int
    wall_seconds: float

    @property
    def p50_ms(self) -> float:
        return percentile(self.latencies_ms, 50.0) if self.latencies_ms else 0.0

    @property
    def p95_ms(self) -> float:
        return percentile(self.latencies_ms, 95.0) if self.latencies_ms else 0.0

    @property
    def p99_ms(self) -> float:
        return percentile(self.latencies_ms, 99.0) if self.latencies_ms else 0.0

    @property
    def max_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def command_rate(self) -> float:
        return self.commands_replied / self.wall_seconds \
            if self.wall_seconds > 0 else 0.0

    def gate_failures(self, budget_ms: float = P95_BUDGET_MS) -> list[str]:
        expected = self.clients * self.commands_per_client
        failures = []
        if self.commands_sent != expected:
            failures.append(f"sent {self.commands_sent}, expected {expected}")
        if self.commands_replied != expected:
            failures.append(
                f"replied {self.commands_replied}, expected {expected}")
        if self.per_client_counts != [self.commands_per_client] * self.clients:
            failures.append(
                f"per-client replies were {self.per_client_counts}")
        if self.protocol_errors:
            failures.append(
                f"{len(self.protocol_errors)} protocol error(s)")
        if not self.latencies_ms:
            failures.append("no latency samples")
        elif self.p95_ms > budget_ms:
            failures.append(
                f"p95 {self.p95_ms:.3f} ms exceeds {budget_ms:.3f} ms")
        return failures

    def as_dict(self, budget_ms: float = P95_BUDGET_MS) -> dict[str, Any]:
        return {
            "clients": self.clients,
            "commands_per_client": self.commands_per_client,
            "commands_sent": self.commands_sent,
            "commands_replied": self.commands_replied,
            "broadcasts": self.broadcasts,
            "wall_seconds": round(self.wall_seconds, 6),
            "commands_per_second": round(self.command_rate, 3),
            "latency_ms": {
                "p50": round(self.p50_ms, 6),
                "p95": round(self.p95_ms, 6),
                "p99": round(self.p99_ms, 6),
                "max": round(self.max_ms, 6),
                "samples": len(self.latencies_ms),
            },
            "protocol_errors": self.protocol_errors,
            "budget_ms": budget_ms,
            "passed": not self.gate_failures(budget_ms),
        }


def format_report(result: LoadResult,
                  budget_ms: float = P95_BUDGET_MS) -> str:
    """A compact multi-line report for pytest, the CLI, and CI summaries."""
    failures = result.gate_failures(budget_ms)
    verdict = "PASS" if not failures else "FAIL"
    lines = [
        (f"load smoke: {result.clients} clients x "
         f"{result.commands_per_client} commands"),
        (f"commands: {result.commands_sent} sent, "
         f"{result.commands_replied} replied in {result.wall_seconds:.3f}s "
         f"({result.command_rate:.0f}/s)"),
        (f"broadcasts read: {result.broadcasts}"),
        (f"latency ms: p50={result.p50_ms:.3f} p95={result.p95_ms:.3f} "
         f"p99={result.p99_ms:.3f} max={result.max_ms:.3f} "
         f"(n={len(result.latencies_ms)})"),
        f"protocol errors: {len(result.protocol_errors)}",
    ]
    for error in result.protocol_errors[:10]:
        lines.append(f"  - {error}")
    if len(result.protocol_errors) > 10:
        lines.append(f"  ... {len(result.protocol_errors) - 10} more")
    lines.append(
        f"gate: {verdict} (zero protocol errors; p95 <= {budget_ms:g} ms)")
    for failure in failures:
        lines.append(f"  ! {failure}")
    return "\n".join(lines)


def write_report(result: LoadResult, path: str | Path,
                 budget_ms: float = P95_BUDGET_MS) -> None:
    """Write the run as JSON, creating the parent directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.as_dict(budget_ms), indent=2) + "\n",
        encoding="utf-8")


async def _wait_for_bind(server: V2Server, task: asyncio.Task,
                         attempts: int = 500) -> None:
    for _ in range(attempts):
        if server.port:
            return
        if task.done():
            await task  # Surface a startup failure instead of timing out.
        await asyncio.sleep(0.01)
    raise RuntimeError("v2 server did not bind an ephemeral port")


async def _close_all(clients: list[LoadClient]) -> None:
    """Close clients one at a time, letting the server's cleanup keep up.

    Closing all of them at once races the server's `room.leave` cleanup
    against sockets that are already gone. That is harmless server logging,
    but it makes a load report noisy, so give each handler a tick.
    """
    for client in clients:
        await client.close()
        await asyncio.sleep(0.01)


def client_names(count: int, prefix: str = "Load") -> list[str]:
    """`Load01`..`LoadNN`, valid per the 2-24 character name rule."""
    return [f"{prefix}{index:02d}" for index in range(1, count + 1)]


async def run_scenario(
        uri: str,
        *,
        clients: int = DEFAULT_CLIENTS,
        commands: int = DEFAULT_COMMANDS,
        prefix: str = "Load") -> LoadResult:
    """Connect, register, and run the command loop against a live server."""
    if clients < 1:
        raise ValueError("clients must be positive")
    script = command_script(commands)
    load_clients = [
        LoadClient(uri, name, script)
        for name in client_names(clients, prefix)
    ]

    try:
        await asyncio.gather(*(client.connect() for client in load_clients))
        await asyncio.gather(*(client.register() for client in load_clients))
    except BaseException:
        await _close_all(load_clients)
        raise

    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(client.run(gate)) for client in load_clients
    ]
    gate.set()
    started = time.perf_counter()
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        wall_seconds = time.perf_counter() - started
        await _close_all(load_clients)

    for client, outcome in zip(load_clients, outcomes):
        if isinstance(outcome, BaseException):
            client.errors.append(
                f"{type(outcome).__name__}: {outcome}")

    latencies: list[float] = []
    for client in load_clients:
        latencies.extend(client.latencies_ms)
    errors = [
        f"{client.name}: {error}"
        for client in load_clients
        for error in client.errors
    ]
    return LoadResult(
        clients=clients,
        commands_per_client=commands,
        latencies_ms=latencies,
        per_client_counts=[client.replied for client in load_clients],
        protocol_errors=errors,
        commands_sent=sum(client.sent for client in load_clients),
        commands_replied=sum(client.replied for client in load_clients),
        broadcasts=sum(client.broadcasts for client in load_clients),
        wall_seconds=wall_seconds,
    )


async def run_load(
        *,
        db_path: str | Path,
        world_path: str | Path = DEFAULT_WORLD,
        clients: int = DEFAULT_CLIENTS,
        commands: int = DEFAULT_COMMANDS,
        limits: LimitsConfig | None = None,
        timeout: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        prefix: str = "Load") -> LoadResult:
    """Boot the real server on port 0 and run one load scenario."""
    engine = Engine()
    await load_world(engine, world_path)
    server = V2Server(
        engine,
        host="localhost",
        port=0,
        db_path=db_path,
        auth_required=True,
        limits=limits if limits is not None else load_limits(),
        server_commit=LOAD_SERVER_COMMIT,
        # The idle sweep is #85's behavior, not this criterion; keep it out of
        # the timed window instead of racing a once-a-second callback.
        idle_poll_seconds=3600.0,
    )
    task = asyncio.create_task(server.run())
    try:
        await _wait_for_bind(server, task)
        return await asyncio.wait_for(
            run_scenario(
                f"ws://localhost:{server.port}",
                clients=clients, commands=commands, prefix=prefix),
            timeout=timeout,
        )
    finally:
        server.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the v2 load smoke in-process and print metrics")
    parser.add_argument("--clients", type=int, default=DEFAULT_CLIENTS)
    parser.add_argument("--commands", type=int, default=DEFAULT_COMMANDS)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="database path (default: a temporary file)")
    parser.add_argument(
        "--world", type=Path, default=DEFAULT_WORLD,
        help="world file (default: data/worlds/starting_village.yaml)")
    parser.add_argument(
        "--budget-ms", type=float, default=P95_BUDGET_MS,
        help="p95 latency budget in milliseconds")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_RUN_TIMEOUT_SECONDS,
        help="whole-run timeout in seconds")
    parser.add_argument(
        "--report", type=Path, default=None,
        help="write the JSON metrics to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="mud-load-") as tmp:
        db_path = args.db if args.db is not None else Path(tmp) / "load.db"
        result = asyncio.run(run_load(
            db_path=db_path,
            world_path=args.world,
            clients=args.clients,
            commands=args.commands,
            timeout=args.timeout,
        ))
    print(format_report(result, args.budget_ms))
    if args.report is not None:
        write_report(result, args.report, args.budget_ms)
    return 0 if not result.gate_failures(args.budget_ms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
