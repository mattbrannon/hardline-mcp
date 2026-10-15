"""A streaming run is judged by silence, not age (_run_cmd's idle_s).

Real child processes, since the point is what a pipe actually delivers and
when. Bounds are generous for loaded CI runners: a chatty child writes every
0.1 s against a 2 s idle limit.
"""

import os
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
    assert out["reply"] == "caf\u00e9\nx\ny"  # the reply is stripped


def test_a_sequence_split_across_reads_decodes_whole():
    """A UTF-8 character and a CRLF each arrive in two writes, so two reads."""
    code = (
        "import sys, time\nw = sys.stdout.buffer\n"
        "for part in (b'caf\\xc3', b'\\xa9 x\\r', b'\\ny'):\n"
        "    w.write(part); w.flush(); time.sleep(0.3)\n"
    )
    out = adapters._run_cmd(_child(code), timeout_s=60, idle_s=10)
    assert out.get("reply") == "caf\u00e9 x\ny", out


GRANDCHILD = (
    "import subprocess, sys\n"
    "kid = subprocess.Popen([sys.executable, '-c', {kid!r}])\n"
    "print('grandchild', kid.pid, flush=True)\n"
)


def _grandchild_pid(out: dict) -> int:
    text = out.get("partial_stdout", {}).get("raw_excerpt", "") or out.get("reply", "")
    return int(text.split("grandchild", 1)[1].split()[0])


def test_limits_hold_over_a_grandchild_left_holding_the_pipes():
    """The launcher exits; what it left keeps the output open and says nothing.
    The run still ends at the idle limit instead of waiting on the grandchild."""
    code = GRANDCHILD.format(kid="import time; time.sleep(30)")
    started = time.monotonic()
    out = adapters._run_cmd(_child(code), timeout_s=120, idle_s=1)
    assert time.monotonic() - started < 25, "waited on the grandchild"
    assert out.get("timed_out") is True and out.get("timeout_kind") == "idle", out
    if os.name != "nt":
        # Its group is addressed directly, so it is reached after the launcher
        # is gone. On Windows nothing reaches an orphan by its exited parent,
        # and the result says what could not be captured.
        assert procid.instance_state(_grandchild_pid(out), None) == procid.DEAD
    else:
        assert out.get("capture_incomplete") is True


def test_a_limit_kills_the_tree_and_drains_it():
    """A live launcher's chatty grandchild dies with it, so both pipes close."""
    kid = "import time\nfor _ in range(300):\n    print('k', flush=True); time.sleep(0.1)\n"
    code = GRANDCHILD.format(kid=kid) + "import time; time.sleep(30)\n"
    out = adapters._run_cmd(_child(code), timeout_s=2, idle_s=60)
    assert out.get("timeout_kind") == "ceiling", out
    assert "capture_incomplete" not in out, "a descendant outlived the kill"
    assert procid.instance_state(_grandchild_pid(out), None) == procid.DEAD


def test_a_pipe_is_drained_before_it_is_closed(monkeypatch):
    """Killing the writer leaves what it wrote in the pipe: a reader behind the
    kill must still get it, so the pipe is not closed under that reader."""
    real, reads = adapters._read_piece, {}

    def lagging(raw):
        reads[id(raw)] = reads.get(id(raw), 0) + 1
        if reads[id(raw)] == 2:
            # 'tail' lands in the pipe meanwhile, unread; longer than the kill
            # takes (taskkill is about a second), so the reader is still behind
            # it when the kill returns.
            time.sleep(5)
        return real(raw)

    monkeypatch.setattr(adapters, "_read_piece", lagging)
    code = (
        "import time\nprint('first', flush=True)\ntime.sleep(0.3)\n"
        "print('tail', flush=True)\ntime.sleep(30)\n"
    )
    out = adapters._run_cmd(_child(code), timeout_s=120, idle_s=1)
    assert out.get("timeout_kind") == "idle", out
    assert "tail" in out.get("partial_stdout", {}).get("raw_excerpt", ""), out


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
