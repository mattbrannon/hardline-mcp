"""A streaming run is judged by silence, not age (_run_cmd's idle_s).

Real child processes, since the point is what a pipe actually delivers and
when. Bounds are generous for loaded CI runners: a chatty child writes every
0.1 s against a 2 s idle limit.
"""

import sys
import time

import pytest

from hardline_mcp import adapters, procid


@pytest.fixture(autouse=True)
def quick_watch(monkeypatch):
    monkeypatch.setattr(adapters, "_WATCH_TICK_S", 0.05)


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


CHATTY = (
    "import sys, time\n"
    "for i in range(40):\n"
    "    print('tick', i, flush=True); time.sleep(0.1)\n"
)


def test_a_run_that_keeps_writing_outlives_the_idle_limit():
    out = adapters._run_cmd(_child(CHATTY), timeout_s=120, idle_s=2)
    assert out.get("ok") is True, out
    assert out["reply"].count("tick") == 40


def test_writing_to_stderr_alone_counts_as_alive():
    code = CHATTY.replace("print('tick', i, flush=True)", "sys.stderr.write('.'); sys.stderr.flush()")
    out = adapters._run_cmd(_child(code + "print('done')"), timeout_s=120, idle_s=2)
    assert out.get("ok") is True, out


def test_a_silent_run_is_stopped_as_wedged_with_what_it_wrote():
    # Every child here ends itself within 30 s, so a mutation that removes a
    # limit fails by assertion instead of hanging the run.
    code = "import time\nprint('started', flush=True)\ntime.sleep(30)\n"
    started = time.monotonic()
    pids = []
    out = adapters._run_cmd(_child(code), timeout_s=120, idle_s=1, on_spawn=pids.append)
    assert time.monotonic() - started < 25
    assert out.get("timed_out") is True and out.get("timeout_kind") == "idle", out
    assert out.get("idle_s") == 1
    assert "no output for 1s" in out.get("error", "")
    assert "started" in out.get("partial_stdout", {}).get("raw_excerpt", "")
    assert procid.instance_state(pids[0], None) == procid.DEAD


def test_the_ceiling_still_ends_a_run_that_never_stops_writing():
    code = "import time\nfor _ in range(300):\n    print('x', flush=True); time.sleep(0.1)\n"
    out = adapters._run_cmd(_child(code), timeout_s=2, idle_s=60)
    assert out.get("timed_out") is True and out.get("timeout_kind") == "ceiling", out
    assert out.get("error") == "timeout after 2s"


def test_output_decodes_as_text_mode_would():
    code = "import sys\nsys.stdout.buffer.write('caf\\u00e9\\r\\nx\\ry\\n'.encode())\n"
    out = adapters._run_cmd(_child(code), timeout_s=60, idle_s=10)
    assert out.get("ok") is True, out
    assert out["reply"] == "café\nx\ny"  # the reply is stripped


@pytest.mark.parametrize("streaming", [False, True])
def test_only_a_streaming_agent_run_is_watched(monkeypatch, streaming):
    for name in ("HARDLINE_CLAUDE_TIMEOUT_S", "HARDLINE_CLAUDE_IDLE_S"):
        monkeypatch.delenv(name, raising=False)
    seen = {}
    monkeypatch.setattr(adapters, "_run_cmd", lambda argv, **kw: seen.update(kw) or {"ok": True})
    adapters._run_agent_cmd("claude", ["x"], streaming=streaming)
    if streaming:
        assert (seen.get("timeout_s"), seen.get("idle_s")) == (
            adapters._STREAMING_TIMEOUT_S, adapters._IDLE_S,
        )
    else:
        assert (seen.get("timeout_s"), seen.get("idle_s")) == (adapters._CLAUDE_TIMEOUT_S, None)


def test_the_idle_limit_can_be_configured_and_is_validated(monkeypatch):
    seen = {}
    monkeypatch.setattr(adapters, "_run_cmd", lambda argv, **kw: seen.update(kw) or {"ok": True})
    monkeypatch.setenv("HARDLINE_CODEX_IDLE_S", "300")
    adapters._run_agent_cmd("codex", ["x"], streaming=True)
    assert seen.get("idle_s") == 300
    monkeypatch.setenv("HARDLINE_CODEX_IDLE_S", "never")
    out = adapters._run_agent_cmd("codex", ["x"], streaming=True)
    assert out.get("ok") is False and "HARDLINE_CODEX_IDLE_S" in out.get("error", "")
