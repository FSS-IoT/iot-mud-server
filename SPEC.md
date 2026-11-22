# iot-mud-server spec — v2.1

Signed: Micah, 2026-11-21

Auth revision: Micah, 2026-11-21 — character keys replace passwords (key-only).

Reviewed: Doc's agent, 2026-11-21 — one change requested: v2.1.1 owns the
store, v2.1.2 depends on it. Applied before merge; the owner accepted with the
admin bypass.

No v2.1 code is written before the signature line exists. The merge gate is a
pull request with the required `test` and `claim-guard` checks; reviews are
requested when a change is risky or cross-cutting, not for every PR. Admins can
merge on green checks when no review was requested.

This spec covers **v2.1**. v2.0 (common-server parity) is delivered; v2.2 is a
placeholder to be grilled later. The historical design is
`docs/design/agentic-mud-v2.1.md`; where it and this spec disagree, this spec
wins.

## Scope

The club's common MUD server becomes a persistent, multi-user world. A member
registers a character with a name and receives a one-time character key,
explores 30–40 rooms with NPCs, and plays for 20 minutes with other members
without a server restart. The
server owns the world and the rules; members write their own clients against
the v2 protocol. The wire contract is the product, and it stays additive within
v2.

## Milestones

| Milestone | What it makes real | Detailed enough for tickets? |
|---|---|---|
| v2.1.0 Foundation | SQLite persistence, YAML world, config, seed/epoch, protocol metadata | yes |
| v2.1.1 Identity | Key registration/login, persistent characters, sleep/wake, admin rekey | yes |
| v2.1.2 Depth | `who`/`emote`/`tell`, `close`/`get from`, exit locks, carry limit | yes |
| v2.1.3 World | 30–40 rooms, 5+ NPCs, dialogue and wandering, effects seam | yes |
| v2.1.4 Acceptance | Rate limits, idle timeout, load, docs, ops, club playtest | yes |
| v2.2 | IoT bridge and optional LLM narration | no, grill first |

**Order:** v2.1.0 blocks identity and world and builds the write-through store
(`PlayerStore`/`WorldStore`: the interface plus the SQLite schema for location
and contents), because its durability criterion needs it. v2.1.1 owns identity
and the character lifecycle on that store; v2.1.2 depends on it for the verb
logic, so identity and depth can run in parallel after v2.1.0 without two
competing persistence layers. v2.1.3 content can start as soon as the YAML
format and validator exist. v2.1.4 closes the milestone.

## Acceptance criteria

Each criterion maps to exactly one subticket.

### v2.1.0 Foundation

- [ ] The server starts against a temporary SQLite file created from the
      schema, and a second start reuses it; `user_version` drives migrations.
- [ ] A take/drop/move is committed to SQLite before its event is sent; after a
      hard kill and restart, the mutation is still there.
- [ ] `admin seed --reset --yes` reproduces the YAML world on a clean database
      and changes the world epoch.
- [ ] `content check` rejects duplicate ids, dangling exits, unreachable rooms,
      cyclic containers, missing keys, and malformed effects.
- [ ] `starting_village.json` is converted to YAML with identical behavior under
      the v2.0 conformance suite.
- [ ] `hello` carries `server_commit`, and `PROTOCOL.md` has a changelog entry
      for every protocol change.

### v2.1.1 Identity

- [ ] A new client registers a name and receives `auth.ok` followed by
      `room.state`; the response carries a one-time, high-entropy character
      key, and the server stores only a key id and a one-way hash, never the
      key.
- [ ] Login with a valid key returns `auth.ok` without repeating the key; the
      key is shown only in the registration response and is never logged.
- [ ] An invalid key and a duplicate name return typed errors; auth attempts
      are rate-limited, and names are not locked.
- [ ] A character's room and inventory survive a server restart; login resumes
      the same room.
- [ ] Disconnecting puts the character to sleep (stone, invincible) in the
      world; `who` and room state show it asleep, `dropped_on_sleep` items drop
      into the room, the rest of its inventory stays, and login wakes it in
      place with that inventory.
