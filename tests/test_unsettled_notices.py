"""A notice whose send may not have delivered is sent again, the same notice,
until a receipt settles it (announce.py, RETRY_S).

Seen 2026-10-16: a Codex wake's steer was refused and its ``codex queue`` then
failed, inserting nothing. The notice stayed outstanding with no receipt ever
coming, and that session was never woken again: five messages unannounced.
"""

import threading

import anyio
import pytest

from hardline_mcp import announce, claude_inbox, delivery, mailbox, server
from test_claude_inbox import INBOX, Posts, claude, store  # noqa: F401 - fixture
from test_claude_inbox import nonce_of as inbox_nonce
from test_channel import LANE
from test_codex_queue import Queue, _armed, call, codex, lane, nonce_of  # noqa: F401 - fixture


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _wake(monkeypatch):
    wake = _armed(monkeypatch, [{"id": 1, "recipient": "codex:a"}], ["codex:a"])
    wake.monotonic = Clock()
    return wake


# ── the schedule, directly ──────────────────────────────────────────────────


def test_an_unsettled_notice_is_due_again_with_its_own_receipt(monkeypatch):
    wake = _wake(monkeypatch)
    address, nonce, ids = wake.poll()
    assert ids == {1}
    wake.unsettled(nonce, "codex queue failed")
    wake.monotonic.t += announce.RETRY_S - 1
    assert wake.poll() is None, "not before its wait"
    wake.monotonic.t += 1
    assert wake.poll() == (address, nonce, None), "the same notice: its mail is already announced"
    assert wake.poll() is None, "one attempt at a time; its outcome reschedules"
    wake.monotonic.t += announce.MAX_RETRY_S
    assert wake.poll() is None, "an attempt that did not fail is not sent again"


def test_the_wait_doubles_to_its_cap(monkeypatch):
    wake = _wake(monkeypatch)
    _, nonce, _ = wake.poll()
    waits = []
    for _ in range(6):
        wake.unsettled(nonce, "x")
        waits.append(wake._retry_at - wake.monotonic.t)
    assert waits == [300, 600, 1200, 2400, 3600, 3600]


def test_a_receipt_settles_it_even_when_it_lands_before_the_failure(monkeypatch):
    """The send may deliver and be receipted before its own failure returns."""
    wake = _wake(monkeypatch)
    _, nonce, _ = wake.poll()
    assert wake.accept_receipt(nonce)
    wake.unsettled(nonce, "late failure")
    wake.monotonic.t += announce.MAX_RETRY_S
    assert wake.poll() is None and wake._retry_at is None


def test_a_receipt_after_a_resend_settles_it(monkeypatch):
    wake = _wake(monkeypatch)
    _, nonce, _ = wake.poll()
    wake.unsettled(nonce, "x")
    wake.monotonic.t += announce.RETRY_S
    assert wake.poll()[1] == nonce
    assert wake.accept_receipt(nonce), "either copy's receipt is the notice's"
    assert wake.outstanding is None and wake._failures == 0


def test_a_receipt_clears_a_pending_resend(monkeypatch):
    """Receipted before its resend was due: that deadline must not outlive it
    and resend the next notice, which completed."""
    wake = _wake(monkeypatch)
    _, first, _ = wake.poll()
    wake.unsettled(first, "x")
    assert wake.accept_receipt(first)
    rows = [{"id": 1, "recipient": "codex:a"}, {"id": 2, "recipient": "codex:a"}]
    monkeypatch.setattr(announce.channel, "unread", lambda owned, after=0: (rows, 0))
    _, second, ids = wake.poll()
    assert ids == {2} and second != first
    wake.monotonic.t += announce.MAX_RETRY_S
    assert wake.poll() is None, "a completed notice was resent on its predecessor's deadline"


def test_a_notice_for_lanes_held_before_is_resent_too(monkeypatch):
    """No new mail on a held lane, only newer mail at a name held before."""
    wake = _armed(monkeypatch, [], ["codex:a"])
    wake.monotonic = Clock()
    monkeypatch.setattr(announce.channel, "waiting_before", lambda: {"codex:old": (9, 1)})
    address, nonce, ids = wake.poll()
    assert ids == set()
    wake.unsettled(nonce, "x")
    wake.monotonic.t += announce.RETRY_S
    assert wake.poll() == (address, nonce, None)


def test_a_stopped_wake_sends_nothing_again(monkeypatch):
    wake = _wake(monkeypatch)
    _, nonce, _ = wake.poll()
    wake.unsettled(nonce, "x")
    wake.stopped = True  # a second top-level thread: no further notices
    wake.monotonic.t += announce.RETRY_S
    assert wake.poll() is None


def test_a_resend_leaves_the_push_owed_since_the_first(monkeypatch):
    wake = _wake(monkeypatch)
    _, nonce, _ = wake.poll()
    owed = wake.facts["oldest_unreceipted_at"]
    wake.unsettled(nonce, "x")
    wake.monotonic.t += announce.RETRY_S
    wake.poll()
    assert wake.facts["oldest_unreceipted_at"] == owed
    assert delivery.derive(wake.facts, owed + delivery.GRACE) == "unreceipted"


