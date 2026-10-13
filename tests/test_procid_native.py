"""procid against real processes, on whichever OS runs the suite.

Every other identity test patches the probes, which proves the decisions and
nothing about the platform underneath. These ask the OS, so each CI platform
proves its own probe: a macOS that silently returned None for every token
passed the whole suite while identity there degraded to bare pids.
"""

import os
import subprocess
import sys
import time

import pytest

from hardline_mcp import procid


@pytest.fixture
def child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield proc
    proc.kill()
    proc.wait()


def test_a_child_is_its_own_instance_and_ours(child):
    mine = procid.process_key(os.getpid())
    theirs = procid.process_key(child.pid)
    assert mine and theirs and mine != theirs
    assert procid.process_key(child.pid) == theirs  # stable
    assert procid.parent_pid_of(child.pid) == os.getpid()
    assert "python" in (procid.image_name(child.pid) or "").lower()
    # A reused parent pid cannot be younger than its living child.
    assert procid._created_after(theirs, mine)
    assert not procid._created_after(mine, theirs)


def test_an_exited_child_is_a_different_instance_than_its_record(child):
    key = procid.process_key(child.pid)
    child.kill()
    child.wait()
    assert procid.instance_state(child.pid, key) == procid.DEAD


@pytest.mark.skipif(os.name == "nt", reason="Windows has no zombies to tell apart")
def test_an_unreaped_child_is_not_alive(child):
    """kill(pid, 0) succeeds for a zombie: only the process state says it is dead."""
    child.stdin.close()  # it exits; nothing here reaps it until the fixture does
    deadline = time.monotonic() + 10
    while procid.pid_alive(child.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child.returncode is None, "reaped: no zombie was tested"
    assert procid.pid_alive(child.pid) is False
