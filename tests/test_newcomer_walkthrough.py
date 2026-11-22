"""Ticket #90: a newcomer connects from `BOOTSTRAP.md` alone.

The walkthrough is a user path, not an implementation detail, so it is tested
the way a newcomer runs it:

1. the three onboarding pages name the same scratch database, port, and URI;
2. the documented `admin seed` and `v2_server` commands start a real server in
   a subprocess (the same `python -m` commands behind `uv run python -m`);
3. `examples/reference_client.py --save-key` registers a character, writes the
   key to a `0600` file, and `--key-file` logs in without printing it again;
4. `docs/SESSION.md` runs end to end against that server.

Nothing writes into the checkout and nothing binds 8765; the database is under
`tmp_path` and the port is chosen from the kernel's free range.
"""

from __future__ import annotations

import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.golden.session import run_acceptance_session
from tests.test_docs_examples import parse_fences

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = "BOOTSTRAP.md"
CLIENTS = "docs/CLIENTS.md"
SESSION = "docs/SESSION.md"
REFERENCE_CLIENT = "examples/reference_client.py"

# The onboarding pages must agree on one copy-pasteable scratch setup. These
# are the strings the test asserts and the pages show. The development server
# and the acceptance server use different files: `admin seed --reset` keeps
# characters, so reseeding the development database is not a fresh acceptance.
WALKTHROUGH_DB = ".scratch/session.db"
DEV_DB = ".scratch/dev.db"
WALKTHROUGH_PORT = "8766"
WALKTHROUGH_URI = f"ws://localhost:{WALKTHROUGH_PORT}"

DOCS = (BOOTSTRAP, CLIENTS, SESSION)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(
                f"server exited before binding port {port}:\n{output}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    process.kill()
    raise AssertionError(f"server did not bind port {port}")


def _documented_limits() -> str:
    """The exact `[limits]` block SESSION.md tells an automated client to use."""
    for fence in parse_fences(SESSION):
        if fence.language == "toml" and "[limits]" in fence.text:
            return fence.text
    raise AssertionError(
        "SESSION.md must show the [limits] config for an automated client"
    )


@contextmanager
def documented_server(tmp_path: Path, *, scripted: bool = False) -> Iterator[int]:
    """Seed and start the server with the commands the docs give."""
    db = tmp_path / "session.db"
    seed = subprocess.run(
        [sys.executable, "-m", "src.mud.admin", "seed", "--db", str(db)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    assert "seeded" in seed.stdout

    port = _free_port()
    arguments = [
        sys.executable, "-m", "src.mud.net.v2_server",
        "--port", str(port), "--db", str(db),
    ]
    if scripted:
        # A scripted SESSION route is faster than a human; use the limits the
        # docs provide instead of mistaking a rate limit for a client bug.
        config = tmp_path / "acceptance.toml"
        config.write_text(_documented_limits(), encoding="utf-8")
        arguments.extend(["--config", str(config)])
    process = subprocess.Popen(
        arguments,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_port(port, process)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _run_reference(
    name: str,
    port: int,
    *extra: str,
    commands: str = "look\nquit\n",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, REFERENCE_CLIENT,
            "--name", name,
            "--uri", f"ws://localhost:{port}",
            *extra,
        ],
        cwd=REPO_ROOT,
        input=commands,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_onboarding_pages_agree_on_one_scratch_walkthrough():
    for doc in DOCS:
        text = (REPO_ROOT / doc).read_text(encoding="utf-8")
        assert WALKTHROUGH_DB in text, (
            f"{doc} must name the same scratch database as the walkthrough"
        )
        assert f"--port {WALKTHROUGH_PORT}" in text, (
            f"{doc} must name the server port the client connects to"
        )
        assert WALKTHROUGH_URI in text, (
            f"{doc} must show the URI that matches `--port`"
        )

    bootstrap = (REPO_ROOT / BOOTSTRAP).read_text(encoding="utf-8")
    assert DEV_DB in bootstrap, (
        "BOOTSTRAP keeps the development server separate from the acceptance"
    )
    assert "--save-key" in bootstrap, "BOOTSTRAP shows how to store the key"
    assert "--key-file" in bootstrap, "BOOTSTRAP shows how to log in again"
    assert "--reset --yes" in bootstrap, (
        "BOOTSTRAP explains how to replace an existing world"
    )
    assert "keeps characters" in bootstrap, (
        "BOOTSTRAP warns that a world reset is not a fresh acceptance"
    )
    assert "rm -f .scratch/session.db" in bootstrap, (
        "BOOTSTRAP shows how to get a genuinely fresh acceptance database"
    )

    session = (REPO_ROOT / SESSION).read_text(encoding="utf-8")
    assert "command_burst = 1000" in session, (
        "SESSION tells an automated client how to avoid the command burst"
    )
    assert "--config ~/.config/iot-mud/acceptance.toml" in session
    assert "keeps characters" in session, (
        "SESSION warns that reseeding is not a fresh acceptance"
    )
    assert "rm -f .scratch/session.db" in session


def test_reference_client_registers_saves_the_key_and_logs_in(tmp_path):
    with documented_server(tmp_path) as port:
        key_file = tmp_path / "keys" / "ada.key"
        registered = _run_reference(
            "Ada", port, "--save-key", str(key_file),
            commands="!malformed\nlook\nquit\n",
        )
        output = registered.stdout + registered.stderr
        assert registered.returncode == 0, output
        assert "Registered as: Ada" in output
        assert "Protocol: 2" in output
        assert "Auth: required (key)" in output
        assert "Server commit: " in output
        assert "World: The Commons" in output
        assert "Village Gate" in output
        assert "[malformed_message]" in output, (
            "!malformed must show the typed error code"
        )
        assert "Connection closed: 1000" in output

        assert key_file.is_file(), (
            f"--save-key did not create {key_file}:\n{output}"
        )
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"key file mode is {oct(mode)}, want 0o600"
        key = key_file.read_text(encoding="utf-8").strip()
        assert key.startswith("mud1-")
        assert len(key.splitlines()) == 1
        assert output.count(key) == 1, "the key is shown exactly once"

        # A second registration must not overwrite a working key file.
        again = _run_reference(
            "Grace", port, "--save-key", str(key_file), commands="quit\n"
        )
        assert again.returncode != 0
        assert "already exists" in again.stdout + again.stderr
        assert key_file.read_text(encoding="utf-8").strip() == key

        logged_in = subprocess.run(
            [
                sys.executable, REFERENCE_CLIENT,
                "--key-file", str(key_file),
                "--uri", f"ws://localhost:{port}",
            ],
            cwd=REPO_ROOT,
            input="look\nquit\n",
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = logged_in.stdout + logged_in.stderr
        assert logged_in.returncode == 0, output
        assert "Logged in as: Ada" in output
        assert key not in output, "a login must not repeat the key"
        assert "Your character key" not in output
        assert "Connection closed: 1000" in output


@pytest.mark.asyncio
async def test_the_session_acceptance_runs_against_the_documented_server(
    tmp_path,
):
    with documented_server(tmp_path, scripted=True) as port:
        await run_acceptance_session(f"ws://localhost:{port}")
