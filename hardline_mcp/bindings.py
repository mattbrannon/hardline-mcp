"""Which Codex thread each claimed name belongs to (``thread_names``).

A claimed name lives in its process, and a Codex session's process changes on
every restart or reconnect. Its thread does not: Codex hands every
``tools/call`` the thread id, and a resumed thread keeps it while a fork gets a
new one. So a claim made from a request on the wake's pinned top-level thread
is recorded against that thread, and the thread's next server claims the name
back through the ordinary ownership rule (``sessions.restore``). See
docs/session-continuity.md, "A Codex thread's names".

One row per name: the last *attributed* grant of it. Only an attributed
action writes here - an explicit claim (bind or unbind), a restoration, a
release, a dismissal. An automatic grant (startup, heartbeat, repair) never
does, so a name another process merely re-acquired keeps its thread's claim.

The SQL here runs in the caller's transaction; ``unbind`` and ``unheld`` open
their own. Every lane is a qualified recipient (``codex:name``), never a label.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional

from .mailbox import THREAD_NAMES_INDEX, THREAD_NAMES_TABLE, _connect, _resolve_db


def ensure(conn: sqlite3.Connection) -> None:
    """The table and its index, inside the caller's transaction."""
    conn.execute(THREAD_NAMES_TABLE)
    conn.execute(THREAD_NAMES_INDEX)


def _absent(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def bind(conn, *, lane: str, thread: str, pid: int, key: str, stamp: str) -> None:
    """``lane`` now belongs to ``thread``, held by (pid, key); placed by ``renumber``."""
    ensure(conn)
    conn.execute(
        "INSERT OR REPLACE INTO thread_names (lane, thread, seq, pid, process_key, bound_at)"
        " VALUES (?, ?, 0, ?, ?, ?)",
        (lane, thread, pid, key, stamp),
    )


def unbind_lane(conn, lane: str) -> None:
    """An explicit claim that cannot be attributed: the name no longer belongs
    to whichever thread last claimed it."""
    ensure(conn)
    conn.execute("DELETE FROM thread_names WHERE lane = ?", (lane,))


def withdraw(conn, *, lane: str, pid: int, key: Optional[str], thread: Optional[str]) -> int:
    """A release of ``lane``: drop the binding this process wrote, or the one
    for its pinned ``thread`` whoever wrote it, so a released name never comes
    back. A binding another thread holds through another writer stays."""
    ensure(conn)
    return conn.execute(
        "DELETE FROM thread_names WHERE lane = ?"
        " AND ((pid = ? AND process_key IS ?) OR (? IS NOT NULL AND thread = ?))",
        (lane, pid, key, thread, thread),
    ).rowcount


def renumber(conn, *, thread: str, pid: int, key: Optional[str], order: Iterable[str]) -> None:
    """Rewrite this process's bindings of ``thread`` to its local ``order``
    (every held recipient, oldest first), above any other writer's, so the
    thread's next server restores them in the order this one answers to them.
    Lanes without an own binding match nothing."""
    base = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM thread_names"
        " WHERE thread = ? AND NOT (pid = ? AND process_key IS ?)",
        (thread, pid, key),
    ).fetchone()[0]
    conn.executemany(
        "UPDATE thread_names SET seq = ?"
        " WHERE lane = ? AND thread = ? AND pid = ? AND process_key IS ?",
        [(base + i, lane, thread, pid, key) for i, lane in enumerate(dict.fromkeys(order), 1)],
    )


def own_order(conn, *, thread: str, pid: int, key: Optional[str]) -> list[tuple[str, int]]:
    """``(lane, seq)`` of this process's bindings of ``thread``, by seq."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT lane, seq FROM thread_names WHERE thread = ? AND pid = ? AND process_key IS ?"
            " ORDER BY seq",
            (thread, pid, key),
        )
    ]


def page(
    conn, *, thread: str, prefix: str, before: Optional[int], limit: int
) -> Optional[list[dict]]:
    """One page of ``thread``'s bindings under ``prefix`` (``codex:``), newest
    seq first, below ``before``; None when the table does not exist (a store
    an older revision built), which is not the same as no bindings."""
    try:
        rows = conn.execute(
            "SELECT lane, thread, seq, pid, process_key FROM thread_names"
            " WHERE thread = ? AND substr(lane, 1, ?) = ? AND (? IS NULL OR seq < ?)"
            " ORDER BY seq DESC LIMIT ?",
            (thread, len(prefix), prefix, before, before, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _absent(exc):
            raise
        return None
    keys = ("lane", "thread", "seq", "pid", "process_key")
    return [dict(zip(keys, r)) for r in rows]


def unbind(lane: str, thread: str, *, db_path: Optional[Path] = None) -> bool:
    """Dismiss ``lane`` for ``thread``: it is not brought back. True if a row went."""
    with closing(_connect(_resolve_db(db_path))) as conn:
        with conn:
            ensure(conn)
            return conn.execute(
                "DELETE FROM thread_names WHERE lane = ? AND thread = ?", (lane, thread)
            ).rowcount > 0


def unheld(
    thread: str, prefix: str, held: Iterable[str], *, db_path: Optional[Path] = None
) -> list[dict]:
    """``thread``'s bindings under ``prefix`` that are not in ``held``, newest
    first: what async admission refuses on. One indexed read, as of now; another
    process can bind after it, which admission accepts as its boundary."""
    held = set(held)
    with closing(_connect(_resolve_db(db_path))) as conn:
        try:
            rows = conn.execute(
                "SELECT lane, pid, process_key FROM thread_names"
                " WHERE thread = ? AND substr(lane, 1, ?) = ? ORDER BY seq DESC",
                (thread, len(prefix), prefix),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if not _absent(exc):
                raise
            return []
    return [
        {"lane": lane, "pid": pid, "process_key": key}
        for lane, pid, key in rows
        if lane not in held
    ]
