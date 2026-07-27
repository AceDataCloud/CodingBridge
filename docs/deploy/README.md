# Running `coding-bridge` as a service

> **Most people don't need this page.** The built-in commands handle the unit
> for you:
>
> - **Main daemon:** `coding-bridge pair` then `coding-bridge service install`
>   registers and starts it (or `brew services start coding-bridge` on
>   Homebrew) — see [../service.md](../service.md).
> - **Chat bridge:** `coding-bridge channels install-service` writes the unit
>   and prints the command(s) that enable it — see [../channels.md](../channels.md).
>
> The templates below are the manual, hand-tuned fallback — reach for them when
> you want to customize the unit (hardening, logging, an `EnvironmentFile`, a
> system-wide install). Each `channels start` example has a `run` variant: swap
> the command to run the **main daemon** instead of the chat bridge.

The `coding-bridge channels start` command is a long-running, outbound-only
daemon: it connects to each enabled WeChat gateway endpoint, relays `/ask …` messages
to a local coding agent, and posts the reply back. To keep it running across
logout / reboot, install it as an OS service.

Pick your platform:

| Platform | Template | Manager |
|---|---|---|
| Linux | [systemd.md](systemd.md) | `systemd --user` (or system-wide) |
| Windows | [windows.md](windows.md) | Task Scheduler (built-in) or NSSM |
| macOS | [launchd.md](launchd.md) | `launchd` (LaunchAgent) |

## Before you install a service

1. **Pair / configure once, interactively**, so the daemon has everything it
   needs to start unattended:
   ```bash
   coding-bridge channels init          # writes ~/.ace-bridge/channels.toml
   # edit channels.toml: set instance_id, base_url, token_env, allowed_senders,
   #   then flip enabled = true
   export WECHAT_TOKEN_MYNODE=...        # the token_env you referenced
   coding-bridge channels doctor         # verify config + token are accepted
   coding-bridge channels smoke          # verify your local claude/codex works
   ```
2. **Decide how the token reaches the service.** Services don't inherit your
   interactive shell's environment. Each template below shows where to put the
   `WECHAT_TOKEN_*` value:
   - systemd → an `EnvironmentFile=` (mode `0600`), **not** inline `Environment=`.
   - Windows → a User environment variable, or a `.env`-style wrapper script.
   - launchd → the `EnvironmentVariables` dict in the plist, or a wrapper.

   > **Never commit the token or bake it into a world-readable unit file.**
   > `channels.toml` only stores the env-var *name*; the value lives in the
   > service's private environment.

3. **Choose the account** the service runs as. The agent executes code and
   shell commands with that account's permissions — the same local trust
   boundary as an interactive session. Run it as *your own* user, not root /
   SYSTEM, unless you specifically want that.

## What the service should run

The canonical command is:

```bash
coding-bridge channels start
```

Add global flags if you don't rely on the defaults / environment:

```bash
coding-bridge channels start --config-dir /home/you/.ace-bridge
```

The process:

- runs in the foreground (does not fork) — let the service manager supervise it;
- exits `0` on `SIGINT`/`SIGTERM` (clean shutdown, all adapters closed);
- exits non-zero on a config error (`2`) or when no instances are enabled (`1`)
  — a good restart policy treats these as "fix the config", not "restart-loop".

## Restart policy guidance

- Restart on *unexpected* exit (network drop, crash) with a small backoff.
- Do **not** infinite-restart on exit code `1`/`2` (misconfiguration) — you'll
  just spin. The templates below use `on-failure` with a delay rather than
  `always`.
