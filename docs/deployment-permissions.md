# Deployment: restricting the mesh IRCd

The mesh IRCd (AgentIRC) must be reachable only by the parties that need it.
Guests and other outside users reach the mesh through the lens, never by
talking IRC to the server directly. This page describes how to restrict the
listener and how to verify it. No specific host or address is named here; the
owner supplies them during cutover.

## Who must still connect

- **The lens** (`irc-lens serve`): holds the IRC connection that the web
  console and its guests use.
- **Mesh agents**: each agent daemon connects as an IRC client (typically
  from the same machine).
- **Federation links**: peer servers listed under `links:` (below) connect
  server to server.

Everything else (the public internet, other hosts on the network) must be
refused.

## Restriction options

Apply one (or both) of these. The change itself is made by the owner during
cutover; this repo only documents it and ships a verification script.

1. **Bind address.** Start the IRCd with a listen address (`--host`, or
   `server.host` in `mesh.yaml`). The default is all interfaces, which is the
   wide-open case to avoid. Bind to loopback only when the lens and all agents
   run on the same machine and there are no inbound federation links. If
   links must reach this server, bind to the specific private interface they
   use, not all interfaces.
2. **Firewall.** Default-deny inbound traffic to the IRC port and allow only
   the lens, the agents' source addresses and the configured link peers. Use
   this when the IRCd must listen beyond loopback.

## Federation link configuration

Read from the culture server config schema (`culture_core/mesh_config.py`),
not assumed. Links live in `mesh.yaml`, nested under `server:` as a `links:`
list:

```yaml
server:
  name: <this server>
  host: <listen address>
  port: <listen port>
  links:
    - name: <peer name>
      host: <peer host>
      port: <peer port>
      trust: full   # per-link trust level
```

Link keys: `name`, `host`, `port`, `trust`. Passwords are not stored in the
file; they live in the OS credential store. `server.yaml` carries no link
configuration.

Peers configured on the deployment this was written against (names only):

- `thor`
- `orin`

Each peer's source must stay allowed through whichever restriction is applied.
Re-read `mesh.yaml` at cutover; the list above is a snapshot.

## Verification

`scripts/verify_mesh_ircd.py` makes one direct TCP connect and sends nothing.
Run it from a source that is NOT allowed (not the lens, an agent or a link):

```bash
python scripts/verify_mesh_ircd.py --host <ircd host> --port <ircd port>
```

`--host` and `--port` are required; there are no defaults. Exit codes:

- `0`: connect refused or timed out, so the restriction holds.
- `1`: connect succeeded, so the restriction is broken.
- `2`: usage error.

Also confirm from an allowed source (the lens host) that the lens still
connects, so the restriction did not cut off a required party.
