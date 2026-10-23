"""Another agent as the playtester, over MCP."""

import threading

import pytest
from PIL import Image

from visor.xr.playtest.brains import AgentBrain, Call, Observation


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
