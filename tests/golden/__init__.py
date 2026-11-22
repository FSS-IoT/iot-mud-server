"""Golden scenarios for v2.1.3 (#83).

`harness` starts a real auth-required v2 server on port 0 with a temp SQLite
database; `scenarios` drives real websocket clients through the product world;
`events` turns `docs/PROTOCOL.md` into the event list the coverage test checks.
"""
