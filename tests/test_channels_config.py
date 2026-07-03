"""Tests for ``coding_bridge.channels.config`` — the TOML schema loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_bridge.channels.config import (
    ChannelsConfig,
    ConfigError,
    WeChatInstanceConfig,
    load_channels_config,
    parse_channels_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "channels.toml"
    p.write_text(body, encoding="utf-8")
    return p


class TestParseHappyPath:
    def test_empty_file_returns_empty_config(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "")
        assert load_channels_config(p) == ChannelsConfig()

    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        assert load_channels_config(tmp_path / "does-not-exist.toml") == ChannelsConfig()

    def test_single_wechat_instance_with_token_env(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "beijing-cvm"
base_url = "http://82.156.126.14:8000"
token_env = "WECHAT_TOKEN_BEIJING"
enabled = true
""",
        )
        cfg = load_channels_config(p)
        assert len(cfg.wechat) == 1
        inst = cfg.wechat[0]
        assert inst.instance_id == "beijing-cvm"
        assert inst.base_url == "http://82.156.126.14:8000"
        assert inst.token_env == "WECHAT_TOKEN_BEIJING"
        assert inst.enabled is True
        assert inst.default_provider is None
        assert cfg.enabled_wechat == (inst,)

    def test_multiple_wechat_instances(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "one"
base_url = "http://a"
token_env = "T1"

[[channels.wechat]]
instance_id = "two"
base_url = "https://b"
token_env = "T2"
enabled = true
default_provider = "codex"
""",
        )
        cfg = load_channels_config(p)
        assert [inst.instance_id for inst in cfg.wechat] == ["one", "two"]
        # `enabled=false` by default
        assert cfg.wechat[0].enabled is False
        assert cfg.wechat[1].enabled is True
        assert cfg.wechat[1].default_provider == "codex"
        assert cfg.enabled_wechat == (cfg.wechat[1],)

    def test_base_url_trailing_slash_stripped(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "x"
base_url = "https://example.com/"
token_env = "T"
""",
        )
        cfg = load_channels_config(p)
        assert cfg.wechat[0].base_url == "https://example.com"

    def test_token_file_alternative(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "x"
base_url = "http://a"
token_file = "/run/secrets/wechat"
""",
        )
        cfg = load_channels_config(p)
        assert cfg.wechat[0].token_file == "/run/secrets/wechat"
        assert cfg.wechat[0].token_env is None


class TestParseFailFast:
    def test_unknown_top_level_key_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"\[channels\]: unknown key\(s\)"):
            parse_channels_config({"channels": {"foo": []}})

    def test_channels_must_be_a_table(self) -> None:
        with pytest.raises(ConfigError, match=r"\[channels\]: must be a table"):
            parse_channels_config({"channels": "not a table"})

    def test_wechat_array_must_be_list(self) -> None:
        with pytest.raises(ConfigError, match=r"\[\[channels\.wechat\]\]"):
            parse_channels_config({"channels": {"wechat": "not a list"}})

    def test_unknown_wechat_key_rejected(self) -> None:
        # Typo: `intance_id` instead of `instance_id`
        with pytest.raises(ConfigError, match=r"unknown key\(s\).*intance_id"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "intance_id": "typo",
                                "base_url": "http://a",
                                "token_env": "T",
                            }
                        ]
                    }
                }
            )

    def test_missing_instance_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"required field 'instance_id'"):
            parse_channels_config(
                {"channels": {"wechat": [{"base_url": "http://a", "token_env": "T"}]}}
            )

    def test_missing_base_url_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"required field 'base_url'"):
            parse_channels_config(
                {"channels": {"wechat": [{"instance_id": "x", "token_env": "T"}]}}
            )

    def test_empty_instance_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"required field 'instance_id'"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {"instance_id": "", "base_url": "http://a", "token_env": "T"}
                        ]
                    }
                }
            )

    def test_bad_base_url_scheme_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"base_url must start with http"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "ftp://oops",
                                "token_env": "T",
                            }
                        ]
                    }
                }
            )

    def test_both_token_env_and_token_file_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"set token_env OR token_file, not both"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "token_file": "/tmp/tok",
                            }
                        ]
                    }
                }
            )

    def test_enabled_must_be_bool(self) -> None:
        with pytest.raises(ConfigError, match=r"enabled must be a bool"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "enabled": "yes",
                            }
                        ]
                    }
                }
            )

    def test_default_provider_must_be_string(self) -> None:
        with pytest.raises(ConfigError, match=r"default_provider must be a string"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "default_provider": 123,
                            }
                        ]
                    }
                }
            )

    def test_default_provider_must_be_known(self) -> None:
        # A typo like "gpt4" must be rejected at load time, not silently
        # fall through to Claude at runtime.
        with pytest.raises(ConfigError, match=r"unknown default_provider 'gpt4'"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "default_provider": "gpt4",
                            }
                        ]
                    }
                }
            )

    @pytest.mark.parametrize("provider", ["claude", "codex", "copilot"])
    def test_default_provider_accepts_known(self, provider: str) -> None:
        cfg = parse_channels_config(
            {
                "channels": {
                    "wechat": [
                        {
                            "instance_id": "x",
                            "base_url": "http://a",
                            "token_env": "T",
                            "default_provider": provider,
                        }
                    ]
                }
            }
        )
        assert cfg.wechat[0].default_provider == provider

    def test_duplicate_instance_id_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"duplicate instance_id 'dup'"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {"instance_id": "dup", "base_url": "http://a", "token_env": "T"},
                            {"instance_id": "dup", "base_url": "http://b", "token_env": "U"},
                        ]
                    }
                }
            )

    def test_malformed_toml_rejected(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "[[channels.wechat\ninstance_id = 'oops'")
        with pytest.raises(ConfigError, match=r"is not valid TOML"):
            load_channels_config(p)

    def test_channels_file_is_directory_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "channels.toml"
        p.mkdir()
        with pytest.raises(ConfigError, match=r"is not a regular file"):
            load_channels_config(p)

    def test_channels_file_over_size_limit_rejected(self, tmp_path: Path) -> None:
        # Write just over 1 MB of harmless comments so parser never runs.
        p = tmp_path / "channels.toml"
        p.write_bytes(b"# padding\n" * 110_000)
        with pytest.raises(ConfigError, match=r"exceeds .*-byte limit"):
            load_channels_config(p)

    def test_base_url_with_embedded_userinfo_rejected(self) -> None:
        with pytest.raises(ConfigError, match=r"embedded credentials"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://user:pass@host",
                                "token_env": "T",
                            }
                        ]
                    }
                }
            )

    def test_enabled_as_int_rejected(self) -> None:
        # TOML doesn't parse `1` as bool, but if a caller passes a Python int
        # from a hand-built dict this must still fail — not silently truthy.
        with pytest.raises(ConfigError, match=r"enabled must be a bool"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "enabled": 1,
                            }
                        ]
                    }
                }
            )


class TestResolveToken:
    def test_resolve_from_env(self) -> None:
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_env="MY_TOKEN"
        )
        assert inst.resolve_token({"MY_TOKEN": "secret-123"}) == "secret-123"

    def test_missing_env_raises_without_leaking_value(self) -> None:
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_env="MISSING_VAR"
        )
        with pytest.raises(ConfigError) as exc:
            inst.resolve_token({})
        # Message references the *name*, not any value
        assert "MISSING_VAR" in str(exc.value)
        assert "unset or empty" in str(exc.value)

    def test_empty_env_value_treated_as_missing(self) -> None:
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_env="EMPTY"
        )
        with pytest.raises(ConfigError, match=r"unset or empty"):
            inst.resolve_token({"EMPTY": ""})

    def test_resolve_from_file(self, tmp_path: Path) -> None:
        f = tmp_path / "tok"
        f.write_text("file-secret\n", encoding="utf-8")
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(f)
        )
        # Trailing whitespace stripped
        assert inst.resolve_token({}) == "file-secret"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(tmp_path / "nope")
        )
        with pytest.raises(ConfigError) as exc:
            inst.resolve_token({})
        assert "is not a regular file" in str(exc.value)

    def test_token_file_is_directory_rejected(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(d)
        )
        with pytest.raises(ConfigError, match=r"is not a regular file"):
            inst.resolve_token({})

    def test_token_file_with_invalid_utf8_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "tok"
        f.write_bytes(b"\xff\xfe\xfd")
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(f)
        )
        with pytest.raises(ConfigError, match=r"not valid UTF-8"):
            inst.resolve_token({})

    def test_token_file_over_size_limit_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "tok"
        # 64 KB + 1 byte, filled with an ASCII char that would decode fine —
        # the size check must fire *before* the read.
        f.write_bytes(b"a" * (64 * 1024 + 1))
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(f)
        )
        with pytest.raises(ConfigError, match=r"exceeds .*-byte limit"):
            inst.resolve_token({})

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "tok"
        f.write_text("   \n\n", encoding="utf-8")
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_file=str(f)
        )
        with pytest.raises(ConfigError, match=r"is empty"):
            inst.resolve_token({})

    def test_no_source_configured_raises(self) -> None:
        inst = WeChatInstanceConfig(instance_id="x", base_url="http://a")
        with pytest.raises(ConfigError, match=r"either token_env or token_file must be set"):
            inst.resolve_token({})

    def test_repr_never_contains_resolved_token(self) -> None:
        # The config object holds only the env-var NAME / file PATH, never the
        # secret. `repr()` (used by logging %r, tracebacks, debuggers) must not
        # be able to expose a token because the token isn't stored on it.
        inst = WeChatInstanceConfig(
            instance_id="x", base_url="http://a", token_env="MY_SECRET_ENV"
        )
        token = inst.resolve_token({"MY_SECRET_ENV": "super-secret-value-xyz"})
        assert token == "super-secret-value-xyz"
        # The resolved token is a local — the frozen config never captured it.
        assert "super-secret-value-xyz" not in repr(inst)
        assert "super-secret-value-xyz" not in str(inst)
        # The env-var NAME is fine to appear (it's not the secret).
        assert "MY_SECRET_ENV" in repr(inst)


class TestSettingsIntegration:
    def test_channels_config_path_is_under_config_dir(self, tmp_path: Path) -> None:
        from coding_bridge.config import Settings

        s = Settings(config_dir=tmp_path)
        assert s.channels_config_path == tmp_path / "channels.toml"


class TestPolicyFields:
    def test_defaults_match_channelpolicy_defaults(self) -> None:
        from coding_bridge.channels.policy import ChannelPolicy

        inst = WeChatInstanceConfig(instance_id="x", base_url="http://a", token_env="T")
        default_policy = ChannelPolicy()
        assert inst.trigger_prefix == default_policy.trigger_prefix
        assert inst.allowed_senders == default_policy.allowed_senders
        assert inst.rate_limit_per_min == default_policy.rate_limit_per_min
        assert inst.dedup_window_seconds == default_policy.dedup_window_seconds

    def test_to_policy_roundtrip(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
[[channels.wechat]]
instance_id = "x"
base_url = "http://a"
token_env = "T"
trigger_prefix = "@bot "
allowed_senders = ["wxid_alice", "wxid_bob"]
rate_limit_per_min = 3
dedup_window_seconds = 60.0
""",
        )
        inst = load_channels_config(p).wechat[0]
        pol = inst.to_policy()
        assert pol.trigger_prefix == "@bot "
        assert pol.allowed_senders == ("wxid_alice", "wxid_bob")
        assert pol.rate_limit_per_min == 3
        assert pol.dedup_window_seconds == 60.0

    def test_trigger_prefix_must_be_string(self) -> None:
        with pytest.raises(ConfigError, match=r"trigger_prefix must be a string"):
            parse_channels_config(
                {
                    "channels": {
                        "wechat": [
                            {
                                "instance_id": "x",
                                "base_url": "http://a",
                                "token_env": "T",
                                "trigger_prefix": 123,
                            }
                        ]
                    }
                }
            )

    def test_allowed_senders_must_be_list_of_nonempty_strings(self) -> None:
        for bad in ["nope", [""], [1, 2], [None]]:
            with pytest.raises(ConfigError, match=r"allowed_senders"):
                parse_channels_config(
                    {
                        "channels": {
                            "wechat": [
                                {
                                    "instance_id": "x",
                                    "base_url": "http://a",
                                    "token_env": "T",
                                    "allowed_senders": bad,
                                }
                            ]
                        }
                    }
                )

    def test_rate_limit_must_be_non_negative_int(self) -> None:
        for bad in [-1, "high", True, 3.5]:
            with pytest.raises(ConfigError, match=r"rate_limit_per_min"):
                parse_channels_config(
                    {
                        "channels": {
                            "wechat": [
                                {
                                    "instance_id": "x",
                                    "base_url": "http://a",
                                    "token_env": "T",
                                    "rate_limit_per_min": bad,
                                }
                            ]
                        }
                    }
                )

    def test_dedup_window_validated(self) -> None:
        for bad in [-1, "long", True]:
            with pytest.raises(ConfigError, match=r"dedup_window"):
                parse_channels_config(
                    {
                        "channels": {
                            "wechat": [
                                {
                                    "instance_id": "x",
                                    "base_url": "http://a",
                                    "token_env": "T",
                                    "dedup_window_seconds": bad,
                                }
                            ]
                        }
                    }
                )

    def test_dedup_window_nan_and_inf_rejected(self) -> None:
        for bad in [float("nan"), float("inf"), float("-inf")]:
            with pytest.raises(ConfigError, match=r"dedup_window_seconds must be finite"):
                parse_channels_config(
                    {
                        "channels": {
                            "wechat": [
                                {
                                    "instance_id": "x",
                                    "base_url": "http://a",
                                    "token_env": "T",
                                    "dedup_window_seconds": bad,
                                }
                            ]
                        }
                    }
                )
