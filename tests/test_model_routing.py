"""A model name sent to the wrong tool says which tool serves it.

A Codex session told to ask Fable called ``ask_codex(model="fable")``, got
Codex's rejection, and concluded Fable was unavailable: nothing it read said
that Fable is a Claude model. ``model_hint`` says so from the name alone, and
``list_models`` lists what each tool takes.
"""

from concurrent.futures import Future
import json

import pytest

from hardline_mcp import adapters, server


def _immediate_submit(fn, *args, **kwargs):
    future = Future()
    future.set_result(fn(*args, **kwargs))
    return future


def _catalog(*entries):
    """`codex debug models` output; an entry is a slug or a full model dict."""
    models = [
        {"slug": e, "visibility": "list", "upgrade": None} if isinstance(e, str) else e
        for e in entries
    ]
    return {"ok": True, "reply": json.dumps({"models": models})}


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("HARDLINE_DB", str(tmp_path / "mb.db"))
    monkeypatch.setattr(server._async_executor, "submit", _immediate_submit)


@pytest.mark.parametrize("name", ["fable", "Opus", "sonnet[1m]", "claude-fable-5-1"])
def test_a_claude_name_sent_to_codex_points_at_ask_claude(name):
    hint = adapters.model_hint("codex", name)
    assert hint is not None
    assert "ask_claude" in hint and f"model={name!r}" in hint
    assert adapters.model_hint("claude", name) is None


@pytest.mark.parametrize("name", ["astra", "gpt-6-astra", "gpt-5.6-sol"])
def test_a_codex_name_sent_to_claude_points_at_ask_codex(name):
    hint = adapters.model_hint("claude", name)
    assert hint is not None
    assert "ask_codex" in hint and f"model={name!r}" in hint
    assert adapters.model_hint("codex", name) is None


@pytest.mark.parametrize(
    "agent, name",
    [
        ("claude", "default"),  # Claude Code's reset, letters-only like a family
        ("codex", "default"),  # ...and not a Claude model name either
        ("claude", None),
        ("codex", None),
        # A provider's ID that is neither Claude-known nor Codex-shaped: no
        # guess either way.
        ("claude", "us.anthropic.claude-sonnet-5-5-v1:0"),
        ("codex", "o-series.custom"),
    ],
)
def test_no_hint_where_the_name_gives_no_evidence(agent, name):
    assert adapters.model_hint(agent, name) is None


# (tool, a name meant for the other agent, the tool the hint names)
WRONG_TOOL = [("codex", "fable", "ask_claude"), ("claude", "astra", "ask_codex")]
ANSWERS = [
    {"ok": True, "reply": "served anyway"},  # a custom provider took the name
    {"ok": False, "error": "model not supported"},
]


def _answer(monkeypatch, agent, answer):
    monkeypatch.setattr(server.adapters, f"ask_{agent}", lambda prompt, **k: dict(answer))


@pytest.mark.anyio
@pytest.mark.parametrize("answer", ANSWERS, ids=["answered", "rejected"])
@pytest.mark.parametrize("agent, name, points_at", WRONG_TOOL)
async def test_a_sync_call_carries_the_hint_whatever_the_outcome(
    monkeypatch, agent, name, points_at, answer
):
    _answer(monkeypatch, agent, answer)
    out = await getattr(server, f"ask_{agent}")(prompt="help", model=name)
    assert {k: out[k] for k in answer} == answer  # the answer, untouched
    assert points_at in out.get("model_hint", "")


@pytest.mark.anyio
@pytest.mark.parametrize("agent, name, points_at", WRONG_TOOL)
async def test_an_async_job_carries_the_hint_wherever_it_is_read(
    monkeypatch, store, agent, name, points_at
):
    monkeypatch.setattr(server.adapters, "_run_cmd", lambda argv, **k: _catalog("gpt-6-sol"))
    _answer(monkeypatch, agent, ANSWERS[0])
    receipt = await getattr(server, f"ask_{agent}_async")(
        prompt="help", from_agent="hermes", model=name
    )
    hint = receipt.get("model_hint", "")
    assert receipt["accepted"] is True and points_at in hint

    status = await server.job_status(receipt["job_id"])
    assert status["job"]["request"].get("model_hint") == hint
    # From the request, so it holds for a job with no result of its own too.
    assert (await server.job_result(receipt["job_id"])).get("model_hint") == hint


@pytest.mark.anyio
@pytest.mark.parametrize("agent, name, points_at", WRONG_TOOL)
async def test_a_refusal_carries_the_hint(monkeypatch, store, agent, name, points_at):
    refused = await getattr(server, f"ask_{agent}_async")(
        prompt="   ", from_agent="hermes", model=name
    )
    assert refused["accepted"] is False and points_at in refused.get("model_hint", "")

    monkeypatch.setattr(
        server,
        "_plan_claude_dispatch",
        lambda **k: {"ok": False, "error": "reserve reached", "routing": {}},
    )
    for call in (server.ask_claude(prompt="help", model="astra"),
                 server.ask_claude_async(prompt="help", from_agent="codex", model="astra")):
        out = await call
        assert out["error"] == "reserve reached"
        assert "ask_codex" in out.get("model_hint", "")


