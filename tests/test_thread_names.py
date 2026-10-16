"""A Codex thread's claimed names come back to its next server (bindings).

docs/session-continuity.md, "A Codex thread's names". The previous server is
played by a dead (pid, key) written into ``thread_names``; this process is the
successor. A live foreign process is a real child.
"""

import sqlite3
import subprocess
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from hardline_mcp import adapters, bindings, codex_queue, jobs, mailbox, procid, server, sessions
from test_codex_queue import OTHER, THREAD, meta

GONE = 0x7FFFFFFE  # well-formed and reliably absent: a dead previous server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    mailbox.send("x", "nobody", "creates the store", db_path=db)
    return db


@pytest.fixture
def wake(store):
    """This server's Codex connection, pinned to THREAD."""
    w = codex_queue.CodexWake(queue=lambda thread, text: {"ok": True})
    w.observe({"_meta": meta()})
    assert w.pinned() == THREAD
    server._codex_wake[:] = [w]
    return w


@pytest.fixture
def live():
    """A live foreign process: (pid, key)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield proc.pid, procid.process_key(proc.pid)
    proc.kill()
    proc.wait()


def ctx(thread=THREAD, source="user"):
    """A tool Context whose request carries Codex's ``_meta``."""
    data = meta(thread=thread, source=source)
    return SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(model_dump=lambda **_: data))
    )


def bind(db, lane, seq, *, pid=GONE, key="gone", thread=THREAD):
    with closing(sqlite3.connect(db)) as raw, raw:
        bindings.ensure(raw)
        raw.execute(
            "INSERT OR REPLACE INTO thread_names (lane, thread, seq, pid, process_key, bound_at)"
            " VALUES (?, ?, ?, ?, ?, 't')",
            (lane, thread, seq, pid, key),
        )


def rows(db) -> dict:
    with closing(sqlite3.connect(db)) as raw:
        try:
            got = raw.execute("SELECT lane, thread, seq, pid, process_key FROM thread_names")
        except sqlite3.OperationalError:
            return {}
        return {r[0]: r[1:] for r in got}


def me():
    return procid.current_identity()


# ── binding on claim ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_claim_from_the_pinned_thread_is_bound_to_it(wake, store):
    got = await server.register_session(label="role", ctx=ctx())
    assert got.get("bound_to_thread") is True, got
    assert rows(store).get("codex:role", ())[:2] == (THREAD, 1), "bound, at its place"
    assert rows(store)["codex:role"][2:] == me()


@pytest.mark.anyio
@pytest.mark.parametrize("request_ctx", [ctx(source="subagent"), ctx(thread=OTHER), None])
async def test_a_claim_not_from_the_pinned_thread_unbinds_the_name(wake, store, request_ctx):
    bind(store, "codex:role", 1)
    got = await server.register_session(label="role", ctx=request_ctx)
    assert got.get("ok") is True and got.get("bound_to_thread") is False, got
    assert got.get("binding_note")
    assert "codex:role" not in rows(store)


@pytest.mark.anyio
async def test_a_claim_for_another_agent_is_never_bound(wake, store, monkeypatch):
    monkeypatch.delenv("HARDLINE_AGENT")
    got = await server.register_session(label="role", agent="claude", ctx=ctx())
    assert got.get("bound_to_thread") is False, got
    assert not any(lane.startswith("claude:") for lane in rows(store))


def test_an_automatic_grant_leaves_a_binding_alone(wake, store, monkeypatch):
    """Another thread's name re-acquired at startup keeps that thread's claim."""
    bind(store, "codex:pinned", 3, thread=OTHER)
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    server._announce_self("codex")
    assert rows(store).get("codex:pinned") == (OTHER, 3, GONE, "gone")


def test_bindings_survive_their_process_leaving_the_registry(store):
    bind(store, "codex:role", 1, pid=me()[0], key=me()[1])
    sessions.unregister(db_path=store)
    sessions.live(db_path=store)  # prunes the dead
    assert "codex:role" in rows(store)


# ── release and dismissal ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_release_withdraws_the_pinned_threads_binding_whoever_wrote_it(wake, store):
    """The name was re-bound to this thread by another server; released here,
    it must not come back."""
    assert (await server.register_session(label="role", ctx=ctx()))["ok"]
    bind(store, "codex:role", 5)  # Q bound it to the same thread and exited
    assert (await server.release_session(label="role"))["ok"]
    assert "codex:role" not in rows(store)


