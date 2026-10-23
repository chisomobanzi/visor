"""Who plays a playtest: the saved Anthropic key, and another agent over MCP."""

import os
import stat
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from visor.xr.playtest import credentials
from visor.xr.playtest.brains import AgentBrain, Call, Observation

KEY = "sk-ant-api03-" + "x" * 80 + "Wxyz"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))
    for var in credentials.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(credentials, "check", lambda key: None)  # never ask Anthropic from a test
    return tmp_path


class TestCredentials:
    def test_nothing_at_first(self):
        assert credentials.source() is None and credentials.client_kwargs() == {}

    def test_a_saved_key_is_used_and_only_its_end_shown(self):
        saved = credentials.save(f"  {KEY}\n")
        assert saved["label"] == "your saved key …Wxyz" and KEY not in str(saved)
        assert credentials.source() == {"kind": "saved", "label": "your saved key …Wxyz"}
        assert credentials.client_kwargs() == {"api_key": KEY}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_only_you_can_read_it(self):
        credentials.save(KEY)
        assert stat.S_IMODE(os.stat(credentials.key_path()).st_mode) == 0o600

    def test_the_environment_wins(self, monkeypatch):
        credentials.save(KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        assert credentials.source()["kind"] == "env"
        assert credentials.client_kwargs() == {}  # the SDK reads the environment itself

    def test_an_ant_login_profile_counts(self, home):
        (home / "anthropic" / "credentials").mkdir(parents=True)
        (home / "anthropic" / "credentials" / "default.json").write_text("{}")
        assert credentials.source()["kind"] == "profile" and credentials.client_kwargs() == {}

    def test_a_refused_key_is_not_kept(self, monkeypatch):
        def refuse(key):
            raise credentials.CredentialsError("Anthropic didn't accept that key.")

        monkeypatch.setattr(credentials, "check", refuse)
        with pytest.raises(credentials.CredentialsError):
            credentials.save(KEY)
        assert not credentials.key_path().exists()

    def test_junk_is_refused_without_asking(self):
        for junk in ("", "   ", "sk-ant two words"):
            with pytest.raises(credentials.CredentialsError):
                credentials.save(junk)

    def test_forget(self):
        credentials.save(KEY)
        assert credentials.forget() and credentials.source() is None and not credentials.forget()

    def test_the_check_tells_a_wrong_key_from_no_network(self, monkeypatch):
        """Against the real SDK, with the HTTP layer faked."""
        anthropic = pytest.importorskip("anthropic")
        try:
            import httpx2 as httpx  # what the SDK speaks HTTP with since 1.x
        except ImportError:
            import httpx

        monkeypatch.undo()  # the real check
        statuses = iter([401, 200])

        def handler(request):
            assert request.url.path.endswith("/v1/models/claude-opus-5-5")
            code = next(statuses)
            body = ({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
                    if code == 401 else {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                                         "created_at": "2026-01-01T00:00:00Z"})
            return httpx.Response(code, json=body)

        real = anthropic.Anthropic

        def fake(**kw):
            return real(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

        monkeypatch.setattr(anthropic, "Anthropic", fake)
        with pytest.raises(credentials.CredentialsError, match="didn't accept"):
            credentials.check(KEY)
        assert credentials.check(KEY) is None


def _obs(n=0):
    return Observation(image=Image.new("RGB", (64, 64)), text=f"frame {n}", step=n, steps_left=10 - n)


class TestAgentBrain:
    def test_moves_and_views_take_turns(self):
        brain = AgentBrain()
        got = []
        player = threading.Thread(target=lambda: got.append(brain.first(_obs())))
        player.start()
        obs, results = brain.view(timeout=5)
        assert obs.text == "frame 0" and results == []
        brain.move([Call("look", {"yaw": 10, "pitch": 0})])
        player.join(5)
        assert got[0][0].name == "look" and brain.moves == 1

    def test_calling_it_off_finishes_the_playtest(self):
        brain = AgentBrain()
        got = []
        player = threading.Thread(target=lambda: got.append(brain.first(_obs())))
        player.start()
        brain.view(timeout=5)
        brain.stop()
        player.join(5)
        assert got[0][0].name == "finish" and "called it off" in got[0][0].args["summary"]

    def test_an_agent_that_walks_away(self):
        brain = AgentBrain(patience=0.1)
        calls = brain.first(_obs())
        assert calls[0].name == "finish" and "stopped responding" in calls[0].args["summary"]


class TestMcpMoves:
    """What xr_playtest_act accepts, without booting an app."""

    @pytest.fixture
    def run(self, tmp_path):
        from types import SimpleNamespace

        from visor.mcp import playtest_tools as pt

        brain = AgentBrain()
        run = {"test": SimpleNamespace(name="pt-test", dir=tmp_path), "brain": brain, "status": "playing",
               "awaiting": True, "note": "", "report": None, "error": None, "thread": None}
        pt._runs["pt-test"] = run
        yield run
        pt._runs.pop("pt-test", None)

    def _act(self, moves, **kw):
        from visor.mcp import playtest_tools as pt

        return pt.xr_playtest_act(moves, wait_seconds=0.05, **kw)

    def test_unknown_and_incomplete_moves_are_refused(self, run):
        from visor.mcp.server import QuestError

        with pytest.raises(QuestError, match="unknown move 'teleport'"):
            self._act([{"tool": "teleport"}])
        with pytest.raises(QuestError, match="click needs y"):
            self._act([{"tool": "click", "x": 3}])
        with pytest.raises(QuestError, match="no moves"):
            self._act([])
        assert run["brain"]._moves.empty()

    def test_moves_reach_the_playtest_with_the_thought(self, run):
        out = self._act([{"tool": "report_issue", "title": "t", "severity": "minor", "category": "visual",
                          "expected": "e", "actual": "a"}, {"name": "press", "args": {"button": "a"}}],
                        thought="trying A")
        calls = run["brain"]._moves.get_nowait()
        assert [c.name for c in calls] == ["report_issue", "press"] and calls[1].args == {"button": "a"}
        assert all(c.thought == "trying A" for c in calls)
        assert "Still busy" in out[0] and not run["awaiting"]

    def test_one_move_at_a_time(self, run):
        from visor.mcp.server import QuestError

        self._act([{"tool": "wait", "seconds": 1}])
        with pytest.raises(QuestError, match="still playing out"):
            self._act([{"tool": "wait", "seconds": 1}])

    def test_the_next_frame_comes_back(self, run):
        from mcp.server.mcpserver import Image as McpImage

        run["briefed"] = True
        run["brain"]._views.put((_obs(3), [(Call("wait", {"seconds": 1}), "Waited 1.0s.")]))
        out = self._act([{"tool": "wait", "seconds": 1}])
        assert isinstance(out[0], McpImage)
        assert out[1].startswith("wait: frame 3") and "(playtest pt-test;" in out[1]


class TestDashboardKey:
    @pytest.fixture
    def client(self):
        from visor.ui.server import app

        return TestClient(app, raise_server_exceptions=False)

    def test_save_show_forget(self, client):
        assert client.get("/api/xr/claude").json() == {"ready": False}
        r = client.post("/api/xr/claude", json={"key": KEY})
        assert r.status_code == 200 and r.json()["label"] == "your saved key …Wxyz" and KEY not in r.text
        assert client.get("/api/xr/claude").json()["ready"] is True
        assert client.delete("/api/xr/claude").json() == {"ready": False}

    def test_a_refused_key_says_why(self, client, monkeypatch):
        def refuse(key):
            raise credentials.CredentialsError("Anthropic didn't accept that key.")

        monkeypatch.setattr(credentials, "check", refuse)
        r = client.post("/api/xr/claude", json={"key": KEY})
        assert r.status_code == 400 and "didn't accept" in r.json()["detail"] and KEY not in r.text

    def test_claude_playtests_need_a_key_first(self, client):
        r = client.post("/api/xr/playtest", json={"target": "demo", "brain": "claude"})
        assert r.status_code == 400 and "API key" in r.json()["detail"]

    def test_another_site_cannot_post_a_key(self, client):
        """A cross-site form can only send text/plain or form data, which the route refuses."""
        r = client.post("/api/xr/claude", content=f'{{"key": "{KEY}"}}', headers={"content-type": "text/plain"})
        assert r.status_code == 422 and not credentials.key_path().exists()
