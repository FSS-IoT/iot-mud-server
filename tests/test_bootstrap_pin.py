"""Issue #91: the bootstrap pin is concrete, documented, and executable.

The base pin landed with #125: `BOOTSTRAP.md` names a commit that exists in
the repository, `docs/PROTOCOL.md` records `hello.data.server_commit`, and CI
checks out full history so the pin is resolvable. This follow-up moves the pin
to the first commit whose reference client prints `Server commit:`, adds the
v2.1.4 changelog entry, and makes the instruction executable with
`--expect-commit`.

The live-server check uses a fresh `port=0` server and a temporary database;
nothing binds 8765. The client runs in a subprocess because its exit code is
part of the contract.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

from examples import reference_client
from tests.golden.harness import golden_client, golden_server

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "BOOTSTRAP.md"
PROTOCOL = REPO_ROOT / "docs" / "PROTOCOL.md"
REFERENCE_CLIENT = "examples/reference_client.py"

# The v2.1.4 reference server commit. It includes #90, the first commit whose
# reference client prints `Server commit:`, and must be an ancestor of HEAD.
PIN = "0e2b2c3bad33df6f3da062e3eacc4e35572b90c6"
COMMIT_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")


def _changelog() -> str:
    text = PROTOCOL.read_text(encoding="utf-8")
    match = re.search(
        r"^## Changelog\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "docs/PROTOCOL.md has no ## Changelog section"
    return match.group(1)


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^{re.escape(heading)}\s*$(.*?)(?=^#{2,3} |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing changelog section {heading!r}"
    return match.group(1)


def _object_exists(sha: str) -> bool:
    # `git cat-file -e` exits 0 when the object exists and produces no output,
    # so the return code is the answer, not the stdout.
    try:
        subprocess.run(
            ["git", "cat-file", "-e", sha],
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _run_reference(port: int, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, REFERENCE_CLIENT,
            "--name", "Ada",
            "--uri", f"ws://localhost:{port}",
            *args,
        ],
        cwd=REPO_ROOT,
        input="quit\n",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bootstrap_pins_a_commit_that_exists() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    lowered = text.lower()

    # A pin instruction names a concrete 40-character commit.
    commits = COMMIT_RE.findall(text)
    assert commits, (
        "BOOTSTRAP.md must tell a client to pin a server commit; name the "
        "commit's SHA so the client builds against a fixed point, not 'latest'"
    )

    # The named commit must exist in the repo, so the pin is real and resolvable.
    for sha in commits:
        assert _object_exists(sha), (
            f"BOOTSTRAP.md pins commit {sha}, which does not exist in the repo"
        )

    assert PIN in commits, (
        f"BOOTSTRAP.md must name the v2.1.4 reference commit {PIN}"
    )

    # The pin has to say where to record it and how to compare it.
    assert "pin the server commit" in lowered
    assert "readme" in lowered, (
        "BOOTSTRAP.md must say to record the pin in the client README"
    )
    assert "hello.data.server_commit" in text, (
        "BOOTSTRAP.md must say which hello field to compare with the pin"
    )
    assert "changelog" in lowered, (
        "BOOTSTRAP.md must point at the protocol changelog"
    )

    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", PIN, "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ancestor.returncode == 0, (
        f"the pinned commit {PIN} must exist and be an ancestor of HEAD: "
        f"{ancestor.stdout}{ancestor.stderr}"
    )


def test_hello_reports_server_commit() -> None:
    # The field is part of the wire contract, documented in PROTOCOL.md.
    protocol = PROTOCOL.read_text(encoding="utf-8")
    assert "server_commit" in protocol, (
        "hello.data.server_commit must be documented in docs/PROTOCOL.md"
    )
    # And it is actually produced by the server, not only described.
    from src.mud.protocol import v2

    assert hasattr(v2, "is_server_commit") or "server_commit" in str(v2.__file__), (
        "the server must resolve and report hello.data.server_commit"
    )


def test_protocol_has_a_changelog() -> None:
    protocol = PROTOCOL.read_text(encoding="utf-8")
    assert "## Changelog" in protocol, (
        "docs/PROTOCOL.md must keep a changelog section"
    )
    # The changelog should name the additive commit field a pinned client reads.
    assert "server_commit" in protocol, (
        "the changelog should record hello.data.server_commit"
    )

    changelog = _changelog()
    assert PIN in changelog, (
        f"the protocol changelog must name the v2.1.4 pin {PIN}"
    )
    entry = _section(changelog, "### v2.1.4 — protocol 2 reference pin")
    assert PIN in entry
    assert "#90" in entry, "the entry must say the pin includes #90"
    assert "hello.data.server_commit" in entry
    lowered = entry.lower()
    assert "additive" in lowered, (
        "the entry must explain that v2.1 entries are additive"
    )
    assert "wire" in lowered, (
        "the entry must say what additive means for the wire, not claim "
        "there were no wire changes"
    )
    assert "no wire changes" not in lowered


def test_commit_comparison_accepts_the_pin_and_explains_a_mismatch():
    assert reference_client.commit_error(PIN, PIN) is None

    wrong = "f" * 40
    mismatch = reference_client.commit_error(PIN, wrong)
    assert mismatch is not None
    assert PIN in mismatch and wrong in mismatch, (
        "a mismatch message must print both the expected and reported commit"
    )

    unknown = reference_client.commit_error(PIN, "unknown")
    assert unknown is not None
    assert "unknown" in unknown.lower() and PIN in unknown


@pytest.mark.asyncio
async def test_reference_client_enforces_the_pin_against_a_live_server(
    tmp_path,
):
    async with golden_server(tmp_path / "pin") as server:
        async with golden_client(server) as probe:
            assert probe.hello is not None
            reported = probe.hello["data"]["server_commit"]
        assert isinstance(reported, str) and COMMIT_RE.fullmatch(reported), (
            f"a live server must report a 40-hex commit, got {reported!r}"
        )

        accepted = await asyncio.to_thread(
            _run_reference, server.port, "--expect-commit", reported)
        output = accepted.stdout + accepted.stderr
        assert accepted.returncode == 0, output
        assert f"Server commit matches pin: {reported}" in output
        assert "Registered as: Ada" in output

        wrong = "f" * 40 if reported != "f" * 40 else "e" * 40
        rejected = await asyncio.to_thread(
            _run_reference, server.port, "--expect-commit", wrong)
        output = rejected.stdout + rejected.stderr
        assert rejected.returncode == 1, output
        assert "mismatch" in output.lower()
        assert wrong in output and reported in output, (
            "the rejected run must print the expected and reported commits"
        )
        assert "Registered as: Ada" not in output, (
            "the client must stop before auth when the pin does not match"
        )