def test_a_release_leaves_another_threads_binding_by_another_writer(store):
    sessions.register(agent="codex", lane="codex:role", db_path=store)
    bind(store, "codex:role", 1, thread=OTHER)
    sessions.drop_lane("codex:role", thread=THREAD, db_path=store)
    assert rows(store).get("codex:role") == (OTHER, 1, GONE, "gone")


@pytest.mark.anyio
async def test_a_release_that_fails_durably_keeps_the_name(wake, store, monkeypatch):
    assert (await server.register_session(label="role", ctx=ctx()))["ok"]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sessions, "drop_lane", broken)
    with pytest.raises(Exception):
        await server.release_session(label="role")
    assert "role" in adapters.claimed_lanes(), "the name went locally but not durably"


@pytest.mark.anyio
async def test_a_dismissal_of_an_unheld_name_unbinds_it_and_says_so(wake, store):
    bind(store, "codex:old", 1)
    got = await server.release_session(label="old")
    assert got.get("ok") is True and got.get("unbound") == "codex:old", got
    assert "forgotten" not in got
    assert "codex:old" not in rows(store)


@pytest.mark.anyio
async def test_releasing_a_claimed_derived_name_keeps_the_lane_at_its_place(
    wake, store, monkeypatch
):
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "pinned")
    for label in ("x", "pinned"):
        assert (await server.register_session(label=label, ctx=ctx()))["ok"]
    assert "codex:pinned" in rows(store)
    got = await server.release_session(label="pinned")
    assert got.get("ok") is True, got
    assert "codex:pinned" not in rows(store)
    assert adapters.claimed_lanes() == ("x",)
    entry = sessions.live(db_path=store)[0]
    assert entry["lanes"] == ["codex:pinned", "codex:x"] and entry["lane"] == "codex:x"


# ── restoration ──────────────────────────────────────────────────────────────


def test_the_threads_names_come_back_in_their_order(wake, store):
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    server._restore_pending()
    assert adapters.claimed_lanes() == ("a", "b")
    assert adapters.lane_suffix() == "b"
    assert {lane: r[2:] for lane, r in rows(store).items()} == {
        "codex:a": me(), "codex:b": me(),
    }
    entry = sessions.live(db_path=store)[0]
    assert entry["lane"] == "codex:b"
    assert sorted(server._restoration_report()["restored"]) == ["codex:a", "codex:b"]


def test_another_threads_names_are_not_restored(wake, store):
    bind(store, "codex:a", 1, thread=OTHER)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()


