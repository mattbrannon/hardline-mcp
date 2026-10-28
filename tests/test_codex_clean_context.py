"""``context="clean"``: a Codex builder without the user's Codex home.

Reviewed before it was built (2026-10-28): the contract is narrow on purpose.
It removes the user's Codex home from what Codex loads and from what its
commands can read, takes instructions from a workdir that is a repository's
root, refuses where it cannot keep that word, and reports the rest rather
than promising it.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from hardline_mcp import adapters, server
from test_adapters import _codex_reply, _pin
from test_codex_agents_md import _refused
from test_github_forwarding import _recorder, async_store  # noqa: F401 - fixture


@pytest.fixture
def home(monkeypatch, tmp_path):
    """The user's Codex home: ChatGPT auth, and a global AGENTS.md and skill
    that a clean run must not take in."""
    codex_home = tmp_path / "user-codex"
    (codex_home / "skills" / "global").mkdir(parents=True)
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    (codex_home / "AGENTS.md").write_text("GLOBAL_SENTINEL", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


def _git(root: Path, name: str = ".git") -> None:
    (root / name).mkdir(parents=True)
    (root / name / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _link(target: Path, link: Path) -> None:
    """A directory link anyone can make: a junction on Windows, where a symlink
    needs privilege, and a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "frozen"
    _git(root)
    (root / "AGENTS.md").write_text("FROZEN_RULES", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
    return root


@pytest.fixture
def codex(monkeypatch, tmp_path, home):
    """Scripted runs under a profile sandbox; each run's argv, cwd and env kept."""
    monkeypatch.setattr(adapters, "_codex_docs_refused_by", {})
    monkeypatch.setattr(adapters, "_modified", lambda exe: 1)
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}, profile=True))
    root = tmp_path / "neutral-root"
    root.mkdir()
    monkeypatch.setattr(adapters.tempfile, "mkdtemp", lambda prefix: str(root))
    monkeypatch.setattr(adapters.shutil, "rmtree", lambda path, ignore_errors: None)
    runs, seen = [], []

    def run(agent, argv, **kw):
        home = Path(kw["env"]["CODEX_HOME"])
        seen.append({"argv": argv, "cwd": kw.get("cwd"), "env": kw["env"],
                     "home": sorted(p.name for p in home.iterdir())})
        return dict(runs.pop(0))

    monkeypatch.setattr(adapters, "_run_agent_cmd", run)
    return runs, seen, root


def _ask(repo, **kw):
    return adapters.ask_codex(
        "build it", workdir=str(repo), write=True, context="clean", resolve_model=False, **kw
    )


@pytest.fixture(autouse=True)
def writes_allowed(monkeypatch):
    monkeypatch.setenv("HARDLINE_ALLOW_WRITE", "1")


# -- where it refuses ------------------------------------------------------


def test_clean_needs_a_repository_root(codex, repo):
    nested = repo / "src"
    out = adapters.ask_codex("x", workdir=str(nested), context="clean", resolve_model=False)
    assert out["ok"] is False and "repository's root" in out["error"]
    assert codex[1] == [], "refused before anything ran"


def test_a_git_file_is_not_a_root_of_its_own(codex, tmp_path):
    """A linked worktree's .git is a file pointing at another repository."""
    worktree = tmp_path / "linked"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = adapters.ask_codex("x", workdir=str(worktree), context="clean", resolve_model=False)
    assert out["ok"] is False and "repository's root" in out["error"]


@pytest.mark.parametrize("shape", ["linked", "empty", "upper-case"])
def test_a_git_directory_must_be_a_repository_of_its_own(codex, tmp_path, shape):
    """A link to another repository's .git, an empty .git, and a .GIT that
    Windows would accept while the digest hashed it as working tree."""
    root = tmp_path / "root"
    root.mkdir()
    if shape == "linked":
        _git(tmp_path / "elsewhere")
        _link(tmp_path / "elsewhere" / ".git", root / ".git")
    elif shape == "empty":
        (root / ".git").mkdir()
    else:
        _git(root, ".GIT")
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = adapters.ask_codex("x", workdir=str(root), context="clean", resolve_model=False)
    assert out["ok"] is False and "repository's root" in out["error"]


@pytest.mark.parametrize(
    "options, says",
    [
        ({"mode": "advisory"}, "advisory"),
        ({}, "requires workdir"),
    ],
    ids=["advisory", "no-workdir"],
)
def test_clean_refuses_what_it_cannot_honour(codex, options, says):
    out = adapters.ask_codex("x", context="clean", resolve_model=False, **options)
    assert out["ok"] is False and says in out["error"]


