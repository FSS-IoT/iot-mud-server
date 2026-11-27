# Operations

This page is the offline path for the shared server: backup, restore, the
systemd unit, the database/config path, and what happens when a database is
newer than the server.

**Stop the server for both backup and restore.** The backup API produces a
consistent copy, including rows committed to the WAL, but an offline server also
means one known world epoch, no writes racing the copy, and no process holding
the file a restore replaces. With the unit below:

```bash
sudo systemctl stop iot-mud
```

Start it again only after the command has finished:

```bash
sudo systemctl start iot-mud
```

## Where the files are

| Thing | Default | Override |
|---|---|---|
| Database | `data/mud.db` | `--db PATH`, or `[storage].path` in `mud.toml` |
| Config | `./mud.toml` when it exists | `--config PATH` |
| systemd state directory | `/var/lib/iot-mud` | `StateDirectory=` in the unit |
| Service database | `/var/lib/iot-mud/mud.db` | `--db` in `ExecStart` |
| Backup file | none; `--to` is required | the operator names it |

`--db` wins over `[storage].path`; a missing default `mud.toml` is fine, a
missing `--config` file is an error.

The **backup destination is an owner decision, not a built-in path**. The owner
names it before the drill. This page uses `/var/backups/iot-mud/` only as an
example; `admin backup` has no default and will not guess.

The unit runs as a dedicated `iot-mud` system user. Create it once, before the
first start:

```bash
sudo useradd --system --home-dir /var/lib/iot-mud \
  --shell /usr/sbin/nologin iot-mud
```