- [ ] `admin rekey` issues a new key offline, revokes the old one, and keeps the
      character's room and inventory.
- [ ] A v2 client that sends `join` without authenticating gets
      `not_authenticated` and the socket stays open.

### v2.1.2 Depth

- [ ] `who` lists awake and asleep characters server-wide, `emote` is
      room-local, and `tell` reaches an awake player privately and errors for
      an asleep one.
- [ ] `close` closes an open container and `get <item> from <container>` moves
      an item to inventory, each with typed success and error events.
- [ ] A locked exit refuses movement until the matching key is used; key and
      lock are declared in content.
- [ ] Carry limit and target resolution return typed errors (`too_heavy`,
      `not_found`, `ambiguous`) and never drop the session.
- [ ] A container cannot contain itself, directly or transitively, in the
      validator and at runtime.

### v2.1.3 World

- [ ] The world has 30–40 rooms, all reachable from `start_room`, plus 5+ NPCs
      and 10+ interactive objects (container, readable, key, locked exit).
- [ ] `talk <npc>` and `ask <npc> about <topic>` return scripted dialogue, with
      a fallback for unknown topics.
- [ ] NPC wandering is deterministic for a fixed seed, runs on the 1 Hz tick,
      and never places an NPC in an invalid room.
- [ ] Golden scenarios touch every area and every event type in
      `PROTOCOL.md`.
- [ ] Effects are parsed and emitted to the bus and event log, but not sent to
      clients when `features.effects=false`; disabling effects changes no
      gameplay.

### v2.1.4 Acceptance

- [ ] Rate limits and the idle timeout are configurable; a limited command
      returns `error{rate_limited}` without dropping the socket, and an idle
      client is put to sleep.
- [ ] 20 in-process clients × 200 commands complete with zero protocol errors
      and p95 ≤ 100 ms (load marker, run nightly).
- [ ] `docs/PROTOCOL.md`, `docs/CLIENTS.md`, `docs/CONTENT.md`, and
      `docs/OPERATIONS.md` exist; every fenced example is parsed or executed in
      CI.
- [ ] `admin backup`, restore, and a systemd unit verify; a newer
      `user_version` refuses to start with the documented message.
- [ ] 3+ members connect with their own clients and play 20 minutes on a fresh
      server with no unhandled exceptions or protocol errors; command p95
      ≤ 100 ms.
- [ ] A member who has never seen the code connects using only the docs.
- [ ] `BOOTSTRAP.md` tells a client to pin a server commit; the changelog and
      `hello.server_commit` let the client see what it is missing and whether
      the protocol changed.

## Non-goals

v2.1 does **not** deliver:

- Combat, HP, death, PvP, or loot.
- LLM narration or agent NPCs (v2.2+).
- The MQTT/IoT bridge, hardware support, or procedural generation (v2.2+).
- A web UI, public-internet exposure, TLS termination, or horizontal scaling.
- In-game administration, offline mail, guilds, or accounts beyond a character
  key (no profiles, email, or self-service recovery).
- A second server. Clients are individual repos; the server is this one.

## Constraints

- Python 3.12+ and asyncio. One process, one SQLite file in WAL mode, one
  config file.
- The engine is authoritative. Write to SQLite before applying in memory; a
  write failure returns `internal` and leaves state unchanged.
- Deterministic core: seeded RNG and an injected clock; no wall-clock reads or
  unseeded randomness in game logic.
- Auth keys use OS security randomness (`secrets`), explicitly outside the
  deterministic-game rule.
- The protocol is the product: v2 changes are additive; clients ignore unknown
  events and fields. A breaking change needs a v3 and a new grill.
- Docs are host-agnostic. No server addresses, credentials, or `.env` contents
  in the repo. MIT.
- Client repos are individual. This repo owns the contract, not the clients.
- Coordination: the assignee is the lock; one open PR per issue; CI checks the
  issue link and assignee; tickets declare `Touches:` paths; branch protection
  serializes merges, with a merge queue when parallel merges begin.

## Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-11-21 | Full M1 exit, not a foundation-only slice | The club playtest is the proof; splitting it would delay the only real acceptance. |
| 2026-11-21 | Name + server-issued high-entropy character key; open registration on the tailnet; offline `rekey` | A bearer key fits CLI clients and auto-login; no password reuse or reset; admin rekey preserves work. |
| 2026-11-21 | One key per character initially; the key table allows more | A lost device can be revoked later without locking out the others; the playtest needs one. |
| 2026-11-21 | Rate-limit auth attempts; do not lock names | The key is high entropy; locking a name only enables denial of service. |
| 2026-11-21 | v2 additive with an auth phase | Existing v2 clients should not be rewritten for one new phase. |
| 2026-11-21 | World + characters + event log in SQLite, write-through | Exploration accumulates, and a restart is safe. |
| 2026-11-21 | YAML only, 30–40 rooms; agents author areas in parallel | Non-coders can contribute; the validator catches the errors parallel authors make. |
| 2026-11-21 | NPC dialogue and deterministic wandering, no LLM | Proves NPC state without a model dependency. |
| 2026-11-21 | Five sub-milestones | Foundation blocks, then depth and world parallelize, acceptance last. |
| 2026-11-21 | Assignee lock + claim guard + `Touches:` warning + branch protection (merge queue when parallel merges begin) | Agents on different machines and models must not duplicate claims or collide on files. |
| 2026-11-21 | Close the 28 old M1 tickets and cut a fresh set | They assume a different architecture; a stale ticket is a duplicate-work trap. |
| 2026-11-21 | Clients pin a server commit; changelog + `hello.server_commit` | A client can see what it is missing and whether the protocol moved. |
| 2026-11-22 | Disconnect sleeps the character in place as stone (present and invincible); only `dropped_on_sleep` items drop into the room, the rest of the inventory wakes with it | A sleeping character is not removed from the world; the chest key must be recoverable by other players, while ordinary work stays with its owner. |

## Open questions

Each has a resolution and an owner; none blocks the spec signature.

- **Playtest host and date** — a v2.1.4 ticket; the owner decides before the
  playtest. Working assumption: the owner's LAN/tailnet.
- **Recruiting 3+ members with clients** — the owner, before the playtest. The
  client template and `docs/SESSION.md` are the onboarding path.
- **Backup destination** — the ops ticket; the owner names it before the drill.
- **`tell` to an asleep player** — proposed: a typed error, no offline queue.
  Confirm at sign-off.
- **Idle timeout** — proposed default 15 minutes, configurable.
- **Self-service key rotation** — admin `rekey` for v2.1.1; an authenticated
  `rekey` command can follow after the playtest.

## Changes

- 2026-11-21: first v2.1 spec, written from the grill. v2.0 is complete; the
  28 M1 tickets are design input and will be replaced.
- 2026-11-21: auth revised to key-only. A server-issued character key replaces
  passwords; `admin rekey` replaces `set-password`; `auth.ok` is the auth
  success event; the password minimum and the name lock are removed.
- 2026-11-21: milestone split accepted from review. v2.1.1 owns the store
  (`PlayerStore`/`WorldStore`) and v2.1.2 depends on it for the verb logic.
- 2026-11-21: ticket cut resolved the store ownership. v2.1.0 builds the
  write-through store because its durability criterion needs it; v2.1.1 owns
  identity and the character lifecycle on it. No acceptance criterion moved.
- 2026-11-21: the repo-wide required approval was removed. Reviews are
  request-only for risky or cross-cutting changes; the required checks are
  `test` and `claim-guard`. CODEOWNERS and the blanket review signoff are gone.
- 2026-11-22: owner revision to v2.1.1 criterion 5. A sleeping character turns
  to stone in place (present and invincible) instead of being removed. Only
  items authored `dropped_on_sleep: true` drop into the room; the rest of the
  inventory stays and wakes with the character. The gold key is tagged; the
  content model, loader, seeder, engine, server, and protocol docs carry the
  flag.