def test_clean_needs_a_sandbox_that_denies_reads(codex, repo, monkeypatch):
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}))
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = _ask(repo)
    assert out["ok"] is False and "denies none" in out["error"]


def test_clean_cannot_run_inside_a_home_it_denies(codex, home):
    inside = home / "project"
    _git(inside)
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = _ask(inside)
    assert out["ok"] is False and "cannot run inside" in out["error"]


@pytest.mark.parametrize("name", [".codex", ".agents"])
def test_clean_cannot_run_inside_the_users_default_homes(codex, monkeypatch, tmp_path, name):
    """Under ~/.codex though CODEX_HOME points elsewhere, and under ~/.agents."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    inside = tmp_path / "user" / name / "project"
    _git(inside)
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = _ask(inside)
    assert out["ok"] is False and "cannot run inside" in out["error"]


def test_clean_cannot_run_around_a_home_it_denies(codex, home, tmp_path):
    """A workdir holding the user's Codex home would digest its auth.json."""
    _git(tmp_path)
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = _ask(tmp_path)
    assert out["ok"] is False and "cannot run around" in out["error"]


def test_clean_is_codex_only(repo):
    error, _ = adapters.validate_request(
        "claude", model=None, effort="default", mode="default", workdir=str(repo),
        write=False, context="clean",
    )
    assert error is not None and "Codex only" in error["error"]


def test_an_unknown_context_is_refused(codex, repo):
    out = adapters.ask_codex("x", workdir=str(repo), context="pristine", resolve_model=False)
    assert out["ok"] is False and "unsupported context" in out["error"]


# -- what it does ----------------------------------------------------------


def test_clean_runs_in_the_workdir_from_a_home_of_its_own(codex, repo, home):
    runs, seen, root = codex
    runs.append({"ok": True, "reply": _codex_reply("built")})
    out = _ask(repo)
    assert out["ok"] is True, out
    run = seen[0]
    assert run["cwd"] == str(repo)
    assert run["argv"][run["argv"].index("-C") + 1] == str(repo)
    assert "--ignore-user-config" in run["argv"] and "--ignore-rules" in run["argv"]
    assert 'project_root_markers=[".git"]' in run["argv"], "the root cannot move above workdir"
    assert run["env"]["CODEX_HOME"] == str(root / "codex-home") != str(home)
    assert run["home"] == ["auth.json"], "no global AGENTS.md, skills or config"
    assert not (root / "workspace").exists(), "advisory's neutral directory is not used"
    assert not any("developer_instructions" in a for a in run["argv"])
    assert _pin(run["argv"]) == "workspace-write"
    assert out["subscription_configured"] is True


def test_clean_denies_the_users_homes_whole(home, tmp_path):
    """The global AGENTS.md and skills a default call leaves readable are not."""
    own = tmp_path / "own-home"
    own.mkdir()
    (own / "auth.json").write_text("{}", encoding="utf-8")
    env = {"CODEX_HOME": str(own)}
    clean = adapters._codex_unreadable(env, clean=True)
    assert home in clean and Path.home() / ".codex" in clean
    assert Path.home() / ".agents" in clean
    assert own / "auth.json" in clean, "Codex reads it; its commands must not"
    assert own not in clean, "the child's own home keeps what its commands need"
    default = adapters._codex_unreadable(env)
    assert home not in default and Path.home() / ".agents" not in default


def test_the_pin_carries_the_whole_denial(codex, repo, home):
    runs, seen, _ = codex
    runs.append({"ok": True, "reply": _codex_reply("built")})
    _ask(repo)
    argv = seen[0]["argv"]
    table = next(a for a in argv if ".filesystem=" in a)
    assert json.dumps(str(home)) + '="deny"' in table


def test_the_sandbox_check_proves_the_whole_denial(codex, repo, monkeypatch):
    checked = []

    def started(exe, overrides, env, cwd, on_spawn=None, clean=False):
        checked.append(clean)
        return {"ok": True}

    monkeypatch.setattr(adapters, "_codex_sandbox_started", started)
    codex[0].append({"ok": True, "reply": _codex_reply("built")})
    _ask(repo)
    assert checked == [True], "the check must deny what the run denies"


def _started(text="built"):
    return json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n" + _codex_reply(text)


