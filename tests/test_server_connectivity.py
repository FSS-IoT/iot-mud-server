import pytest
import websockets
import asyncio

@pytest.mark.asyncio
async def test_server_handshake_fails_when_no_server():
    """[TASK-001] RED PHASE: Connection should fail if the server is not running."""
    try:
        # Attempt to connect to a port we expect our server to use
        async with websockets.connect("ws://localhost:8765"):
            pass
    except (ConnectionRefusedError, OSError):
        # This is the expected failure for the RED phase.
        # asyncio wraps ConnectionRefusedError in an OSError sometimes.
        return 
    except Exception as e:
        pytest.fail(f"Unexpected exception type: {type(e).__name__}: {e}")
    else:
        pytest.fail("Failed to receive connection error, but no server was running.")

