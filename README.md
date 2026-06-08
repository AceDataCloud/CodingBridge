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
│  Browser   │ ───────────────►  │ coding-bridge │ ───────────────────► │  coding-bridge-agent │
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
pipx install coding-bridge-agent      # recommended
# or
pip install coding-bridge-agent
```

For the ASCII-QR pairing helper, install the optional extra:

```bash
pipx install "coding-bridge-agent[qr]"
```

## Quick start

```bash
coding-bridge-agent up
```

On first run this pairs your machine: it prints a short pair code (and a QR
code). Open the link in Nexior, sign in with your Ace account, and enter the
code. Once claimed, the daemon stores a node token at
`~/.ace-bridge/credentials.json` (mode `0600`) and starts serving sessions.

Subsequent runs reuse the stored token, so `coding-bridge-agent up` just
connects.

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
  locally with `0600` permissions. `coding-bridge-agent logout` removes it; the
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
- **coding-bridge-agent** (this repo) — the node daemon you run locally.
- **Nexior** — the web/mobile UI that pairs nodes and renders sessions.

The wire protocol (envelope `type`s, the inner `Action`/`Event` sub-protocol)
is documented in [`coding_bridge_agent/protocol.py`](coding_bridge_agent/protocol.py).

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
