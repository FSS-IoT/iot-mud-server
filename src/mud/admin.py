"""Offline administration commands.

Run as `python -m src.mud.admin <command>`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from src.mud.config import DEFAULT_WORLD, ConfigError, load_config
from src.mud.content.models import ContentModelError
from src.mud.content.validator import check_world
from src.mud.storage.db import SCHEMA_VERSION, DatabaseError, open_database
from src.mud.world.geometry import WorldDataError
from src.mud.world.seed import SeedError, SeedResult, seed_world

# Repository root: <root>/src/mud/admin.py
REPO_ROOT = Path(__file__).resolve().parents[2]

DESTRUCTIVE_WARNING = (
    "warning: a reset is destructive: it replaces the world (rooms, exits, "
    "objects, and NPCs) and clears every character's inventory. Characters "
    "and their character keys are kept and placed at the new start room."
)


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

    content = commands.add_parser(
        "content", help="validate authored world content")
    content_commands = content.add_subparsers(
        dest="content_command", required=True)
    check = content_commands.add_parser(
        "check", help="validate a YAML world without touching the database")
    check.add_argument(
        "--world", type=Path, required=True,
        help="YAML world file, or a directory with world.yaml and areas/")

    seed = commands.add_parser(
        "seed", help="write the YAML world into the database")
    seed.add_argument(
        "--config", type=Path, default=None,
        help="TOML config file (default: ./mud.toml if it exists)")
    seed.add_argument(
        "--db", type=Path, default=None,
        help="SQLite database path (overrides [storage].path)")
    seed.add_argument(
        "--world", type=Path, default=None,
        help=f"YAML world file or directory (default: {DEFAULT_WORLD})")
    seed.add_argument(
        "--reset", action="store_true",
        help="replace an existing world; requires --yes")
    seed.add_argument(
        "--yes", action="store_true",
        help="confirm the destructive --reset")
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

    if args.command == "content":
        errors = check_world(args.world)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(f"{args.world}: ok")
        return 0

    if args.command == "seed":
        return _seed(args)

    return 2


def _seed(args: argparse.Namespace) -> int:
    """Seed or reset the world; return the process exit code."""
    if args.yes and not args.reset:
        print("error: --yes only confirms --reset and does nothing on its own",
              file=sys.stderr)
        return 2
    if args.reset and not args.yes:
        print(DESTRUCTIVE_WARNING, file=sys.stderr)
        print("error: --reset replaces the world; pass --yes to confirm",
              file=sys.stderr)
        return 2
    if args.reset:
        print(DESTRUCTIVE_WARNING, file=sys.stderr)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    path = args.db or config.storage.path
    world_path = args.world or (REPO_ROOT / DEFAULT_WORLD)

    try:
        conn = open_database(path)
    except DatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        result = seed_world(conn, world_path, reset=args.reset)
    except (SeedError, ContentModelError, WorldDataError) as exc:
        for line in str(exc).splitlines():
            print(f"error: {line}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: could not write {path}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(_seed_summary(result, path))
    return 0


def _seed_summary(result: SeedResult, path: Path) -> str:
    counts = (
        f"{result.rooms} rooms, {result.exits} exits, "
        f"{result.objects} objects, {result.npcs} NPCs"
    )
    if result.reset:
        characters = (
            f"{result.characters} character"
            f"{'' if result.characters == 1 else 's'}"
        )
        return (
            f"reset {result.name} in {path}: {counts}; kept {characters} at "
            f"start room {result.start_room!r} with inventory cleared; "
            f"epoch {result.epoch}"
        )
    return f"seeded {result.name} into {path}: {counts}; epoch {result.epoch}"


if __name__ == "__main__":
    sys.exit(main())