@pytest.mark.anyio
async def test_the_hint_reads_the_callers_spelling_not_the_expansion(monkeypatch, store):
    # A custom Codex catalog with an "opus" family expands the name; the caller
    # still wrote a Claude alias, and is told so.
    monkeypatch.setattr(server.adapters, "_run_cmd", lambda argv, **k: _catalog("gpt-6-opus"))
    _answer(monkeypatch, "codex", ANSWERS[0])
    receipt = await server.ask_codex_async(prompt="help", from_agent="claude", model="opus")
    assert receipt["model_resolution"]["resolved"] == "gpt-6-opus"
    assert "ask_claude" in receipt.get("model_hint", "")
    job = (await server.job_status(receipt["job_id"]))["job"]
    assert job["request"].get("model_hint") == receipt["model_hint"]


@pytest.mark.anyio
async def test_list_models_names_each_tool_and_what_it_takes(monkeypatch):
    catalog = _catalog(
        "gpt-6-astra",
        "gpt-7-astra-mini",  # a variant: listed, but never stands in for its family
        "gpt-6-sol",
        "gpt-5.6-sol",
        "gpt-6-terra",
        "oss-6-terra",  # one family under two prefixes: no single newest
        {"slug": "gpt-6-luna", "visibility": "hide", "upgrade": None},
        {"slug": "gpt-5.5-luna", "visibility": "list", "upgrade": {"model": "gpt-6-luna"}},
    )
    monkeypatch.setattr(server.adapters, "_run_cmd", lambda argv, **k: catalog)

    got = await server.list_models()

    assert got["ok"] is True
    assert got["claude"]["tool"] == "ask_claude"
    assert "fable" in got["claude"]["aliases"]
    assert got["codex"] == {
        "tool": "ask_codex",
        "families": {"astra": "gpt-6-astra", "sol": "gpt-6-sol", "terra": None},
        "models": [
            "gpt-5.6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-terra",
            "gpt-7-astra-mini", "oss-6-terra",
        ],
        "source": "codex debug models",
    }


@pytest.mark.anyio
async def test_a_family_name_listed_as_a_model_runs_as_itself(monkeypatch):
    # Dispatch runs "nova" as is when the catalog lists it (a custom provider),
    # so the list must not advertise gpt-6-nova for it.
    monkeypatch.setattr(server.adapters, "_run_cmd", lambda argv, **k: _catalog("nova", "gpt-6-nova"))
    assert (await server.list_models())["codex"]["families"] == {"nova": "nova"}
    assert server.adapters.resolve_codex_model("nova") == ("nova", None)


@pytest.mark.anyio
async def test_a_failed_codex_lookup_is_codexs_error_alone(monkeypatch):
    monkeypatch.setattr(
        server.adapters, "_run_cmd", lambda argv, **k: {"ok": False, "error": "not signed in"}
    )
    got = await server.list_models()
    assert "not signed in" in got["codex"]["error"]
    assert got["claude"]["aliases"] == list(adapters.CLAUDE_MODEL_ALIASES)

    def unresolvable(*args, **kwargs):
        raise PermissionError("bin root denied")

    monkeypatch.setattr(server.adapters, "codex_models", unresolvable)
    got = await server.list_models()
    assert got["codex"] == {"tool": "ask_codex", "error": "PermissionError: bin root denied"}
    assert got["ok"] is True


@pytest.mark.anyio
async def test_list_models_reads_the_catalog_its_options_name(monkeypatch, tmp_path):
    user_home = tmp_path / "user-codex-home"
    user_home.mkdir()
    (user_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    asked = []

    def run(argv, **kwargs):
        asked.append((kwargs.get("cwd"), (kwargs.get("env") or {}).get("CODEX_HOME")))
        return _catalog("gpt-6-sol")

    monkeypatch.setattr(server.adapters, "_run_cmd", run)
    await server.list_models(workdir=str(tmp_path))
    assert asked == [(str(tmp_path.resolve()), str(user_home))]

    await server.list_models(mode="advisory")  # an isolated home, as advisory runs in
    cwd, home = asked[-1]
    assert home != str(user_home) and cwd != str(tmp_path.resolve())

    # A request ask_codex would refuse is Codex's error, beside the Claude half.
    missing = await server.list_models(workdir=str(tmp_path / "absent"))
    assert missing["ok"] is True and "error" in missing["codex"]
    assert missing["claude"]["aliases"] and len(asked) == 2
