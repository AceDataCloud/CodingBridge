# Chat channels (WeChat & Telegram)

Besides the browser front-end, the daemon can be driven from a **chat account**
— message it and each message runs one Claude Code or Codex turn whose reply is
sent straight back into the chat. Handy for kicking off and steering tasks from
your phone. Two channels ship today and run side by side:

- **WeChat** — via a small self-hosted gateway.
- **Telegram** — via a bot you create with [@BotFather](https://t.me/BotFather),
  talking to the official Bot API directly (no gateway to run).

Both connect **outbound only** — like the relay path they open no listening
ports, and your code still only ever runs on your machine.

```
WeChat    ⭢  WeChat gateway  ⭢ (wss, outbound) ⭢
                                                 ├─►  coding-bridge  ─►  Claude Code / Codex
Telegram  ⭢  Bot API (getUpdates long-poll) ⭢───┘
```

## Commands

| Command | What it does |
| --- | --- |
| `init` | Write a skeleton `channels.toml` (refuses to overwrite) |
| `enable` | Add/enable a WeChat instance from a URL or env vars (no manual editing) |
| `doctor` | Validate `channels.toml` and ping every enabled channel |
| `smoke` | Run one real provider turn locally to prove your provider works |
| `start` | Connect to each enabled channel and serve replies until Ctrl-C |
| `portal` | Open a local web UI to edit `channels.toml` (pick admins, trigger mode) |
| `install-service` | Write an OS service unit that runs `channels start` at login/boot (writes the file only — it prints the command(s) that enable it) |

## WeChat setup

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

Windows PowerShell: `$env:WECHAT_TOKEN_MY_WECHAT = "…"`.
Windows Command Prompt: `set "WECHAT_TOKEN_MY_WECHAT=…"`.

## Telegram setup

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

Find your own numeric id by messaging [@userinfobot](https://t.me/userinfobot). In
a group, add the bot and keep a `trigger_prefix` so it only answers on `/ask …`
(Telegram may also require disabling the bot's privacy mode in BotFather to see
group messages). Running a self-hosted Bot API server? Set `api_base` on the
block (defaults to `https://api.telegram.org`).

## Provider sign-in

Each message runs a real **Claude Code** (or Codex) turn **on your machine**, so
that CLI has to be signed in first. Sign in the usual way, or route the provider
through **AceDataCloud** with your `api.acedata.cloud` key:

```bash
# Claude Code → AceDataCloud
export ANTHROPIC_BASE_URL="https://api.acedata.cloud"
export ANTHROPIC_AUTH_TOKEN="…"       # your api.acedata.cloud API key
```

(Codex is analogous — sign it in, or point it at your OpenAI-compatible base URL
and key.) `channels smoke` confirms this end to end: it should print the model's
reply, not `Not logged in`.

## Verify, then run

```bash
export TELEGRAM_TOKEN_MY_TELEGRAM="123456:ABC-…"   # and/or the WeChat token
coding-bridge channels doctor   # validate config + confirm every token is accepted
coding-bridge channels smoke    # run ONE real provider turn locally (no channel/network)
coding-bridge channels start    # long-polls Telegram + serves WeChat, together
```

The same `doctor` / `start` commands drive every channel at once. After the
one-time setup, day-to-day use is a single command — `coding-bridge channels
start` — and you steer everything from WeChat or Telegram.

`channels smoke` flags: `--provider {claude,codex,copilot}` (default `claude`),
`--prompt` (default `"Reply with the single word: pong"`), `--timeout` seconds
(default 120).

To keep `channels start` running across logout/reboot:

```bash
coding-bridge channels install-service
```

Unlike `coding-bridge service install`, this **writes the unit file only** and
prints the command(s) that actually enable and start it (two on systemd, one
elsewhere) — run them to finish. Hand-written unit templates live in
[deploy/](deploy/README.md).

## Portal — edit the config in your browser

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
  none checked = every group), choose the provider, and enable/disable the instance;
- **sign in by QR** — if the account is signed out, the portal shows the gateway's
  login QR and continues automatically once you scan it;
- **approve tool actions live** — when an instance sets `require_approval = true`,
  anything the agent wants to run on your machine waits in the portal for your
  **Approve / Deny** instead of running unattended.

Saving writes `channels.toml` — restart `channels start` to apply. The gateway
token stays server-side and never reaches the browser. Flags: `--port` (default
`8765`), `--no-open` (don't launch a browser).

## Example

In WeChat, message your bridged account (the `default_provider` runs the turn):

```
You:  /ask what git branch am I on and is the tree clean?
Bot:  You're on `main` with a clean working tree — nothing staged or modified.
```

## Safety

- **Off by default.** Every instance is `enabled = false` until you flip it, so a
  stray `channels.toml` never puts an unsupervised bot online.
- **Gated inbound.** Trigger prefix, sender allowlist, per-sender rate limit, and
  dedup all run _before_ any provider turn.
- **No content in logs.** Only sizes and outcome codes are recorded per turn — the
  message text and the reply body are never written to logs.
- **Same local trust boundary.** Provider turns run on your machine under your
  account; the channel (WeChat gateway or Telegram Bot API) only carries messages.
