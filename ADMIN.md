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
- **World editor** — area/room tree, a visual world map, room form, SVG
  geometry editor, exits, objects, NPCs, dialogue, effects, flags, validate,
  save, and apply.

The frontend is **Svelte 5 + Vite + TypeScript**, plain Svelte with no SvelteKit
and no router. The backend is the Python standard library
(`ThreadingHTTPServer`); there is no new Python dependency and no aiohttp or
FastAPI. The map is hand-rolled SVG; there is no graph library.

## The world editor views

The World tab opens on a three-pane editor: the area tree, the map, and the room
inspector. A `Tiles` / `Connections` toggle above the map chooses the view;
`Tiles` is the default. The inspector has tabs for Room, Geometry, Exits,
Objects, NPCs, and Effects. Geometry remains the per-room tile editor.

### Tiles (spatial world)

Tiles builds one continuous floor plan per level from each room's `geometry`
and exits. It does not use `layout.json`.

- **Footprints** — authored geometry is used exactly. A room without geometry
  gets the same deterministic 9×7 `defaultGeometry(room)` the Geometry tab
  offers. It is drawn with a dashed outline and counted by `Generate missing
  grids`.
- **Exact placement** — cardinal exits align the two door cells and share the
  boundary wall: east/west join on a shared column, north/south on a shared row.
  One-way exits still place the target. `up`/`down` place the target on a
  different level and the levels stack vertically with headings. `in`/`out` and
  unknown directions are portal badges, not 2D placement.
- **Portals** — the first placement is exact and never moved. If a room would
  overlap an exact room or repeat a position, its dependent component is
  relocated to a nearby free slot when one exists within six cells; otherwise
  it moves to a clearly separated Portals band. A dashed connector with a
  direction and target label marks every exit that became a portal. Subtree
  geometry stays local; up/down portal levels remain separate (`Portals ·
  Ground`, `Portals · Level -1`, ...). The exact map and the start village never
  move.
- **Missing maps** — `Generate missing grids` asks once, writes the default
  9×7 grid into every room that lacks geometry in the browser draft, marks it
  dirty, and lets Save write the YAML. The grid includes the reciprocal
  doors/stairs that neighbouring exits imply, because the content geometry
  validator checks the effective exit map. The view itself never mutates
  content. The per-room Geometry tab still has `Add default grid`.
- **Warnings** — a `portals · cannot tile in 2D` banner lists every portal exit
  with source/target buttons and the reason (`relocated` or `cycle`). Genuine
  authoring errors (missing source door/stairs, self-link, unknown room) stay
  in a separate data-error box. Detached rooms have their own band. No room
  footprint overlaps another; exact/exact and same-component shared walls are
  intentional.
- **Selection and palette** — click any tile or room label to select that room
  in the inspector. Drag an `Item`, `Container`, `Readable`, or `NPC` chip onto
  a footprint, or arm a chip and click it, to add the entity to that room.
- **Camera** — drag empty canvas to pan, wheel to zoom at the pointer, and use
  `+`, `−`, and `Fit`. `Fit` frames all levels, the Portals bands, and the
  Detached band.

For `club_world`, the five authored rooms place the start village
spatially: Mysterious Forest above the Marketplace, the Alchemist Shop west,
the Old Tavern east, and the Village Gate below; the South Road continues
south. Rooms without geometry are generated around them by their exits.

### Connections (graph)

The Connections view is the earlier hand-positioned graph. It uses
`layout.json`/auto-layout and is the place for manual room placement and exit
handles.

- **Rooms** are nodes coloured by area, with the name, id, object/NPC counts, a
  `START` marker for `world.start_room`, and a red badge/border for validation
  errors. Drag a node to move it.
- **Exits** are edges. Reciprocal pairs are drawn once with an arrow and a
  direction label at both ends. Locked exits are amber; one-way exits are dashed
  blue and carry a `one-way` badge. Click an edge to select its source room on
  the Exits tab.
