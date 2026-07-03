# Coding Bridge Agent

Run **Claude Code** or **OpenAI Codex** on your own machine and drive it from the
AceDataCloud web app (Nexior) or your phone — without exposing your machine to
the internet.

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

## Why

You want the power of Claude Code (your repo, your shell, your MCP servers) but
the convenience of kicking off and steering tasks from a browser or phone while
away from your desk. This daemon makes that possible while keeping the trust
boundary where it belongs: **on your hardware**. Every tool the agent wants to
run is surfaced to your browser as an approval prompt that you allow or deny.

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

```bash
pipx install coding-bridge      # recommended
# or
pip install coding-bridge
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

To keep the daemon (or the `coding-bridge channels start` chat bridge) running
across logout and reboot, install it as an OS service. Copy-paste templates for
each platform are in [docs/deploy/](docs/deploy/README.md): systemd (Linux),
Task Scheduler / NSSM (Windows), and launchd (macOS).

## Commands

| Command   | What it does                                              |
| --------- | -------------------------------------------------------- |
| `up`      | Pair if needed, then run (default if no command given)   |
| `pair`    | Pair this machine and exit                               |
| `run`     | Run using stored credentials (errors if not paired)      |
| `status`  | Show configuration and whether this machine is paired    |
| `logout`  | Remove stored credentials                                |

Run flags (`up` / `run`):

| Flag                    | Purpose                                              |
| ----------------------- | ---------------------------------------------------- |
| `--model`               | Default Claude model for new sessions                |
| `--cwd`                 | Default working directory for new sessions           |
| `--permission-timeout`  | Seconds to wait for a permission decision (0 = wait) |

Global flags: `--bridge-url`, `--name`, `--config-dir`.

## Chat channels (WeChat)

Besides the browser/Nexior front-end, the daemon can be driven from a **personal
WeChat account**: message the account and each message runs one Claude Code or
Codex turn whose reply is sent straight back into the chat — handy for kicking
off and steering tasks from your phone.

A small **WeChat gateway** (a separate service you run, e.g. on a CVM) bridges
WeChat and this daemon. The daemon connects _outbound_ to the gateway over a
WebSocket, so — exactly like the relay path — it opens no listening ports and
your code still only ever runs on your machine.

```
WeChat  ⇄  WeChat gateway  ⇄ (wss, outbound) ⇄  coding-bridge  ─►  Claude Code / Codex
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
| `doctor` | Validate `channels.toml` and ping every enabled gateway endpoint   |
| `smoke`  | Run one real provider turn locally to prove your provider works    |
| `start`  | Connect to each enabled gateway and serve replies until Ctrl-C     |
| `portal` | Open a local web UI to edit `channels.toml` (pick admins, trigger mode) |

After that one-time setup, day-to-day use is a **single command** —
`coding-bridge channels start` — and you steer everything from WeChat.

`channels smoke` flags: `--provider {claude,codex,copilot}` (default `claude`),
`--prompt` (default `"Reply with the single word: pong"`), `--timeout` seconds
(default 120).

To keep `channels start` running across logout/reboot, install it as a service —
templates in [docs/deploy/](docs/deploy/README.md).

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
  account; the gateway only relays messages.

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
