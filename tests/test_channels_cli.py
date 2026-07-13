"""Tests for the ``coding-bridge channels`` CLI subcommand group.

Focus: pure surface — arg parsing, init file write behavior, doctor output
against mocked httpx. `start` is thin glue and is smoke-tested by the E2E.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest

from coding_bridge import channels_cli, cli
from coding_bridge.config import Settings

# ---------- helpers -----------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path, log_dir=tmp_path / "logs")


def _capture(fn) -> tuple[int, str, str]:
    """Run ``fn()`` and capture (rc, stdout, stderr). ``fn`` must return int."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn()
    return rc, out.getvalue(), err.getvalue()


# ---------- init --------------------------------------------------------------


class TestChannelsInit:
    def test_creates_file_with_safe_defaults(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_init(s))
        assert rc == 0
        assert s.channels_config_path.exists()
        body = s.channels_config_path.read_text(encoding="utf-8")
        # Every example is commented out — nothing enabled by default
        assert "# [[channels.wechat]]" in body
        # `enabled = false` appears in the template
        assert "enabled = false" in body
        # Confirmation printed
        assert str(s.channels_config_path) in out

    def test_refuses_to_overwrite(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.channels_config_path.parent.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text("# already here", encoding="utf-8")
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_init(s))
        assert rc == 1
        assert "Refusing to overwrite" in err
        # Original file untouched
        assert s.channels_config_path.read_text(encoding="utf-8") == "# already here"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits don't apply on Windows")
    def test_posix_permissions_are_0600(self, tmp_path: Path) -> None:
        import stat as _stat

        s = _settings(tmp_path)
        channels_cli.cmd_channels_init(s)
        mode = _stat.S_IMODE(s.channels_config_path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ---------- doctor ------------------------------------------------------------


class TestChannelsDoctor:
    def test_no_config_prints_hint(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 0
        assert "No channels configured" in out
        assert "channels init" in out

    def test_bad_config_exits_2(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\ninstance_id = 'x'\n"  # missing base_url
            "token_env = 'X'\n",
            encoding="utf-8",
        )
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 2
        assert "config error" in err

    def test_disabled_instances_are_skipped(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'sleepy'\n"
            "base_url = 'http://never-called'\n"
            "token_env = 'MISSING'\n"
            "enabled = false\n",
            encoding="utf-8",
        )
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 0
        assert "sleepy" in out
        assert "skipped" in out.lower()
        # Never tried to resolve the missing env var
        assert "MISSING" not in err

    def test_enabled_reachable_endpoint_marked_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch WeChatClient to use a MockTransport where the tasks endpoint
        # returns 404 (token accepted, probe id unknown) — the happy path.
        monkeypatch.setenv("MY_TOKEN", "abc")
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'live'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'MY_TOKEN'\n"
            "enabled = true\n",
            encoding="utf-8",
        )

        # Monkey-patch WeChatClient.__init__ to plug in a MockTransport
        original_init = channels_cli.WeChatClient.__init__

        def patched_init(self, base_url, token, *, timeout=10.0, transport=None):
            def handler(request: httpx.Request) -> httpx.Response:
                assert request.url.path.startswith("/api/tasks/")
                # Token was accepted; the probe task just doesn't exist.
                return httpx.Response(404, json={"error": "task not found"})

            original_init(
                self, base_url, token, timeout=timeout, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(channels_cli.WeChatClient, "__init__", patched_init)

        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 0
        assert "live" in out
        assert "[OK]" in out
        out.encode("cp936")
        # Token value never appears in output
        assert "abc" not in out
        assert "abc" not in err

    def test_enabled_401_marked_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BAD_TOKEN", "wrong")
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'rejected'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'BAD_TOKEN'\n"
            "enabled = true\n",
            encoding="utf-8",
        )

        original_init = channels_cli.WeChatClient.__init__

        def patched_init(self, base_url, token, *, timeout=10.0, transport=None):
            def handler(request: httpx.Request) -> httpx.Response:
                assert request.url.path.startswith("/api/tasks/")
                return httpx.Response(401, json={"error": "unauthorized"})

            original_init(
                self, base_url, token, timeout=timeout, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(channels_cli.WeChatClient, "__init__", patched_init)

        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 1
        assert "rejected" in out
        assert "[FAIL]" in out
        assert "401" in out
        out.encode("cp936")
        # Token never leaked
        assert "wrong" not in out

    def test_enabled_500_marked_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 5xx on tasks + no /health → server error reported, not "OK"
        monkeypatch.setenv("TOK", "t")
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'broken'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'TOK'\n"
            "enabled = true\n",
            encoding="utf-8",
        )

        original_init = channels_cli.WeChatClient.__init__

        def patched_init(self, base_url, token, *, timeout=10.0, transport=None):
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(500, json={"error": "boom"})

            original_init(
                self, base_url, token, timeout=timeout, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(channels_cli.WeChatClient, "__init__", patched_init)

        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        assert rc == 1
        assert "broken" in out
        assert "500" in out

    def test_missing_token_env_reports_config_error(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'no-tok'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'THIS_ENV_IS_NOT_SET_XYZ'\n"
            "enabled = true\n",
            encoding="utf-8",
        )
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_doctor(s))
        # doctor exits non-zero because the enabled instance failed
        assert rc == 1
        # Message references the env var name, not any value
        assert "THIS_ENV_IS_NOT_SET_XYZ" in out


# ---------- start (arg-parse + no-enabled-instances path) --------------------


class TestChannelsStart:
    def test_no_config_exits_2(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text("garbage = ", encoding="utf-8")
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_start(s))
        assert rc == 2
        assert "config error" in err

    def test_no_enabled_instances_exits_1(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'idle'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'T'\n"
            "enabled = false\n",
            encoding="utf-8",
        )
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_start(s))
        assert rc == 1
        assert "No enabled channels" in err

    def test_missing_token_env_exits_2_before_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _settings(tmp_path)
        s.config_dir.mkdir(parents=True, exist_ok=True)
        s.channels_config_path.write_text(
            "[[channels.wechat]]\n"
            "instance_id = 'x'\n"
            "base_url = 'http://wechat.local'\n"
            "token_env = 'ABSOLUTELY_NOT_SET_9999'\n"
            "enabled = true\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("ABSOLUTELY_NOT_SET_9999", raising=False)
        rc, out, err = _capture(lambda: channels_cli.cmd_channels_start(s))
        assert rc == 2
        assert "ABSOLUTELY_NOT_SET_9999" in err


# ---------- smoke (offline, stub provider) -----------------------------------


def _install_stub_factory(monkeypatch: pytest.MonkeyPatch, script) -> None:
    """Patch ``default_provider_factory`` so smoke runs with no real binary.

    ``script`` is a callable ``(session_id) -> list[event_payload]`` that the
    stub provider emits on ``start``.
    """
    from coding_bridge import providers as _providers

    def _factory(_settings):
        def _make(_provider_name, session_id, emit, _ask):
            class _Stub:
                name = "stub"

                async def start(self, _prompt, **_kw):
                    for payload in script(session_id):
                        await emit(payload)

                async def send(self, *_a, **_kw):
                    return

                async def edit(self, *_a, **_kw):
                    return

                async def interrupt(self):
                    return

                async def aclose(self):
                    return

            return _Stub()

        return _make

    monkeypatch.setattr(_providers, "default_provider_factory", _factory)


class TestChannelsSmoke:
    def test_smoke_prints_provider_reply(self, tmp_path: Path, monkeypatch) -> None:
        from coding_bridge.protocol import Event, event_payload

        def script(sid):
            return [
                event_payload(Event.SESSION_TEXT, sid, text="pong"),
                event_payload(Event.SESSION_RESULT, sid, text=""),
            ]

        _install_stub_factory(monkeypatch, script)
        s = _settings(tmp_path)
        rc, out, err = _capture(
            lambda: channels_cli.cmd_channels_smoke(
                s, provider="claude", prompt="say pong", timeout=5.0
            )
        )
        assert rc == 0
        assert "pong" in out
        assert "say pong" in out  # prompt echoed in the header

    def test_smoke_provider_error_exits_1(self, tmp_path: Path, monkeypatch) -> None:
        from coding_bridge.protocol import Event, event_payload

        def script(sid):
            return [event_payload(Event.SESSION_ERROR, sid, message="kaboom")]

        _install_stub_factory(monkeypatch, script)
        s = _settings(tmp_path)
        rc, out, err = _capture(
            lambda: channels_cli.cmd_channels_smoke(s, provider="claude", prompt="x", timeout=5.0)
        )
        assert rc == 1
        assert "kaboom" in out

    def test_smoke_empty_reply_exits_1(self, tmp_path: Path, monkeypatch) -> None:
        from coding_bridge.protocol import Event, event_payload

        def script(sid):
            # Result with no text and no streamed text → dispatcher yields "(no reply)"
            return [event_payload(Event.SESSION_RESULT, sid, text="")]

        _install_stub_factory(monkeypatch, script)
        s = _settings(tmp_path)
        rc, out, err = _capture(
            lambda: channels_cli.cmd_channels_smoke(s, provider="claude", prompt="x", timeout=5.0)
        )
        assert rc == 1

    def test_smoke_timeout_no_text_exits_1(self, tmp_path: Path, monkeypatch) -> None:
        # Provider emits NOTHING and never signals completion. With a tiny
        # turn_timeout the dispatcher synthesises "(provider timed out; no
        # reply)" — smoke must report failure (rc=1), not a false-healthy 0.
        # (This is the BLOCKER the adversarial review caught: the old exit-code
        # check ignored the timeout marker.)
        def script(_sid):
            return []  # start() returns immediately having emitted nothing

        _install_stub_factory(monkeypatch, script)
        s = _settings(tmp_path)
        rc, out, err = _capture(
            lambda: channels_cli.cmd_channels_smoke(s, provider="claude", prompt="x", timeout=0.2)
        )
        assert rc == 1
        assert "timed out" in out

    def test_smoke_unknown_provider_exits_2(self, tmp_path: Path) -> None:
        # Parity with the channels.toml default_provider validation: an unknown
        # --provider must fail loudly (rc=2), not silently fall back to Claude.
        s = _settings(tmp_path)
        rc, out, err = _capture(
            lambda: channels_cli.cmd_channels_smoke(s, provider="gpt4", prompt="x", timeout=5.0)
        )
        assert rc == 2
        assert "unknown provider" in err
        assert "gpt4" in err


# ---------- argparse integration ---------------------------------------------


class TestArgparseIntegration:
    def test_channels_subcommand_parses(self, tmp_path: Path) -> None:
        # Sanity: verify the top-level parser sees the `channels` subcommand
        # and refuses to run `channels` without a sub-subcommand.
        with pytest.raises(SystemExit):
            cli.main(["channels", "--config-dir", str(tmp_path)])

    def test_channels_init_end_to_end(self, tmp_path: Path) -> None:
        # Direct `cli.main` invocation of the init subcommand
        with pytest.raises(SystemExit) as ei:
            cli.main(["channels", "--config-dir", str(tmp_path), "init"])
        assert ei.value.code == 0

    def test_channels_smoke_parses(self, tmp_path: Path, monkeypatch) -> None:
        from coding_bridge.protocol import Event, event_payload

        def script(sid):
            return [event_payload(Event.SESSION_RESULT, sid, text="ok")]

        _install_stub_factory(monkeypatch, script)
        with pytest.raises(SystemExit) as ei:
            cli.main(
                [
                    "channels",
                    "--config-dir",
                    str(tmp_path),
                    "smoke",
                    "--provider",
                    "claude",
                    "--prompt",
                    "hi",
                    "--timeout",
                    "5",
                ]
            )
        assert ei.value.code == 0

    def test_channels_init_refuses_second_run(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as ei1:
            cli.main(["channels", "--config-dir", str(tmp_path), "init"])
        assert ei1.value.code == 0
        with pytest.raises(SystemExit) as ei2:
            cli.main(["channels", "--config-dir", str(tmp_path), "init"])
        assert ei2.value.code == 1
