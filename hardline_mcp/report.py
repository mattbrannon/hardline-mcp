"""A read-only audit of one store: which code still runs against it, and who
consumed each job's result.

Two questions the docs leave to evidence (docs/architecture.md, "Stable
instance addresses"): has "recorded provenance shows misdelivery" fired, and
does any process predating ``agent_sessions`` remain? Neither gets a verdict
here. The report shows what the records establish and names what they cannot:
a process that never registers is invisible, and a result consumed by another
process is as likely a relaunch, a move to the background or an inherited role
as a misdelivery.

Read-only: opened ``mode=ro`` in one read transaction, never through the
initializing ``mailbox._connect``; nothing is pruned or created. A section
whose table or column is missing reads ``unavailable``, never zero.

Usage: hardline-mcp report [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .mailbox import _iso, _resolve_db
from .procid import DEAD, instance_state
from .sessions import host_state

SAMPLE = 20  # other-instance notices shown, newest first; the count is whole

NOTES = [
    "revision 'unrecorded': code older than revision recording, or no identity token",
    "a process that never registers is invisible: no live legacy process is not proof none remain",
    "other_instance: consumed by another process (a relaunch, a /bg move, an inherited role, "
    "or a misdelivery), not a verdict; notices are matched by job id, sender and recipient, "
    "which a hand-sent copy can imitate",
]


def report(db_path: Optional[Path] = None) -> dict:
    """The audit of the store at ``db_path`` (resolved as the servers resolve it)."""
    db = _resolve_db(db_path).resolve()
    db.stat()  # a missing store is an error, never an empty report
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # one snapshot for every section
        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "db_path": str(db),
            "processes": _section(_processes, conn),
            "legacy_sessions": _section(_legacy_sessions, conn),
            "job_notices": _section(_job_notices, conn),
            "notes": NOTES,
        }


def _section(read: Callable[[sqlite3.Connection], dict], conn: sqlite3.Connection) -> dict:
    try:
        return read(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc) and "no such column" not in str(exc):
            raise
        return {"unavailable": str(exc)}


def _optional(conn: sqlite3.Connection, table: str, sql: str) -> Optional[list]:
    """Rows of a table current code creates on use; None where it never was."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone():
        return None
    return conn.execute(sql).fetchall()


def _processes(conn: sqlite3.Connection) -> dict:
    """Registered server processes, one entry per identity, probed once each."""
    by_identity: dict[tuple, list] = {}
    for row in conn.execute("SELECT * FROM agent_sessions ORDER BY pid, seq"):
        by_identity.setdefault((row["pid"], row["process_key"]), []).append(row)
    revisions = {
        (pid, key): revision
        for pid, key, revision in _optional(
            conn, "process_revisions", "SELECT pid, process_key, revision FROM process_revisions"
        )
        or ()
    }
    registered = []
    for (pid, key), rows in by_identity.items():
        # One probe per distinct launching host; a row of older code has none.
        hosts = {tuple(r[k] if k in r.keys() else None for k in ("host_pid", "host_key")): r for r in rows}
        registered.append(
            {
                "pid": pid,
                "process_key": key,
                "state": instance_state(pid, key),
                "host_states": sorted({host_state(r) for r in hosts.values()}),
                "revision": revisions.get((pid, key)),
                "lanes": [r["lane"] for r in rows],
            }
        )
    running = [p for p in registered if p["state"] != DEAD]
    return {
        "revisions": dict(Counter(p["revision"] or "unrecorded" for p in running)),
        "states": dict(Counter(p["state"] for p in registered)),
        "registered": registered,
    }


def _legacy_sessions(conn: sqlite3.Connection) -> dict:
    """The pre-``agent_sessions`` registry, which only older revisions write."""
    rows = _optional(conn, "sessions", "SELECT DISTINCT pid, process_key FROM sessions")
    if rows is None:
        return {"table": False}
    return {
        "table": True,
        "processes": dict(Counter(instance_state(pid, key) for pid, key in rows)),
    }


def _job_notices(conn: sqlite3.Connection) -> dict:
    """Acked job notices: consumed by the process that dispatched the job, by
    another, or by one nothing recorded (an older revision, or no token)."""
    jobs = {
        r["job_id"]: r
        for r in conn.execute("SELECT job_id, agent, requester, owner_pid, owner_key FROM jobs")
    }
    acks = _optional(
        conn, "message_acks", "SELECT message_id, pid, process_key, conversation FROM message_acks"
    )
    by_message = {a["message_id"]: a for a in acks or ()}
    counts: Counter = Counter(same_instance=0, other_instance=0, unknown=0)
    sample = []
    notices = conn.execute(
        "SELECT id, sender, recipient, body, created_at, acked_at FROM messages"
        " WHERE acked_at IS NOT NULL AND body LIKE '{\"type\": \"job_finished\"%' ORDER BY id DESC"
    )
    for notice in notices:
        try:
            job = jobs.get(json.loads(notice["body"]).get("job_id"))
        except (ValueError, AttributeError):
            continue
        if job is None or (notice["sender"], notice["recipient"]) != (job["agent"], job["requester"]):
            continue
        ack = by_message.get(notice["id"])
        if ack is None or ack["process_key"] is None or job["owner_key"] is None:
            counts["unknown"] += 1
        elif (ack["pid"], ack["process_key"]) == (job["owner_pid"], job["owner_key"]):
            counts["same_instance"] += 1
        else:
            counts["other_instance"] += 1
            if len(sample) < SAMPLE:
                sample.append(
                    {
                        "message_id": notice["id"],
                        "job_id": job["job_id"],
                        "recipient": notice["recipient"],
                        "finished_at": notice["created_at"],
                        "acked_at": notice["acked_at"],
                        "dispatched_by": {"pid": job["owner_pid"], "process_key": job["owner_key"]},
                        "acked_by": dict(ack),
                    }
                )
    unrecorded = None
    if acks is not None:
        unrecorded = conn.execute(
            "SELECT COUNT(*) FROM messages m WHERE acked_at IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM message_acks a WHERE a.message_id = m.id)"
        ).fetchone()[0]
    return {
        **counts,
        "other_instance_sample": sample,
        "acks_recorded": acks is not None,
        # Every kind of message, so it overlaps "unknown" above; not additive.
        "acked_messages_without_record": unrecorded,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hardline-mcp report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, help="store to audit (default: as the servers resolve it)")
    args = parser.parse_args(argv)
    try:
        audit = report(args.db)
    except FileNotFoundError as exc:
        print(f"hardline-mcp report: no store at {exc.filename}", file=sys.stderr)
        return 1
    print(json.dumps(audit, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
