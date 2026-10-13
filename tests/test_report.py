"""The store audit: what it classifies, and that it never writes."""

import os
import sqlite3
import subprocess
import sys
from collections import Counter

import pytest

from hardline_mcp import jobs, mailbox, procid, report, sessions


def _schema(db):
    raw = sqlite3.connect(db)
    names = sorted(r[0] for r in raw.execute("SELECT name FROM sqlite_master"))
    raw.close()
    return names


@pytest.fixture
def child():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    yield proc.pid, procid.process_key(proc.pid)
    proc.kill()
    proc.wait()


def test_revisions_are_counted_per_running_process(tmp_path, child):
    db = tmp_path / "mb.db"
    me = procid.current_identity()
    sessions.register(agent="codex", lanes=["codex:a", "codex:b"], db_path=db)
    sessions.record_revision("abc123", db_path=db)
    pid, key = child
    sessions.register(agent="codex", lane="codex:old", pid=pid, db_path=db)
    raw = sqlite3.connect(db)
    with raw:  # a dead registrant, and a stale revision for a reused pid
        raw.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (2147483646, 'codex:gone', 'codex', 'k', 't', 't', 't')"
        )
        raw.execute("INSERT INTO process_revisions VALUES (?, 'earlier', 'zzz')", (pid,))
    raw.close()

    got = report.report(db)["processes"]
    assert got["revisions"] == {"abc123": 1, "unrecorded": 1}
    assert got["states"] == {procid.ALIVE: 2, procid.DEAD: 1}
    mine = next(p for p in got["registered"] if (p["pid"], p["process_key"]) == me)
    assert mine["lanes"] == ["codex:a", "codex:b"]


def test_job_notices_are_classified_by_who_acked_them(tmp_path, monkeypatch):
    db = tmp_path / "mb.db"

    def finished(owner):
        monkeypatch.setattr(jobs, "current_identity", lambda: owner)
        job = jobs.create(agent="codex", requester="claude:r", label=None, request={}, db_path=db)
        jobs.mark_running(job, db_path=db)
        jobs.finish(job, result={"ok": True}, db_path=db)
        return job

    me = procid.current_identity()
    finished(me)
    elsewhere = finished((me[0] + 1, "another-instance"))
    finished((me[0] + 2, None))  # an owner with no token proves nothing
    mailbox.send("codex", "claude:other", f'{{"type": "job_finished", "job_id": "{elsewhere}"}}', db_path=db)
    monkeypatch.setattr(mailbox, "current_identity", lambda: me)
    mailbox.inbox(["claude:r", "claude:other"], owned=["claude:r", "claude:other"], db_path=db)

    got = report.report(db)["job_notices"]
    assert (got["same_instance"], got["other_instance"], got["unknown"]) == (1, 1, 1)
    (other,) = got["other_instance_sample"]
    assert other["job_id"] == elsewhere
    assert other["dispatched_by"] == {"pid": me[0] + 1, "process_key": "another-instance"}
    assert (other["acked_by"]["pid"], other["acked_by"]["process_key"]) == me
    assert got["acked_messages_without_record"] == 0


def test_any_body_is_free_text(tmp_path, monkeypatch):
    """A body is whatever a sender wrote: odd ones are skipped, never fatal."""
    db = tmp_path / "mb.db"
    monkeypatch.setattr(jobs, "current_identity", lambda: (1, "owner"))
    job = jobs.create(agent="codex", requester="claude:r", label=None, request={}, db_path=db)
    for body in (
        '{"type": "job_finished", "job_id": []}',
        '{"type": "job_finished", "job_id": {"x": 1}}',
        f'{{"type": "jobXfinished", "job_id": "{job}"}}',
        '["job_finished"]',
        "not json, but it says \"job_finished\"",
        f'{{"type": "note", "about": "job_finished", "job_id": "{job}"}}',
        f'{{"type":"job_finished","job_id":"{job}"}}',  # compact, and real
    ):
        mailbox.send("codex", "claude:r", body, db_path=db)
    mailbox.inbox("claude:r", owned="claude:r", db_path=db)

    got = report.report(db)["job_notices"]
    assert (got["same_instance"], got["other_instance"], got["unknown"]) == (0, 1, 0)


