"""Experimental browser admin console and world editor.

A stdlib ``ThreadingHTTPServer`` on a second port. The server is disabled by
default, binds loopback by default, and every ``/api/*`` route needs a bearer
token compared with :func:`hmac.compare_digest`. The built Svelte app is served
from ``admin/dist`` at ``/``; it is a static login screen until the token is
entered and kept in browser ``sessionStorage``.

There is no CORS support: the UI and API are same-origin by construction. The
console is an operator tool, not a public service.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import logging
import mimetypes
import re
import sqlite3
import threading
import urllib.parse
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.mud.auth import RekeyError, rekey_character
from src.mud.config import AdminConfig
from src.mud.content.models import ContentModelError
from src.mud.storage.db import DatabaseError, open_database
from src.mud.world.editing import (
    WorldEditError,
    read_world_payload,
    save_world_payload,
    validate_world_payload,
)
from src.mud.world.geometry import WorldDataError

if TYPE_CHECKING:
    from src.mud.net.v2_server import V2Server

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DIST = REPO_ROOT / "admin" / "dist"
MAX_BODY_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_REKEY_PATH = re.compile(r"^/api/players/([^/]+)/rekey$")
_MIME_OVERRIDES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}
_HTML_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
MISSING_BUILD_MESSAGE = (
    "The admin UI build is missing.\n"
    "Run: cd admin && npm ci && npm run build\n"
    "Then reload this page. The API is still available at /api/.\n"
)


class AdminConsoleError(RuntimeError):
    """The admin console cannot start or answer a request."""


class ApiError(Exception):
    """A typed HTTP API failure safe to show to the operator."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class AdminContext:
    server: V2Server
    api: AdminConsoleServer
    config: AdminConfig
    world_path: Path
    loop: asyncio.AbstractEventLoop
    dist_dir: Path
    token: bytes


class AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], context: AdminContext):
        self.context = context
        super().__init__(address, AdminRequestHandler)