- **Pan/zoom** — drag empty canvas to pan, use the wheel to zoom at the pointer,
  and use `+`, `−`, `Fit`, and `Auto-layout` in the toolbar. The map opens
  centered on `start_room` at a readable zoom; `Fit` frames every room.
  `Auto-layout` recomputes positions and marks the draft dirty.
- **Add room** — click `Add room`, then click empty canvas. A small form asks for
  area, id, and name at that point. The tree's `+ room` button still adds a room
  with a generated id.
- **Connect exits** — drag a node's `N`, `E`, `S`, or `W` handle onto another
  room. In the default two-way mode the editor declares the exit once and the
  loader creates the reciprocal; check `One-way connect` to set `one_way: true`.
  Clicking a handle also arms it, so a following room click connects for
  keyboard/touch use. Self-links and duplicate links are refused; an occupied
  source direction, or an occupied opposite exit on the target, asks before
  replacing. `up`, `down`, `in`, and `out` remain in the Exits tab.
- **Palette** — drag an `Item`, `Container`, `Readable`, or `NPC` chip onto a
  room, or arm a chip and click a room. The object/NPC gets a unique id and
  sensible defaults, and the inspector switches to the right tab with the new
  entity selected.

Both views edit the same browser draft. Save, Validate, Apply, the
unsaved-change indicator, player confirmation, backups, and all existing
property editing are unchanged.

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

## Editor layout: `layout.json`

Connections positions are editor metadata, not world content. They live beside
the world at:

```text
data/worlds/club_world/layout.json
```

The file is a plain room-id map: `{"gate": {"x": 60, "y": 60}, ...}`. It is not
part of the YAML content model and never reaches the v2 wire protocol. If the
file is absent, Connections computes a deterministic layout: rooms reachable
from `start_room` are layered by exit distance, and disconnected rooms/areas
fall into a grid below. `Auto-layout` recomputes positions only when clicked.
Tiles ignores this file.

Save sanitizes the layout (finite `x`/`y`, known room ids only), writes it
atomically, and includes it in the backup when an old copy exists. A malformed
`layout.json` is treated as empty, not as a world error. Single-file worlds use
`<world>.layout.json` next to the file.

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
- The map's connect handles cover `north`, `east`, `south`, and `west`. Use the
  Exits tab for `up`, `down`, `in`, and `out`, locks, keys, labels, and editing an
  existing one-way flag.
- Tiles derives every position from geometry and exits; it has no manual room
  dragging. Use Connections for free placement and `layout.json`.
- `Generate missing grids` writes reciprocal doors/stairs as well as each
  room's declared exits. The content geometry validator uses the effective exit
  map, so a target without its own authored reciprocal would fail Save. The 5
  authored `club_world` rooms already have their reciprocal doors and are never
  rewritten.
- When a component cannot close in 2D, its rooms move to the Portals band; a
  residual internal cycle is reported as a data error and gridded in the band.
- Portal relocation prefers a free slot within six cells. Further-away space is
  left to the clearly separated Portals band instead of scattering rooms.
- The exact map never moves a first placement; a portal link is the record of
  the edge that could not be a shared wall.
- The map declares an exit once; the loader creates the reciprocal. It never
  writes both directions into YAML, because the content validator rejects the
  duplicate. `One-way connect` sets `one_way: true` on the source exit.
- The layout is free-form; there is no snapping, grouping, or per-area frame.
- The admin listener is plain HTTP. Put it behind a trusted network or a TLS
  proxy if it ever leaves localhost.
- The console is a held experimental branch. There is no PR and no merge
  commitment.

## Tests and build gates

```bash
uv run ruff check src/mud/config.py src/mud/net/v2_server.py \
  src/mud/net/admin_console.py src/mud/world/editing.py src/mud/world/loader.py
uv run pytest -q tests/test_admin_console.py
cd admin && npm ci && npm run check && npm run build
```

The normal Vite build is the local frontend gate; the branch CI may only run the
Python tests.
