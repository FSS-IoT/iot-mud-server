# Glossary

This page defines the common terms once, so new members and new agents share
one meaning for each term.

## Terms

**client** — A program that connects to the server over WebSocket and speaks
the protocol. Each member client is a separate project; the protocol is the
shared contract.

**server** — The common server, the club's shared product. It accepts WebSocket
connections, runs the engine, and sends events. It is a terminal: it never
changes world state on its own.

**engine** — The authoritative part of the server, in `src/mud/engine/`. It
holds the world state, parses commands, applies them, and produces the events.

**adapter** — The protocol layer in `src/mud/protocol/` that turns an engine
result into wire events. It adds only transport concerns: the actor/room split,
the `self` flag, and broadcasts.

**event** — A server-to-client message with a `type` and a `data` payload, such
as `room.state` or `object.taken`. Most events carry a recipient-specific
`data.text`, but state-only pushes such as `player.inventory` carry none; the
structured fields are the contract.

**command** — A line a player sends in an `input` message, such as `go north`
or `take coin`. The server parses it; clients do not send structured actions.

**room** — A location in the world. It has an `id`, a `name`, a description,
exits to other rooms, and the objects, players, and NPCs inside it.

**actor** — The player whose action caused an event. Some replies, such as
`object.taken` and `player.said`, carry `self: true` for the actor, but
`room.enter` and `room.leave` go to the other occupants and have no `self`.

**item** — A kind of object a player can take, drop, and carry, such as a coin
or a key; in content, `item` is the default object kind (the others are
`container` and `readable`). Carried items appear in `player.inventory.items[]`.

**NPC** — A scripted character in a room. NPCs answer `talk` and `ask` with
authored dialogue; they are not objects, so they cannot be taken or dropped and
have no carry weight.

**wire protocol** — The JSON-over-WebSocket contract between the server and
clients, version 2, defined in `docs/PROTOCOL.md`. It names the envelope, the
client messages, the server events, and the error codes.

**conformance test** — The scripted session that proves a client speaks the
protocol correctly. `tests/test_protocol_v2.py` runs it against the server;
`docs/SESSION.md` is the full client version.
