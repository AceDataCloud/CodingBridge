"""Real subprocess regressions: long JSONL, cancellation and detached shell jobs."""

import asyncio
import os
import sys

import pytest

from coding_bridge.config import Settings
from coding_bridge.providers import codex
from coding_bridge.providers.codex import CodexProvider


def provider(tmp_path):
    events = []

    async def emit(event):
        events.append(event)

    return CodexProvider("probe", emit, None, Settings(default_cwd=str(tmp_path))), events


def executable(monkeypatch, source):
    spawn = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        return await spawn(sys.executable, "-u", "-c", source, **kwargs)

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(codex.capabilities, "resolve_cli", lambda *a: sys.executable)


async def run(p, tmp_path):
    await p.start("test", cwd=str(tmp_path), model=None, permission_mode="default")


@pytest.mark.parametrize("size", [70_000, 250_000, 2_000_000])
async def test_large_stdout_and_stderr_do_not_block(tmp_path, monkeypatch, size):
    executable(
        monkeypatch,
        f"""import json,sys
sys.stderr.write('e' * {size});sys.stderr.flush()
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"x"*{size}}}}}))
print(json.dumps({{"type":"turn.completed"}}))""",
    )
    p, events = provider(tmp_path)
    await asyncio.wait_for(run(p, tmp_path), 10)
    assert next(e["text"] for e in events if e["event"] == "session.text") == "x" * size
    assert events[-1]["event"] == "session.result"


async def test_oversize_event_stops_process_before_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "_MAX_EVENT_BYTES", 100_000)
    executable(
        monkeypatch,
        "import sys,time;sys.stdout.write('x'*500000);sys.stdout.flush();time.sleep(60)",
    )
    p, _ = provider(tmp_path)
    with pytest.raises(RuntimeError, match="8 MiB"):
        await asyncio.wait_for(run(p, tmp_path), 8)
    assert p._proc is None


async def test_clean_exit_without_result_is_an_error(tmp_path, monkeypatch):
    executable(monkeypatch, "pass")
    p, events = provider(tmp_path)
    await run(p, tmp_path)
    assert events[-1]["event"] == "session.error"
    assert "without a turn result" in events[-1]["message"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX detached process-group regression")
async def test_interrupt_stops_detached_descendant_and_emits_terminal(tmp_path, monkeypatch):
    marker = tmp_path / "late.txt"
    child = (
        f"import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r})"
        ".write_text('bad');time.sleep(60)"
    )
    executable(
        monkeypatch,
        f"""import subprocess,sys,json,time
subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True)
print(json.dumps({{"type":"thread.started","thread_id":"test"}}))
time.sleep(60)""",
    )
    p, events = provider(tmp_path)
    task = asyncio.create_task(run(p, tmp_path))
    try:

        async def identified():
            while not p._announced_identity:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(identified(), 5)
        await p.interrupt()
        await asyncio.wait_for(task, 8)
        await asyncio.sleep(1.1)
        assert not marker.exists()
        terminal = [e for e in events if e["event"] in ("session.error", "session.result")]
        assert len(terminal) == 1
        assert terminal[0]["subtype"] == "interrupted"
    finally:
        await p.aclose()


async def test_cancelled_reader_reaps_process(tmp_path, monkeypatch):
    executable(monkeypatch, "import time;time.sleep(60)")
    p, _ = provider(tmp_path)
    task = asyncio.create_task(run(p, tmp_path))
    while p._proc is None:
        await asyncio.sleep(0.01)
    proc = p._proc
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 8)
    assert proc.returncode is not None
