# Contributing to the common server

The server is the club's shared product. The clients are individual projects;
the server is where we work together through issues, branches, tests, and
reviews.

## The loop

1. **Pick one issue and claim it.** If there is not one, file one with the
   behavior you want and why. **Assign it to yourself before the first edit**
   (`gh issue edit <n> --add-assignee @me`); one assignee at a time. Unassign
   it if you stop, so another member can pick it up. The assignee is the lock
   that prevents duplicate work. The `claim-guard` CI fails a PR when the linked
   issue is unassigned, assigned to another account, or already has an open PR.
   One issue per PR; keep it small enough for one session.
2. **Fork and branch.** Fork `FSS-IoT/iot-mud-server`, then branch from `main`:
   `fix/<issue>-<what>` or `feat/<issue>-<what>`.
3. **Write the failing check first.** A test that fails for the right reason is
   the start of the ticket. Then make it pass.
4. **Run the suite.** `uv sync --all-extras` once, then `uv run pytest`.
   The suite binds ephemeral ports, so a running server on 8765 does not
   disturb it.
5. **Open a PR.** Say what changed, link the issue (`Closes #N`), keep the
   changed paths inside the issue's `Touches:` list (or add `scope:expanded`),
   and include the test result.
6. **Request a review when it matters.** Ask for a review when the change is
   risky or cross-cutting: the protocol, persistence, auth, the world format,
   or a shared contract. Otherwise the required `test` and `claim-guard` checks
   are the gate. A requested reviewer rejects and sends back; they do not fix
   the branch.
7. **Merge** on green checks (and after a requested review). Delete the branch.

Work lands through a pull request. Do not push to `main` directly. Forking is
deliberate: it is the same workflow an open-source project uses.

## What a good ticket looks like

- One behavior, one command, or one protocol field.
- A test that fails on the parent commit and passes after the change.
- No unrelated refactoring in the same PR.
- If it changes the wire protocol: update `docs/PROTOCOL.md`,
  `docs/SESSION.md`, and the conformance test in the same PR.

## Protocol rules

- Version 2 is additive. Add fields and event types; do not rename or remove
  one. A breaking change is version 3.
- `data.text` is presentation only. Structured fields are the contract.
- A client must be able to ignore unknown event types and fields.
- Errors are non-fatal unless the close code says otherwise. Malformed input
  must not disconnect a client (`FSS-IoT/iot-mud#25`).
- The command parser resolves display names, not object ids. `object.id` is the
  stable handle a client uses for rendering and disambiguation.

## Code layout

| Path | Responsibility |
|---|---|
| `src/mud/engine/` | Authoritative state, commands, events. The brain. |
| `src/mud/net/v2_server.py` | v2 WebSocket session and transport. |
| `src/mud/protocol/` | v2 envelope, outbound types, engine adapter. |
| `src/mud/world/` | World loader. |
| `data/worlds/` | Content. |
| `tests/` | Server and engine tests; `test_protocol_v2.py` is the conformance session. |

The engine is authoritative. The server is a terminal: it never mutates world
state directly, and the protocol adapter never invents a result the engine did
not produce.

## Style and checks

- Python 3.12+; asyncio; type hints on public functions.
- `uv run pytest` must pass. CI runs it on every PR.
- `uv run ruff check .` reports existing findings in the imported code; do not
  reformat unrelated files. New files should be clean.
- No secrets, `.env` contents, database files, or server addresses in a commit.

## Issues and milestones

- **v2.0 — common server parity** is complete. The **v2.1** contract is
  [`SPEC.md`](SPEC.md); the fresh set is milestones **v2.1.0–v2.1.4**, issues
  **#62–#91**. Start with a claimed ticket; no v2.1 code lands without one.
- **Assignee = in progress; unassigned = free.** That is how two agents avoid
  picking the same ticket.
- The 28 `M1` tickets are closed as superseded by the fresh set. The `M2`–`M5`
  epics remain parked for v2.2+. See `docs/ROADMAP.md`.
- `good first issue` is for a self-contained ticket with a clear test.

## Credit and license

The engine, v1 server, world data, and v2 adapter derive from
`FSS-IoT/iot-mud`, originally written by Doc. Keep the credit in `NOTICE.md`.

The common server is MIT; see `LICENSE`. Doc's repository is MIT too, and the
common server's history joins his original commits.
