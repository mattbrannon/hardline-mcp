"""Fixed-window, pre-dispatch notice decisions against an isolated mailbox."""

import anyio
import pytest

from hardline_mcp import adapters, announce, channel, mailbox


LANE = "codex:batch"
OLD = "codex:before"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Wake(announce.Announcer):
    client = "test-client"
    transport = "test-transport"

    def __init__(self):
        self.elapsed = 0.0
        super().__init__(address="address", poll_s=2.0, monotonic=lambda: self.elapsed)
        self.calls = []
        self.on_send = lambda nonce: None

    def notice(self, nonce):
        return nonce

    def send(self, address, text):
        self.calls.append((address, text, self.elapsed))
        self.on_send(text)


@pytest.fixture
def wake(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mail.db"))
    monkeypatch.setattr(adapters, "owned_recipients", lambda: (LANE,))
    monkeypatch.setattr(announce.sessions, "granted", lambda owned: tuple(owned))
    monkeypatch.setattr(channel, "held_before", lambda: [])
    result = Wake()
    monkeypatch.setattr(result, "_flush", lambda: None)
    return result


def mail(lane=LANE):
    return mailbox.send("claude:sender", lane, "data")["message_id"]


def ack(message_id, lane=LANE):
    assert mailbox.ack(message_id, owned=[lane])["ok"]


class EndLoop(BaseException):
    pass


async def run_steps(wake, monkeypatch, steps):
    """Replace elapsed sleeps by ordered actions, without scheduling races."""
    sleeps = []
    actions = iter(steps)

    async def sleep(seconds):
        sleeps.append((seconds, len(wake.calls)))
        wake.elapsed += seconds
        action = next(actions, None)
        if action is None:
            raise EndLoop
        action()

    monkeypatch.setattr(announce.anyio, "sleep", sleep)
    ready = anyio.Event()
    ready.set()
    with pytest.raises(EndLoop):
        await wake.run(ready, {"name": wake.client})
    return sleeps


@pytest.mark.anyio
async def test_ack_between_reservation_and_dispatch_cancels_unsent_notice(wake, monkeypatch):
    first = mail()
    sleeps = await run_steps(wake, monkeypatch, [lambda: ack(first)])
    assert sleeps == [(2.0, 0), (2.0, 0)]
    assert wake.calls == []
    assert wake.outstanding is None and wake.announced == set()
    assert wake.facts["last_push_at"] is None
    assert wake.facts["oldest_unreceipted_at"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["grant", "owned", "stop"])
async def test_initial_dispatch_rechecks_ownership_and_stop(wake, monkeypatch, loss):
    mail()

    def lose():
        if loss == "grant":
            monkeypatch.setattr(announce.sessions, "granted", lambda owned: ())
        elif loss == "owned":
            monkeypatch.setattr(adapters, "owned_recipients", lambda: ())
        else:
            wake.stopped = True

    await run_steps(wake, monkeypatch, [lose])
    assert wake.calls == [] and wake.outstanding is None


@pytest.mark.anyio
async def test_burst_is_one_fixed_window_and_one_receipt(wake, monkeypatch):
    first = mail()
    added = []
    announced_at_send = []

    def receipt(nonce):
        announced_at_send.append(set(wake.announced))
        assert wake.accept_receipt(nonce)

    wake.on_send = receipt
    sleeps = await run_steps(
        wake, monkeypatch, [lambda: added.append(mail()), lambda: None, lambda: None]
    )
    assert sleeps[:2] == [(2.0, 0), (2.0, 1)], "send after exactly one batch window"
    assert len(wake.calls) == 1
    assert announced_at_send == [{first, *added}], "absorb arrivals before dispatch, not next poll"
    assert wake.announced == {first, *added}, "the receipt covers window arrivals too"


@pytest.mark.anyio
async def test_continuous_arrivals_cannot_extend_the_window(wake, monkeypatch):
    mail()
    sleeps = await run_steps(wake, monkeypatch, [mail, mail, mail, mail])
    assert len(wake.calls) == 1 and wake.calls[0][2] == 2.0
    assert sleeps[1][1] == 1, "new arrivals never slide the dispatch deadline"
    assert wake.outstanding is not None, "unread mail still wakes once and awaits its receipt"


@pytest.mark.anyio
async def test_mail_arriving_during_cancellation_gets_its_own_window(wake, monkeypatch):
    first = mail()
    added = []
    take_back = wake.take_back

    def cancel(nonce, ids):
        added.append(mail())
        take_back(nonce, ids)

    monkeypatch.setattr(wake, "take_back", cancel)
    await run_steps(wake, monkeypatch, [lambda: ack(first), lambda: None, lambda: None])
    assert len(wake.calls) == 1
    assert wake.calls[0][2] == 6.0
    assert wake.announced == set(added)


@pytest.mark.anyio
async def test_ambiguous_retry_keeps_nonce_after_ack_without_another_batch(wake, monkeypatch):
    first = mail()
    monkeypatch.setattr(announce, "RETRY_S", 2.0)

    def fail_once(nonce):
        if len(wake.calls) == 1:
            raise OSError("may have delivered")

    wake.on_send = fail_once
    sleeps = await run_steps(wake, monkeypatch, [lambda: None, lambda: ack(first)])
    assert len(wake.calls) == 2
    assert wake.calls[0][1] == wake.calls[1][1]
    assert sleeps == [(2.0, 0), (2.0, 1), (2.0, 2)], "retry has no initial batch wait"
    assert wake.outstanding is not None, "ack cannot release a possibly delivered notice"


@pytest.mark.anyio
async def test_not_sent_restores_every_id_absorbed_in_the_window(wake, monkeypatch):
    first = mail()
    added = []

    def not_sent(nonce):
        raise announce.NotSent("nothing delivered")

    wake.on_send = not_sent
    await run_steps(wake, monkeypatch, [lambda: added.append(mail())])
    assert len(wake.calls) == 1 and added
    assert wake.announced.isdisjoint({first, *added}) and wake.outstanding is None


@pytest.mark.anyio
@pytest.mark.parametrize("consume", [False, True])
async def test_held_before_hints_survive_or_cancel_with_their_unread_mail(wake, monkeypatch, consume):
    first = mail(OLD)
    monkeypatch.setattr(channel, "held_before", lambda: [OLD])
    await run_steps(wake, monkeypatch, [lambda: ack(first, OLD) if consume else None])
    assert len(wake.calls) == (0 if consume else 1)
    assert wake.pointed == ({} if consume else {OLD: first})


@pytest.mark.anyio
async def test_hint_revalidation_failure_is_unsent_and_retried(wake, monkeypatch):
    mail(OLD)
    monkeypatch.setattr(channel, "held_before", lambda: [OLD])
    waiting = channel.waiting

    def unavailable():
        def fail(recipients):
            raise channel.Unavailable("temporary failure")

        monkeypatch.setattr(channel, "waiting", fail)

    def restore():
        monkeypatch.setattr(channel, "waiting", waiting)

    sleeps = await run_steps(wake, monkeypatch, [unavailable, restore, lambda: None])
    assert len(wake.calls) == 1, "an unreadable advisory hint must not be lost"
    assert sleeps[1] == (4.0, 0), "unavailable is a failed preflight, not an empty hint"


@pytest.mark.anyio
async def test_reserved_id_beyond_front_page_still_dispatches(wake, monkeypatch):
    mail()
    monkeypatch.setattr(channel, "_SCAN", 1)
    monkeypatch.setattr(channel, "_PAGES", 1)
    wake._cursor = 1
    newest = mail()
    await run_steps(wake, monkeypatch, [lambda: None])
    assert len(wake.calls) == 1 and newest in wake.announced
    assert wake._cursor == newest, "preflight must not consume the rotating poll cursor"


@pytest.mark.anyio
@pytest.mark.parametrize("loss", ["grant", "stop"])
async def test_changes_during_the_final_mail_read_cancel_initial_dispatch(wake, monkeypatch, loss):
    mail()
    read = channel._read

    def change_during_read(sql, params):
        rows = read(sql, params)
        if "WHERE id IN" in sql:
            if loss == "grant":
                monkeypatch.setattr(announce.sessions, "granted", lambda owned: ())
            else:
                wake.stopped = True
        return rows

    monkeypatch.setattr(channel, "_read", change_during_read)
    await run_steps(wake, monkeypatch, [lambda: None])
    assert wake.calls == [] and wake.outstanding is None


@pytest.mark.anyio
async def test_mail_revalidation_failure_rolls_back_then_retries(wake, monkeypatch):
    first = mail()
    read = channel._read

    def unavailable():
        def fail(sql, params):
            raise channel.Unavailable("temporary failure")

        monkeypatch.setattr(channel, "_read", fail)

    def restore():
        assert wake.outstanding is None and first not in wake.announced
        monkeypatch.setattr(channel, "_read", read)

    await run_steps(wake, monkeypatch, [unavailable, restore, lambda: None])
    assert len(wake.calls) == 1 and first in wake.announced