def test_the_report_keeps_configured_discovered_and_observed_apart(codex, repo):
    runs, _, _ = codex
    runs.append({"ok": True, "reply": _started()})
    context = _ask(repo)["context"]
    assert context["name"] == "clean"
    assert context["configured"]["instructions"] == "automatic_loading_requested"
    assert context["observed"] == {"thread_started": True}
    assert context["configured"]["ignore_user_config"] is True
    agents = repo / "AGENTS.md"
    assert context["discovered"] == {
        "instructions": [{
            "path": str(agents), "bytes": agents.stat().st_size,
            "sha256": hashlib.sha256(agents.read_bytes()).hexdigest(),
        }],
        "expected": str(agents),
    }
    assert "not proof" in context["observed_reason"]
    assert "web search" in context["not_removed"]
    assert context["workdir_digest"]["scheme"] == "hardline-tree-v1"


def test_a_run_that_never_started_says_so(codex, repo):
    """Asked is not done: a failed launch must not read as instructions loaded."""
    runs, _, _ = codex
    runs.append({"ok": False, "error": "failed to start codex: not found"})
    out = _ask(repo)
    assert out["ok"] is False
    assert out["context"]["observed"] == {"thread_started": False}
    assert out["context"]["configured"]["instructions"] == "automatic_loading_requested"


def test_a_thread_that_started_is_seen_though_the_run_failed(codex, repo):
    """The reduced result drops the thread id when a run times out after its
    thread started; the stream still shows it."""
    runs, _, _ = codex
    thread = json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n"
    runs.append({"ok": False, "error": "timed out", "timed_out": True, "_stdout": thread})
    out = _ask(repo)
    assert out["ok"] is False
    assert out["context"]["observed"] == {"thread_started": True}


def test_the_digest_is_taken_before_the_builder_runs(codex, repo, monkeypatch):
    runs, _, _ = codex
    before = adapters._tree_digest(str(repo))

    def builder(agent, argv, **kw):
        (repo / "src" / "a.py").write_text("print(2)\n", encoding="utf-8")
        return {"ok": True, "reply": _codex_reply("built")}

    monkeypatch.setattr(adapters, "_run_agent_cmd", builder)
    out = _ask(repo)
    assert out["context"]["workdir_digest"] == before
    assert adapters._tree_digest(str(repo)) != before


def test_the_digest_is_taken_before_the_calls_own_writes(codex, repo, monkeypatch):
    """A temporary directory inside workdir: the call's own home lands there."""
    runs, _, _ = codex
    before = adapters._tree_digest(str(repo))

    def mkdtemp(prefix):
        (repo / "tmp").mkdir()
        return str(repo / "tmp")

    monkeypatch.setattr(adapters.tempfile, "mkdtemp", mkdtemp)
    runs.append({"ok": True, "reply": _codex_reply("built")})
    out = _ask(repo)
    assert out["context"]["workdir_digest"] == before