# ── through the serving loop ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_failed_codex_queue_is_queued_again_until_receipted(lane, monkeypatch):
    monkeypatch.setattr(announce, "RETRY_S", 0.05)
    queue = Queue(ok=False)
    async with anyio.create_task_group() as tg:
        wire, _ = await codex(tg, queue)
        await call(wire)
        mailbox.send("claude", lane, "one")
        _, first = await queue.wait(1)
        queue.ok = True
        _, second = await queue.wait(2)
        assert nonce_of(second) == nonce_of(first)
        await queue.none_beyond(2)  # a send that completed is not sent again
        got = await server.inbox(agent="codex", auto_ack=False, receipt=nonce_of(first))
        assert got["receipt"] == "accepted"
        mailbox.send("claude", lane, "two")
        _, third = await queue.wait(3)
        assert nonce_of(third) != nonce_of(first), "settled: the next notice is new"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_claude_write_that_failed_is_posted_again(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(announce, "RETRY_S", 0.05)
    posts = Posts(fail=OSError("broken pipe"))
    async with anyio.create_task_group() as tg:
        await claude(tg, monkeypatch, posts)
        mailbox.send("codex", LANE, "one")
        _, first = await posts.wait(1)
        posts.fail = None
        _, second = await posts.wait(2)
        assert inbox_nonce(second) == inbox_nonce(first)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_resend_that_reached_nothing_leaves_the_notice_unsettled(store, monkeypatch):  # noqa: F811
    """NotSent on a resend says nothing of the first send, which may have
    landed: it is tried again, not taken back."""
    monkeypatch.setattr(announce, "RETRY_S", 0.05)
    posts = Posts(fail=OSError("broken pipe"))
    async with anyio.create_task_group() as tg:
        await claude(tg, monkeypatch, posts)
        mailbox.send("codex", LANE, "one")
        _, first = await posts.wait(1)
        (wake,) = [w for w in announce._installed if isinstance(w, claude_inbox.ClaudeInboxWake)]
        owed = wake.facts["oldest_unreceipted_at"]
        posts.fail = announce.NotSent("pipe busy")
        await posts.wait(2)
        posts.fail = None
        _, third = await posts.wait(3, timeout=10.0)
        assert inbox_nonce(third) == inbox_nonce(first)
        assert wake.facts["oldest_unreceipted_at"] == owed, "the push is still owed since the first"
        assert delivery.derive(wake.facts, owed + delivery.GRACE) == "unreceipted"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_resend_chosen_before_its_receipt_goes_out_and_settles_nothing(store, monkeypatch):  # noqa: F811
    """poll picks the resend, the receipt lands, then the copy goes and fails:
    the one copy past settlement is allowed, and its failure revives nothing."""
    monkeypatch.setattr(announce, "RETRY_S", 0.05)
    sends = []

    def post(inbox, text):
        sends.append(text)
        if len(sends) == 2:  # the resend, already chosen: its receipt arrives first
            (wake,) = [w for w in announce._installed if isinstance(w, claude_inbox.ClaudeInboxWake)]
            assert wake.accept_receipt(inbox_nonce(text))
        raise OSError("broken pipe")

    async with anyio.create_task_group() as tg:
        await claude(tg, monkeypatch, post)
        mailbox.send("codex", LANE, "one")
        with anyio.fail_after(10):
            while len(sends) < 2:
                await anyio.sleep(0.01)
        await anyio.sleep(0.3)  # many polls, past any RETRY_S
        (wake,) = [w for w in announce._installed if isinstance(w, claude_inbox.ClaudeInboxWake)]
        assert len(sends) == 2, "the settled notice was sent again"
        assert wake.outstanding is None and wake._retry_at is None
        tg.cancel_scope.cancel()


def _timed_out_write(monkeypatch, result):
    """A wake whose write of "notice" timed out; set the returned event to let
    it finish with ``result`` (None for success), then join ``wake._late``."""
    release = threading.Event()

    def write(inbox, payload, outcome):
        release.wait(30)
        outcome.append(result)

    monkeypatch.setattr(claude_inbox, "_write", write)
    monkeypatch.setattr(claude_inbox, "SEND_TIMEOUT_S", 0.05)
    wake = claude_inbox.ClaudeInboxWake(address=INBOX)
    with pytest.raises(TimeoutError):
        wake.send(INBOX, "notice")
    posts = Posts()
    monkeypatch.setattr(claude_inbox, "post", posts)
    return wake, release, posts


def _finish(wake, release):
    writer = wake._late[1]
    release.set()
    writer.join(5)


def test_a_blocked_inbox_write_is_never_joined_by_a_second(monkeypatch):
    """post() abandons the wait, not the writer: retrying beside a write still
    holding its connection would leak one per retry."""
    wake, release, posts = _timed_out_write(monkeypatch, None)
    with pytest.raises(TimeoutError, match="still blocked"):
        wake.send(INBOX, "notice")
    assert posts.calls == [], "no second writer while the first lives"
    release.set()


def test_a_write_that_finished_late_is_not_written_again(monkeypatch):
    wake, release, posts = _timed_out_write(monkeypatch, None)
    _finish(wake, release)
    wake.send(INBOX, "notice")  # the resend: its first write delivered after all
    assert posts.calls == []
    wake.send(INBOX, "notice")
    assert len(posts.calls) == 1, "the late outcome answers one send, not every one"


def test_a_write_that_failed_late_is_written_again(monkeypatch):
    wake, release, posts = _timed_out_write(monkeypatch, OSError("broken pipe"))
    _finish(wake, release)
    wake.send(INBOX, "notice")
    assert len(posts.calls) == 1


def test_a_late_write_of_a_settled_notice_decides_nothing_for_the_next(monkeypatch):
    wake, release, posts = _timed_out_write(monkeypatch, None)
    _finish(wake, release)
    wake.send(INBOX, "another notice")
    assert len(posts.calls) == 1
