# Changelog

Coding Bridge ships continuously: every merge to `main` publishes a new
[CalVer](https://calver.org/) `YYYY.M.D.BUILD` version to
[PyPI](https://pypi.org/project/coding-bridge/) (`.BUILD` increments per day).
This file records notable, user-facing changes grouped by theme rather than by
individual build number.

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
