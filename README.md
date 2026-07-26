# Coding Bridge Agent

Run **Claude Code** or **OpenAI Codex** on your own machine and drive it from
wherever you already are — the AceDataCloud web app (Nexior), your phone, or a
**chat account you already use (WeChat / Telegram)** — without exposing your
machine to the internet.

The agent is a small, outbound-only daemon. It connects to the
[coding-bridge](https://coding-bridge.acedata.cloud) relay, registers your
machine as a *node*, and runs local Claude Code or Codex sessions on your behalf.
Code execution, file access, and tool permissions all stay **local**. The bridge
only relays JSON messages between your browser and this daemon; it never sees
your files and never runs anything.

```
┌────────────┐   wss (Ace JWT)   ┌───────────────┐   wss (node token)   ┌──────────────────────┐
│  Browser   │ ───────────────►  │ coding-bridge │ ───────────────────► │  coding-bridge       │
│ (Nexior)   │ ◄───────────────  │    (relay)    │ ◄─────────────────── │  (this daemon)       │
└────────────┘                   └───────────────┘                      │   └─ Claude Code     │
                                                                        └──────────────────────┘
                                                                              runs on YOUR machine
```

**Two ways to drive it — same daemon, same local trust boundary:**

- **Browser / phone** — the [Nexior](https://studio.acedata.cloud) web & mobile
  app pairs your machine and renders full interactive sessions (the diagram
  above). Start it with `coding-bridge up`.
- **A chat account you already use** — message a **WeChat** or **Telegram** bot
  and every message runs one Claude Code / Codex turn, with the reply sent
  straight back into the chat. Nothing to install on your phone; great for
  kicking off and steering tasks on the go. Start it with
  `coding-bridge channels start` — see
  [Chat channels](#chat-channels-wechat--telegram) below.

Either way, code execution, files, and tool approvals never leave your hardware.

## Why

You want the power of Claude Code (your repo, your shell, your MCP servers) but
the convenience of kicking off and steering tasks from a browser, your phone, or
a WeChat / Telegram chat while away from your desk. This daemon makes that
possible while keeping the trust boundary where it belongs: **on your hardware**.
Every tool the agent wants to run is surfaced for an approval you allow or deny.

## Install

Requires Python 3.10+ and a working
[Claude Code](https://docs.claude.com/en/docs/claude-code) installation (the
agent uses `claude-agent-sdk`, which drives your local Claude Code CLI and
authentication).

To use the **Codex** provider, also install the
[Codex CLI](https://github.com/openai/codex) and sign in (`codex login`). Codex
sessions run via `codex exec`, so the session permission mode maps to a Codex
sandbox policy (plan → read-only, default/acceptEdits → workspace-write,
bypassPermissions → danger-full-access).

Pick whichever fits your setup — they all install the same `coding-bridge` CLI:

| Method | Command |
| --- | --- |
| **Homebrew** (macOS/Linux) | `brew tap acedatacloud/tap && brew install coding-bridge` |
| **Scoop** (Windows) | `scoop bucket add acedata https://github.com/AceDataCloud/scoop-bucket`<br>`scoop install coding-bridge` |
| **pipx** | `pipx install coding-bridge` |
| **uv** | `uv tool install coding-bridge` (or one-shot `uvx coding-bridge`) |
| **pip** | `pip install coding-bridge` |

Verify it landed:

```bash
coding-bridge status
```

The Homebrew formula also ships a service definition, so after pairing you can
`brew services start coding-bridge` (see *Running as a background service*).

On Windows without Scoop or `pipx`, use the Python launcher directly:

```powershell
py -m pip install --user --upgrade coding-bridge
```

If `pip` reports an old `0.1.0` editable install or `coding_bridge` cannot be
imported, remove the stale install before upgrading:

```powershell
py -m pip uninstall -y coding-bridge
py -m pip install --user --no-cache-dir "coding-bridge[wechat]"
```

For the ASCII-QR pairing helper, install the optional extra:

```bash
pipx install "coding-bridge[qr]"
```

## Quick start

```bash
coding-bridge up
```

On first run this pairs your machine: it prints a short pair code (and a QR
code). Open the link in Nexior, sign in with your Ace account, and enter the
code. Once claimed, the daemon stores a node token at
`~/.ace-bridge/credentials.json` (mode `0600`) and starts serving sessions.

Subsequent runs reuse the stored token, so `coding-bridge up` just
connects.

### Running as a background service

To keep the daemon running across logout and reboot, register it as a
**user-scoped** OS service. Pair once first — a service can't pair
interactively:

```bash
coding-bridge pair              # once, interactively
coding-bridge service install   # registers + starts it, and enables it at login
```

That's it. On a Homebrew install you can equivalently use
`brew services start coding-bridge`.

| Command | What it does |
| --- | --- |
| `service install` | Write the unit, register it, start it now, enable at login (`--force` overwrites an existing unit) |
| `service start` | Start the installed service |
| `service stop` | Stop it (stays installed) |
| `service status` | Ask the OS service manager for its state |
| `service uninstall` | Stop, deregister, and delete the unit file |

Under the hood it uses your platform's native manager — nothing to configure:

| Platform | Manager | Unit written to |
| --- | --- | --- |
| Linux | `systemd --user` | `~/.config/systemd/user/coding-bridge.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/cloud.acedata.coding-bridge.plist` |
| Windows | Task Scheduler | `~/.ace-bridge/run-daemon.cmd`, task `CodingBridge` |

**Check it's working.** After `service install` the node should show up online
in the web app. To confirm locally:

```bash
coding-bridge service status
tail -f ~/.ace-bridge/logs/agent.log     # look for "registered with bridge"
```

A healthy start logs (the first line only when it had to widen `PATH`):

```
added to PATH for CLI discovery: [...]
connected to bridge as node node_XXXXXXX
registered with bridge
```

#### Notes and troubleshooting

- **It runs as *you*, never as root/SYSTEM.** That's deliberate: the daemon
  shells out to your `claude` / `codex` CLIs and needs your login and your
  `PATH` (including nvm/volta installs, which it re-discovers on startup).
- **Don't also run `coding-bridge up` in a terminal** while the service is
  active — two daemons sharing one node token would fight over the connection
  and tear down every session, so the second one refuses to start and tells you.
- **`Not paired` on install** → run `coding-bridge pair` first.
- **Service starts then dies** → check the log above. The usual cause is an
  unpaired or revoked token. If it's instead failing to find your `claude` /
  `codex` CLI: the daemon already re-discovers nvm/volta/`~/.local/bin` on
  startup, but if you need an explicit override you must add it to the unit
  itself — the `--claude-path` flag only applies to `coding-bridge run`, and a
  service doesn't inherit your shell's exports. For systemd, add
  `Environment="CODING_BRIDGE_CLAUDE_PATH=/path/to/claude"` to
  `~/.config/systemd/user/coding-bridge.service`, then
  `systemctl --user daemon-reload && systemctl --user restart coding-bridge.service`
  (macOS: the plist's `EnvironmentVariables` dict; Windows: a `set` line in
  `run-daemon.cmd`).
- **Linux: keep it running after logout** → `sudo loginctl enable-linger $USER`.

For the WeChat/Telegram chat bridge, the parallel command is
`coding-bridge channels install-service`. Hand-written copy-paste templates for
both (systemd, launchd, Task Scheduler / NSSM) live in
[docs/deploy/](docs/deploy/README.md) if you'd rather configure the unit
yourself.

## Commands

| Command   | What it does                                              |
| --------- | -------------------------------------------------------- |
| `up`      | Pair if needed, then run (default if no command given)   |
| `pair`    | Pair this machine and exit                               |
| `run`     | Run using stored credentials (errors if not paired)      |
| `status`  | Show configuration and whether this machine is paired    |
| `logout`  | Remove stored credentials                                |
| `service` | Manage the daemon as a background OS service (see above)  |

Run flags (`up` / `run`):

| Flag                    | Purpose                                              |
| ----------------------- | ---------------------------------------------------- |
| `--model`               | Default Claude model for new sessions                |
| `--cwd`                 | Default working directory for new sessions           |
| `--permission-timeout`  | Seconds to wait for a permission decision (0 = wait) |
| `--claude-path`         | Explicit path to the `claude` CLI (nvm/volta installs) |
| `--codex-path`          | Explicit path to the `codex` CLI                     |
| `--copilot-path`        | Explicit path to the GitHub Copilot CLI              |

Global flags: `--bridge-url`, `--name`, `--config-dir`.

## Chat channels (WeChat & Telegram)

Besides the browser/Nexior front-end, the daemon can be driven from a **chat
account** — message it and each message runs one Claude Code or Codex turn whose
reply is sent straight back into the chat, handy for kicking off and steering
tasks from your phone. Two channels ship today and run side by side:

- **WeChat** — via a small self-hosted gateway (below).
- **Telegram** — via a bot you create with [@BotFather](https://t.me/BotFather),
  talking to the official Bot API directly (no gateway to run).

Both connect **outbound only** — like the relay path they open no listening
ports, and your code still only ever runs on your machine.

```
WeChat    ⭢  WeChat gateway  ⭢ (wss, outbound) ⭢
                                                 ├─►  coding-bridge  ─►  Claude Code / Codex
Telegram  ⭢  Bot API (getUpdates long-poll) ⭢───┘
```

### Setup

```bash
pipx install "coding-bridge[wechat]"   # marker extra; pulls no extra wheels
coding-bridge channels init            # writes ~/.ace-bridge/channels.toml (0600)
```

Edit `~/.ace-bridge/channels.toml` — uncomment the `[[channels.wechat]]` block
and fill it in:

```toml
[[channels.wechat]]
instance_id = "my-wechat"               # unique per instance
base_url = "http://127.0.0.1:8000"      # your WeChat gateway
token_env = "WECHAT_TOKEN_MY_WECHAT"    # env var holding the token (never the token itself)
enabled = true                          # explicit opt-in
require_approval = false                # true = hold tool use for Approve/Deny in the portal

trigger_prefix = "/ask "                # prefix to require; "" = free-form (reply to every message)
allowed_senders = ["wxid_your_own_id"]  # allowlist; empty = allow all
allowed_groups = []                     # groups the bot may answer in; empty = all groups
rate_limit_per_min = 6                  # per-sender sliding window; 0 disables
dedup_window_seconds = 300.0            # drop upstream retries; 0 disables
```

The **token never lives in the file** — it references an env var (`token_env`)
or a secrets-file path (`token_file`). Export it before starting:

```bash
export WECHAT_TOKEN_MY_WECHAT="…"
```

Windows PowerShell:

```powershell
$env:WECHAT_TOKEN_MY_WECHAT = "…"
```

Windows Command Prompt:

```bat
set "WECHAT_TOKEN_MY_WECHAT=…"
```

#### Provider sign-in

Each message runs a real **Claude Code** (or Codex) turn **on your machine**, so
that CLI has to be signed in first. Sign in the usual way, or route the provider
through **AceDataCloud** with your `api.acedata.cloud` key:

```bash
# Claude Code → AceDataCloud
export ANTHROPIC_BASE_URL="https://api.acedata.cloud"
export ANTHROPIC_AUTH_TOKEN="…"       # your api.acedata.cloud API key
```

(Codex is analogous — sign it in, or point it at your OpenAI-compatible base URL
and key.) `channels smoke` (below) confirms this end to end: it should print the
model's reply, not `Not logged in`.

### Verify, then run

```bash
coding-bridge channels doctor   # validate config + confirm the token is accepted
coding-bridge channels smoke    # run ONE real provider turn locally (no WeChat gateway)
coding-bridge channels start    # connect and serve until Ctrl-C
```

| Command  | What it does                                                       |
| -------- | ------------------------------------------------------------------ |
| `init`   | Write a skeleton `channels.toml` (refuses to overwrite)            |
| `doctor` | Validate `channels.toml` and ping every enabled channel            |
| `smoke`  | Run one real provider turn locally to prove your provider works    |
| `start`  | Connect to each enabled channel and serve replies until Ctrl-C     |
| `portal` | Open a local web UI to edit `channels.toml` (pick admins, trigger mode) |

After that one-time setup, day-to-day use is a **single command** —
`coding-bridge channels start` — and you steer everything from WeChat or Telegram.

`channels smoke` flags: `--provider {claude,codex,copilot}` (default `claude`),
`--prompt` (default `"Reply with the single word: pong"`), `--timeout` seconds
(default 120).

To keep `channels start` running across logout/reboot, install it as a service —
templates in [docs/deploy/](docs/deploy/README.md).

### Telegram

Telegram needs no gateway — create a bot and point the daemon at it:

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the
   **bot token** it gives you.
2. Add a `[[channels.telegram]]` block to `~/.ace-bridge/channels.toml`
   (`coding-bridge channels init` writes a commented example):

```toml
[[channels.telegram]]
instance_id = "my-telegram"                 # unique per instance
token_env = "TELEGRAM_TOKEN_MY_TELEGRAM"    # env var holding the bot token (never the token itself)
enabled = true                              # explicit opt-in
require_approval = false                    # true = hold tool use for Approve/Deny in the portal

trigger_prefix = "/ask "                    # prefix to require; "" = reply to every message
allowed_senders = ["123456789"]             # numeric Telegram user ids; empty = allow all
allowed_groups = []                         # group/supergroup chat ids (negative); empty = all groups
rate_limit_per_min = 6                      # per-sender sliding window; 0 disables
dedup_window_seconds = 300.0                # drop duplicate updates; 0 disables
```

Export the token, then verify and run — the **same** `doctor` / `start` commands
drive every channel at once, WeChat and Telegram together:

```bash
export TELEGRAM_TOKEN_MY_TELEGRAM="123456:ABC-…"
coding-bridge channels doctor   # getMe confirms the bot token is accepted
coding-bridge channels start    # long-polls Telegram + serves WeChat, together
```

Find your own numeric id by messaging [@userinfobot](https://t.me/userinfobot). In
a group, add the bot and keep a `trigger_prefix` so it only answers on `/ask …`
(Telegram may also require disabling the bot's privacy mode in BotFather to see
group messages). Running a self-hosted Bot API server? Set `api_base` on the
block (defaults to `https://api.telegram.org`).

### Portal — edit the config in your browser

Hand-editing `channels.toml` means knowing each admin's raw `wxid`. Instead, run
the portal:

```bash
coding-bridge channels portal        # opens http://127.0.0.1:8765/?token=…
```

It serves a **localhost-only** page (bound to 127.0.0.1, gated by a one-time
token printed to the console) that talks to your WeChat gateway so you can:

- **pick admins by searching your contacts** (name or id, with avatars) instead
  of pasting `wxid`s — this fills `allowed_senders`;
- **toggle Free-form ⇄ Require prefix** (Free-form = `trigger_prefix = ""`, reply
  to every message; Prefix = only when a message starts with `/ask `);
- **pick which groups the bot may answer in** (checkboxes → `allowed_groups`;
  none checked = every group), choose the provider, and enable/disable the instance.
- **sign in by QR** — if the account is signed out, the portal shows the gateway's
  login QR and continues automatically once you scan it.
- **approve tool actions live** — when an instance sets `require_approval = true`,
  anything the agent wants to run on your machine waits in the portal for your
  **Approve / Deny** instead of running unattended.

Saving writes `channels.toml` — restart `channels start` to apply. The gateway
token stays server-side and never reaches the browser. Flags: `--port` (default
`8765`), `--no-open` (don't launch a browser).

### Example

In WeChat, message your bridged account (the `default_provider` runs the turn):

```
You:  /ask what git branch am I on and is the tree clean?
Bot:  You're on `main` with a clean working tree — nothing staged or modified.
```

### Safety

- **Off by default.** Every instance is `enabled = false` until you flip it, so a
  stray `channels.toml` never puts an unsupervised bot online.
- **Gated inbound.** Trigger prefix, sender allowlist, per-sender rate limit, and
  dedup all run _before_ any provider turn.
- **No content in logs.** Only sizes and outcome codes are recorded per turn — the
  message text and the reply body are never written to logs.
- **Same local trust boundary.** Provider turns run on your machine under your
  account; the channel (WeChat gateway or Telegram Bot API) only carries messages.

## Configuration

All settings can come from the environment (see [.env.example](.env.example)):

| Variable                           | Default                                          | Meaning                                  |
| ---------------------------------- | ------------------------------------------------ | ---------------------------------------- |
| `CODING_BRIDGE_URL`                | `https://coding-bridge.acedata.cloud`            | Relay base URL                           |
| `CODING_BRIDGE_NODE_NAME`          | hostname                                         | Display name for this node               |
| `CODING_BRIDGE_CONFIG_DIR`         | `~/.ace-bridge`                                  | Credential storage directory             |
| `CODING_BRIDGE_HEARTBEAT_INTERVAL` | `15`                                             | Heartbeat seconds                        |
| `CODING_BRIDGE_PERMISSION_TIMEOUT` | `300`                                            | Permission wait seconds (`0` = forever)  |
| `CODING_BRIDGE_MODEL`              | —                                                | Default model                            |
| `CODING_BRIDGE_CLAIM_URL`          | `https://studio.acedata.cloud/coding-bridge?code={code}`| Pairing claim link template      |

CLI flags override environment values.

## Security model

- **Execution is local.** The bridge is a dumb relay. Your code, files, and
  shell only ever touch this machine.
- **Outbound only.** The daemon makes a single outbound WebSocket connection. It
  opens no listening ports.
- **Per-tool approval.** Each tool the agent wants to use is relayed to your
  browser as a permission request. Nothing runs until you allow it; a configurable
  timeout denies by default.
- **Scoped token.** Pairing yields a node token tied to your Ace account, stored
  locally with `0600` permissions. `coding-bridge logout` removes it; the
  bridge can revoke it server-side.
- **Directory browser & images stay local.** The `fs.list` action lets the
  paired browser list directories to pick a working directory, and pasted images
  are decoded to `<cwd>/.tmp/images/`. Both run within the OS permissions of the
  account running the daemon — the same local trust boundary as a session.
- **Your Claude auth stays put.** The agent uses your existing local Claude Code
  authentication via `claude-agent-sdk`.

## How it fits together

- **coding-bridge** — the stateful relay (one component of the AceDataCloud
  platform) that authenticates browsers (Ace JWT) and nodes (node token), and
  forwards messages between them.
- **coding-bridge** (this repo) — the node daemon you run locally.
- **Nexior** — the web/mobile UI that pairs nodes and renders sessions.

The node is the source of truth for the options the UI offers: it answers
`capabilities.get` with the providers it supports and each one's models, effort
tiers, and permission modes (see [`capabilities.py`](coding_bridge/capabilities.py)).
The browser renders whatever the node reports, so adding a model is a node-side
change — never a web rebuild.

The wire protocol (envelope `type`s, the inner `Action`/`Event` sub-protocol)
is documented in [`coding_bridge/protocol.py`](coding_bridge/protocol.py).

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
```

## Attribution & license

This project is licensed under **AGPL-3.0-or-later** (see [LICENSE](LICENSE)).

The remote permission-relay design — forwarding a coding agent's tool-approval
decision to a remote approver — was inspired by
[VibeBridge](https://github.com/Swayyyyy/VibeBridge) (GPL-3.0). This is an
independent implementation built on the public `claude-agent-sdk`; **no
VibeBridge source code is included.** See [NOTICE](NOTICE).
