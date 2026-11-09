docker compose -p nb-demo \
  --env-file /tmp/nb-demo/work/.env \
  -f docker-compose.yml \
  -f docker-compose.multiuser.yml \
  -f /tmp/nb-demo/work/override.yml \
  up -d --build nanobot-gateway
# Multi-User Deployment

Multi-user mode lets one gateway serve several accounts. Each account gets its own workspace, sessions, memory, uploads, usage records, and MCP settings, and every account's shell commands run in a bubblewrap sandbox. This page covers the Docker Compose setup, the two files an administrator maintains, and the limits you accept by running it.

Multi-user mode is for a small group that you trust enough to share a host, not for mutually hostile tenants. Read [Accepted Risks and Known Limitations](#accepted-risks-and-known-limitations) before you expose it.

## What You Need

| Item | Notes |
|---|---|
| Docker with Compose v2 | Linux server, or Docker Desktop on macOS |
| `docker-compose.multiuser.yml` | Adds the bubblewrap permissions and passes the initial password hash to the gateway |
| `NANOBOT_INITIAL_PASSWORD_HASH` | A scrypt hash that every new account uses as its first password |
| A users file | The allowlist of account ids |
| A catalog file (optional) | The MCP servers and plugins users may switch on |

## Generate the Initial Password Hash

New accounts start with one shared initial password and are expected to change it after the first login. Only the hash is stored in configuration. Generate it with the nanobot image so the format matches what the gateway checks:

```bash
docker compose run --rm --entrypoint python nanobot-cli -c "import getpass; from nanobot.multiuser.accounts import hash_password; print(hash_password(getpass.getpass('Initial password: ')))"
```

The output looks like `scrypt$16384$8$1$<salt>$<key>`. In a source checkout you can run the same two lines with `python -c` instead.

Put the hash in a `.env` file next to `docker-compose.yml`. Single-quote the value, because Compose treats an unquoted `$` as a variable reference:

```bash
NANOBOT_INITIAL_PASSWORD_HASH='scrypt$16384$8$1$...$...'
```

The gateway refuses to start in multi-user mode without a valid hash.

## Configure Multi-User Mode

Add a `multiUser` section to `~/.nanobot/config.json`. In the container, `~/.nanobot` is mounted at `/home/nanobot/.nanobot`, so write paths as the container sees them:

```json
{
  "multiUser": {
    "enabled": true,
    "usersFile": "/home/nanobot/.nanobot/users.txt",
    "admins": ["alice"],
    "catalogFile": "/home/nanobot/.nanobot/catalog.json",
    "workspacesRoot": "/home/nanobot/.nanobot/workspaces",
    "maxActiveUsers": 32,
    "userIdleTtlS": 1800
  }
}
```

| Key | Meaning |
|---|---|
| `enabled` | Turns multi-user mode on. The gateway then requires a working sandbox and refuses to start without one |
| `usersFile` | Allowlist file. Required |
| `admins` | Account ids that may change settings, channels, and workspaces and read `/api/admin/usage`. Admins log in like everyone else, so list them in the users file too |
| `catalogFile` | Administrator MCP and plugin catalog. Omit it to offer no MCP servers |
| `workspacesRoot` | Directory holding one subdirectory per account. Required |
| `maxActiveUsers` | Soft cap on account runtimes kept in memory. Busy runtimes are never closed |
| `userIdleTtlS` | Seconds of inactivity before an account runtime is closed |
| `sandboxNet` | `none` (default) gives shell commands no network; `share` lets them use the container network |
| `allowDevSeatbelt` | macOS host development only, see [macOS](#macos) |

The WebSocket channel carries all account traffic. Accounts log in at the WebUI; the WebUI exchanges the account token for a single-use ticket and opens the WebSocket with `?ticket=`.

## Chat Models

Administrators configure providers and enable the primary and backup model presets under Settings → Models. The chat picker defaults to “Model name (Recommended)”: conversations use the current primary preset and its configured fallback sequence, including later administrator changes. Users may explicitly select an enabled model for the current conversation, or select “Model name (Recommended)” again to clear that choice. Retained but disabled presets are not available to accounts, including through slash commands. Ordinary accounts cannot change providers, credentials, or the model catalog.

If an administrator disables or deletes a selected preset, the next turn clears the conversation's obsolete selection and uses the current primary preset. Already running turns keep their admitted model. The primary preset cannot be deleted until another preset is made primary. The chat picker refreshes after runtime updates, reconnection, and returning focus to the page.

The authenticated `GET /api/chat-models` endpoint returns the primary selection, model choices, ordering, and provider display/configured status only. It excludes provider credentials, endpoints, and request overrides. It is advertised by the optional `webui.chat-models.v1` capability under WebUI protocol 1. The optional `webui.chat-model-follow.v1` capability adds the follow option and the `/model-follow` command; clients without that capability continue using their existing settings read path, which remains administrator-only in multi-user mode.

## Users File

A plain text file, one account id per line. Blank lines and lines starting with `#` are ignored:

```text
# engineering
alice
bob
carol
```

The gateway watches the file. Adding a line lets that person log in. Removing a line closes that account's open connections and stops further logins and requests; a missing file during an editor save does not revoke anyone. An account's data stays on disk when it is removed.

## Admin Catalog File

A JSON file with an `entries` list. Users can only switch an entry on or off and fill in the parameters it declares. They cannot add a server or change its command or address:

```json
{
  "entries": [
    {
      "name": "notes",
      "kind": "mcp",
      "description": "Team notes",
      "config": {"command": "notes-mcp", "env": {"NOTES_TOKEN": "{{token}}"}},
      "perUserInstance": false,
      "paramSchema": {
        "token": {"secret": true, "description": "Your personal token", "required": true}
      }
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | Letters, digits, `_` and `-`, up to 64 characters, starting with a letter or digit |
| `kind` | `mcp` (default) or `plugin` |
| `config` | Standard MCP server settings (`command`, `args`, `env`, or `url`). Required for `mcp` entries |
| `perUserInstance` | When `true`, each account gets its own server process |
| `paramSchema` | Parameters the user fills in. `secret: true` values are encrypted at rest. `required: true` blocks enabling until set |

`{{name}}` placeholders in `config` strings are replaced with the calling account's own parameter values. Users manage entries through `GET /api/mcp/catalog`, `POST /api/mcp/catalog/enable`, and `POST /api/mcp/catalog/params`.

## Start with Docker Compose

The same steps work on Linux and macOS. The container is always Linux, so the sandbox is bubblewrap in both cases.

1. Create `~/.nanobot/config.json`, the users file, and the optional catalog file as described above.
2. Create `.env` with `NANOBOT_INITIAL_PASSWORD_HASH`.
3. Start the gateway with both Compose files:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.multiuser.yml up -d --build nanobot-gateway
   docker compose -f docker-compose.yml -f docker-compose.multiuser.yml logs -f nanobot-gateway
   ```
   > for test 
   ```bash
   docker compose -p nb-demo \
    --env-file /tmp/nb-demo/work/.env \
    -f docker-compose.yml \
    -f docker-compose.multiuser.yml \
    -f /tmp/nb-demo/work/override.yml \
    up -d --build nanobot-gateway
   ```

4. Open `http://127.0.0.1:8765`, log in with an account id from the users file and the initial password, and change the password.

`docker-compose.multiuser.yml` adds `SYS_ADMIN`, `apparmor=unconfined`, `seccomp=unconfined`, and `systempaths=unconfined` to the gateway so that bubblewrap can create namespaces and mount `/proc`. These relaxations apply to the whole container; see the risks below.

### Linux

`~/.nanobot` must be writable by uid 1000 or the container entrypoint must be allowed to change its owner (it runs as root first and then drops to `nanobot`). The kernel must allow unprivileged user namespaces; on Debian and Ubuntu check `sysctl kernel.unprivileged_userns_clone` and `sysctl user.max_user_namespaces`. If the gateway exits with "bubblewrap is installed but cannot create a user namespace", fix that setting first.

### macOS

Use Docker Desktop (or another Linux VM based engine) and the steps above. Running the gateway directly on macOS is development only: it needs `multiUser.allowDevSeatbelt` set to `true`, and shell commands still fail because multi-user exec is hard-wired to bubblewrap. Do not use a native macOS gateway for real users.

## Check the Deployment

The repository includes an optional end-to-end test that builds the image, starts the gateway through both Compose files, logs two users in over the HTTP and WebSocket contract, and checks session and memory isolation, shell confinement, MCP visibility, allowlist removal, and `/api/admin/usage`. It needs Docker, a free port 8765, and no API key (it uses a scripted model):

```bash
NANOBOT_E2E_DOCKER=1 .venv/bin/python -m pytest tests/multiuser/e2e -q
```

## Accepted Risks and Known Limitations

Security boundaries:

- All accounts share one kernel and one container. A bubblewrap escape or kernel vulnerability exposes every account's data and the gateway process. The container also runs with relaxed seccomp, AppArmor, and `/proc` protections and `SYS_ADMIN`, which weakens the container boundary itself.
- The catalog address allow set is process-global. The addresses of catalog MCP servers are allowed for the SSRF guard, so any account's `web_fetch` can reach those exact addresses, not only accounts that enabled the entry.
- MCP resource and prompt calls do not carry the caller's user id (the SDK offers no metadata slot for them). Only tool calls send `_meta.user_id`. A catalog server that serves per-user data must not rely on it for resources or prompts.
- Account tokens last 12 hours and are bearer credentials. WebSocket connections use a 30-second single-use ticket instead of the token, but signed media URLs are bearer links without expiry, as in single-user mode.
- Chat ids can be squatted by someone who guesses a random id; this only blocks that chat and needs a random UUID.
- Provider usage is attributed to an account through a context variable. A provider call from a background task created outside an account's runtime would be recorded without a user id.

Behavior:

- Cron, Dream, and heartbeat run only while an account's runtime is open. After an idle eviction, and for every account after a gateway restart, schedules do not fire until that account logs in or sends a message. Idle-evicted runtimes also skip expired-session and auto-compact checks. Raise `userIdleTtlS` if this matters.
- Projected MCP servers do not reconnect on their own after a session terminates. The next enable or parameter change, or the next runtime open, reconnects them. A user's first runtime open waits for their enabled servers to connect, and first connections are serialized.
- Workspace plugin MCP declarations are not connected for accounts; only the administrator catalog is.
- Automations, workspace selection, and recovery actions are administrator-only in the WebUI. Image generation, the subagent manager, and MCP status or reload are not wired for accounts.
- `nanobot agent`, `nanobot serve`, and `Nanobot.from_config` refuse a multi-user config because they have no per-account sandbox. Accounts connect only through the WebSocket channel. Every other channel is ignored at startup and cannot be enabled at runtime.
- A failing account runtime open (for example a disk error) drops that account's queued messages with a log entry and no error shown to the user.
- Each open runtime builds its own provider instance on a config refresh, so memory grows with the number of open accounts.
- Upload capacity is counted globally, so one account can use up the space for the others.
- Deleting a user from the users file keeps their data on disk. Remove `workspaces/<id>` yourself if it must go.
