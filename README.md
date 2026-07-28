# Coding Bridge

Run **Claude Code** or **OpenAI Codex** on your own machine, and drive it from
somewhere else — the [AceDataCloud studio](https://studio.acedata.cloud/coding-bridge)
in a browser, your phone, or a WeChat / Telegram chat.

`coding-bridge` is a small outbound-only daemon. You pair it once with your Ace
account, and after that your machine shows up as a remote-controllable device.
Your code, files, shell, and tool approvals never leave your hardware — the
relay only carries JSON messages.

## Install

Requires **Python 3.10+** and a signed-in
[Claude Code](https://docs.claude.com/en/docs/claude-code) CLI. To use the
**Codex** provider instead, install the
[Codex CLI](https://github.com/openai/codex) and run `codex login`.

| | Command |
| --- | --- |
| **pipx** (recommended) | `pipx install coding-bridge` |
| **pip** | `pip install coding-bridge` |
| **Homebrew** (macOS / Linux) | `brew tap acedatacloud/tap && brew install coding-bridge` |
| **Scoop** (Windows) | `scoop bucket add acedata https://github.com/AceDataCloud/scoop-bucket`<br>`scoop install coding-bridge` |
| **uv** | `uv tool install coding-bridge` |

On Windows without Scoop or pipx, use the Python launcher:
`py -m pip install --user --upgrade coding-bridge`.

Check it landed:

```bash
coding-bridge --version
```

## Upgrade

Releases are date-versioned (`2026.7.28.0`). Upgrade with the same tool you
installed with:

| | Command |
| --- | --- |
| **pipx** | `pipx upgrade coding-bridge` |
| **pip** | `pip install --upgrade coding-bridge` |
| **Homebrew** | `brew update && brew upgrade coding-bridge` |
| **Scoop** | `scoop update coding-bridge` |
| **uv** | `uv tool upgrade coding-bridge` |
| **Windows (py)** | `py -m pip install --user --upgrade coding-bridge` |

Confirm with `coding-bridge --version` (or `coding-bridge status`, which now
prints it too).

**A running daemon keeps the old code until you restart it** — upgrading the
package doesn't touch the service:

```bash
coding-bridge service stop
coding-bridge service install --force    # rewrite the unit with the new paths
```

Stop first, then reinstall — there is no `service restart`, and `install
--force` alone only restarts the daemon on macOS. Reinstalling also matters on
Homebrew, where the unit file names a *versioned* Cellar interpreter path that
the upgrade deletes; a stale unit then fails silently in a relaunch loop
(`launchctl print gui/$UID/cloud.acedata.coding-bridge` shows `last exit code =
78`). If you start the daemon via `brew services` instead,
`brew services restart coding-bridge` is enough.

If `brew upgrade` insists an old version is `already installed`, the local tap
clone is stuck; re-tap and retry:

```bash
brew untap acedatacloud/tap && brew tap acedatacloud/tap
brew upgrade coding-bridge
```

## Setup

**1. Get a pair code.** On the machine you want to control:

```bash
coding-bridge pair
```

It prints a short pair code, a direct link, and — with the `qr` extra — a QR
code.

**2. Claim it in the browser.** Open the link the CLI printed (it fills the code
in for you), or go to
[studio.acedata.cloud/coding-bridge](https://studio.acedata.cloud/coding-bridge),
click **Pair device**, and type the code.

Sign in with your Ace account if you aren't already. Once claimed, the daemon
stores a node token at `~/.ace-bridge/credentials.json` (mode `0600`) — you
never pair this machine again.

**3. Run it.** Foreground, to try it out:

```bash
coding-bridge up          # pairs if needed, then serves sessions until Ctrl-C
```

Or as a background service that survives logout and reboot — this is what you
want day to day:

```bash
coding-bridge service install    # registers, starts now, and enables at login
```

On a Homebrew install, `brew services start coding-bridge` starts the daemon
through Homebrew's own service manager instead — pick one, not both. Either way,
pair first (step 1): the service runs `coding-bridge run`, which exits non-zero
when unpaired.

The node should now show **online** on the device page, and you can start a
session from the browser or your phone.

```bash
coding-bridge service status              # ask the OS service manager
tail -f ~/.ace-bridge/logs/agent.log      # look for "registered with bridge"
```

> Don't run `coding-bridge up` in a terminal while the service is active — two
> daemons sharing one node token would fight over the connection, so the second
> refuses to start.

More on the service (all subcommands, where the unit file lands,
troubleshooting): **[docs/service.md](https://github.com/AceDataCloud/CodingBridge/blob/main/docs/service.md)**.

## How it works

The daemon opens **one outbound WebSocket** to the relay and waits. The browser
connects to the same relay with your Ace JWT. The relay matches the two and
forwards messages — it never sees your files and never runs anything.

```
┌────────────┐   wss (Ace JWT)   ┌───────────────┐   wss (node token)   ┌──────────────────────┐
│  Browser   │ ───────────────►  │ coding-bridge │ ───────────────────► │   coding-bridge      │
│  / phone   │ ◄───────────────  │    (relay)    │ ◄─────────────────── │   (this daemon)      │
└────────────┘                   └───────────────┘                      │    └─ Claude Code    │
                                                                        └──────────────────────┘
                                                                              runs on YOUR machine
```

That shape is the whole security model:

- **Execution is local.** Your repo, shell, and MCP servers only ever touch this
  machine.
- **Outbound only.** No listening ports, so nothing to expose or firewall.
- **Per-tool approval — in the modes that ask.** In `default` mode every tool
  the agent wants to run is relayed to you as a permission request; nothing runs
  until you allow it, and a timeout denies by default. The looser modes are
  looser on purpose: `acceptEdits` auto-accepts edits, and `bypassPermissions`
  runs everything unattended. Codex never asks interactively at all — `codex
  exec` maps the permission mode to a sandbox policy instead (plan →
  `read-only`, default/acceptEdits → `workspace-write`, bypassPermissions →
  `danger-full-access`). Pick the mode accordingly.
- **Scoped, revocable token.** Pairing yields a node token tied to your Ace
  account. `coding-bridge logout` removes it locally; the relay can revoke it.
- **Your provider auth stays put.** The agent drives your already-signed-in
  local Claude Code / Codex CLI.

Two local capabilities run inside that same boundary: `fs.list` lets the paired
browser list directories so you can pick a working directory, and pasted images
are decoded to `<cwd>/.tmp/images/`. Both act with the OS permissions of the
account running the daemon.

The node is also the source of truth for what the UI offers: it answers
`capabilities.get` with its providers, models, effort tiers, and permission
modes ([`capabilities.py`](https://github.com/AceDataCloud/CodingBridge/blob/main/coding_bridge/capabilities.py)), so adding a model is
a node-side change, never a web rebuild. The wire protocol is documented in
[`protocol.py`](https://github.com/AceDataCloud/CodingBridge/blob/main/coding_bridge/protocol.py).

## Drive it from WeChat or Telegram

Besides the browser, the same daemon can be driven from a **chat account you
already use**. Message it, and each message runs one Claude Code / Codex turn
whose reply lands back in the chat — handy for kicking off and steering tasks
from your phone with nothing to install on it.

```bash
coding-bridge channels init      # writes ~/.ace-bridge/channels.toml
coding-bridge channels portal    # pick admins/groups in a local web UI
coding-bridge channels doctor    # validate config + tokens
coding-bridge channels start     # connect and serve
```

Both channels connect **outbound only** and keep the same local trust boundary.
Every instance is disabled until you explicitly opt in, and a trigger prefix,
sender allowlist, rate limit, and dedup all run before any provider turn.

Full setup — gateway config, Telegram bot token, the approval portal, running it
as a service: **[docs/channels.md](https://github.com/AceDataCloud/CodingBridge/blob/main/docs/channels.md)**.

## Reference

Commands:

| Command | What it does |
| --- | --- |
| `up` | Pair if needed, then run (default if no command given) |
| `pair` | Pair this machine and exit |
| `run` | Run using stored credentials (errors if not paired) |
| `status` | Show the installed version, configuration, and whether this machine is paired |
| `logout` | Remove stored credentials |
| `service` | Manage the daemon as a background OS service — [docs](https://github.com/AceDataCloud/CodingBridge/blob/main/docs/service.md) |
| `channels` | Drive the daemon from WeChat / Telegram — [docs](https://github.com/AceDataCloud/CodingBridge/blob/main/docs/channels.md) |

Run flags (`up` / `run`): `--model`, `--cwd`, `--permission-timeout`,
`--claude-path`, `--codex-path`, `--copilot-path`.
Global flags: `--bridge-url`, `--name`, `--config-dir`. `coding-bridge --version`
prints the installed release.

Environment (see [.env.example](https://github.com/AceDataCloud/CodingBridge/blob/main/.env.example)) — CLI flags win:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODING_BRIDGE_URL` | `https://coding-bridge.acedata.cloud` | Relay base URL |
| `CODING_BRIDGE_NODE_NAME` | hostname | Display name for this node |
| `CODING_BRIDGE_CONFIG_DIR` | `~/.ace-bridge` | Credential storage directory |
| `CODING_BRIDGE_HEARTBEAT_INTERVAL` | `15` | Heartbeat seconds |
| `CODING_BRIDGE_PERMISSION_TIMEOUT` | `1800` | Permission wait seconds (`0` = forever) |
| `CODING_BRIDGE_MODEL` | — | Default model |
| `CODING_BRIDGE_CLAUDE_ENTRYPOINT` | `claude-vscode` | Entrypoint stamped on Claude transcripts. The default keeps bridge sessions listed in the VSCode extension and `claude --resume`; `sdk-py` hides them there |
| `CODING_BRIDGE_CLAIM_URL` | `https://studio.acedata.cloud/coding-bridge?code={code}` | Pairing claim link template |

Optional extras: `coding-bridge[qr]` for the ASCII-QR pairing helper;
`coding-bridge[wechat]` / `[telegram]` are intent markers (they pull no extra
wheels).

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -q
```

## Attribution & license

Licensed under **AGPL-3.0-or-later** (see [LICENSE](https://github.com/AceDataCloud/CodingBridge/blob/main/LICENSE)).

The remote permission-relay design — forwarding a coding agent's tool-approval
decision to a remote approver — was inspired by
[VibeBridge](https://github.com/Swayyyyy/VibeBridge) (GPL-3.0). This is an
independent implementation built on the public `claude-agent-sdk`; **no
VibeBridge source code is included.** See [NOTICE](https://github.com/AceDataCloud/CodingBridge/blob/main/NOTICE).
