"""Ticket #35: the v2 server reads host and port from the command line."""

import pytest

from src.mud.net.v2_server import parse_args


def test_defaults_are_localhost_8765():
    args = parse_args([])

    assert args.host == "localhost"
    assert args.port == 8765


def test_explicit_host_and_port():
    args = parse_args(["--host", "0.0.0.0", "--port", "9000"])

    assert args.host == "0.0.0.0"
    assert args.port == 9000


@pytest.mark.parametrize("port", ["0", "65536", "abc", ""])
def test_invalid_port_exits_with_a_message(port, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--port", port])

    assert exc.value.code == 2
    assert "port" in capsys.readouterr().err.lower()


def test_empty_host_exits_with_a_message(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--host", "   "])

    assert exc.value.code == 2
    assert "host" in capsys.readouterr().err.lower()
