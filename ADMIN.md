# Experimental admin console and world editor

**Held branch.** This lives only on `experimental/admin-console`. It is
experimental, is not an official part of the server, has no PR, and must not be
merged to `main` by this work. The owner uses the branch directly. Everything
here can change or disappear.

The MUD server hosts a small browser console on a second HTTP port. It has
three screens:

- **Status** — commit, world name/epoch, paths, room and character counts, live
  sessions, features, and limits.
- **Players** — the SQLite roster overlaid with live engine state, plus a
  one-time rekey.
- **World editor** — area/room tree, room form, SVG geometry editor, exits,
  objects, NPCs, dialogue, effects, flags, validate, save, and apply.

The frontend is **Svelte 5 + Vite + TypeScript**, plain Svelte with no SvelteKit
and no router. The backend is the Python standard library
(`ThreadingHTTPServer`); there is no new Python dependency and no aiohttp or
FastAPI.

## Run it

The built SPA is committed, so the branch runs without Node. To use the
checked-in assets:

```bash
export MUD_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

cat > mud.toml <<'EOF'
[admin]
enabled = true
host = "127.0.0.1"
port = 8766
EOF

./run_server.sh
```

Then open <http://127.0.0.1:8766/> and paste the token. The static login page is
unauthenticated; every `/api/*` request is not. The token is kept in the tab's
`sessionStorage` and sent only in `Authorization: Bearer <token>`.

To rebuild after changing the UI:

```bash
cd admin
npm ci
npm run build
cd ..
```

`MUD_ADMIN_TOKEN` always overrides `[admin].token`, so the secret can stay out of
`mud.toml`. If `enabled = true` and no token is configured, the server refuses
to start the admin listener with a clear error. The console is **disabled by
default**.

## Security: it is not a public service

- Default bind is `127.0.0.1`. A config may set a LAN/tailnet address such as
  `host = "0.0.0.0"` or a `100.x` tailnet address, but only for a trusted
  network. The console has **no TLS**, no user accounts, and a single bearer
  token. Do not expose it to the internet.
- Every `/api/*` route requires the token. Comparison is constant-time
  (`hmac.compare_digest`). A missing or wrong token is `401`.
- The token never appears in a URL, a log line, the HTML, or the repository. Do
  not put it in `mud.toml` in a clone you might commit; prefer
  `MUD_ADMIN_TOKEN`.
- There is no CORS support. The UI and API are same-origin by construction.
- No seed, reset, or backup buttons are exposed. Destructive database operations
  stay in the offline `python -m src.mud.admin` CLI.

## Save, validate, apply

The editor reads the configured YAML world — the product world is
`data/worlds/club_world/` (`DEFAULT_WORLD`).

1. **Validate** checks the draft in the browser through the content model and
   semantic validator. It writes nothing.
2. **Save** validates the draft again, writes canonical YAML atomically, and
   keeps a timestamped backup. Each save also writes a `manifest.json` listing
   the files, sizes, and hashes copied.
3. **Apply** reloads the live engine from the **saved YAML on disk**. It is
   disabled while the editor has unsaved changes. If players are online, the UI
   asks for confirmation first.

Apply closes every MUD client socket with `1001 “World reload”`, clears the live
session maps, and swaps the engine. The admin HTTP thread stays up. SQLite
characters survive; reconnecting and logging in restores the saved room and
inventory (#71). Apply changes the live world name/epoch so clients can notice
the reload.

Backups are under:

```text
data/worlds/club_world/backups/<UTC timestamp>/
  manifest.json
  world.yaml
  areas/*.yaml
```

Saving writes **canonical YAML**: comments and authored key order are not
preserved. The old files are always in the backup directory. `git diff` on a
saved world will be noisy even for a small edit. That is the documented
tradeoff; use the backup or the previous commit when the comments matter.

## Known limitations

- Apply does not reseed SQLite. World content is always loaded from YAML, while
  the database keeps characters, keys, and their durable room/inventory rows.
  A restart preserves the YAML world but the database's seeded `world_meta`
  name/epoch is not rewritten by Apply.
- One editor, no conflict detection. Two operators saving at once get
  last-writer-wins.
- Removing the last room in an area is blocked. Removing an entire area file is
  manual; the editor writes the areas it was given and does not delete omitted
  files.
- Rekeying does not end an already-authenticated live session.
- The admin listener is plain HTTP. Put it behind a trusted network or a TLS
  proxy if it ever leaves localhost.
- The console is a held experimental branch. There is no PR and no merge
  commitment.

## Tests and build gates

```bash
uv run ruff check src/mud/config.py src/mud/net/v2_server.py \
  src/mud/net/admin_console.py src/mud/world/editing.py src/mud/world/loader.py
uv run pytest -q tests/test_admin_console.py
cd admin && npm ci && npm run build
```

The normal Vite build is the local frontend gate; the branch CI may only run the
Python tests.
