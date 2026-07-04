"""CLI plumbing for the ``coding-bridge channels`` subcommand group.

Sub-subcommands:

* ``coding-bridge channels init`` — write a skeleton ``channels.toml`` next to
  the credentials file (safe defaults: ``enabled=false``).
* ``coding-bridge channels start`` — read ``channels.toml``, spin up one
  ``PolicyGate → SessionDispatcher`` per enabled instance, and run the
  adapter loop until Ctrl-C.
* ``coding-bridge channels doctor`` — validate ``channels.toml``, resolve
  each token (without ever printing it), and ping each channel (WeChat gateway
  endpoint / Telegram ``getMe``).

The dispatcher-wiring lives ONLY here — the `coding_bridge.channels.*`
package stays pure library code that can be reused by other CLIs or by tests.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import stat
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .channels import ConfigError, PolicyGate, load_channels_config
from .channels.telegram import TelegramAdapter, TelegramClient, TelegramError
from .channels.wechat import WeChatAdapter, WeChatClient
from .config import Settings

if TYPE_CHECKING:
    from .channels import ChannelAdapter, TelegramInstanceConfig, WeChatInstanceConfig
    from .channels.dispatcher import SessionDispatcher

logger = logging.getLogger("coding-bridge.channels.cli")


# ---------- init --------------------------------------------------------------

_INIT_TEMPLATE = """\
# coding-bridge channels config
# Every instance is `enabled = false` by default — flip to true only when the
# token env var is set and the sender allowlist is filled in. See
# https://github.com/AceDataCloud/CodingBridge for docs.

# [[channels.wechat]]
# instance_id = "my-wechat"
# base_url = "http://127.0.0.1:8000"
# token_env = "WECHAT_TOKEN_MY_WECHAT"
# enabled = false
#
# # Only respond when a message starts with this prefix. Empty string disables
# # the prefix check. Group chats basically require this.
# trigger_prefix = "/ask "
#
# # Only accept messages from these sender IDs (WeChat wxid). Empty list means
# # allow all — safe only when the token is exclusively yours.
# allowed_senders = []
#
# # At most this many messages per sender_id per 60 s. 0 disables the limit.
# rate_limit_per_min = 6
#
# # Drop repeat msg_id inside this window (upstream retries). 0 disables.
# dedup_window_seconds = 300.0