Run the admin commands as root against the absolute database path. Restoring as
root creates a root-owned database file, so give it back to the service user
before starting (shown under [Restore](#restore)).

## Configuration

One TOML file holds every runtime setting. The server reads `./mud.toml` when
it exists (or the path given with `--config`), and every admin command that
touches the database accepts the same flags:

```toml
[storage]
path = "data/mud.db"

[auth]
max_attempts = 20
window_seconds = 60

[limits]
commands_per_second = 10
command_burst = 20
idle_timeout_seconds = 900

[features]
effects = false
```

The defaults are shown. A missing default `mud.toml` is fine; a missing
`--config` file is an error, and a malformed value is an error naming the file
and key. The server reads the file at startup, so restart the service after a
change.

| Section | Key | Meaning |
|---|---|---|
| `[storage]` | `path` | SQLite database path. `--db` wins over this; the built-in default is `data/mud.db`. |
| `[auth]` | `max_attempts` | Failed auth exchanges allowed per connection inside the window. A positive integer. |
| `[auth]` | `window_seconds` | Length of the auth-failure window. A positive finite number. |
| `[limits]` | `commands_per_second` | Token-bucket refill rate for `input`, per connection. A positive finite number. |
| `[limits]` | `command_burst` | Bucket size; the burst a client may spend at once. A positive integer. |
| `[limits]` | `idle_timeout_seconds` | Silence before an authenticated character sleeps. A positive finite number. |
| `[features]` | `effects` | `true` sends authored `effect` events to the room; `false` (default) keeps them on the bus and in `event_log`. |

Auth and command limits are **separate budgets**. `[auth]` bounds guessing at
a key; `[limits]` bounds command traffic from an authenticated session. A
failed login does not spend a command token, and a command burst does not count
toward the auth budget. Neither limiter stores state in SQLite, and neither is
keyed by character name: one connection's budget cannot lock a character out.

`[features].effects` changes delivery only. The engine publishes every authored
effect and appends the `event_log` row whether the flag is on or off, so
disabling effects cannot change a reply, location, inventory, lock, or NPC
position.

Character keys are not configured here. `admin rekey <name>` issues a new key
and revokes the old one; it prints the new key once and stores only the key id,
salt, and scrypt hash. See `PROTOCOL.md`, "v2.1 auth".

## Seed and reset

The server does **not** invent a world. Seed the database before the first
start; an unseeded database is not the supported deployment for players:

```bash
uv run python -m src.mud.admin seed --db data/mud.db
```

`admin seed` creates the database and applies the schema if needed, validates
the YAML world first, and records the world name and epoch. Seeding a database
that already holds a world is refused; replacing it needs an explicit reset:

```bash
uv run python -m src.mud.admin seed --db data/mud.db --reset --yes
```

`--reset` without `--yes` prints the destructive warning and exits 2. A reset
replaces rooms, exits, objects, and NPCs, clears every character's inventory,
and moves every character to the new start room. Characters and character keys
are kept; the epoch changes, so clients drop cached maps. Use
`--world PATH` to seed a world other than the built-in
`data/worlds/club_world`, and `content check` first when authoring.

## Runtime limits

Command rate limits and the idle timeout live in the same `mud.toml` as auth,
under `[limits]`:

```toml
[limits]
commands_per_second = 10
command_burst = 20
idle_timeout_seconds = 900
```

The defaults are shown: 10 commands per second, a burst of 20, and a 15-minute
idle timeout. All three must be positive; `commands_per_second` and
`idle_timeout_seconds` are finite numbers and `command_burst` is an integer.
The server reads the file at startup, so restart the service after a change.

- **Command budget.** Each connection has one token bucket, starting full with
  `command_burst` tokens. Every `input` spends a token and the bucket refills
  at `commands_per_second`. A command with no token left gets a direct
  `error{code:"rate_limited"}` that echoes the request id; the command is not
  executed and the socket stays open. The next command succeeds after enough
  time passes to refill a token. A second connection has its own bucket.
- **Auth is separate.** `[auth].max_attempts` and `[auth].window_seconds` are
  the #70 auth-failure budget, not the command bucket. A failed login neither
  spends a command token nor changes the command refill rate; a burst of
  commands does not count toward a name or an auth failure.
- **Idle timeout.** The timer starts at the last message from the client;
  server pushes do not reset it. When an authenticated client is silent for
  `idle_timeout_seconds`, the server puts the character to sleep in place,
  drops only `dropped_on_sleep` items, broadcasts the sleep `room.leave`, and
  closes the socket with `4004`. The character stays in its room as
  asleep/stone and appears in `who`; logging in again wakes it where it slept.
  Guest sessions (`auth_required = false`) are not put to sleep.

Operators tuning a playtest should raise `command_burst` for client macros, not
`commands_per_second`; the sustained rate is what bounds a flood. Lower
`idle_timeout_seconds` only when the client reconnects promptly, because an
idle close is a real disconnect after the character has already slept.

## Load smoke

SPEC v2.1.4 criterion 2 is proven by `tests/test_load.py`: 20 in-process
clients, each sending 200 commands (4,000 total) to a real `V2Server` on an
ephemeral port with a temporary SQLite database and key registration, against
the 34-room `data/worlds/club_world` product world. The gate is **zero protocol
errors** and **command p95 ≤ 100 ms**. The run also checks the v2 envelope on
every message (`v`, `seq`, `data`, request-id pairing), so a malformed or
misordered reply fails even when the latency is good.

The test is marked `load`. The PR `test` job runs the normal suite with the
marker excluded:

```bash
uv run pytest -m "not load"
```

Run just the load smoke locally:

```bash
uv run pytest tests/test_load.py -m load -q
```

Run every load-marked test (the load smoke and its p95 guard):

```bash
uv run pytest -m load -q
```

The same harness is a CLI when an operator wants the raw numbers without
pytest:

```bash
uv run python -m tools.load_smoke \
  --clients 20 --commands 200 \
  --report load-report.json
```

It prints sent/replied counts, wall time, commands per second, p50/p95/p99/max
latency, and any protocol errors, and exits non-zero when the gate fails. A
run takes about three seconds on a laptop. `--db` names a database path; the
default is a temporary directory. The server always binds port 0, so the load
smoke is safe while a real server still holds 8765.

### Why the limits are injected

The harness uses `load_limits()`: 1,000 commands/s, a burst of 1,000, and the
normal 15-minute idle timeout. This is deliberate. The production default is 10
commands/s with a burst of 20 (#85), and an unpaced 200-command burst spends
that burst in two seconds and then gets `error{rate_limited}` for most commands.
That would trip the zero-protocol-error gate while measuring the command
limiter, not load. `tests/test_limits.py` owns the limiter; the load smoke
raises the budget so every command in the run is a real engine/transport round
trip. The red evidence for #86 was exactly this: with the production default,
the same test failed with 3,500 `rate_limited` errors.

Do not "fix" a load failure by changing `[limits]` in `mud.toml`; the test
constructs its own `LimitsConfig` and never reads the service config.

### Latency is client-observed

Each sample is monotonic time from sending one `input` to receiving the direct
reply that echoes its request id. Broadcasts (`room.enter`, `player.said`,
`player.inventory`, and so on) are read and validated by a background reader,
not counted as command latency. Counting them would let a client that never
drains its socket report a low p95 while the server is blocked on backpressure;
the harness deliberately keeps reading.

The mixed script cycles through `look`, `who`, `inventory`, `examine`, movement
between two rooms, `say`, and `read`, so room-state rendering and broadcast
fan-out are under load too. A p95 failure therefore points at the whole path,
not one command. The clients also close together at the end, which exercises
the simultaneous-disconnect room cleanup fixed with this ticket.

### Nightly workflow

`load-nightly` runs daily at **04:17 UTC** and can be started by hand with
"Run workflow" (`workflow_dispatch`):

```text
.github/workflows/load-nightly.yml
```

It runs `uv run pytest -m load -q`, writes `load-report.json` through
`MUD_LOAD_REPORT`, and uploads it as the `load-report` artifact even when the
gate fails. The job has a 15-minute timeout.

What the numbers mean when it fails:

- **Any protocol error** is a release blocker. Read the first lines of the
  report; they name the client, command, and error code. A `rate_limited` error
  means the injected limits did not take effect, not that the server is slow.
- **p95 over 100 ms with zero errors** is usually host contention: check the
  runner's CPU and whether another job shares it. Re-run by hand before
  changing code. On the owner's laptop the expected p95 is roughly 20 ms at
  2,500 commands/s; a CI runner is slower, which is why the gate is 100 ms.
- **A timeout or short reply count** means the server stopped answering. The
  command timeout is 10 s and the whole run is capped at 180 s in the test.

## First start

Install the dependencies in the checkout once. Then seed the world before the
first start (the [seed section](#seed-and-reset) has the semantics):

```bash
cd /opt/iot-mud
uv sync --all-extras

sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin seed \
  --db /var/lib/iot-mud/mud.db

# The seed ran as root; give the state directory to the service user.
sudo chown -R iot-mud:iot-mud /var/lib/iot-mud

sudo systemctl daemon-reload
sudo systemctl enable --now iot-mud
```

`admin seed` also creates the database and applies the schema if it does not
exist yet. Use `admin seed --reset --yes` only when replacing an existing world.

## Backup

From a checkout, while the server is stopped:

```bash
uv run python -m src.mud.admin backup \
  --db data/mud.db \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

For a host installed at `/opt/iot-mud`, use that checkout's interpreter:

```bash
sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin backup \
  --db /var/lib/iot-mud/mud.db \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

A config file works in place of `--db`:

```bash
uv run python -m src.mud.admin backup \
  --config mud.toml \
  --to /var/backups/iot-mud/mud-2026-11-21.db
```

What the command does and refuses:

- It copies through SQLite's online backup API (`Connection.backup`), not a
  file copy. A committed row still sitting in `mud.db-wal` is included and the
  snapshot is internally consistent.
- It refuses a missing database, a file that is not SQLite, a foreign SQLite
  file without the MUD tables, and a database that fails `integrity_check`.
- It refuses a newer `user_version`; a newer server wrote that database.
- It refuses `--to` equal to the source database.
- It refuses to overwrite an existing `--to` file unless `--yes` is passed.
- It writes a temporary file next to the destination and renames it into place,
  so a failed copy does not leave a half-written backup under the final name.

Success prints one line:

```text
backed up data/mud.db to /var/backups/iot-mud/mud-2026-11-21.db (schema version 1)
```

The backup file is one SQLite database. Copy the single file, not `-wal` or
`-shm`; a completed backup has no sidecars. Keep it outside the repository and
outside the service's `StateDirectory`.

## Restore

```bash
sudo systemctl stop iot-mud

sudo /opt/iot-mud/.venv/bin/python -m src.mud.admin restore \
  --from /var/backups/iot-mud/mud-2026-11-21.db \
  --db /var/lib/iot-mud/mud.db \
  --yes

# The restore ran as root; the service user must be able to write the file.
sudo chown iot-mud:iot-mud /var/lib/iot-mud/mud.db

sudo systemctl start iot-mud
```

Running the restore as the service user (`sudo -u iot-mud ...`) also produces
the right owner, but then the backup file must be readable by `iot-mud`. Pick
one path and check `ls -l /var/lib/iot-mud/mud.db` before starting; root-owned
`0600` is the usual cause of an "attempt to write a readonly database" error.

Target selection is the same as every other admin command: `--db` first, then
`[storage].path` from `--config`/`mud.toml`, then the built-in default.

**Restore policy: an existing target needs `--yes`.** Restoring a backup
replaces the target database, so the command refuses to touch an existing file
without that explicit confirmation and exits 2. A fresh path does not need it.
There is no "merge": either the whole backup replaces the target or nothing
changes.

The restore refuses:

- a missing backup file;
- a file that is not SQLite;
- a valid SQLite file that is not an IoT MUD database (missing `world_meta`,
  `rooms`, `players`, `character_keys`, `objects`, `npcs`, `exits`, or
  `event_log`);
- a database that fails `integrity_check`;
- a **newer** `user_version`, with the documented message below;
- `--from` equal to the target database.

An older backup is allowed: it is migrated to this server's schema before it
replaces the target. Success names both versions:

```text
restored <backup> to <target> (migrated schema version <old> -> <current>)
```

The restore is staged in the target's directory and swapped in with an atomic
rename only after the copy and migration succeed. A failed restore leaves the
existing target untouched. After the swap, stale `-wal`, `-shm`, and `-journal`
files from the replaced database are removed; replaying them against the new
database would mix two worlds. This is another reason the server must be
offline.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | The backup or restore completed. |
| `1` | A database, backup, or filesystem problem; nothing was replaced. |
| `2` | A confirmation or usage problem: missing `--yes`, source equals target, or a bad config. |

## systemd unit

The generic unit is [`deploy/iot-mud.service`](../deploy/iot-mud.service). It is
a template, not a path baked into this checkout:

1. Copy it to `/etc/systemd/system/iot-mud.service`.
2. Replace `/opt/iot-mud` with the checkout (or installed copy) on this host.
   That directory must be the project root and must have a `.venv` created by
   `uv sync --all-extras`.
3. Reload and start:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now iot-mud
   ```

The unit sets:

- `StateDirectory=iot-mud`, so systemd creates `/var/lib/iot-mud` and makes it
  writable by the service. The database is
  `/var/lib/iot-mud/mud.db`, passed explicitly with `--db`.
- `Restart=on-failure` with a two-second delay. A clean stop is not restarted.
- `WorkingDirectory=/opt/iot-mud` and a `PATH` whose first entry is that
  checkout's `.venv/bin`, so `python -m src.mud.net.v2_server` uses the project
  dependencies.
- `User=iot-mud` and `Group=iot-mud`; the account is created once as shown
  above, and the service user must be able to read the checkout.
- Read-only system paths (`ProtectSystem=strict`, `ProtectHome=read-only`) and
  no new privileges; only the state directory is writable.

The unit binds `127.0.0.1` by default. Change `--host` to the tailnet address,
or to `0.0.0.0` only behind the tailnet-only firewall described in
[`HOSTING.md`](HOSTING.md), before members connect from other machines.

Check the unit before installing it:

```bash
systemd-analyze verify deploy/iot-mud.service
```

`systemd-analyze verify` must exit 0. The test suite runs the same check and
skips cleanly when `systemd-analyze` is not installed.

### Config file instead of a hard-coded path

The unit passes `--db`, which overrides `[storage].path`. A host that prefers a
config file can create `/opt/iot-mud/mud.toml`:

```toml
[storage]
path = "/var/lib/iot-mud/mud.db"
```

and remove `--db` from `ExecStart`. Do not point `[storage].path` at a path
outside the state directory unless the unit also grants write access to it.

## A newer `user_version`

`PRAGMA user_version` is the schema version. A database written by a newer
server must not be opened by an older one: the older code does not know the
newer tables or columns, and a downgrade migration does not exist.

The server and `admin restore` refuse with the same message:

```text
<path> has schema version <new>, newer than this server supports (<supported>); upgrade the server or restore an older backup
```

For example:

```text
/var/lib/iot-mud/mud.db has schema version 2, newer than this server supports (1); upgrade the server or restore an older backup
```

The server refuses before it binds a port. It does not migrate down, does not
open the world, and does not write. The two legal responses are:

1. run the newer server that wrote the database; or
2. restore a backup taken before the upgrade with `admin restore`.

The same check runs on a restore source, so a newer backup cannot be used to
downgrade a host. `admin restore` and `admin backup` also refuse a foreign
database rather than copying it.

## Backup drill

The owner names the destination before this runs. The drill proves the backup
is restorable without touching the live database:

1. `sudo systemctl stop iot-mud`
2. Take the backup with `admin backup --db /var/lib/iot-mud/mud.db --to <owner-destination>`.
3. Restore that backup into a scratch path, not the live one:
   `admin restore --from <owner-destination> --db /tmp/iot-mud-drill.db`.
4. Compare the world epoch and row counts in the scratch database, then delete
   it.
5. `sudo systemctl start iot-mud`

A manual copy of `mud.db` while the server is running is not a backup: it can
miss committed WAL frames or catch a write in progress. Use `admin backup`.

## Host packaging

A host module (for example the NixOS module) must mirror this page rather than
re-implement it:

- a state directory equivalent to `StateDirectory=iot-mud` and a database at
  `/var/lib/iot-mud/mud.db` (or pass the host's path explicitly with `--db`);
- `Restart=on-failure`;
- the source/checkout path as configuration, not a literal from this
  repository;
- stop the service before backup and restore;
- keep the newer-`user_version` refusal unchanged, so operators see the same
  message on every host.
