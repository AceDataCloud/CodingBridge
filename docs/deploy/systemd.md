# systemd — `coding-bridge channels`

Recommended: a **user** service (`systemd --user`), so the daemon runs as you
with your Claude/Codex auth and needs no root. Fall back to a system service
only if you must start before login.

## 1. Find the executable

```bash
which coding-bridge
# e.g. /home/you/.local/bin/coding-bridge  (pipx / pip --user)
#  or  /home/you/.venvs/bridge/bin/coding-bridge  (venv)
```

Use the **absolute** path in the unit — services don't inherit your `PATH`.

## 2. Put the token in an EnvironmentFile

Never inline the secret in the unit (units are often world-readable). Create a
private env file instead:

```bash
install -m 600 /dev/null ~/.config/coding-bridge.env
cat >> ~/.config/coding-bridge.env <<'EOF'
WECHAT_TOKEN_MYNODE=paste-the-real-token-here
EOF
```

The name (`WECHAT_TOKEN_MYNODE`) must match the `token_env` in your
`channels.toml`.

## 3. Create the unit

`~/.config/systemd/user/coding-bridge-channels.service`:

```ini
[Unit]
Description=coding-bridge channels (WeChat → local coding agent)
# Only meaningful for a *system* unit; harmless for --user.
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Absolute path from step 1:
ExecStart=%h/.local/bin/coding-bridge channels start
EnvironmentFile=%h/.config/coding-bridge.env

# Restart on crash / network drop, but NOT in a tight loop, and NOT on a
# config error (exit 1 = no enabled instances, exit 2 = bad config).
Restart=on-failure
RestartSec=10
# Treat "misconfigured" as a clean stop so we don't restart-loop on it.
SuccessExitStatus=0 1 2

# Light hardening (optional but recommended). Loosen if your sessions need
# to write outside $HOME.
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=default.target
```

`%h` expands to your home directory in a user unit. For a **system** unit,
replace `%h/...` with an absolute path and add `User=you` / `Group=you` under
`[Service]`, and use `WantedBy=multi-user.target`.

## 4. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable --now coding-bridge-channels.service

# Optional: keep --user services running after you log out (so the bot
# survives an SSH disconnect / GUI logout):
loginctl enable-linger "$USER"
```

## 5. Verify

```bash
systemctl --user status coding-bridge-channels.service
journalctl --user -u coding-bridge-channels.service -f
```

You should see one `channel started: <instance_id> → <base_url>` line per
enabled instance. Send yourself a `/ask hello` on WeChat to confirm.

## Updating config

After editing `channels.toml` or the token:

```bash
systemctl --user restart coding-bridge-channels.service
```

`channels start` reads the config fresh on each start, so a restart is all it
takes.
