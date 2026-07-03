"""Tests for ``coding_bridge.channels.policy`` — the abuse-control gate."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from coding_bridge.channels.base import (
    ChannelAdapter,
    ChannelTarget,
    IncomingMessage,
    MessageHandler,
    SendResult,
)
from coding_bridge.channels.policy import ChannelPolicy, PolicyGate

# ---------- test fixtures ------------------------------------------------------


class _StubAdapter:
    """Minimal ``ChannelAdapter`` for the gate to log against."""

    name = "wechat"
    instance_id = "test-instance"
    handler: MessageHandler | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def run(self) -> None:  # pragma: no cover - not exercised
        raise NotImplementedError

    async def send(
        self, target: ChannelTarget, text: str, *, reply_to: str | None = None
    ) -> SendResult:  # pragma: no cover
        return SendResult(ok=True)

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _msg(
    text: str,
    *,
    sender_id: str = "wxid_alice",
    upstream_id: str | None = "u1",
    conversation_id: str = "wxid_alice",
    msg_type: str = "text",
    direction: str = "inbound",
) -> IncomingMessage:
    return IncomingMessage(
        sender_id=sender_id,
        sender_name="Alice",
        target=ChannelTarget(conversation_id=conversation_id),
        text=text,
        msg_type=msg_type,
        direction=direction,
        upstream_id=upstream_id,
    )


@dataclass
class _Recorder:
    """Captures each ``(msg, adapter)`` the downstream handler sees."""

    calls: list[tuple[IncomingMessage, ChannelAdapter]]

    def __init__(self) -> None:
        self.calls = []

    def make_handler(self) -> MessageHandler:
        async def handler(msg: IncomingMessage, adapter: ChannelAdapter) -> None:
            self.calls.append((msg, adapter))
        return handler


class _Clock:
    """Manually-advanceable monotonic clock for the sliding-window tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------- trigger prefix ----------------------------------------------------


class TestTriggerPrefix:
    @pytest.mark.asyncio
    async def test_default_prefix_ask_forwards_and_strips(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(ChannelPolicy(), recorder.make_handler())
        await gate.handle(_msg("/ask hello"), _StubAdapter())
        assert len(recorder.calls) == 1
        # Text stripped of prefix (and leading whitespace)
        assert recorder.calls[0][0].text == "hello"

    @pytest.mark.asyncio
    async def test_message_without_prefix_is_dropped(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(ChannelPolicy(), recorder.make_handler())
        await gate.handle(_msg("hello just chatting"), _StubAdapter())
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_empty_after_prefix_is_dropped(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(ChannelPolicy(), recorder.make_handler())
        await gate.handle(_msg("/ask   "), _StubAdapter())
        assert recorder.calls == []

    @pytest.mark.asyncio
    async def test_empty_prefix_disables_check(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(trigger_prefix="", rate_limit_per_min=0),
            recorder.make_handler(),
        )
        await gate.handle(_msg("no prefix needed"), _StubAdapter())
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0].text == "no prefix needed"

    @pytest.mark.asyncio
    async def test_custom_prefix(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(trigger_prefix="@bot "), recorder.make_handler()
        )
        await gate.handle(_msg("@bot go"), _StubAdapter())
        assert recorder.calls[0][0].text == "go"


# ---------- sender allowlist --------------------------------------------------


class TestAllowlist:
    @pytest.mark.asyncio
    async def test_empty_allowlist_allows_all(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(ChannelPolicy(allowed_senders=()), recorder.make_handler())
        await gate.handle(_msg("/ask x", sender_id="stranger"), _StubAdapter())
        assert len(recorder.calls) == 1

    @pytest.mark.asyncio
    async def test_allowlist_hit_forwards(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(allowed_senders=("wxid_alice",)), recorder.make_handler()
        )
        await gate.handle(_msg("/ask x", sender_id="wxid_alice"), _StubAdapter())
        assert len(recorder.calls) == 1

    @pytest.mark.asyncio
    async def test_allowlist_miss_drops(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(allowed_senders=("wxid_alice",)), recorder.make_handler()
        )
        await gate.handle(_msg("/ask x", sender_id="wxid_evil"), _StubAdapter())
        assert recorder.calls == []


# ---------- rate limit --------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_under_limit_all_forwarded(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=3, dedup_window_seconds=0),
            recorder.make_handler(),
            clock=clock,
        )
        for i in range(3):
            await gate.handle(_msg("/ask x", upstream_id=f"m{i}"), _StubAdapter())
        assert len(recorder.calls) == 3

    @pytest.mark.asyncio
    async def test_over_limit_drops(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=2, dedup_window_seconds=0),
            recorder.make_handler(),
            clock=clock,
        )
        for i in range(5):
            await gate.handle(_msg("/ask x", upstream_id=f"m{i}"), _StubAdapter())
        assert len(recorder.calls) == 2

    @pytest.mark.asyncio
    async def test_window_slides(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=1, dedup_window_seconds=0),
            recorder.make_handler(),
            clock=clock,
        )
        await gate.handle(_msg("/ask a", upstream_id="m1"), _StubAdapter())
        clock.now += 30
        await gate.handle(_msg("/ask b", upstream_id="m2"), _StubAdapter())
        assert len(recorder.calls) == 1  # still inside 60s window
        clock.now += 31  # now past the 60s edge
        await gate.handle(_msg("/ask c", upstream_id="m3"), _StubAdapter())
        assert len(recorder.calls) == 2

    @pytest.mark.asyncio
    async def test_per_sender_isolation(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=1, dedup_window_seconds=0),
            recorder.make_handler(),
            clock=clock,
        )
        await gate.handle(_msg("/ask a", sender_id="alice", upstream_id="a1"), _StubAdapter())
        await gate.handle(_msg("/ask a", sender_id="alice", upstream_id="a2"), _StubAdapter())
        await gate.handle(_msg("/ask a", sender_id="bob", upstream_id="b1"), _StubAdapter())
        # alice hit her limit at msg 2; bob is a separate bucket
        assert [c[0].sender_id for c in recorder.calls] == ["alice", "bob"]

    @pytest.mark.asyncio
    async def test_zero_limit_disables_rate_limit(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=0, dedup_window_seconds=0),
            recorder.make_handler(),
        )
        for i in range(50):
            await gate.handle(_msg("/ask x", upstream_id=f"m{i}"), _StubAdapter())
        assert len(recorder.calls) == 50