def test_a_name_whose_writer_lives_is_not_restored(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["outcomes"]["codex:a"][0] == "blocked"


def test_a_name_held_by_a_live_process_is_not_restored(wake, store, live):
    bind(store, "codex:a", 1)
    sessions.register(agent="codex", lane="codex:a", pid=live[0], db_path=store)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert rows(store)["codex:a"][2:] == (GONE, "gone"), "writer untouched"


def _candidate(lane, seq, pid=GONE, key="gone"):
    return {"lane": lane, "thread": THREAD, "seq": seq, "pid": pid, "process_key": key,
            "kind": "acquire", "new": True}


def _restore(candidates, store, free=8):
    return sessions.restore(
        THREAD, candidates, agent="codex", free=free,
        plan=lambda new, order: list(new), attribution=lambda t: t, db_path=store,
    )


def test_the_write_phase_rechecks_the_writer_itself(store, live):
    """Not only the survey: a writer that is alive at write time refuses."""
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    got = _restore([_candidate("codex:a", 1, *live)], store)
    assert got["accepted"] == [] and got["outcomes"]["codex:a"][0] == "blocked"


def test_the_write_phase_rechecks_the_ownership_rule(store, live):
    bind(store, "codex:a", 1)
    sessions.register(agent="codex", lane="codex:a", pid=live[0], db_path=store)
    got = _restore([_candidate("codex:a", 1)], store)
    assert got["accepted"] == [] and got["outcomes"]["codex:a"][0] == "blocked"


def test_a_stopped_wake_restores_nothing(wake, store):
    bind(store, "codex:a", 1)
    wake.observe({"_meta": meta(thread=OTHER)})  # a second top-level thread
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert server._restoration["policy"].startswith("stopped")


def test_a_stop_between_survey_and_write_commits_nothing(wake, store, monkeypatch):
    bind(store, "codex:a", 1)
    survey = sessions.survey

    def then_stop(*a, **k):
        found = survey(*a, **k)
        wake.stopped = True
        return found

    monkeypatch.setattr(sessions, "survey", then_stop)
    server._restore_pending()
    assert adapters.claimed_lanes() == ()
    assert rows(store)["codex:a"][2:] == (GONE, "gone")
    assert not sessions.holders("codex:a", db_path=store)


def test_a_changed_candidate_is_skipped_and_the_next_one_restored(wake, store, monkeypatch):
    """One slot: A changes before the write, so B gets it."""
    for i in range(adapters.MAX_CLAIMED_LANES - 1):
        adapters.claim_lane(f"filler{i}")
    bind(store, "codex:a", 2)
    bind(store, "codex:b", 1)
    survey = sessions.survey

    def then_change(*a, **k):
        found = survey(*a, **k)
        # Another dead server re-bound it meanwhile: same place, new writer,
        # which only the five-field re-read can see.
        with closing(sqlite3.connect(store)) as raw, raw:
            raw.execute(f"UPDATE thread_names SET pid = {GONE - 1} WHERE lane = 'codex:a'")
        return found

    monkeypatch.setattr(sessions, "survey", then_change)
    server._restore_pending()
    assert "b" in adapters.claimed_lanes() and "a" not in adapters.claimed_lanes()
    assert server._restoration["outcomes"]["codex:a"][0] == "changed"
    assert not sessions.holders("codex:a", db_path=store), "nothing of A was written"


def test_a_full_session_still_adopts_a_name_it_already_holds(wake, store):
    """A writer transfer adds no entry, so it costs no slot."""
    for i in range(adapters.MAX_CLAIMED_LANES - 1):
        adapters.claim_lane(f"filler{i}")
    adapters.claim_lane("a")
    bind(store, "codex:a", 1)
    bind(store, "codex:b", 2)
    server._restore_pending()
    assert rows(store)["codex:a"][2:] == me()
    assert "b" not in adapters.claimed_lanes()
    assert server._restoration["outcomes"]["codex:b"][0] == "cap"


def test_a_failing_stage_does_not_starve_restoration(wake, store, monkeypatch):
    bind(store, "codex:a", 1)

    def broken():
        raise RuntimeError("pending claims broke")

    monkeypatch.setattr(server, "_fulfil_pending", broken)
    try:
        server._regain_lanes()
    except RuntimeError:
        pass  # judged by what was restored, below
    assert adapters.claimed_lanes() == ("a",), "the failing stage starved restoration"


def test_the_first_poll_after_the_pin_restores(store):
    w = codex_queue.CodexWake(queue=lambda thread, text: {"ok": True})
    w._last_fulfil = object()  # as if fulfil had just run
    w.observe({"_meta": meta()})
    assert w._last_fulfil is None


def test_restoration_runs_on_no_tool_path():
    """Tools read what restoration published; only the wake's poll restores."""
    import inspect

    for tool in ("inbox", "list_agents", "register_session", "_admit_async", "_register_locked"):
        assert "_restore_pending" not in inspect.getsource(getattr(server, tool)), tool


# ── async admission ──────────────────────────────────────────────────────────


def _admit(from_agent="codex"):
    return server._admit_async(
        "codex", lambda prompt, **kw: {"ok": True, "reply": "r"}, "do it", from_agent,
        label=None, model=None, effort="default", mode="default", workdir=None, write=False,
    )


def test_a_dispatch_is_refused_while_the_threads_names_are_unheld(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    for _ in range(2):  # refused every time, and the slot is given back
        got = _admit()
        assert got.get("retryable") is True and got.get("accepted") is False, got
        assert [n["lane"] for n in got.get("unheld_names", [])] == ["codex:a"]
    with closing(sqlite3.connect(store)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0, "a job was created"


def test_a_thread_without_names_dispatches_with_a_note(wake, store):
    got = _admit()
    assert got.get("accepted") is True, got
    assert "continuity_note" in got


@pytest.mark.anyio
async def test_a_held_claim_dispatches_despite_an_unheld_name(wake, store, live):
    bind(store, "codex:a", 1, pid=live[0], key=live[1])
    assert (await server.register_session(label="mine", ctx=ctx()))["ok"]
    got = _admit()
    assert got.get("accepted") is True and got.get("lane") == "codex:mine", got
    assert "continuity_note" not in got


def test_an_unpinned_server_never_asks(store, monkeypatch):
    def unread(*a, **k):
        raise AssertionError("read the bindings without a pinned thread")

    monkeypatch.setattr(bindings, "unheld", unread)
    assert _admit().get("accepted") is True


def test_a_from_agent_that_is_not_this_session_is_refused(wake, store):
    got = _admit(from_agent="claude")
    assert got.get("accepted") is False and "not this session's identity" in got.get("error", "")


@pytest.mark.anyio
async def test_claims_and_jobs_publish_under_the_identity_lock(wake, store, monkeypatch):
    """A dispatch sees a claim whole or not at all."""
    claim_lane, create = adapters.claim_lane, jobs.create
    seen = []

    def claiming(label):
        seen.append(("claim", server._identity_lock.locked()))
        return claim_lane(label)

    def creating(**kw):
        seen.append(("job", server._identity_lock.locked()))
        return create(**kw)

    monkeypatch.setattr(adapters, "claim_lane", claiming)
    monkeypatch.setattr(jobs, "create", creating)
    assert (await server.register_session(label="mine", ctx=ctx()))["ok"]
    assert _admit().get("accepted") is True
    assert seen == [("claim", True), ("job", True)], seen


def _restart(store):
    """This server exits and the thread's next one starts: the binding's
    writer is dead, nothing of this process is registered or remembered."""
    with closing(sqlite3.connect(store)) as raw, raw:
        raw.execute(f"UPDATE thread_names SET pid = {GONE}, process_key = 'gone'")
    sessions.unregister(db_path=store)
    adapters.reset_claimed_lanes()
    server._restoration.update(outcomes={}, cursor=None, seen=set(), policy="no thread")


@pytest.mark.anyio
async def test_a_result_dispatched_after_a_restart_is_read_after_the_next(wake, store):
    """The acceptance test: P claims R and exits; S1 restores R, dispatches
    without claiming, the job completes; S1 exits; S2 restores R and reads the
    completion at R."""
    import time

    bind(store, "codex:r", 1)  # P claimed R on this thread, then exited
    server._restore_pending()  # S1
    assert adapters.lane_suffix() == "r"
    receipt = _admit()
    assert receipt.get("accepted") is True and receipt.get("lane") == "codex:r", receipt
    deadline = time.monotonic() + 30
    while (jobs.get(receipt["job_id"], db_path=store) or {}).get("state") != jobs.COMPLETED:
        assert time.monotonic() < deadline, "the job never finished"
        time.sleep(0.05)
    _restart(store)  # S1 exits; S2 starts
    server._restore_pending()  # S2
    assert adapters.lane_suffix() == "r"
    read = await server.inbox(agent="codex")
    bodies = [m.get("body", "") for m in read.get("messages", [])]
    assert any(receipt["job_id"] in body for body in bodies), read


# ── order, and the request's thread ──────────────────────────────────────────


def test_restored_names_go_in_by_durable_place_below_explicit_ones():
    claims, restored = adapters._with_restored(["E"], set(), ["R"], [("R", 3)])
    assert claims == ["R", "E"]
    # Round 5's trace: A restored, renumbered to 2; B, bound later at 4, after it.
    claims, restored = adapters._with_restored(["A"], {"A"}, ["B"], [("A", 2), ("B", 4)])
    assert claims == ["A", "B"]
    # An older one restored late goes below the newer.
    claims, _ = adapters._with_restored(["B"], {"B"}, ["A"], [("A", 1), ("B", 2)])
    assert claims == ["A", "B"]
    # Already listed: stays where it is, with its origin.
    claims, restored = adapters._with_restored(["X", "R"], set(), ["R"], [("R", 1)])
    assert claims == ["X", "R"] and "R" not in restored


@pytest.mark.parametrize(
    "data, expected",
    [
        (meta(), THREAD),
        (meta(source="subagent"), None),
        ({**meta(), "threadId": "not-a-uuid"}, None),
        ({**meta(), "x-codex-turn-metadata": {**meta()["x-codex-turn-metadata"], "thread_id": OTHER}}, None),
        ({}, None),
    ],
)
def test_a_request_is_attributed_only_to_its_own_top_level_thread(data, expected):
    assert codex_queue.thread_of(data) == expected


def test_a_subagent_call_does_not_stop_the_wake(wake):
    wake.observe({"_meta": meta(thread=OTHER, source="subagent")})
    assert wake.stopped is False and wake.attribution(THREAD) == THREAD
