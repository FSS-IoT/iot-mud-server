"""Ticket #87: every docs example is parsed or executed in CI.

The four v2.1 docs — `PROTOCOL.md`, `CLIENTS.md`, `CONTENT.md`, and
`OPERATIONS.md` — are part of the contract, so their examples are tested like
code:

* every fenced JSON block must parse; client messages are sent to a fresh
  port-0 server and the direct reply is paired by `id`;
* every fenced TOML block must load through `src.mud.config.load_config`;
* every fenced YAML block must parse and match the content model it shows;
* every command block is split with `shlex` and each command is either run
  (admin commands against a temporary database, `pytest --collect-only`,
  `systemd-analyze verify`) or explicitly recognised as a host/network/root
  command that cannot run in a test. An unrecognised command fails, so a new
  example cannot slip through unclassified;
* a missing doc, an unclosed fence, an untagged fence, or an unknown language
  fails.

The acceptance-session test walks the concrete `SESSION.md` route against a
fresh golden server: auth, take/drop, speech, social verbs, NPC dialogue,
containers, exit locks, and sleep/wake. Nothing binds 8765 and every database
is temporary.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from src.mud.config import ConfigError, load_config
from src.mud.content.models import Area, GameObject, Room, WorldFile
from tests.golden.harness import (
    GoldenClient,
    command,
    golden_client,
    golden_server,
    wait_until,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

PROTOCOL = "docs/PROTOCOL.md"
CLIENTS = "docs/CLIENTS.md"
CONTENT = "docs/CONTENT.md"
OPERATIONS = "docs/OPERATIONS.md"
REQUIRED_DOCS = (PROTOCOL, CLIENTS, CONTENT, OPERATIONS)
# The follow-up tickets read the same fences, so they are checked too.
EXPANDED_DOCS = (
    "docs/SESSION.md",
    "docs/HOSTING.md",
    "README.md",
    "BOOTSTRAP.md",
)
ALL_DOCS = REQUIRED_DOCS + EXPANDED_DOCS

KNOWN_LANGUAGES = {
    "json", "toml", "yaml", "bash", "sh", "powershell", "text", "nix",
}
COMMAND_LANGUAGES = {"bash", "sh", "powershell"}

# Commands that need root, the network, or another host. They are parsed with
# `shlex` and the repository paths they name are checked; they are not run.
HOST_ONLY = {
    "cd", "chown", "gh", "git", "systemctl", "tailscale", "useradd",
    "websocat",
}

# A module that is shown, not started: the docs bind a real interface or run
# an interactive client, neither of which belongs in a unit test.
PARSE_ONLY_MODULES = {
    "src.mud.net.v2_server",
    "tools.mock_v2_server",
}
PARSE_ONLY_FILES = {
    "examples/reference_client.py",
    "tools/mock_v2_server.py",
}


@dataclass(frozen=True)
class Fence:
    """One fenced block, with the 1-based line of its first content line."""

    doc: str
    line: int
    language: str
    text: str

    @property
    def where(self) -> str:
        return f"{self.doc}:{self.line}"


_OPENING = re.compile(
    r"^(?P<indent> *)```(?P<language>[A-Za-z0-9_+.-]*)\s*$"
)


def parse_fences(doc: str) -> list[Fence]:
    """Every fence in a Markdown file, dedented by its opening fence."""
    path = REPO_ROOT / doc
    lines = path.read_text(encoding="utf-8").splitlines()
    fences: list[Fence] = []
    index = 0
    while index < len(lines):
        opening = _OPENING.match(lines[index])
        if opening is None:
            index += 1
            continue

        indent = len(opening.group("indent"))
        language = opening.group("language").lower()
        body: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if stripped.startswith("```") and not stripped[3:].strip():
                break
            line = lines[cursor]
            if line.startswith(" " * indent):
                line = line[indent:]
            body.append(line)
            cursor += 1

        if cursor >= len(lines):
            raise AssertionError(
                f"{doc}:{index + 1}: unclosed fenced block"
            )

        fences.append(Fence(
            doc=doc,
            line=index + 2,
            language=language,
            text="\n".join(body),
        ))
        index = cursor + 1
    return fences


def fences_for(doc: str) -> list[Fence]:
    return [fence for fence in parse_fences(doc) if fence.text.strip()]


def _json_values(doc: str) -> list[object]:
    values: list[object] = []
    for fence in fences_for(doc):
        if fence.language == "json":
            values.append(json.loads(fence.text))
    return values


def _message_examples(doc: str) -> list[dict]:
    return [value for value in _json_values(doc) if isinstance(value, dict)]


# ---------------------------------------------------------------------------
# Fence shape
# ---------------------------------------------------------------------------


def test_the_four_required_docs_exist_with_examples():
    for doc in REQUIRED_DOCS:
        path = REPO_ROOT / doc
        assert path.is_file(), f"{doc} is required by SPEC v2.1.4 criterion 3"
        assert path.stat().st_size > 1000, f"{doc} looks empty"
        assert fences_for(doc), f"{doc} has no fenced examples"


def test_every_fence_is_tagged_and_known():
    for doc in ALL_DOCS:
        for fence in fences_for(doc):
            assert fence.language, (
                f"{fence.where}: fenced block has no language tag; use "
                f"`text` for output and prose blocks"
            )
            assert fence.language in KNOWN_LANGUAGES, (
                f"{fence.where}: unknown fence language {fence.language!r}"
            )
            if fence.language == "nix":
                # The HOSTING firewall snippet is the one shape we cannot
                # parse without a Nix evaluator.
                assert doc == "docs/HOSTING.md", (
                    f"{fence.where}: only HOSTING.md may carry an opaque "
                    f"Nix fence"
                )


def test_text_fences_are_non_empty_output_or_prose():
    for doc in ALL_DOCS:
        for fence in fences_for(doc):
            if fence.language == "text":
                assert fence.text.strip(), f"{fence.where}: empty text fence"
            if fence.language == "nix":
                assert fence.text.strip(), f"{fence.where}: empty Nix fence"
                assert fence.text.count("{") == fence.text.count("}"), (
                    f"{fence.where}: unbalanced Nix block"
                )


# ---------------------------------------------------------------------------
# Structured examples
# ---------------------------------------------------------------------------


def test_every_json_fence_parses_and_protocol_messages_have_v2_shape():
    for doc in ALL_DOCS:
        for fence in fences_for(doc):
            if fence.language != "json":
                continue
            value = json.loads(fence.text)
            if doc != PROTOCOL or not isinstance(value, dict):
                continue
            if "v" in value:
                assert value["v"] == 2, (
                    f"{fence.where}: protocol examples must use v: 2"
                )
                assert "type" in value, f"{fence.where}: a v2 message has a type"
                assert "data" in value, f"{fence.where}: a v2 message has data"


def test_every_toml_fence_loads_through_the_config_loader(tmp_path):
    for doc in ALL_DOCS:
        for index, fence in enumerate(fences_for(doc)):
            if fence.language != "toml":
                continue
            parsed = tomllib.loads(fence.text)
            assert isinstance(parsed, dict), fence.where
            config_path = tmp_path / f"config-{doc.replace('/', '-')}-{index}.toml"
            config_path.write_text(fence.text, encoding="utf-8")
            try:
                load_config(config_path)
            except ConfigError as exc:
                raise AssertionError(
                    f"{fence.where}: the server rejects this config: {exc}"
                ) from exc


def test_every_yaml_fence_parses_and_matches_the_content_model():
    for doc in ALL_DOCS:
        for fence in fences_for(doc):
            if fence.language != "yaml":
                continue
            value = yaml.safe_load(fence.text)
            assert value is not None, f"{fence.where}: empty YAML"
            _validate_yaml_example(fence, value)


def _validate_yaml_example(fence: Fence, value: object) -> None:
    if isinstance(value, list):
        # A list of objects, as shown for a container.
        for item in value:
            GameObject.model_validate(item)
        return

    assert isinstance(value, dict), f"{fence.where}: YAML must be a mapping"
    keys = set(value)
    if {"name", "start_room", "rng_seed"} <= keys:
        WorldFile.model_validate(value)
    elif {"area", "rooms"} <= keys:
        Area.model_validate(value)
    elif "npcs" in keys or "exits" in keys or "effects" in keys or (
        "geometry" in keys
    ):
        Room.model_validate({"id": "example_room", "name": "Example Room",
                             **value})
    else:
        raise AssertionError(
            f"{fence.where}: YAML block is not a world, area, room, object "
            f"list, NPC list, effects block, or geometry block"
        )


# ---------------------------------------------------------------------------
# Command examples
# ---------------------------------------------------------------------------


def _shell_commands(fence: Fence) -> list[list[str]]:
    text = fence.text.replace("\\\n", " ")
    commands: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            words = shlex.split(stripped, comments=True)
        except ValueError as exc:
            raise AssertionError(
                f"{fence.where}: shell quoting does not parse: {exc}"
            ) from exc
        if words:
            commands.append(words)
    assert commands, f"{fence.where}: command block has no commands"
    return commands


def test_every_command_fence_runs_or_is_an_explicit_host_command(tmp_path):
    executed: set[str] = set()
    for doc in ALL_DOCS:
        for index, fence in enumerate(fences_for(doc)):
            if fence.language not in COMMAND_LANGUAGES:
                continue
            sandbox = _Sandbox(tmp_path / f"cmd-{doc.replace('/', '-')}-{index}")
            for words in _shell_commands(fence):
                execute = fence.language != "powershell"
                _check_command(fence, words, sandbox, execute=execute,
                               executed=executed)

    assert {
        "admin content", "admin seed", "admin backup", "admin restore",
        "load-smoke",
    } <= executed, (
        "the docs must exercise content check, seed, backup, restore, and the "
        f"load smoke; executed {sorted(executed)}"
    )


class _Sandbox:
    """Temporary paths and a config for one command block."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = self.directory / "mud.db"
        self.backup = self.directory / "backup.db"
        self.config = self.directory / "mud.toml"
        self.report = self.directory / "load-report.json"
        self.config.write_text(
            f'[storage]\npath = "{self.db.as_posix()}"\n', encoding="utf-8"
        )