# ---------- dedup -------------------------------------------------------------


class TestDedup:
    @pytest.mark.asyncio
    async def test_repeat_msg_id_dropped(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=0), recorder.make_handler()
        )
        await gate.handle(_msg("/ask x", upstream_id="dup-1"), _StubAdapter())
        await gate.handle(_msg("/ask x", upstream_id="dup-1"), _StubAdapter())
        assert len(recorder.calls) == 1

    @pytest.mark.asyncio
    async def test_missing_upstream_id_bypasses_dedup(self) -> None:
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=0), recorder.make_handler()
        )
        await gate.handle(_msg("/ask x", upstream_id=None), _StubAdapter())
        await gate.handle(_msg("/ask x", upstream_id=None), _StubAdapter())
        assert len(recorder.calls) == 2

    @pytest.mark.asyncio
    async def test_dedup_window_expiry(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=0, dedup_window_seconds=10.0),
            recorder.make_handler(),
            clock=clock,
        )
        await gate.handle(_msg("/ask x", upstream_id="d"), _StubAdapter())
        clock.now += 11
        await gate.handle(_msg("/ask x", upstream_id="d"), _StubAdapter())
        assert len(recorder.calls) == 2

    @pytest.mark.asyncio
    async def test_dedup_cache_bounded(self) -> None:
        # With max=3 the LRU evicts the oldest so the 4th unique id displaces id-1
        recorder = _Recorder()
        gate = PolicyGate(
            ChannelPolicy(
                rate_limit_per_min=0, dedup_max_ids=3, dedup_window_seconds=3600.0
            ),
            recorder.make_handler(),
        )
        for i in range(4):
            await gate.handle(_msg("/ask x", upstream_id=f"id-{i}"), _StubAdapter())
        # id-0 was evicted after id-3 filled the cache → resend of id-0 goes through
        await gate.handle(_msg("/ask x", upstream_id="id-0"), _StubAdapter())
        assert len(recorder.calls) == 5


# ---------- downstream isolation ---------------------------------------------


class TestDownstreamIsolation:
    @pytest.mark.asyncio
    async def test_downstream_exception_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        async def boom(msg: IncomingMessage, adapter: ChannelAdapter) -> None:
            raise RuntimeError("dispatcher exploded")

        gate = PolicyGate(ChannelPolicy(), boom)
        with caplog.at_level(logging.ERROR, logger="coding-bridge.channels"):
            await gate.handle(_msg("/ask hi"), _StubAdapter())
        # No exception raised out of gate.handle → adapter loop survives
        assert any("downstream handler raised" in r.message for r in caplog.records)


# ---------- log redaction: NOTHING sensitive leaves via logs ------------------