# [[channels.telegram]]
# instance_id = "my-telegram"
# # Create a bot with @BotFather, then export the token it hands you:
# token_env = "TELEGRAM_TOKEN_MY_TELEGRAM"
# enabled = false
#
# # Only respond when a message starts with this prefix. In groups, add the bot
# # and keep a prefix so it doesn't answer every line. Empty answers everything.
# trigger_prefix = "/ask "
#
# # Only accept messages from these Telegram numeric user IDs (as strings).
# # Empty list means allow all — safe only for a private bot. Message
# # @userinfobot to find your own id.
# allowed_senders = []
#
# # Group / supergroup chat IDs (usually negative, as strings) the bot may
# # answer in. Empty = every group it's added to. Never filters private DMs.
# allowed_groups = []
#
# # At most this many messages per sender per 60 s. 0 disables the limit.
# rate_limit_per_min = 6
#
# # Drop duplicate updates inside this window. 0 disables.
# dedup_window_seconds = 300.0
"""


def _write_secure_file(path: Path, body: str) -> None:
    """Atomically create ``path`` with ``body``. Fails if it already exists.

    Uses ``open(..., 'x')`` (exclusive create) so a concurrent second
    ``channels init`` can't race between our ``exists()`` check and our
    ``write_text()``. On POSIX, chmod 0600 so token references aren't
    world-readable. On Windows, the file inherits the parent's ACL — since
    the file only ever contains env-var *names* and file *paths*, not the
    tokens themselves, this is acceptable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # `x` = exclusive create; raises FileExistsError if the file exists.
    with path.open("x", encoding="utf-8") as f:
        f.write(body)
    if hasattr(stat, "S_IMODE") and sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def cmd_channels_init(settings: Settings) -> int:
    """Write a skeleton `channels.toml`. Refuses to overwrite an existing file."""
    path = settings.channels_config_path
    try:
        _write_secure_file(path, _INIT_TEMPLATE)
    except FileExistsError:
        print(f"Refusing to overwrite existing config: {path}", file=sys.stderr)
        print(
            "Delete it or edit it in place. `coding-bridge channels init` is "
            "one-shot on purpose so you never lose a working config.",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {path}")
    print("Next steps:")
    print("  1. Uncomment a [[channels.wechat]] or [[channels.telegram]] block.")
    print("  2. Export the token env var referenced by `token_env`.")
    print("  3. Fill `allowed_senders` with your own sender id(s).")
    print("  4. Flip `enabled = true`.")
    print("  5. Run `coding-bridge channels doctor` to verify.")
    print("  6. Run `coding-bridge channels start`.")
    return 0


# ---------- doctor ------------------------------------------------------------


async def _doctor_one(inst: WeChatInstanceConfig) -> tuple[bool, str]:
    """Return (ok, message) for one instance. Never raises; never logs secrets.

    Actually tests that the token is accepted by hitting an authenticated
    endpoint (``GET /api/messages/tasks/<probe>``). The gateway's response:

    * 401 → token rejected → FAIL (loudly, since this is the whole point of doctor)
    * 404 → token accepted but probe id unknown → PASS (this is expected)
    * 2xx → token accepted, endpoint returned a status → PASS
    * 5xx / other → server broken → FAIL
    * network error → unreachable → FAIL

    Falls back to ``/health`` if the tasks endpoint isn't implemented, but
    warns that auth wasn't verified.
    """
    try:
        token = inst.resolve_token()
    except ConfigError as exc:
        return False, f"token: {exc}"

    token_len = len(token)  # only the length is safe to display
    client = WeChatClient(inst.base_url, token, timeout=5.0)
    try:
        # Probe id is a syntactically valid but almost-certainly-unknown token.
        # It uses only characters allowed by _TASK_ID_RE so `get_task_status`
        # will actually issue the request.
        probe = "coding-bridge-doctor-probe"
        try:
            await client.get_task_status(probe)
            # 2xx means the probe id happened to exist (extremely unlikely) —
            # still counts as auth OK.
            return True, f"OK (token accepted, {token_len} bytes)"
        except httpx.HTTPStatusError as http_exc:
            code = http_exc.response.status_code
            if code == 401:
                return False, "token rejected (401)"
            if code == 403:
                return False, "token forbidden (403)"
            if code == 404:
                # Token was fine — server just didn't have that task. This is
                # the happy path for a fresh install.
                return True, f"OK (token accepted, {token_len} bytes)"
            if 500 <= code < 600:
                # Try /health as a last-ditch reachability check so we don't
                # mis-report a 5xx on a gateway that doesn't implement the
                # tasks endpoint at all.
                try:
                    resp = await client._client.get("/health")  # noqa: SLF001
                    if 200 <= resp.status_code < 300:
                        return False, (
                            f"reachable at /health but /api/messages/tasks "
                            f"returned {code} (gateway may be misconfigured)"
                        )
                except Exception:  # noqa: BLE001
                    pass
                return False, f"server error {code}"
            return False, f"unexpected {code}"
    except Exception as exc:  # noqa: BLE001 - diagnostic; class name only
        return False, f"unreachable: {exc.__class__.__name__}"
    finally:
        await client.aclose()


async def _doctor_one_telegram(inst: TelegramInstanceConfig) -> tuple[bool, str]:
    """Return (ok, message) for one Telegram instance. Never logs the token.

    Calls ``getMe`` — Telegram's canonical auth probe. 200 → token good (report
    the bot's ``@username``); 401 → token rejected; anything else → unreachable
    or transient server error.
    """
    try:
        token = inst.resolve_token()
    except ConfigError as exc:
        return False, f"token: {exc}"

    client = TelegramClient(token, api_base=inst.api_base, timeout=8.0)
    try:
        me = await client.get_me()
        username = me.get("username")
        who = f"@{username}" if username else f"id={me.get('id')}"
        return True, f"OK (token accepted, bot {who})"
    except TelegramError as exc:
        if exc.error_code == 401:
            return False, "token rejected (401)"
        return False, f"api error {exc.error_code}"
    except Exception as exc:  # noqa: BLE001 - diagnostic; class name only
        return False, f"unreachable: {exc.__class__.__name__}"
    finally:
        await client.aclose()


def cmd_channels_doctor(settings: Settings) -> int:
    """Load config + check each instance's token + reachability."""
    path = settings.channels_config_path
    try:
        cfg = load_channels_config(path)
    except ConfigError as exc:
        print(f"config error in {path}: {exc}", file=sys.stderr)
        return 2

    if not cfg.wechat and not cfg.telegram:
        print(f"No channels configured in {path}. Run `channels init` first.")
        return 0

    total = len(cfg.wechat) + len(cfg.telegram)
    enabled = len(cfg.enabled_wechat) + len(cfg.enabled_telegram)
    print(f"Config file: {path}")
    print(f"Total instances: {total} (enabled: {enabled})")
    print()

    all_ok = True

    async def _run() -> None:
        nonlocal all_ok
        for inst in cfg.wechat:
            state = "ENABLED" if inst.enabled else "disabled"
            print(f"[{state}] wechat/{inst.instance_id}: {inst.base_url}")
            if not inst.enabled:
                print("  → skipped (enabled=false)")
                continue
            ok, msg = await _doctor_one(inst)
            marker = "✓" if ok else "✗"
            print(f"  {marker} {msg}")
            if not ok:
                all_ok = False
        for tg in cfg.telegram:
            state = "ENABLED" if tg.enabled else "disabled"
            print(f"[{state}] telegram/{tg.instance_id}: {tg.api_base}")
            if not tg.enabled:
                print("  → skipped (enabled=false)")
                continue
            ok, msg = await _doctor_one_telegram(tg)
            marker = "✓" if ok else "✗"
            print(f"  {marker} {msg}")
            if not ok:
                all_ok = False

    asyncio.run(_run())
    return 0 if all_ok else 1


# ---------- start -------------------------------------------------------------


def cmd_channels_start(settings: Settings) -> int:
    """Load channels + spin up one adapter per enabled instance until Ctrl-C.

    Blocks. Exits 0 on Ctrl-C, non-zero on config error or if no enabled
    instances are configured.
    """
    from .channels.dispatcher import SessionDispatcher  # local import: heavy deps
    from .providers import default_provider_factory
    path = settings.channels_config_path
    try:
        cfg = load_channels_config(path)
    except ConfigError as exc:
        print(f"config error in {path}: {exc}", file=sys.stderr)
        return 2

    enabled_wechat = cfg.enabled_wechat
    enabled_telegram = cfg.enabled_telegram
    if not enabled_wechat and not enabled_telegram:
        print(
            "No enabled channels — set `enabled = true` on at least one "
            "[[channels.wechat]] or [[channels.telegram]] block in " + str(path),
            file=sys.stderr,
        )
        return 1

    # Resolve tokens up front so a missing env var kills us before any
    # network connection instead of silently.
    wechat_resolved: list[tuple[WeChatInstanceConfig, str]] = []
    telegram_resolved: list[tuple[TelegramInstanceConfig, str]] = []
    for inst in enabled_wechat:
        try:
            token = inst.resolve_token()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        wechat_resolved.append((inst, token))
    for inst in enabled_telegram:
        try:
            token = inst.resolve_token()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        telegram_resolved.append((inst, token))

    provider_factory = default_provider_factory(settings)
    total = len(wechat_resolved) + len(telegram_resolved)

    async def _go() -> None:
        from .channels.approvals import ApprovalStore

        adapters: list[ChannelAdapter] = []
        dispatchers: list[SessionDispatcher] = []
        runners: list[asyncio.Task[None]] = []
        stop = asyncio.Event()
        # Shared across instances; only used by instances with require_approval.
        approval_store = ApprovalStore(settings.config_dir / "approvals")

        # Run status + recent-turn ring the portal reads (content-free).
        from .channels.observability import TurnEvent, set_turn_sink
        from .channels.status import StatusStore

        status_store = StatusStore(settings.config_dir / "status")
        started_at = time.time()
        channel_descs = [
            {"adapter": "wechat", "instance_id": i.instance_id, "endpoint": i.base_url}
            for i, _ in wechat_resolved
        ] + [
            {"adapter": "telegram", "instance_id": i.instance_id, "endpoint": i.api_base}
            for i, _ in telegram_resolved
        ]

        def _record_turn(ev: TurnEvent) -> None:
            status_store.record_turn(
                {
                    "ts": time.time(),
                    "adapter": ev.adapter,
                    "instance_id": ev.instance_id,
                    "provider": ev.provider,
                    "outcome": ev.outcome,
                    "latency_ms": ev.latency_ms,
                    "prompt_chars": ev.prompt_chars,
                    "reply_chars": ev.reply_chars,
                }
            )

        async def _heartbeat() -> None:
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    status_store.write_run(channel_descs, started_at=started_at)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=20)

        def _make_dispatcher(inst: object) -> SessionDispatcher:
            return SessionDispatcher(
                settings,
                provider_factory,
                default_provider=getattr(inst, "default_provider", None) or "claude",
                approval_store=approval_store,
                require_approval=getattr(inst, "require_approval", False),
            )

        for inst, token in wechat_resolved:
            dispatcher = _make_dispatcher(inst)
            gate = PolicyGate(inst.to_policy(), dispatcher.handle_message)
            adapter: ChannelAdapter = WeChatAdapter(
                instance_id=inst.instance_id,
                base_url=inst.base_url,
                token=token,
                stop_event=stop,
            )
            adapter.set_handler(gate.handle)
            dispatchers.append(dispatcher)
            adapters.append(adapter)
            runners.append(asyncio.create_task(adapter.run()))
            print(f"channel started: wechat/{inst.instance_id} → {inst.base_url}")

        for inst, token in telegram_resolved:
            dispatcher = _make_dispatcher(inst)
            gate = PolicyGate(inst.to_policy(), dispatcher.handle_message)
            tg_adapter: ChannelAdapter = TelegramAdapter(
                instance_id=inst.instance_id,
                token=token,
                api_base=inst.api_base,
                stop_event=stop,
            )
            tg_adapter.set_handler(gate.handle)
            dispatchers.append(dispatcher)
            adapters.append(tg_adapter)
            runners.append(asyncio.create_task(tg_adapter.run()))
            print(f"channel started: telegram/{inst.instance_id} → {inst.api_base}")

        set_turn_sink(_record_turn)
        status_store.write_run(channel_descs, started_at=started_at)
        hb_task = asyncio.create_task(_heartbeat())

        try:
            # Wait for any runner to exit (auth failure, etc.) or Ctrl-C.
            done, pending = await asyncio.wait(
                runners, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                if t.exception():
                    logger.error("adapter exited: %s", t.exception().__class__.__name__)
        finally:
            stop.set()
            set_turn_sink(None)
            hb_task.cancel()
            with contextlib.suppress(BaseException):
                await hb_task
            with contextlib.suppress(Exception):
                status_store.clear_run()
            for a in adapters:
                with contextlib.suppress(Exception):
                    await a.aclose()
            for d in dispatchers:
                with contextlib.suppress(Exception):
                    await d.aclose()
            for t in runners:
                if not t.done():
                    t.cancel()
                with contextlib.suppress(BaseException):
                    await t

    print(f"Starting {total} channel(s). Ctrl-C to stop.")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ---------- smoke -------------------------------------------------------------


def cmd_channels_smoke(
    settings: Settings, *, provider: str, prompt: str, timeout: float
) -> int:
    """Run one real provider turn locally — no channel, no gateway, no network.

    This is the ``claude`` / ``codex`` end-to-end check an operator runs BEFORE
    going live: it drives the exact same ``SessionDispatcher`` turn machinery a
    real inbound message would, but feeds a canned prompt through an in-memory
    adapter and prints the reply to stdout. If this prints a sane answer, the
    provider binary + auth + dispatcher wiring are all good; the only remaining
    variable for ``channels start`` is the WeChat gateway token (covered by ``doctor``).

    Exit codes: 0 = provider replied, 1 = provider error / empty / timeout,
    2 = unknown provider name.
    """
    from .channels.base import ChannelTarget, IncomingMessage, SendResult
    from .channels.dispatcher import SessionDispatcher

    # Imported at call time (not module top) so tests can monkeypatch
    # `coding_bridge.providers.default_provider_factory` before we resolve it,
    # and so the heavy provider deps stay out of the CLI import path.
    from .providers import KNOWN_PROVIDERS, default_provider_factory

    # Validate up front for parity with the channels.toml `default_provider`
    # check — otherwise `--provider gpt4` would silently fall back to Claude and
    # the operator would think they'd smoke-tested codex.
    if provider not in KNOWN_PROVIDERS:
        allowed = ", ".join(KNOWN_PROVIDERS)
        print(f"unknown provider {provider!r} (allowed: {allowed})", file=sys.stderr)
        return 2

    captured: dict[str, str] = {}

    class _StdoutAdapter:
        name = "smoke"
        instance_id = "local"

        def set_handler(self, _h) -> None:  # not used — we call the dispatcher directly
            return

        async def run(self) -> None:
            return

        async def send(self, _target, text, *, reply_to=None):
            captured["reply"] = text
            return SendResult(ok=True, upstream_id="local", latency_ms=0)

        async def aclose(self) -> None:
            return

    async def _run() -> None:
        dispatcher = SessionDispatcher(
            settings,
            default_provider_factory(settings),
            turn_timeout=timeout,
            default_provider=provider,
        )
        adapter = _StdoutAdapter()
        msg = IncomingMessage(
            sender_id="smoke-local",
            sender_name="smoke",
            target=ChannelTarget(conversation_id="smoke-local"),
            text=prompt,
            msg_type="text",
            direction="inbound",
        )
        try:
            await dispatcher.handle_message(msg, adapter)
            # Poll until the fire-and-forget turn posts a reply (bounded by
            # turn_timeout + a small slack so we never hang forever).
            deadline = timeout + 5.0
            waited = 0.0
            while "reply" not in captured and waited < deadline:
                await asyncio.sleep(0.05)
                waited += 0.05
        finally:
            await dispatcher.aclose()

    print(f"Provider: {provider}")
    print(f"Prompt:   {prompt!r}")
    print("Running one turn locally (no channel/network)...")
    asyncio.run(_run())

    reply = captured.get("reply")
    if not reply:
        print("✗ no reply (provider timed out or produced nothing)", file=sys.stderr)
        return 1
    print("--- reply ---")
    print(reply)
    print("-------------")
    # The dispatcher synthesises these sentinel strings for the failure paths
    # (see SessionDispatcher._run_turn). A smoke run that lands on any of them
    # must exit non-zero — a timed-out or errored provider is NOT a healthy
    # setup even though it technically produced "a reply".
    failure_markers = ("(provider error:", "(provider timed out")
    if reply == "(no reply)" or reply.startswith(failure_markers):
        return 1
    return 0


# ---------- portal ------------------------------------------------------------


def cmd_channels_portal(
    settings: Settings, *, host: str, port: int, open_browser: bool
) -> int:
    """Serve the local channels.toml config web UI (loopback only) until Ctrl-C."""
    from .channels.portal import serve  # local import: pulls httpx + the HTML blob

    return serve(settings, host=host, port=port, open_browser=open_browser)


# ---------- argparse wiring ---------------------------------------------------


def register_subparsers(
    channels_parser: argparse.ArgumentParser, common: argparse.ArgumentParser
) -> None:
    """Attach ``init`` / ``start`` / ``doctor`` under a top-level ``channels``.

    Called from ``coding_bridge.cli.main`` so the entry point stays one file.
    """
    sub = channels_parser.add_subparsers(dest="channels_command", required=True)

    p_init = sub.add_parser(
        "init", help="Write a skeleton channels.toml (safe defaults)", parents=[common]
    )
    p_init.set_defaults(func=_dispatch_init)

    p_start = sub.add_parser(
        "start",
        help="Run every enabled [[channels.wechat]] instance until Ctrl-C",
        parents=[common],
    )
    p_start.set_defaults(func=_dispatch_start)

    p_doctor = sub.add_parser(
        "doctor",
        help="Validate channels.toml + ping every enabled WeChat endpoint",
        parents=[common],
    )
    p_doctor.set_defaults(func=_dispatch_doctor)

    p_smoke = sub.add_parser(
        "smoke",
        help="Run one real provider turn locally (no channel/network) to verify setup",
        parents=[common],
    )
    p_smoke.add_argument(
        "--provider",
        default="claude",
        help="Provider to smoke-test (claude/codex/copilot). Default: claude.",
    )
    p_smoke.add_argument(
        "--prompt",
        default="Reply with the single word: pong",
        help="Prompt to send. Default asks the model to reply 'pong'.",
    )
    p_smoke.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for the provider turn. Default: 120.",
    )
    p_smoke.set_defaults(func=_dispatch_smoke)

    p_portal = sub.add_parser(
        "portal",
        help="Open a local web UI to edit channels.toml (pick admins, set trigger mode)",
        parents=[common],
    )
    p_portal.add_argument(
        "--host", default="127.0.0.1", help="Bind host (loopback only). Default 127.0.0.1."
    )
    p_portal.add_argument("--port", type=int, default=8765, help="Bind port. Default 8765.")
    p_portal.add_argument(
        "--no-open", action="store_true", help="Do not auto-open the browser."
    )
    p_portal.set_defaults(func=_dispatch_portal)


def _dispatch_init(args: argparse.Namespace) -> None:
    from .cli import _build_settings  # local import to avoid circular

    raise SystemExit(cmd_channels_init(_build_settings(args)))


def _dispatch_start(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(cmd_channels_start(_build_settings(args)))


def _dispatch_doctor(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(cmd_channels_doctor(_build_settings(args)))


def _dispatch_smoke(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(
        cmd_channels_smoke(
            _build_settings(args),
            provider=args.provider,
            prompt=args.prompt,
            timeout=args.timeout,
        )
    )


def _dispatch_portal(args: argparse.Namespace) -> None:
    from .cli import _build_settings

    raise SystemExit(
        cmd_channels_portal(
            _build_settings(args),
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
    )


__all__ = [
    "cmd_channels_doctor",
    "cmd_channels_init",
    "cmd_channels_portal",
    "cmd_channels_smoke",
    "cmd_channels_start",
    "register_subparsers",
]