def _plain_admin(sandbox: _Sandbox, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "src.mud.admin", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _ensure_seeded(sandbox: _Sandbox) -> None:
    if sandbox.db.exists():
        return
    result = _plain_admin(sandbox, "seed", "--db", str(sandbox.db))
    assert result.returncode == 0, result.stdout + result.stderr


def _ensure_backup(sandbox: _Sandbox) -> None:
    _ensure_seeded(sandbox)
    if sandbox.backup.exists():
        return
    result = _plain_admin(
        sandbox, "backup", "--db", str(sandbox.db), "--to", str(sandbox.backup)
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _check_command(
    fence: Fence,
    words: list[str],
    sandbox: _Sandbox,
    *,
    execute: bool,
    executed: set[str],
) -> None:
    if words and words[0] == "sudo":
        words = words[1:]
    if not words:
        return

    program = words[0]
    if program in HOST_ONLY:
        _check_host_command(fence, program, words[1:])
        return
    if program == "./run_server.sh":
        assert (REPO_ROOT / "run_server.sh").is_file(), fence.where
        return
    if program == "systemd-analyze":
        _run_systemd_analyze(fence, words, execute=execute)
        return
    if program == "uv":
        _check_uv(fence, words, sandbox, execute=execute, executed=executed)
        return
    if program.endswith(("python", "python3")):
        _check_python(
            fence, words[1:], sandbox, execute=execute, executed=executed
        )
        return
    raise AssertionError(
        f"{fence.where}: unclassified command {program!r}; add a handler or "
        f"move it to HOST_ONLY with a reason"
    )


def _check_host_command(fence: Fence, program: str, args: list[str]) -> None:
    if program == "cd":
        assert args, f"{fence.where}: cd needs a path"
        return
    if program == "git" and args[:1] == ["clone"]:
        assert args[1:], f"{fence.where}: git clone needs a URL"
        return
    if program == "systemctl":
        assert args[:1] in (["stop"], ["start"], ["daemon-reload"],
                            ["enable"]), fence.where
        return
    assert args or program in {"useradd"}, (
        f"{fence.where}: {program} with no arguments"
    )


def _run_systemd_analyze(fence: Fence, words: list[str], *, execute: bool) -> None:
    if words[1:2] != ["verify"]:
        raise AssertionError(f"{fence.where}: only verify is documented")
    unit = words[2] if len(words) > 2 else "deploy/iot-mud.service"
    assert (REPO_ROOT / unit).is_file(), f"{fence.where}: {unit} is missing"
    if not execute or shutil.which("systemd-analyze") is None:
        return
    result = subprocess.run(
        ["systemd-analyze", "verify", str(REPO_ROOT / unit)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _check_uv(
    fence: Fence,
    words: list[str],
    sandbox: _Sandbox,
    *,
    execute: bool,
    executed: set[str],
) -> None:
    if len(words) < 2:
        raise AssertionError(f"{fence.where}: uv needs a subcommand")
    subcommand = words[1]
    if subcommand == "sync":
        return
    if subcommand != "run":
        raise AssertionError(
            f"{fence.where}: only `uv sync` and `uv run` are documented"
        )

    rest = words[2:]
    if rest[:1] == ["pytest"]:
        _run_pytest_collect(fence, rest[1:], execute=execute)
        return
    if rest[:1] == ["python"]:
        _check_python(
            fence, rest[1:], sandbox, execute=execute, executed=executed
        )
        return
    raise AssertionError(
        f"{fence.where}: unclassified `uv run` target {rest[:1]!r}"
    )


def _check_python(
    fence: Fence,
    args: list[str],
    sandbox: _Sandbox,
    *,
    execute: bool,
    executed: set[str],
) -> None:
    if args[:1] == ["-m"]:
        module = args[1] if len(args) > 1 else ""
        module_args = args[2:]
        if module == "src.mud.admin":
            _run_admin(fence, module_args, sandbox, execute=execute,
                       executed=executed)
            return
        if module == "tools.load_smoke":
            _run_load_smoke(fence, module_args, sandbox, execute=execute,
                            executed=executed)
            return
        if module in PARSE_ONLY_MODULES:
            assert (REPO_ROOT / "src/mud/net/v2_server.py").is_file()
            assert (REPO_ROOT / "tools/mock_v2_server.py").is_file()
            return
        raise AssertionError(
            f"{fence.where}: unclassified Python module {module!r}"
        )

    script = args[0] if args else ""
    if script in PARSE_ONLY_FILES:
        assert (REPO_ROOT / script).is_file(), (
            f"{fence.where}: {script} is missing"
        )
        return
    raise AssertionError(
        f"{fence.where}: unclassified Python script {script!r}"
    )


def _run_admin(
    fence: Fence,
    args: list[str],
    sandbox: _Sandbox,
    *,
    execute: bool,
    executed: set[str],
) -> None:
    assert args, f"{fence.where}: admin command needs a subcommand"
    subcommand = args[0]
    if subcommand not in {"init-db", "content", "seed", "backup", "restore"}:
        raise AssertionError(
            f"{fence.where}: unclassified admin subcommand {subcommand!r}"
        )
    if subcommand == "restore":
        _ensure_backup(sandbox)
    elif subcommand == "backup" or (
        subcommand == "seed" and "--reset" in args
    ):
        _ensure_seeded(sandbox)

    translated: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word in {"--db", "--config", "--to", "--from", "--world"}:
            value = args[index + 1] if index + 1 < len(args) else ""
            translated.extend([word, _sandbox_path(word, value, sandbox)])
            index += 2
            continue
        translated.append(word)
        index += 1

    if not execute:
        return
    cache_key = f"admin {subcommand}"
    result = subprocess.run(
        [sys.executable, "-m", "src.mud.admin", *translated],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"{fence.where}: {output}"
    assert "Traceback" not in output, f"{fence.where}: {output}"
    expected = {
        "content": "ok",
        "seed": "seeded",
        "backup": "backed up",
        "restore": "restored",
        "init-db": "initialized",
    }[subcommand]
    if subcommand == "seed" and "--reset" in args:
        expected = "reset"
    assert expected in output, f"{fence.where}: {output}"
    executed.add(cache_key)


def _sandbox_path(option: str, value: str, sandbox: _Sandbox) -> str:
    if option in {"--db", "--config"}:
        return str(sandbox.db if option == "--db" else sandbox.config)
    if option in {"--to", "--from"}:
        return str(sandbox.backup)
    if option == "--world":
        path = Path(value)
        return str(path if path.is_absolute() else REPO_ROOT / path)
    raise AssertionError(option)


def _run_pytest_collect(
    fence: Fence, args: list[str], *, execute: bool
) -> None:
    # A suite-wide selection (no path) is parsed only: collecting the suite
    # from inside the suite is noise, and this test is already a member.
    paths = [
        arg for arg in args
        if arg.endswith(".py") or arg.startswith(("tests/", "src/", "tools/"))
    ]
    if not paths:
        return
    for path in paths:
        assert (REPO_ROOT / path).exists(), (
            f"{fence.where}: pytest target {path!r} does not exist"
        )
    if not execute:
        return
    selection = list(args)
    if "--collect-only" not in selection:
        selection.append("--collect-only")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *selection, "-q"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"{fence.where}: pytest selection does not collect\n"
        f"{result.stdout}{result.stderr}"
    )


def _run_load_smoke(
    fence: Fence,
    args: list[str],
    sandbox: _Sandbox,
    *,
    execute: bool,
    executed: set[str],
) -> None:
    assert "--clients" in args and "--commands" in args, (
        f"{fence.where}: the load example names clients and commands"
    )
    if not execute:
        return
    reduced: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--clients":
            reduced.extend([word, "2"])
            index += 2
            continue
        if word == "--commands":
            reduced.extend([word, "10"])
            index += 2
            continue
        if word == "--report":
            reduced.extend([word, str(sandbox.report)])
            index += 2
            continue
        if word == "--db":
            reduced.extend([word, str(sandbox.db)])
            index += 2
            continue
        reduced.append(word)
        index += 1
    result = subprocess.run(
        [sys.executable, "-m", "tools.load_smoke", *reduced],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.report.is_file(), f"{fence.where}: no load report"
    report = json.loads(sandbox.report.read_text(encoding="utf-8"))
    assert report["clients"] == 2, f"{fence.where}: {report}"
    assert report["protocol_errors"] == [], f"{fence.where}: {report}"
    executed.add("load-smoke")


# ---------------------------------------------------------------------------
# Protocol examples against a fresh server
# ---------------------------------------------------------------------------


def _first_message(messages: list[dict], type_: str) -> dict:
    for message in messages:
        if message.get("type") == type_:
            return message
    raise AssertionError(f"PROTOCOL.md has no {type_!r} message example")


def _first_auth(messages: list[dict], mode: str) -> dict:
    for message in messages:
        data = message.get("data")
        if (
            message.get("type") == "auth"
            and isinstance(data, dict)
            and data.get("mode") == mode
        ):
            return message
    raise AssertionError(f"PROTOCOL.md has no auth example with mode {mode!r}")


async def _direct_reply(client: GoldenClient, msg_id: object) -> dict:
    """Wait for the message that echoes one request id, whatever its type."""
    for index, message in enumerate(client.pending):
        if message.get("id") == msg_id:
            return client.pending.pop(index)
    while True:
        message = await client.queue.get()
        if message.get("id") == msg_id:
            return message
        client.pending.append(message)


@pytest.mark.asyncio
async def test_protocol_client_message_examples_are_sendable(tmp_path):
    messages = _message_examples(PROTOCOL)
    join_example = _first_message(messages, "join")
    input_example = _first_message(messages, "input")
    register_example = _first_auth(messages, "register")
    login_example = _first_auth(messages, "login")

    async with golden_server(tmp_path / "auth") as server:
        async with golden_client(server) as ada:
            await ada.send(register_example)
            registered = await ada.wait("auth.ok")
            assert registered["id"] == register_example["id"]
            key = registered["data"]["key"]
            assert key.startswith("mud1-")
            await ada.wait("room.state")

            await ada.send(input_example)
            reply = await _direct_reply(ada, input_example["id"])
            assert reply["type"] in {"error", "notice", "room.state"}

        await wait_until(
            lambda: bool(server.engine.get_player("Ada") is not None
                         and server.engine.get_player("Ada").asleep),
            description="Ada to sleep",
        )
        async with golden_client(server) as probe:
            login = json.loads(json.dumps(login_example))
            login["data"]["key"] = key
            await probe.send(login)
            logged_in = await probe.wait("auth.ok")
            assert logged_in["id"] == login_example["id"]
            assert logged_in["data"]["mode"] == "login"
            assert "key" not in logged_in["data"]

    async with (
        golden_server(tmp_path / "guest", auth_required=False) as guest,
        golden_client(guest) as visitor,
    ):
        await visitor.send(join_example)
        joined = await visitor.wait("session.ok")
        assert joined["id"] == join_example["id"]
        await visitor.wait("room.state")


@pytest.mark.asyncio
async def test_protocol_hello_and_room_examples_match_live_shapes(tmp_path):
    messages = _message_examples(PROTOCOL)
    documented_hello = next(
        message["data"] for message in messages if message.get("type") == "hello"
    )
    # The room shape example is the object with a gate id, not a full message.
    documented_room = next(
        value for value in _json_values(PROTOCOL)
        if isinstance(value, dict) and value.get("id") == "gate"
    )

    async with (
        golden_server(tmp_path / "shapes") as server,
        golden_client(server) as ada,
    ):
        assert ada.hello is not None
        live_hello = ada.hello["data"]
        for key, value in documented_hello.items():
            assert key in live_hello, f"hello lost documented field {key}"
            assert isinstance(live_hello[key], type(value)), key
        assert set(documented_hello["world"]) <= set(live_hello["world"])

        _, state = await ada.register("Ada")
        live_room = state["data"]["room"]
        for key, value in documented_room.items():
            assert key in live_room, f"room.state lost documented field {key}"
            assert isinstance(live_room[key], type(value)), key


# ---------------------------------------------------------------------------
# The acceptance session from SESSION.md
# ---------------------------------------------------------------------------


def _messages_since(client: GoldenClient, marker: int, type_: str) -> list[dict]:
    return [
        message for message in client.log[marker:]
        if message.get("type") == type_
    ]


async def _run_acceptance_session(server):
    async with (
        golden_client(server) as ada,
        golden_client(server) as ben,
    ):
        ok, _ = await ada.register("Ada")
        key = ok["data"]["key"]
        await ben.register("Ben")

        # Session steps 4-5: move and arrive.
        await command(ada, "go north", "room.state")
        await ben.wait(
            "room.leave",
            lambda m: m["data"]["actor"]["name"] == "Ada",
        )
        await command(ben, "go north", "room.state")
        await ada.wait(
            "room.enter", lambda m: m["data"]["actor"]["name"] == "Ben"
        )

        # Steps 6-8: take and drop send no inventory follow-up.
        marker = len(ada.log)
        await command(ada, "take gold coin", "object.taken")
        assert not _messages_since(ada, marker, "player.inventory")
        await ben.wait(
            "object.taken", lambda m: m["data"]["by"] == "Ada"
        )
        inventory = await command(ada, "inventory", "player.inventory")
        assert any(
            item["name"] == "Gold Coin"
            for item in inventory["data"]["items"]
        )
        marker = len(ada.log)
        await command(ada, "drop gold coin", "object.dropped")
        assert not _messages_since(ada, marker, "player.inventory")

        # Steps 8 and the social section: say, emote, tell, who.
        await command(ada, "say hello", "player.said")
        await ben.wait("player.said")
        await command(ada, "emote waves at the sky", "player.emoted")
        await ben.wait("player.emoted")
        await command(ada, "tell Ben meet me at the gate", "player.told")
        await ben.wait("player.told")
        who = await command(ada, "who", "who.list")
        assert {"Ada", "Ben"} <= {
            row["name"] for row in who["data"]["players"]
        }

        # Step 11: a malformed frame does not close the socket.
        await ada.ws.send("{")
        malformed = await ada.wait("error")
        assert malformed["data"]["code"] == "malformed_message"
        await command(ada, "look", "room.state")

        # Exit locks: the market's east door needs the gold key.
        locked = await command(ada, "go east", "error")
        assert locked["data"]["code"] == "locked"
        await ben.wait("exit.locked")
        await command(ada, "go west", "room.state")
        await command(ada, "take torch", "object.taken")
        await command(ada, "take gold key", "object.taken")
        await command(ada, "light torch", "object.lit")
        npc = await command(ada, "talk Ysolde", "npc.say")
        assert npc["data"]["npc"]["id"] == "npc_alchemist"
        asked = await command(
            ada, "ask Ysolde about chest", "npc.say",
            lambda m: m["data"].get("topic") == "chest",
        )
        assert asked["data"]["self"] is True
        await command(ada, "go east", "room.state")
        unlocked = await command(ada, "use gold key on east", "exit.unlocked")
        assert unlocked["data"]["exit"]["to"] == "tavern"
        await ben.wait("exit.unlocked")
        await command(ada, "go east", "room.state")
        await command(ada, "go west", "room.state")

        # Containers: open, get, put, close, and typed errors.
        await command(ada, "go north", "room.state")
        await command(ada, "use gold key on chest", "object.unlocked")
        await command(ada, "open chest", "object.opened")
        await command(ada, "get brass lantern from chest", "object.got")
        await ada.wait("player.inventory")
        await command(ada, "put brass lantern in chest", "object.put")
        await ada.wait("player.inventory")
        await command(ada, "close chest", "object.closed")
        closed = await command(ada, "get brass lantern from chest", "error")
        assert closed["data"]["code"] == "container_closed"
        await command(ada, "open chest", "object.opened")
        marker = len(ada.log)
        await command(ada, "get brass lantern from chest", "object.got")
        await ada.wait("player.inventory")
        assert _messages_since(ada, marker, "player.inventory")
        await command(ada, "go south", "room.state")

        # Sleep and wake: the gold key drops in the room; no queue.
        await ada.quit()
        leave = await ben.wait(
            "room.leave",
            lambda m: m["data"]["actor"]["name"] == "Ada"
            and m["data"].get("reason") == "disconnect",
        )
        assert leave["data"]["asleep"] is True
        assert leave["data"]["stone"] is True
        assert any(
            item["id"] == "key_gold_1"
            for item in leave["data"].get("dropped", [])
        )
        roster = await command(ben, "who", "who.list")
        ada_row = next(
            row for row in roster["data"]["players"] if row["name"] == "Ada"
        )
        assert ada_row["asleep"] is True and ada_row["stone"] is True
        asleep_tell = await command(ben, "tell Ada hello", "error")
        assert asleep_tell["data"]["code"] == "not_awake"

        await wait_until(
            lambda: bool(server.engine.get_player("Ada") is not None
                         and server.engine.get_player("Ada").asleep),
            description="Ada to sleep",
        )
        async with golden_client(server) as woken:
            login, state = await woken.login(key)
            assert login["data"]["mode"] == "login"
            assert state["data"]["room"]["id"] == "market"
            names = {
                item["name"]
                for item in (
                    await command(woken, "inventory", "player.inventory")
                )["data"]["items"]
            }
            assert "Torch" in names
            assert "Gold Key" not in names
            assert not _messages_since(woken, 0, "player.told")
            await ben.wait(
                "room.enter",
                lambda m: m["data"]["actor"]["name"] == "Ada",
            )
            await command(ben, "take gold key", "object.taken")


@pytest.mark.asyncio
async def test_session_document_route_runs_against_a_fresh_server(tmp_path):
    async with golden_server(tmp_path / "session") as server:
        await _run_acceptance_session(server)
