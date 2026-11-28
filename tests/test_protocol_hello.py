"""Issue #67: hello.server_commit and the protocol changelog.

`hello.data.server_commit` names the commit the server was built from, so a
client that pins a commit can see what it is missing. It is additive: `v`
stays 2 and v2.0 clients ignore the field. `docs/PROTOCOL.md` records every
protocol change in a changelog.
"""

import asyncio
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import websockets

from src.mud.engine.core import Engine
from src.mud.net import v2_server
from src.mud.net.v2_server import V2Server
from src.mud.protocol import v2
from src.mud.world.loader import load_demo_village

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DOC = REPO_ROOT / "docs" / "PROTOCOL.md"
VALID_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
async def engine():
    engine = Engine()
    await load_demo_village(engine)
    return engine


async def _start(engine, **kwargs):
    instance = V2Server(engine, port=0, **kwargs)
    task = asyncio.create_task(instance.run())
    for _ in range(200):
        if instance.port:
            break
        await asyncio.sleep(0.01)
    assert instance.port, "v2 server did not bind"
    return instance, task


async def _stop(instance, task):
    instance.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except TimeoutError:
        task.cancel()


async def _hello(engine, **kwargs):
    instance, task = await _start(engine, **kwargs)
    try:
        async with websockets.connect(
                f"ws://localhost:{instance.port}") as ws:
            return json.loads(await ws.recv())
    finally:
        await _stop(instance, task)


# ----------------------------------------------------------------------
# hello.server_commit
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hello_carries_the_configured_commit(engine):
    hello = await _hello(engine, server_commit=VALID_COMMIT)

    assert hello["type"] == "hello"
    assert hello["v"] == 2
    assert hello["data"]["server_commit"] == VALID_COMMIT


@pytest.mark.asyncio
async def test_hello_carries_the_version_fields(engine):
    hello = await _hello(engine)

    data = hello["data"]
    assert data["server_version"] == v2.SERVER_VERSION
    assert data["feature_level"] == v2.FEATURE_LEVEL
    assert data["capabilities"] == list(v2.CAPABILITIES)
    assert data["protocol"] == [v2.PROTOCOL_VERSION]


def test_the_manifest_version_matches_the_protocol_constant():
    manifest = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["version"] == v2.SERVER_VERSION


def test_the_feature_level_is_the_version_minor():
    major, minor, _patch = v2.SERVER_VERSION.split(".")
    assert int(major) == v2.PROTOCOL_VERSION
    assert int(minor) == v2.FEATURE_LEVEL


def test_version_tags_are_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", v2.SERVER_VERSION)


def test_protocol_doc_names_the_current_server_version():
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    assert f"**Version:** {v2.SERVER_VERSION}" in text
    assert "## Versioning" in text


@pytest.mark.asyncio
async def test_hello_resolves_the_git_head_without_an_override(engine,
                                                              monkeypatch):
    monkeypatch.delenv(v2_server.SERVER_COMMIT_ENV, raising=False)
    expected = v2_server.read_git_head(REPO_ROOT)
    if expected is None:
        pytest.skip("not a git checkout")

    hello = await _hello(engine)

    assert hello["data"]["server_commit"] == expected


@pytest.mark.parametrize("value", [VALID_COMMIT, "a" * 40, "0" * 40])
def test_valid_commit_values(value):
    assert v2.is_server_commit(value) is True


@pytest.mark.parametrize("value", [
    None,
    "",
    "unknown",
    "A" * 40,
    "a" * 39,
    "a" * 41,
    "g" * 40,
    VALID_COMMIT + "\n",
    123,
])
def test_invalid_commit_values_do_not_count(value):
    assert v2.is_server_commit(value) is False


def test_explicit_commit_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(v2_server.SERVER_COMMIT_ENV, "b" * 40)

    assert v2_server.resolve_server_commit(VALID_COMMIT) == VALID_COMMIT


def test_environment_commit_is_used(monkeypatch):
    monkeypatch.setenv(v2_server.SERVER_COMMIT_ENV, "b" * 40)

    assert v2_server.resolve_server_commit() == "b" * 40


def test_an_invalid_explicit_commit_falls_through_to_the_environment(
        monkeypatch):
    monkeypatch.setenv(v2_server.SERVER_COMMIT_ENV, "b" * 40)

    assert v2_server.resolve_server_commit("not-a-commit") == "b" * 40


def test_an_invalid_environment_commit_falls_through_to_git(monkeypatch):
    monkeypatch.setenv(v2_server.SERVER_COMMIT_ENV, "junk")
    monkeypatch.setattr(v2_server, "read_git_head", lambda root: "c" * 40)
    v2_server._git_commit.cache_clear()
    try:
        assert v2_server.resolve_server_commit() == "c" * 40
    finally:
        v2_server._git_commit.cache_clear()


def test_unreadable_git_head_is_unknown(monkeypatch):
    monkeypatch.delenv(v2_server.SERVER_COMMIT_ENV, raising=False)
    monkeypatch.setattr(v2_server, "read_git_head", lambda root: None)
    v2_server._git_commit.cache_clear()
    try:
        assert v2_server.resolve_server_commit() == v2.UNKNOWN_SERVER_COMMIT
    finally:
        v2_server._git_commit.cache_clear()


def test_git_lookup_is_cached(monkeypatch):
    monkeypatch.delenv(v2_server.SERVER_COMMIT_ENV, raising=False)
    calls = []

    def fake_head(root):
        calls.append(root)
        return "d" * 40

    monkeypatch.setattr(v2_server, "read_git_head", fake_head)
    v2_server._git_commit.cache_clear()
    try:
        assert v2_server.resolve_server_commit() == "d" * 40
        assert v2_server.resolve_server_commit() == "d" * 40
        assert len(calls) == 1
    finally:
        v2_server._git_commit.cache_clear()


def test_read_git_head_outside_a_checkout_is_none(tmp_path):
    assert v2_server.read_git_head(tmp_path) is None


def test_read_git_head_matches_git_rev_parse_here():
    checkout = REPO_ROOT / ".git"
    if not checkout.exists():
        pytest.skip("not a git checkout")

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert v2_server.read_git_head(REPO_ROOT) == expected
    assert re.fullmatch(r"[0-9a-f]{40}", expected)


# ----------------------------------------------------------------------
# docs/PROTOCOL.md
# ----------------------------------------------------------------------

def _changelog_section(text):
    match = re.search(
        r"^## Changelog\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else None


def test_protocol_doc_has_a_changelog_entry_for_the_current_version():
    section = _changelog_section(PROTOCOL_DOC.read_text(encoding="utf-8"))
    assert section is not None, "docs/PROTOCOL.md has no ## Changelog section"

    heading = re.search(
        rf"^### v{re.escape(v2.PROTOCOL_RELEASE)}\b[^\n]*$",
        section,
        re.MULTILINE,
    )
    assert heading is not None, (
        f"the changelog has no entry for v{v2.PROTOCOL_RELEASE}")
    assert f"protocol {v2.PROTOCOL_VERSION}" in heading.group(0).lower()


def test_protocol_doc_changelog_names_this_change():
    section = _changelog_section(PROTOCOL_DOC.read_text(encoding="utf-8"))

    assert section is not None
    assert "server_commit" in section


def test_protocol_doc_documents_the_commit_and_its_fallback():
    text = PROTOCOL_DOC.read_text(encoding="utf-8")
    hello_section = text.split("## Session lifecycle", 1)[1]
    hello_section = hello_section.split("## Client messages", 1)[0]

    assert "server_commit" in hello_section
    assert "MUD_SERVER_COMMIT" in hello_section
    assert "unknown" in hello_section