@pytest.mark.skipif(os.name == "nt", reason="a file symlink needs privilege on Windows")
def test_an_instruction_file_may_not_lead_outside_workdir(codex, repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("ELSEWHERE", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside)
    codex[0].append({"ok": True, "reply": _codex_reply("ran")})  # had it not refused
    out = _ask(repo)
    assert out["ok"] is False and "leads outside workdir" in out["error"]


def test_an_override_file_wins_unless_empty(repo):
    override = repo / "AGENTS.override.md"
    override.write_text("OVERRIDE", encoding="utf-8")
    assert adapters._clean_discovery(str(repo))["discovered"]["expected"] == str(override)
    override.write_text("", encoding="utf-8")
    assert adapters._clean_discovery(str(repo))["discovered"]["expected"] == str(repo / "AGENTS.md")


def test_the_rerun_reads_the_workdirs_file_and_no_other(codex, repo):
    """The default rerun points at the Codex home; a clean one must not."""
    runs, seen, _ = codex
    runs += [_refused(), {"ok": True, "reply": _codex_reply("built")}]
    out = _ask(repo)
    tail = seen[1]["argv"][seen[1]["argv"].index("--") + 1:]
    assert tail == [adapters._clean_intake(str(repo / "AGENTS.md")) + "build it"]
    assert str(repo / "AGENTS.md") in tail[0] and "Codex home" not in tail[0]
    assert out["context"]["configured"]["instructions"] == "shell_read_requested"


def test_the_rerun_asks_nothing_when_no_file_is_expected(codex, repo):
    (repo / "AGENTS.md").unlink()
    runs, seen, _ = codex
    runs += [_refused(), {"ok": True, "reply": _codex_reply("built")}]
    out = _ask(repo)
    assert seen[1]["argv"][seen[1]["argv"].index("--") + 1:] == ["build it"]
    assert out["context"]["configured"]["instructions"] == "none_requested"
    assert "instructions_note" not in out, "it was told to read nothing"


def test_a_default_calls_refusal_gives_a_clean_call_the_clean_intake(codex, repo):
    """The refusal memory is per binary and shared: a clean call that skips
    the doomed first attempt must still name only its own file."""
    runs, seen, _ = codex
    runs += [_refused(), {"ok": True, "reply": _codex_reply("one")}]
    adapters.ask_codex("first", resolve_model=False)  # a default call, refused once
    runs.append({"ok": True, "reply": _codex_reply("built")})
    out = _ask(repo)
    tail = seen[-1]["argv"][seen[-1]["argv"].index("--") + 1:]
    assert tail == [adapters._clean_intake(str(repo / "AGENTS.md")) + "build it"]
    assert out["context"]["configured"]["instructions"] == "shell_read_requested"


def test_family_names_resolve_in_the_clean_home(codex, repo, home, monkeypatch):
    seen = []

    def catalog(argv, env=None, cwd=None, **kw):
        seen.append((env["CODEX_HOME"], cwd))
        return {"ok": True, "reply": json.dumps({"models": []})}

    monkeypatch.setattr(adapters, "_run_cmd", catalog)
    adapters._codex_catalog("default", str(repo), "clean")
    assert seen[0][0] != str(home) and seen[0][1] == str(repo)


# -- the digest ------------------------------------------------------------


def test_the_digest_follows_content_not_order_or_history(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for root, names in ((one, ["b.txt", "a.txt"]), (two, ["a.txt", "b.txt"])):
        _git(root)
        for name in names:
            (root / name).write_bytes(name.encode())
    (two / ".git" / "HEAD").write_text("history", encoding="utf-8")
    assert adapters._tree_digest(str(one)) == adapters._tree_digest(str(two))
    (two / "a.txt").write_bytes(b"a.txt\r\n")
    assert adapters._tree_digest(str(one))["sha256"] != adapters._tree_digest(str(two))["sha256"]


def test_an_empty_directory_is_part_of_the_tree(tmp_path):
    (tmp_path / "f").write_text("x", encoding="utf-8")
    before = adapters._tree_digest(str(tmp_path))["sha256"]
    (tmp_path / "empty").mkdir()
    assert adapters._tree_digest(str(tmp_path))["sha256"] != before


def test_a_link_is_recorded_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("one", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    _link(outside, root / "link")
    digest = adapters._tree_digest(str(root))
    assert digest["links"] == 1 and digest["files"] == 0
    (outside / "secret").write_text("changed", encoding="utf-8")
    assert adapters._tree_digest(str(root)) == digest, "the target's content is not read"


# -- the async route -------------------------------------------------------


@pytest.mark.anyio
async def test_async_codex_carries_the_context_to_the_worker(monkeypatch, async_store, repo, home):
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}, profile=True))
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    receipt = await server.ask_codex_async(
        prompt="build", from_agent="claude", workdir=str(repo), context="clean"
    )
    assert receipt["accepted"] is True, receipt
    assert calls[0].get("context") == "clean"
    assert server.jobs.get(receipt["job_id"])["request"].get("context") == "clean"


@pytest.mark.anyio
async def test_async_admission_refuses_an_unkeepable_clean_request(monkeypatch, async_store, tmp_path):
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    receipt = await server.ask_codex_async(
        prompt="build", from_agent="claude", workdir=str(tmp_path), context="clean"
    )
    assert receipt["accepted"] is False and calls == []


@pytest.mark.anyio
async def test_async_admission_refuses_clean_without_chatgpt_auth(
    monkeypatch, async_store, repo, home
):
    """Refused at admission, not in a worker after a slot is taken."""
    monkeypatch.setattr(adapters, "_codex_sandbox", lambda: adapters._CodexSandbox([], {}, profile=True))
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(server.adapters, "ask_codex", _recorder(calls))
    receipt = await server.ask_codex_async(
        prompt="build", from_agent="claude", workdir=str(repo), context="clean"
    )
    assert receipt["accepted"] is False and calls == []
    assert "ChatGPT" in str(receipt)
