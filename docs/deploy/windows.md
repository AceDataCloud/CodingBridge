# Windows — `coding-bridge channels`

Two supported options:

- **Task Scheduler** (built into Windows) — simplest, no extra software.
- **NSSM** (Non-Sucking Service Manager) — if you want a true Windows *service*
  with automatic restart and centralized service management.

Both run `coding-bridge channels start` as your user so it uses your local
Claude/Codex authentication.

## Find the executable

```powershell
(Get-Command coding-bridge).Source
# e.g. C:\Users\you\AppData\Local\Programs\Python\Python310\Scripts\coding-bridge.exe
```

Use that **absolute** path below.

## Provide the token

A scheduled task / service does **not** see the env vars from your interactive
PowerShell session. Set the token as a **User** environment variable so the task
inherits it:

```powershell
# Persists for your user account (name must match token_env in channels.toml):
setx WECHAT_TOKEN_MYNODE "paste-the-real-token-here"
```

> `setx` writes to the user profile, readable only by your account and admins.
> Do not put the token on the command line or in a committed file. Open a **new**
> terminal after `setx` so the value is present.

---

## Option A — Task Scheduler

### Create the task (PowerShell, run as your user)

```powershell
$exe = (Get-Command coding-bridge).Source
$action  = New-ScheduledTaskAction -Execute $exe -Argument "channels start"
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Restart a few times if it exits unexpectedly, then give up (don't loop on a
# config error).
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -DontStopOnIdleEnd
Register-ScheduledTask -TaskName "coding-bridge-channels" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Limited -Description "coding-bridge channels (WeChat -> local agent)"
```

`-RunLevel Limited` = run as your normal user (no elevation). The agent runs
code with your permissions — the same trust boundary as an interactive session.

### Start / stop / inspect

```powershell
Start-ScheduledTask  -TaskName "coding-bridge-channels"
Stop-ScheduledTask   -TaskName "coding-bridge-channels"
Get-ScheduledTask    -TaskName "coding-bridge-channels" | Get-ScheduledTaskInfo
```

Task Scheduler doesn't capture stdout by default. To see logs, either run
`channels start` once in a terminal to confirm, or wrap it in a small script
that redirects output to a file (see the wrapper note below).

---

## Option B — NSSM (true service)

Install [NSSM](https://nssm.cc/) (`choco install nssm` or download), then:

```powershell
$exe = (Get-Command coding-bridge).Source
nssm install coding-bridge-channels $exe channels start
nssm set     coding-bridge-channels AppDirectory  $env:USERPROFILE
# Run as your user so it uses your Claude/Codex login. Supply your account and
# its Windows password (NSSM stores it in the service config). Replace
# YOUR_PASSWORD with your login password:
nssm set     coding-bridge-channels ObjectName ".\$env:USERNAME" "YOUR_PASSWORD"
# Capture logs:
nssm set     coding-bridge-channels AppStdout $env:USERPROFILE\coding-bridge.out.log
nssm set     coding-bridge-channels AppStderr $env:USERPROFILE\coding-bridge.err.log
# Restart on crash with a delay; NSSM's default throttle avoids tight loops.
nssm set     coding-bridge-channels AppExit Default Restart
nssm set     coding-bridge-channels AppRestartDelay 10000
nssm start   coding-bridge-channels
```

Manage it like any service:

```powershell
nssm status  coding-bridge-channels
nssm restart coding-bridge-channels
nssm stop    coding-bridge-channels
nssm remove  coding-bridge-channels confirm
```

> To avoid putting your Windows password in shell history, run `nssm edit
> coding-bridge-channels` and set the **Log on** account + password in the GUI
> instead of the `ObjectName` command line above.

---

## Wrapper script (optional, for logging + token file)

If you'd rather keep the token in a file than a User env var, or want logs from
Task Scheduler, point the task/service at a small wrapper:

`%USERPROFILE%\coding-bridge-channels.ps1`:

```powershell
$env:WECHAT_TOKEN_MYNODE = (Get-Content "$env:USERPROFILE\.ace-bridge\wechat.token" -Raw).Trim()
& coding-bridge channels start *>> "$env:USERPROFILE\coding-bridge.log"
```

Secure the token file so only you can read it:

```powershell
icacls "$env:USERPROFILE\.ace-bridge\wechat.token" /inheritance:r /grant:r "$($env:USERNAME):R"
```

Then set the task action to:
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File %USERPROFILE%\coding-bridge-channels.ps1`

## Updating config

After editing `channels.toml` or the token, restart the task/service:

```powershell
# Task Scheduler
Stop-ScheduledTask -TaskName "coding-bridge-channels"; Start-ScheduledTask -TaskName "coding-bridge-channels"
# NSSM
nssm restart coding-bridge-channels
```
