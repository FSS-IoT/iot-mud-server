# Hosting the common server

One authoritative server, one world, one port. This page is for the member who
hosts and for the members who connect. It is host-agnostic: the same steps work
on any machine with a Tailscale tailnet.

**The model: share the game node, not the tailnet.** A Tailscale tailnet is an
identity domain; two tailnets do not merge. You either invite a person into your
tailnet (a seat, the whole network, your ACL) or share one machine to their
tailnet (per-user, only that machine, still your ACL). For a club whose roster
changes, share the machine.

## Host checklist

### 1. Run the server on a reachable interface

The default binds `localhost`, which only works on the host itself. Seed the
database once before the first start; the world is YAML content and the
character state is durable SQLite:

```bash
uv run python -m src.mud.admin seed --db data/mud.db
./run_server.sh --host 0.0.0.0 --port 8765
```

`0.0.0.0` is safe only with the firewall rule below. On a machine with a stable
tailnet address, `--host <100.x address>` is stricter; either works.

**The world and characters are durable in v2.1.** A restart keeps the same
database, the same world epoch, and every character's room, inventory, and
sleep state. Back up the database with `admin backup` while the server is
stopped; see [`OPERATIONS.md`](OPERATIONS.md). Keep the host awake for the
session, but a restart is no longer a reset.

### 2. Open the port on the tailnet interface only

NixOS:

```nix
networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8765 ];
```

Not the global `networking.firewall.allowedTCPPorts`: that opens the port on the
LAN and every other interface. On other systems, the rule must match the
Tailscale interface (`tailscale0`) and must not open 8765 on the LAN or the
public interface.

Plain `ws://` is correct here. The tailnet is already WireGuard-encrypted; the
MUD does not need TLS, and `wss://` would add a certificate to manage for no
gain. Do not expose 8765 to the public internet.

### 3. Add one ACL grant

The host's tailnet policy must allow the people who accepted a share to reach
the game port. Tailscale is deny-by-default once the policy is edited, and a
tagged host is ACL-filtered like any other. In the admin console's **Access
controls** page, add to `grants`:

```json
{
  "grants": [
    {
      "src": ["autogroup:shared"],
      "dst": ["tag:server"],
      "ip": ["tcp:8765"]
    }
  ]
}
```

Replace `tag:server` with the host's tag, or its 100.x address. `dst` does not
accept a hostname. If the policy uses the older `acls` section instead, the
equivalent rule is:

```json
{ "action": "accept", "src": ["autogroup:shared"], "dst": ["tag:server:8765"] }
```

Audit the policy for a broad `"src": ["*"]` rule while you are there. `*`
includes shared users, so a rule intended for "everyone in my tailnet" can give
a game guest your other services. The grant above is deliberately one port.

### 4. Share the machine

Admin console -> **Machines** -> the host -> **Share**.

- Email invites are per person and revocable. A reusable link is convenient but
  is a bearer token: it works up to 1,000 times, so treat it like a password.
- The recipient must be an Owner, Admin, or IT admin of their own tailnet to
  accept. A personal Tailscale account is.
- Unused invites expire after 30 days.
- Only the individual recipient sees the shared machine, not their whole
  tailnet. Shared machines are quarantined: they can receive connections but
  cannot start them.
- If the recipient's tailnet has **Tailnet Lock** enabled, the shared node
  must be signed in that tailnet before it becomes reachable.

The recipient reaches the shared machine by its **fully qualified name**:

```text
<host>.<host-tailnet>.ts.net
```

A shared node is not reachable by the short hostname. MagicDNS must be on in
the recipient's tailnet for the name; otherwise use the 100.x address.

### 5. Publish the URL and the port

Tell members the exact WebSocket URL and port. Do not put the address in this
repository; publish it in the club channel. This page stays host-agnostic, and
the host can change without changing the protocol.

## Member checklist

1. Install Tailscale and sign in. If you do not have a tailnet, create a free
   personal one; you need to be its owner/admin to accept a share.
2. Accept the host's share invite. If your Tailscale client was on another
   tailnet, switch to the one that accepted the share.
3. Confirm the host appears:

   ```bash
   tailscale status
   tailscale ping <host>
   ```

4. Connect your client to the URL the host published:

   ```text
   ws://<host>.<host-tailnet>.ts.net:8765
   ```

   or `ws://<100.x address>:8765` when MagicDNS is off. The client should take
   the URL from configuration, not hardcode it.

5. A quick protocol check with `websocat`:

   ```bash
   websocat ws://<host>.<host-tailnet>.ts.net:8765
   ```

   The first message is `hello` with `protocol: [2]`. Send `auth` with
   `mode: "register"` once to get a key; keep it out of the repository. The
   reference client in `examples/reference_client.py` is a worked example, and
   [`CLIENTS.md`](CLIENTS.md) is the onboarding path.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The host is absent from `tailscale status` | The invite was not accepted, the client is on another tailnet, the device needs approval, or its key expired. |
| `tailscale ping <host>` fails | The share is not accepted or the host is offline. |
| Ping works, the WebSocket times out | The ACL grant is missing, or the firewall rule is not on `tailscale0`. |
| The FQDN works, the short name does not | Shared nodes are only reachable by FQDN; short MagicDNS names do not cross a share. |
| The host can connect, a member cannot | The grant is for `autogroup:shared` (share recipients), not `autogroup:member` (people you invited into your tailnet), or the policy has no rule for the port. |
| `*.server` resolves on the host but not for a member | Split DNS is a per-tailnet setting and does not follow a shared node. Use the FQDN or the 100.x address for the MUD. |

## Revoking access

Admin console -> **Machines** -> the host -> **Share** -> **Revoke**. The
recipient loses access immediately. If you invited them into the tailnet instead
of sharing, remove the user; that removes their devices. Unused share invites
expire on their own after 30 days.

## First playtest

If the members are in the same room, use the LAN and skip Tailscale entirely:
run the server on a laptop, bind `0.0.0.0`, and give the LAN address. It removes
accounts, ACLs, and share links from the event, and the protocol is the same.
Use the tailnet for members who are remote, and for later sessions.

Run one authoritative server. v2.1 keeps one world in one database, so two
hosts are two worlds even when they run the same YAML. Do not start a second
copy on another member's machine "just in case"; point every client at the one
host that owns the database.

## When the club grows

The share model has no seat limit on the host's tailnet. A **club-owned
tailnet** is the cleaner long-term home if the club wants one network for Gitea,
the MUD, and docs: one identity, MagicDNS and `*.server` names work for every
member, and the server is not tied to a personal account. Tailscale Personal is
6 users; beyond that it is a paid plan. Do not create a club tailnet just for
one playtest.

A **dedicated game node** (a small VM/container with its own Tailscale node, or
a VPS) is worth it before the roster gets large: the share exposes only the
game, not the host's other services, and the ACL has less to protect. Sharing
the host itself is fine for a first session when the grant is one port and the
policy has been audited.

## What this page does not cover

Public access. Funnel or a public reverse proxy is a different design: it needs
TLS (`wss://`), application auth policy (v2.1 has character keys, not a public
account system), rate limiting, and a security review. The tailnet is the
perimeter for the club server; keep it that way. A public deployment must not
put a bare `ws://` MUD on the internet.
