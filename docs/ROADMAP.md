# Roadmap

The common server is the club's shared product. The clients are individual
projects that speak its protocol.

## v2.0 — common server parity (current)

- The reference server in this repo: the authoritative engine and the
  `starting_village` world, over the v2 WebSocket protocol.
- Join, look, move, take/drop, say, containers, locks, light/darkness, and
  non-fatal command errors.
- A two-client conformance session (`tests/test_protocol_v2.py`) and the
  client acceptance script (`docs/SESSION.md`).
- CI runs the suite on every push and PR.

The v1 server remains in the tree as the historical implementation and for its
tests; the v2 server is the one the club runs.

## v2.1 — depth

- Name + password auth, persistent characters, SQLite write-through, YAML
  worlds.
- Social commands: `who`, `emote`, `tell`.
- `close`, `get <item> from <container>`, exit locks, and an explicit container
  `open` flag.
- NPCs, rate limits, idle timeout, and shutdown events.
- The 28 M1 tickets and the M2–M5 epics from the original
  [agentic-mud design](design/agentic-mud-v2.1.md) are inputs for this
  milestone, not the current v2.0 milestone. Re-cut them before working;
  several assume SQLite/YAML/auth that v2.0 deliberately deferred.

## v2.2 — IoT and LLM

- Semantic effect events with stable IDs, and an MQTT/webhook gateway.
- Optional LLM narration and intent fallback. The game must remain fully
  playable with the LLM off.

## Client track

- `BOOTSTRAP.md` is the onboarding path; `docs/SESSION.md` is the acceptance
  test.
- `FSS-IoT/iot-mud-client-template` is a docs-only starting point; each member
  owns their client repo and chooses the stack.
- A member is "done" when their client passes the session. Landing one server
  PR is recommended so they experience both sides.

## Open decisions

- **License.** The engine, v1 server, world, and v2 adapter derive from Doc's
  `FSS-IoT/iot-mud`. A license (for example MIT) should be agreed with Doc
  before the club redistributes or depends on it outside the club. Until then,
  this repo is public for club work but not licensed for reuse.
- **Hosting.** The club server is a LAN/tailnet instance; the repository is
  public. Server addresses and credentials do not belong in the repo.
- **The scaffold PR.** `ticket-1-scaffold` (#33) predates the common server and
  overlaps this work. Its config ideas are useful for v2.1; it should either be
  rebased onto v2.0 or closed as superseded.
