# launchd (macOS) — `coding-bridge channels`

Use a **LaunchAgent** (runs as you, after login) so the daemon inherits your
Claude/Codex authentication. LaunchAgents live in `~/Library/LaunchAgents/`.

## 1. Find the executable

```bash
which coding-bridge
# e.g. /Users/you/Library/Python/3.10/bin/coding-bridge  (pip --user)
#  or  /opt/homebrew/bin/coding-bridge
```

Use the **absolute** path in the plist — launchd does not use your shell `PATH`.

## 2. Create the plist

`~/Library/LaunchAgents/cloud.acedata.coding-bridge.channels.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>cloud.acedata.coding-bridge.channels</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/Library/Python/3.10/bin/coding-bridge</string>
        <string>channels</string>
        <string>start</string>
    </array>

    <!-- The token. Keep this plist mode 0600 (step 3) since it holds a secret.
         The name must match token_env in channels.toml. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>WECHAT_TOKEN_MYNODE</key>
        <string>paste-the-real-token-here</string>
    </dict>

    <!-- Start at login and keep it alive, but back off on crash loops. -->
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <!-- Restart only on a crash/non-zero exit; don't relaunch after a
             clean stop. Combined with ThrottleInterval this avoids a tight
             loop when the config is bad (exit 2) — check the log and fix it. -->
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/you/Library/Logs/coding-bridge-channels.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/Library/Logs/coding-bridge-channels.err.log</string>
</dict>
</plist>
```

Replace `/Users/you/...` with your real paths (there is no `%h`-style expansion
in a plist).

### Prefer a token file over an inline secret?

Drop the `EnvironmentVariables` dict and point `ProgramArguments` at a wrapper:

`~/bin/coding-bridge-channels.sh` (mode `0700`):

```bash
#!/bin/bash
export WECHAT_TOKEN_MYNODE="$(cat "$HOME/.ace-bridge/wechat.token")"
exec coding-bridge channels start
```

```bash
chmod 700 ~/bin/coding-bridge-channels.sh
chmod 600 ~/.ace-bridge/wechat.token
```

Then set `ProgramArguments` to just `["/Users/you/bin/coding-bridge-channels.sh"]`.

## 3. Secure the plist

Because the inline form holds the token:

```bash
chmod 600 ~/Library/LaunchAgents/cloud.acedata.coding-bridge.channels.plist
```

## 4. Load and start

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cloud.acedata.coding-bridge.channels.plist
launchctl kickstart -k gui/$(id -u)/cloud.acedata.coding-bridge.channels
```

(On older macOS: `launchctl load -w ~/Library/LaunchAgents/cloud.acedata.coding-bridge.channels.plist`.)

## 5. Verify

```bash
launchctl print gui/$(id -u)/cloud.acedata.coding-bridge.channels | grep -E 'state|pid'
tail -f ~/Library/Logs/coding-bridge-channels.log
```

Look for one `channel started: <instance_id> → <base_url>` line per enabled
instance, then send yourself `/ask hello` on WeChat.

## Updating config / stopping

```bash
# Restart after editing channels.toml or the token:
launchctl kickstart -k gui/$(id -u)/cloud.acedata.coding-bridge.channels

# Stop and unload:
launchctl bootout gui/$(id -u)/cloud.acedata.coding-bridge.channels
```
