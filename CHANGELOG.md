# Changelog

Coding Bridge ships continuously: every merge to `main` publishes a new
[CalVer](https://calver.org/) `YYYY.M.D.BUILD` version to
[PyPI](https://pypi.org/project/coding-bridge/) (`.BUILD` increments per day).
This file records notable, user-facing changes grouped by theme rather than by
individual build number.

## 2026-07-26 — Run as a service + more install channels

Keep the daemon alive across logout/reboot with a one-liner, and install from the
package managers developers already use.

### Added

- **`coding-bridge service` command** — full-lifecycle, user-scoped management of
  the main daemon as an OS service: `install` / `start` / `stop` / `status` /
  `uninstall`, driving `systemd --user` (Linux), a LaunchAgent (macOS), or a
  per-user scheduled task (Windows). `install` refuses until the machine is
  paired (a service can't pair interactively) and never runs as root/SYSTEM, so
  it keeps your Claude/Codex login.
- **Homebrew tap** — `brew tap acedatacloud/tap && brew install coding-bridge`,
  with a `service` block so `brew services start coding-bridge` works after
  pairing.
- **Windows Scoop bucket** — `scoop bucket add acedata …; scoop install
  coding-bridge`.
- **uv support** documented — `uv tool install coding-bridge` / `uvx
  coding-bridge`.

### Docs

- **README install matrix** — every channel (Homebrew / Scoop / pipx / uv / pip)
  in one table, plus a `coding-bridge status` check to confirm the install.
- **Service section rewritten** — a table of the five `service` subcommands, the
  unit location per platform, how to verify it connected (`registered with
  bridge` in `~/.ace-bridge/logs/`), and troubleshooting for the common failures
  (not paired, provider CLI not on the service's `PATH`, logout on Linux).

## 2026-07-03 — WeChat chat channels

Drive Claude Code / Codex from a personal **WeChat** account: message the account
and each message runs one provider turn whose reply is sent back into the chat.
Opt-in, gated, and outbound-only — the same local trust boundary as the
browser/Nexior path.

### Added

- **WeChat channel adapter** — outbound WSS receive loop against a WeChat gateway,
  with token redaction in logs and a non-raising REST client (`WeChatClient`).
- **`channels.toml` config** — fail-fast schema for `[[channels.wechat]]`
  instances; the token is never stored in the file (referenced via `token_env`
  or `token_file`).
- **Abuse controls** — per-instance trigger prefix, sender allowlist, per-sender
  sliding-window rate limit, and msg-id dedup, all applied before any turn runs.
- **`coding-bridge channels` CLI** — `init` (write a 0600 skeleton config),
  `doctor` (validate + probe each enabled endpoint without printing the token),
  `start` (run the adapter loop), and `smoke` (run one real provider turn
  locally, no network).
- **Provider selection** — per-instance `default_provider`; `claude`, `codex`,
  and `copilot` are validated at config-load and on the CLI.
- **Structured per-turn observability** — one content-free event per turn
  (instance, provider, outcome, latency, sizes); message text and reply body are
  never logged.
- **Deploy templates** — systemd, launchd, and Windows (Task Scheduler / NSSM)
  templates for running `channels start` across logout/reboot
  ([docs/deploy/](docs/deploy/README.md)).
- **Docs** — a "Chat channels (WeChat)" section in the README covering setup,
  commands, and the safety model.

Install the (marker) extra with `pipx install "coding-bridge[wechat]"`.

## Earlier

For changes before this file was introduced, see the
[git history](https://github.com/AceDataCloud/CodingBridge/commits/main). The
core outbound daemon — browser/Nexior pairing, local Claude Code / Codex
sessions, and per-tool approval — predates the changelog.
