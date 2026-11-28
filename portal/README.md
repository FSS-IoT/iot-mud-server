# IoT MUD portal (MVP)

One tailnet port in front of several game servers. The portal lists servers and
proxies the chosen WebSocket. Backends keep their normal v2 `hello` and `auth`
flow.

This is the first test slice of #185. It does **not** yet own identity, filter
the list by key, issue tickets, or enforce leases. Each backend still validates
the character key itself.

## Build

```sh
cd portal
go build -o ../iot-mud-portal .
```

Go 1.27 or newer.

## Run

```sh
./iot-mud-portal -config ~/.config/iot-mud-portal/portal.json
```

Reload the server list with `kill -HUP <pid>`.

## Config

```json
{
  "listen": "100.125.204.103:8770",
  "default_server": "main",
  "servers": [
    {
      "id": "main",
      "name": "The Commons (main)",
      "url": "ws://127.0.0.1:8774",
      "lease": "authoritative",
      "tags": ["main"]
    },
    {
      "id": "dev-fire",
      "name": "Firemaking (dev)",
      "url": "ws://127.0.0.1:8771",
      "lease": "isolated",
      "tags": ["dev", "fire"]
    }
  ]
}
```

`lease` is recorded for the next slice. The MVP does not enforce it yet.

## Client flow

Default is straight into the main server:

```json
{"v":1,"type":"portal.login","id":"1","data":{"dev":false}}
```

The portal answers `portal.enter.ok` and starts relaying. The client then reads
the backend `hello` and sends its normal v2 `auth` with the character key.

Dev servers are opt-in. After the client-local `dev` command, it reconnects and
sends:

```json
{"v":1,"type":"portal.login","id":"1","data":{"dev":true}}
```

The portal answers `portal.servers`:

```json
{
  "v": 1,
  "type": "portal.servers",
  "id": "1",
  "data": {
    "default_server": "main",
    "servers": [
      {"id":"main","name":"The Commons (main)","lease":"authoritative","default":true},
      {"id":"dev-fire","name":"Firemaking (dev)","lease":"isolated","tags":["dev"]}
    ]
  }
}
```

Then:

```json
{"v":1,"type":"portal.enter","id":"2","data":{"server":"dev-fire"}}
```

The portal answers `portal.enter.ok` and relays. A second reconnect returns to
the portal; the client-local `menu` command does that.

## Tests

```sh
cd portal
go test ./...
```

## Test stack

For the local branch test, run four backends on localhost, seed the same
character key into each database, and point the portal at them. The portal
itself does not validate the key; the backend does.

The dev opt-in is a client-side setting in this MVP. It is not access control.
A later slice reads grants from the portal identity and hides dev servers from
keys that do not have them.
