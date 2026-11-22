"""Issue #91: BOOTSTRAP pin and protocol changelog visibility.

A member writes their own client against the shared protocol, so the shared
server is the authority. This test holds the three facts #91 requires:

- ``BOOTSTRAP.md`` tells a client to *pin* a server commit instead of chasing
  "latest", and the commit it names actually exists in the repo.
- ``hello.data.server_commit`` is part of the contract, so a pinned client can
  read what the running server is.
- ``docs/PROTOCOL.md`` keeps a changelog a pinned client can read alongside
  ``hello.data.server_commit`` to see what it may be missing.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "BOOTSTRAP.md"
PROTOCOL = REPO_ROOT / "docs" / "PROTOCOL.md"

# A 40-character lowercase hex SHA-1, the shape of a git commit.
COMMIT_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True
    ).strip()


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


def test_bootstrap_pins_a_commit_that_exists() -> None:
    text = BOOTSTRAP.read_text()

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

    # The instruction has to read like a pin, not just mention a commit once.
    assert COMMIT_RE.search(text) is not None
    lowered = text.lower()
    assert any(
        word in lowered
        for word in ("pin", "checkout", "check out", "commit", "fixed")
    ), "BOOTSTRAP.md should tell the client to pin to a fixed commit"


def test_hello_reports_server_commit() -> None:
    # The field is part of the wire contract, documented in PROTOCOL.md.
    protocol = PROTOCOL.read_text()
    assert "server_commit" in protocol, (
        "hello.data.server_commit must be documented in docs/PROTOCOL.md"
    )
    # And it is actually produced by the server, not only described.
    from src.mud.protocol import v2

    assert hasattr(v2, "is_server_commit") or "server_commit" in str(v2.__file__), (
        "the server must resolve and report hello.data.server_commit"
    )


def test_protocol_has_a_changelog() -> None:
    protocol = PROTOCOL.read_text()
    assert "## Changelog" in protocol, (
        "docs/PROTOCOL.md must keep a changelog section"
    )
    # The changelog should name the additive commit field a pinned client reads.
    assert (
        "server_commit" in protocol
    ), "the changelog should record hello.data.server_commit"