class TestLogRedaction:
    @pytest.mark.asyncio
    async def test_reject_log_never_contains_message_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gate = PolicyGate(
            ChannelPolicy(allowed_senders=("wxid_alice",)),
            _Recorder().make_handler(),
        )
        secret_body = "SUPER_SECRET_TOKEN_abcdef123456"
        with caplog.at_level(logging.INFO, logger="coding-bridge.channels"):
            await gate.handle(
                _msg(f"/ask {secret_body}", sender_id="wxid_evil"), _StubAdapter()
            )
        # Rejection was logged
        assert any("rejected" in r.message for r in caplog.records)
        # ...but the message text (secret) MUST NOT appear anywhere in the logs
        for record in caplog.records:
            assert secret_body not in record.getMessage()
            assert secret_body not in str(record.args or ())

    @pytest.mark.asyncio
    async def test_reject_reason_codes_stable(self, caplog: pytest.LogCaptureFixture) -> None:
        # Grep-friendly reason codes for operator dashboards.
        stub = _StubAdapter()
        recorder = _Recorder()
        cases = [
            (
                ChannelPolicy(trigger_prefix="/ask "),
                _msg("no prefix here"),
                "trigger_prefix_missing",
            ),
            (
                ChannelPolicy(trigger_prefix="/ask "),
                _msg("/ask   "),
                "empty_after_prefix",
            ),
            (
                ChannelPolicy(allowed_senders=("only-alice",)),
                _msg("/ask x", sender_id="bob"),
                "sender_not_allowed",
            ),
        ]
        with caplog.at_level(logging.INFO, logger="coding-bridge.channels"):
            for policy, msg, expected in cases:
                caplog.clear()
                gate = PolicyGate(policy, recorder.make_handler())
                await gate.handle(msg, stub)
                assert any(
                    f"reason={expected}" in r.getMessage() for r in caplog.records
                ), f"missing reason={expected} in {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_traceback_from_downstream_does_not_leak_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret = "SECRET_PAYLOAD_do_not_log_me"

        async def raise_with_secret_in_msg(
            msg: IncomingMessage, adapter: ChannelAdapter
        ) -> None:
            # Deliberately try to sneak the payload into the error message —
            # the gate must NOT format str(exc) into its log NOR call
            # logger.exception() (which would emit the traceback + str(exc)).
            raise RuntimeError(f"boom {msg.text}")

        gate = PolicyGate(ChannelPolicy(), raise_with_secret_in_msg)
        with caplog.at_level(logging.ERROR, logger="coding-bridge.channels"):
            await gate.handle(_msg(f"/ask {secret}"), _StubAdapter())
        # Verify the FULL formatted log output (including any traceback +
        # str(exc)) does not carry the secret. caplog.text is the joined
        # formatted output — this is the string a real operator sees.
        assert secret not in caplog.text, (
            f"secret leaked into log output: {caplog.text!r}"
        )
        # Sanity: the exc class name IS in the log (proves the handler fired)
        assert "RuntimeError" in caplog.text


# ---------- unbounded-memory defense: rate-limit map cap ----------------------


class TestRateLimitMapBounded:
    @pytest.mark.asyncio
    async def test_map_size_capped_by_rate_limit_max_senders(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        # cap=4 so the 5th unique sender causes an eviction; small `dedup=0`
        # so we don't have to reason about dedup here.
        gate = PolicyGate(
            ChannelPolicy(
                rate_limit_per_min=1,
                rate_limit_max_senders=4,
                dedup_window_seconds=0,
            ),
            recorder.make_handler(),
            clock=clock,
        )
        for i in range(20):
            await gate.handle(
                _msg("/ask x", sender_id=f"sender-{i}", upstream_id=f"m{i}"),
                _StubAdapter(),
            )
        # The map never grew past 4 — a 20-sender burst can't OOM us.
        assert len(gate._windows) <= 4

    @pytest.mark.asyncio
    async def test_idle_windows_are_evicted(self) -> None:
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(rate_limit_per_min=1, dedup_window_seconds=0),
            recorder.make_handler(),
            clock=clock,
        )
        # 5 distinct senders at t=0
        for i in range(5):
            await gate.handle(
                _msg("/ask x", sender_id=f"s{i}", upstream_id=f"m{i}"),
                _StubAdapter(),
            )
        assert len(gate._windows) == 5
        # jump 3 min into the future; a fresh message triggers a sweep
        clock.now += 200.0
        await gate.handle(
            _msg("/ask x", sender_id="fresh", upstream_id="fresh-1"), _StubAdapter()
        )
        # All 5 stale windows were pruned; only the fresh one remains
        assert set(gate._windows.keys()) == {"fresh"}

    @pytest.mark.asyncio
    async def test_lru_eviction_preserves_recent_senders(self) -> None:
        # cap=2; hit A, hit B, hit A again, then hit C — B is LRU, gets dropped
        recorder = _Recorder()
        clock = _Clock()
        gate = PolicyGate(
            ChannelPolicy(
                rate_limit_per_min=1,
                rate_limit_max_senders=2,
                dedup_window_seconds=0,
            ),
            recorder.make_handler(),
            clock=clock,
        )
        await gate.handle(_msg("/ask a", sender_id="A", upstream_id="1"), _StubAdapter())
        clock.now += 1
        await gate.handle(_msg("/ask a", sender_id="B", upstream_id="2"), _StubAdapter())
        clock.now += 1
        # A hits again → A is now MRU, B is LRU
        # But A is over rate limit (limit=1) so this is a rejection —
        # rejections DON'T touch the window (they never enter `allow()`
        # before the cap check). Actually A already has a window entry,
        # so `allow()` DOES run and updates last_touch. Verify by checking
        # that C's insertion drops B, not A.
        await gate.handle(_msg("/ask a", sender_id="A", upstream_id="3"), _StubAdapter())
        clock.now += 1
        await gate.handle(_msg("/ask a", sender_id="C", upstream_id="4"), _StubAdapter())
        # A survived, B was evicted, C is new
        assert set(gate._windows.keys()) == {"A", "C"}
