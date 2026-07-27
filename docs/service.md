# Running the daemon as a service

`coding-bridge up` runs in the foreground. To keep the node online across logout
and reboot, register it as a **user-scoped** OS service.

Pair once first — a service can't pair interactively:

```bash
coding-bridge pair
coding-bridge service install
```

That's it: `install` writes the unit, registers it, starts it now, and enables
it at login. On a Homebrew install, `brew services start coding-bridge` does the
same thing.

## Commands

| Command | What it does |
| --- | --- |
| `service install` | Write the unit, register it, start it now, enable at login (`--force` overwrites an existing unit) |
| `service start` | Start the installed service |
| `service stop` | Stop it (stays installed) |
| `service status` | Ask the OS service manager for its state |
| `service uninstall` | Stop, deregister, and delete the unit file |

It uses your platform's native manager — nothing to configure:

| Platform | Manager | Unit written to |
| --- | --- | --- |
| Linux | `systemd --user` | `~/.config/systemd/user/coding-bridge.service` |
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/cloud.acedata.coding-bridge.plist` |
| Windows | Task Scheduler | `~/.ace-bridge/run-daemon.cmd`, task `CodingBridge` |

## Check it's working

After `service install` the node should show online on the
[device page](https://studio.acedata.cloud/coding-bridge). To confirm locally:

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

## Notes and troubleshooting

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
- **Stale `0.1.0` install on Windows** — if `pip` reports an old editable
  install or `coding_bridge` cannot be imported, remove it before upgrading:
  ```powershell
  py -m pip uninstall -y coding-bridge
  py -m pip install --user --no-cache-dir coding-bridge
  ```

For the WeChat/Telegram chat bridge, the counterpart is
`coding-bridge channels install-service` — note it only *writes* the unit and
prints the command(s) that enable it, where `service install` registers and
starts the daemon for you. See [channels.md](channels.md). Hand-written
copy-paste templates for both (systemd, launchd, Task Scheduler / NSSM) live in
[deploy/](deploy/README.md) if you'd rather configure the unit yourself.