class AdminConsoleServer:
    """Own the HTTP thread and answer the JSON API."""

    def __init__(
        self,
        server: V2Server,
        config: AdminConfig,
        world_path: str | Path,
        *,
        loop: asyncio.AbstractEventLoop,
        dist_dir: str | Path | None = None,
    ):
        if not config.enabled:
            raise AdminConsoleError("the admin console is disabled")
        if not config.token:
            raise AdminConsoleError(
                "the admin console is enabled but no token is configured; "
                "set [admin].token or MUD_ADMIN_TOKEN"
            )
        self.server = server
        self.config = config
        self.world_path = Path(world_path)
        self.loop = loop
        self.dist_dir = Path(dist_dir) if dist_dir else DEFAULT_DIST
        self.httpd: AdminHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        context = AdminContext(
            server=self.server,
            api=self,
            config=self.config,
            world_path=self.world_path,
            loop=self.loop,
            dist_dir=self.dist_dir,
            token=self.config.token.encode("utf-8"),
        )
        try:
            self.httpd = AdminHTTPServer(
                (self.config.host, self.config.port), context)
        except OSError as exc:
            raise AdminConsoleError(
                f"could not bind the admin console to "
                f"{self.config.host}:{self.config.port}: {exc}"
            ) from exc
        self.port = int(self.httpd.server_address[1])
        if self.config.host not in LOOPBACK_HOSTS:
            logger.warning(
                "admin console bound to %s; it is not a public service and "
                "must stay on a trusted network",
                self.config.host,
            )
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="mud-admin-http",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "admin console on http://%s:%s (world: %s)",
            self.config.host, self.port, self.world_path,
        )

    def stop(self) -> None:
        httpd, thread = self.httpd, self._thread
        self.httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    # Loop bridge and database
    # ------------------------------------------------------------------

    def _on_loop(self, coro, *, timeout: float = 30.0):
        """Run one coroutine on the MUD event loop from the HTTP thread."""
        if self.loop.is_closed():
            raise AdminConsoleError("the MUD event loop is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise AdminConsoleError(
                "the MUD event loop did not answer in time") from exc

    def _snapshot(self) -> dict[str, Any]:
        return self._on_loop(self.server.admin_snapshot())

    def _read_db(self) -> sqlite3.Connection | None:
        """A read-only connection for status, or None when there is no DB."""
        path = self.server.db_path
        if path is None or not Path(path).is_file():
            return None
        try:
            conn = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise AdminConsoleError(f"could not read the database: {exc}") from exc
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _player_rows(self) -> list[dict[str, Any]]:
        conn = self._read_db()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT p.name, p.room_id, p.asleep, p.last_seen, "
                "(SELECT COUNT(*) FROM character_keys k "
                " WHERE k.player_id = p.id AND k.revoked_at IS NULL) AS keys "
                "FROM players p ORDER BY p.name COLLATE NOCASE"
            ).fetchall()
        except sqlite3.Error as exc:
            raise AdminConsoleError(
                f"could not read the player roster: {exc}") from exc
        finally:
            conn.close()
        return [
            {
                "name": row["name"],
                "room_id": row["room_id"],
                "asleep": bool(row["asleep"]),
                "last_seen": row["last_seen"],
                "key_count": int(row["keys"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def handle(
        self, method: str, path: str, body: Any
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/api/status":
            return 200, self._status()
        if method == "GET" and path == "/api/players":
            return 200, self._players()
        if method == "GET" and path == "/api/world":
            return 200, self._world()
        if method == "POST" and path == "/api/world/validate":
            errors = validate_world_payload(body, self.world_path)
            return 200, {"ok": not errors, "errors": errors}
        if method == "POST" and path == "/api/world/save":
            return 200, self._save(body)
        if method == "POST" and path == "/api/world/apply":
            return 200, self._apply(body)

        match = _REKEY_PATH.fullmatch(path)
        if method == "POST" and match is not None:
            return 200, self._rekey(urllib.parse.unquote(match.group(1)))

        raise ApiError(404, f"no such admin API route: {path}")

    def _status(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        players = self._player_rows()
        server = self.server
        return {
            "commit": server.server_commit,
            "world": snapshot["world"],
            "world_path": str(self.world_path),
            "db_path": str(server.db_path) if server.db_path else None,
            "rooms": snapshot["rooms"],
            "players": len(players),
            "live_sessions": snapshot["live_sessions"],
            "live_players": len(snapshot["live_players"]),
            "features": asdict(server.features),
            "limits": asdict(server.limits),
            "auth_limits": asdict(server.auth_limits),
        }

    def _players(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        live = {
            entry["name"].lower(): entry
            for entry in snapshot["live_players"]
        }
        rows = []
        for row in self._player_rows():
            entry = dict(row)
            entry["room_name"] = snapshot["room_names"].get(row["room_id"])
            online = live.pop(row["name"].lower(), None)
            if online is not None:
                entry["room_id"] = online["room_id"]
                entry["room_name"] = online["room_name"]
                entry["asleep"] = online["asleep"]
                entry["online"] = True
            else:
                entry["online"] = False
            rows.append(entry)
        # A guest has no durable row; only an explicitly auth-free server can
        # produce one, but the roster is honest about it when it happens.
        for entry in live.values():
            rows.append(
                {
                    "name": entry["name"],
                    "room_id": entry["room_id"],
                    "room_name": entry["room_name"],
                    "asleep": entry["asleep"],
                    "last_seen": None,
                    "key_count": 0,
                    "online": True,
                }
            )
        rows.sort(key=lambda item: item["name"].lower())
        return {
            "players": rows,
            "live_sessions": snapshot["live_sessions"],
            "live_players": len(snapshot["live_players"]),
        }

    def _world(self) -> dict[str, Any]:
        try:
            return read_world_payload(self.world_path)
        except ContentModelError as exc:
            raise ApiError(
                422, "the configured world is not valid YAML",
                errors=str(exc).splitlines(),
            ) from exc
        except WorldEditError as exc:
            raise ApiError(
                422, "the configured world could not be read",
                errors=list(exc.errors),
            ) from exc

    def _save(self, body: Any) -> dict[str, Any]:
        try:
            return save_world_payload(self.world_path, body)
        except WorldEditError as exc:
            raise ApiError(
                422, "the world was not saved",
                errors=list(exc.errors),
            ) from exc
        except (ContentModelError, WorldDataError) as exc:
            raise ApiError(
                422, "the world was not saved",
                errors=str(exc).splitlines(),
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise ApiError(
                500, f"the world could not be written: {exc}") from exc

    def _apply(self, body: Any) -> dict[str, Any]:
        force = bool(isinstance(body, dict) and body.get("force"))
        snapshot = self._snapshot()
        online = snapshot["live_players"]
        if online and not force:
            raise ApiError(
                409,
                "players are online; confirm the world reload",
                requires_force=True,
                live_sessions=snapshot["live_sessions"],
                live_players=online,
            )
        try:
            result = self._on_loop(self.server.reload_world(), timeout=60.0)
        except WorldEditError as exc:
            raise ApiError(
                422, "the world could not be applied",
                errors=list(exc.errors),
            ) from exc
        except (ContentModelError, WorldDataError) as exc:
            raise ApiError(
                422, "the world could not be applied",
                errors=str(exc).splitlines(),
            ) from exc
        except OSError as exc:
            raise ApiError(
                500, f"the world could not be applied: {exc}") from exc
        return {
            "applied": True,
            "closed": result["closed"],
            "world": {
                "name": result["world_name"],
                "epoch": result["epoch"],
            },
            "players_notified": online,
        }

    def _rekey(self, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ApiError(400, "a character name is required")
        db_path = self.server.db_path
        if db_path is None or not Path(db_path).is_file():
            raise ApiError(409, "this server has no SQLite database to rekey")
        try:
            conn = open_database(db_path)
        except (DatabaseError, sqlite3.Error) as exc:
            raise ApiError(
                500, f"could not open the database: {exc}") from exc
        try:
            result = rekey_character(conn, name)
        except RekeyError as exc:
            raise ApiError(404, str(exc)) from exc
        except sqlite3.Error as exc:
            raise ApiError(500, f"could not rekey the character: {exc}") from exc
        finally:
            conn.close()
        # The plaintext key exists only in this response body. It is never put
        # in a log line, a URL, or an error message.
        return {
            "name": result.name,
            "key": result.key,
            "revoked": len(result.revoked_key_ids),
            "message": (
                "Copy this key now. It is shown once, is not stored in "
                "plaintext, and ends the character's previous keys."
            ),
        }


class AdminRequestHandler(BaseHTTPRequestHandler):
    """One HTTP request: auth, API routing, or static SPA serving."""

    protocol_version = "HTTP/1.1"
    server: AdminHTTPServer

    # ------------------------------------------------------------------
    # Responses
    # ------------------------------------------------------------------

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
        head: bool = False,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", cache)
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_json(
        self, status: int, payload: dict[str, Any],
        *, extra: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            status, body, "application/json; charset=utf-8", extra=extra)

    def _send_error(
        self, status: int, message: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        payload = {"error": message}
        if extra:
            payload.update(extra)
        self._send_json(status, payload, extra=headers)

    # ------------------------------------------------------------------
    # Auth and input
    # ------------------------------------------------------------------

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        provided = value.strip().encode("utf-8")
        return hmac.compare_digest(provided, self.server.context.token)

    def _read_body(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(400, "invalid Content-Length") from exc
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "the request body is too large")
        if length == 0:
            return {}
        data = self.rfile.read(length)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "the request body is not valid JSON") from exc

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------

    def _serve_static(self, path: str, *, head: bool) -> None:
        root = self.server.context.dist_dir.resolve()
        index = root / "index.html"
        if not root.is_dir() or not index.is_file():
            self._send_bytes(
                503,
                MISSING_BUILD_MESSAGE.encode("utf-8"),
                "text/plain; charset=utf-8",
            )
            return

        decoded = urllib.parse.unquote(path)
        if "\x00" in decoded:
            self._send_error(400, "invalid path")
            return
        relative = decoded.lstrip("/")
        candidate = (root / relative).resolve() if relative else index
        inside = candidate == root or root in candidate.parents
        if not inside:
            self._send_error(404, "not found")
            return
        if candidate.is_dir():
            candidate = candidate / "index.html"

        if not candidate.is_file():
            # SPA fallback for client-side routes, but never for a missing
            # asset with a file extension.
            last = decoded.rsplit("/", 1)[-1]
            if "." in last:
                self._send_error(404, "not found")
                return
            candidate = index

        try:
            body = candidate.read_bytes()
        except OSError as exc:
            logger.exception("could not read admin asset %s", candidate)
            self._send_error(500, f"could not read the asset: {exc}")
            return

        content_type = _MIME_OVERRIDES.get(candidate.suffix.lower())
        if content_type is None:
            guessed, _ = mimetypes.guess_type(candidate.name)
            content_type = guessed or "application/octet-stream"
        if content_type.startswith("text/html"):
            content_type = "text/html; charset=utf-8"
        self._send_bytes(
            200,
            body,
            content_type,
            head=head,
            cache="no-cache" if candidate == index else "public, max-age=31536000",
            extra={"Content-Security-Policy": _HTML_CSP}
            if content_type.startswith("text/html") else None,
        )

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _handle(self, method: str, *, head: bool = False) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api" or path.startswith("/api/"):
            if not self._authorized():
                self._send_error(
                    401,
                    "a bearer token is required",
                    headers={"WWW-Authenticate": 'Bearer realm="mud-admin"'},
                )
                return
            if method not in ("GET", "POST"):
                self._send_error(
                    405, "method not allowed",
                    headers={"Allow": "GET, POST"})
                return
            try:
                body = self._read_body() if method == "POST" else None
                status, payload = self.server.context.api.handle(
                    method, path, body)
            except ApiError as exc:
                self._send_error(exc.status, exc.message, **exc.extra)
                return
            except (AdminConsoleError, DatabaseError) as exc:
                logger.exception("admin API unavailable")
                self._send_error(503, str(exc))
                return
            except Exception:
                logger.exception("admin API request failed")
                self._send_error(500, "the admin API request failed")
                return
            self._send_json(status, payload)
            return

        if method not in ("GET", "HEAD"):
            self._send_error(
                405, "method not allowed", headers={"Allow": "GET, HEAD"})
            return
        self._serve_static(path, head=head)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET", head=True)

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._send_error(
            405, "method not allowed", headers={"Allow": "GET, POST"})

    def do_DELETE(self) -> None:
        self._send_error(
            405, "method not allowed", headers={"Allow": "GET, POST"})

    def do_OPTIONS(self) -> None:
        # Deliberately no CORS headers: the UI is same-origin only.
        self._send_error(
            405, "method not allowed", headers={"Allow": "GET, POST"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request paths and statuses only; never headers, bodies, or tokens.
        logger.debug("admin http: " + fmt, *args)
