"""Offline administration commands.

Run as `python -m src.mud.admin <command>`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.mud.config import ConfigError, load_config
from src.mud.storage.db import SCHEMA_VERSION, DatabaseError, open_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.mud.admin",
        description="Offline IoT MUD administration",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_db = commands.add_parser(
        "init-db", help="create or migrate the SQLite database")
    init_db.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    init_db.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "init-db":
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        path = args.db or config.storage.path
        try:
            conn = open_database(path)
        except DatabaseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        conn.close()
        print(f"initialized {path} (schema version {SCHEMA_VERSION})")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
