"""The client-delivered event list, derived from `docs/PROTOCOL.md`.

The golden coverage test must fail when a new event row appears in the
protocol without a scenario that delivers it. Parsing the document (rather
than keeping a second hand-written list) is what makes that true: a protocol
change is the source, and the scenarios have to catch up.

Two kinds of row are deliberately not client deliveries:

* `shutdown` is documented in the Session table but no v2.1 server path emits
  it, so no golden scenario can observe it. It stays documented; a future
  graceful-shutdown ticket removes the exception and adds the scenario.
* `effect` belongs to #84. While `features.effects=false` the bus and event log
  see it and clients never do (SPEC v2.1.3 criterion 5), so it is not a
  missing client event. If #84 starts delivering it to clients while effects
  are off, the coverage test fails and the list needs revisiting.

`NOT_CLIENT_DELIVERED` is allowed to name an event that is not in the document
yet, so #84 can add its row without breaking #83's parser; only documented
events are subtracted when the missing set is computed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = REPO_ROOT / "docs" / "PROTOCOL.md"

# Event types a client can receive, but which no golden scenario can observe in
# this build. The reason is part of the record; do not add an entry for an
# event the server actually delivers.
NOT_CLIENT_DELIVERED: dict[str, str] = {
    "shutdown": (
        "documented in the Session table, but no v2.1 server path emits it; "
        "a graceful-shutdown ticket owns it"
    ),
    "effect": (
        "#84: bus/event-log only while features.effects=false "
        "(SPEC v2.1.3 criterion 5)"
    ),
}

# Table rows look like `| `room.state` | ... |` in the server-event tables.
# The command map also has backticked cells; only dotted names and the four
# single-word events are protocol events, so commands like `look` or `who` are
# never mistaken for one.
_EVENT_ROW = re.compile(
    r"^\|\s*`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?)`\s*\|"
)
_SINGLE_WORD_EVENTS = frozenset(
    {"hello", "error", "notice", "shutdown", "effect"}
)


def parse_protocol_events(path: str | Path = PROTOCOL_PATH) -> set[str]:
    """Every event type named in a `PROTOCOL.md` table's first cell.

    Parsing the whole document (not only the Server events section) also
    catches tables that a later additive section adds, such as #84's effects
    table. A prose mention is not a table row and is ignored, which is what
    keeps the reserved names out of the delivered set.
    """
    events: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _EVENT_ROW.match(line)
        if match is None:
            continue
        name = match.group(1)
        if "." in name or name in _SINGLE_WORD_EVENTS:
            events.add(name)
    return events


def delivered_events(events: set[str]) -> set[str]:
    """The documented events the coverage test requires a scenario to show."""
    return events - set(NOT_CLIENT_DELIVERED)