def test_a_registry_missing_a_column_reads_unavailable_even_when_empty(tmp_path):
    db = tmp_path / "mb.db"
    raw = sqlite3.connect(db)
    with raw:
        raw.executescript(mailbox._SCHEMA.replace("    process_key TEXT,\n    host_pid", "    host_pid"))
    raw.close()
    assert "no such column: process_key" in str(report.report(db)["processes"])


def test_probes_run_once_per_identity_after_the_snapshot_closes(tmp_path, monkeypatch):
    """No reader is held while processes are probed: a checkpoint completes."""
    db = tmp_path / "mb.db"
    sessions.register(agent="codex", lane="codex:a", host_pid=7, host_key="h", db_path=db)
    sessions.register(agent="codex", lane="codex:b", pid=8, host_pid=7, host_key="h", db_path=db)
    raw = sqlite3.connect(db)
    with raw:  # the same process in the legacy registry too
        raw.execute("CREATE TABLE sessions (pid INTEGER, lane TEXT, process_key TEXT)")
        raw.execute("INSERT INTO sessions VALUES (?, 'codex:a', ?)", procid.current_identity())
    raw.close()
    # Held open, so closing the last connection does not checkpoint the WAL
    # away: the report's reader then pins frames a checkpoint has to wait for.
    keeper = sqlite3.connect(db)
    mailbox.send("a", "b", "c", db_path=db)
    probed: Counter = Counter()
    busy = []

    def probe(pid, key):
        probed[(pid, key)] += 1
        writer = sqlite3.connect(db, timeout=0.1)
        busy.append(writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
        writer.close()
        mailbox.send("a", "b", "c", db_path=db)  # new frames for the next probe
        return procid.ALIVE

    monkeypatch.setattr(report, "instance_state", probe)
    report.report(db)
    keeper.close()
    assert probed and set(probed.values()) == {1}
    assert busy and set(busy) == {0}


def test_the_audit_never_writes(tmp_path):
    """Built raw, not through mailbox: this process must not have initialized
    it, or an initializing open would find it cached and change nothing."""
    db = tmp_path / "mb.db"
    raw = sqlite3.connect(db)
    with raw:  # a store an older revision built: none of the created-on-use tables
        raw.executescript(mailbox._SCHEMA)
        for table in ("process_revisions", "message_acks", "sessions"):
            raw.execute(f"DROP TABLE IF EXISTS {table}")
        pid, key = procid.current_identity()
        raw.execute(
            "INSERT INTO agent_sessions (pid, lane, agent, process_key, started_at,"
            " last_seen, claimed_at) VALUES (?, 'codex:a', 'codex', ?, 't', 't', 't')",
            (pid, key),
        )
    raw.close()
    before = _schema(db)

    got = report.report(db)
    assert got["processes"]["revisions"] == {"unrecorded": 1}
    assert got["legacy_sessions"] == {"table": False}
    assert got["job_notices"]["acks_recorded"] is False
    assert got["job_notices"]["acked_messages_without_record"] is None
    assert _schema(db) == before

    missing = tmp_path / "none.db"
    with pytest.raises(FileNotFoundError):
        report.report(missing)
    assert not missing.exists()


def test_a_missing_table_reads_unavailable_not_zero(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    raw = sqlite3.connect(db)
    with raw:
        raw.execute("DROP TABLE jobs")
    raw.close()
    assert "no such table: jobs" in report.report(db)["job_notices"]["unavailable"]


def test_the_cli_prints_the_audit_of_the_named_store(tmp_path):
    db = tmp_path / "mb.db"
    mailbox.send("a", "b", "c", db_path=db)
    run = subprocess.run(
        [sys.executable, "-m", "hardline_mcp.cli", "report", "--db", str(db)],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "HARDLINE_DB": str(tmp_path / "elsewhere.db")},
    )
    assert run.returncode == 0, run.stderr
    assert '"db_path": ' in run.stdout and "mb.db" in run.stdout
    assert not (tmp_path / "elsewhere.db").exists()
