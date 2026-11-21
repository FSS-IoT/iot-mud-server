import socket

import pytest
import websockets


@pytest.mark.asyncio
async def test_server_handshake_fails_when_no_server():
    """Connecting to a reserved, closed port fails.

    This deliberately does not use 8765: the old version of this test passed
    only when the club server was *not* running, so starting the server before
    pytest made it fail. A port bound and released here is free regardless.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            pass
    except (ConnectionRefusedError, OSError):
        return
    except Exception as exc:  # noqa: BLE001 - report the unexpected type in the failure
        pytest.fail(f"Unexpected exception type: {type(exc).__name__}: {exc}")
    else:
        pytest.fail("Connected to a port that should have been closed.")
