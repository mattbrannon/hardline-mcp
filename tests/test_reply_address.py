"""A message's sender is where its reply goes: a session sending as its own
bare agent name is recorded as its lane, and sending to a bare name says it is
shared. Observed 2026-10-16: a Codex session sent as bare ``codex``, its Claude
peer replied to ``codex``, and once that session went idle six replies sat in
the shared mailbox, which no session is woken for."""

import pytest

from hardline_mcp import mailbox, server, sessions


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def codex_me(monkeypatch, tmp_path):
    """A Codex session whose lane is codex:me."""
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "me")
    server._announce_self("codex")
    return db


def _stored_sender(db, result) -> str:
    return mailbox.peek(result["message_id"], db_path=db)["sender"]


@pytest.mark.anyio
async def test_a_bare_sender_naming_this_session_is_sent_as_its_lane(codex_me):
    got = await server.send(from_agent="codex", to_agent="claude:peer", message="hi")
    assert got.get("ok") is True
    assert _stored_sender(codex_me, got) == "codex:me"
    assert got.get("from_agent") == "codex:me" and "sender_note" in got


@pytest.mark.anyio
@pytest.mark.parametrize("sender", ["claude", "codex:other", "hermes"])
async def test_any_other_sender_is_kept_as_given(codex_me, sender):
    got = await server.send(from_agent=sender, to_agent="claude:peer", message="hi")
    assert _stored_sender(codex_me, got) == sender
    assert "from_agent" not in got and "sender_note" not in got


@pytest.mark.anyio
async def test_a_session_without_a_lane_keeps_its_bare_name(monkeypatch, tmp_path):
    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    got = await server.send(from_agent="codex", to_agent="claude:peer", message="hi")
    assert _stored_sender(db, got) == "codex"


@pytest.mark.anyio
async def test_a_lane_this_process_is_refused_is_not_given_as_the_reply_address(
    monkeypatch, tmp_path
):
    """Another live process holds codex:me, so this one's derived lane is not
    granted: a reply there would reach that process, so the sender stays bare."""
    import subprocess
    import sys

    db = tmp_path / "mb.db"
    monkeypatch.setenv("HARDLINE_DB", str(db))
    monkeypatch.setenv("HARDLINE_AGENT", "codex")
    monkeypatch.setenv("HARDLINE_AGENT_LABEL", "me")
    holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        sessions.register(agent="codex", lane="codex:me", pid=holder.pid, db_path=db)
        server._announce_self("codex")  # refused: contested
        got = await server.send(from_agent="codex", to_agent="claude:peer", message="hi")
    finally:
        holder.kill()
        holder.wait()
    assert _stored_sender(db, got) == "codex"
    assert "holds no lane" in got.get("sender_note", ""), got


@pytest.mark.anyio
async def test_the_delivery_notice_names_the_stored_sender(codex_me, monkeypatch):
    notices = []
    monkeypatch.setattr(server.adapters, "deliver", lambda agent, text: notices.append(text) or {"ok": True})
    await server.send(from_agent="codex", to_agent="claude:peer", message="hi", deliver=True)
    assert notices and "from codex:me." in notices[0], notices


@pytest.mark.anyio
async def test_a_send_to_a_bare_name_says_it_is_shared_and_who_to_address(codex_me):
    got = await server.send(from_agent="claude:peer", to_agent="codex", message="hi")
    assert got.get("ok") is True
    warning = got.get("warning", "")
    assert "shared mailbox" in warning and "codex:me" in warning, got


@pytest.mark.anyio
async def test_a_bare_name_nobody_has_a_lane_under_is_not_warned_about(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    got = await server.send(from_agent="claude:peer", to_agent="hermes", message="hi")
    assert "warning" not in got
    assert not sessions.live(agent="hermes", db_path=tmp_path / "mb.db")
