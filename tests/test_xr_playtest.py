"""Autonomous playtests: personas, oracles, brains, reports, fleets."""

import json
import sys
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from visor.xr.client import XrError
from visor.xr.playtest import brains
from visor.xr.playtest.brains import BrainContext, Call, MonkeyBrain, Observation, describe_controls
from visor.xr.playtest.oracles import Oracles, scan_log
from visor.xr.playtest.personas import PERSONAS, get_persona


def _ctx(persona="first-timer", size=(960, 1006)):
    return BrainContext(persona=get_persona(persona), goal="finish onboarding", app_name="Game",
                        controls="- right trigger/value: ia_select", image_size=size, max_steps=10)


def _obs(step=0, left=10):
    return Observation(image=Image.new("RGB", (96, 100), (40, 40, 40)), text="ok", step=step, steps_left=left)


class TestPersonas:
    def test_registry(self):
        assert {"first-timer", "speedrunner", "seated", "motion-sensitive", "one-handed"} <= set(PERSONAS)

    def test_bodies_differ(self):
        assert get_persona("seated").eye_height < 1.3
        assert get_persona("one-handed").hands == ("left",)
        assert get_persona("motion-sensitive").max_turn_speed < 90

    def test_lookup_is_forgiving(self):
        assert get_persona("Motion Sensitive").key == "motion-sensitive"
        with pytest.raises(ValueError, match="unknown persona"):
            get_persona("pirate")


class TestOracles:
    def test_freeze_needs_actions_with_no_visible_change(self):
        o = Oracles(freeze_steps=3)
        img = Image.new("RGB", (64, 64), (90, 90, 90))
        issues = []
        for step in range(1, 6):
            issues += o.after_step(step, step * 10, img, acted=True, screenshot=None)
        assert [i.source for i in issues] == ["oracle:freeze"]

    def test_waiting_is_not_freezing(self):
        o = Oracles(freeze_steps=2)
        img = Image.new("RGB", (64, 64), (90, 90, 90))
        assert not [i for s in range(6) for i in o.after_step(s, s, img, acted=False, screenshot=None)]

    def test_black_screen(self):
        o = Oracles(black_steps=2)
        black = Image.new("RGB", (64, 64))
        issues = o.after_step(1, 1, black, True, None) + o.after_step(2, 2, black, True, None)
        assert any(i.source == "oracle:black" for i in issues)

    def test_log_scan_skips_engine_boilerplate(self):
        lines = [
            "[2026.10.20-17.56.26:246][  0]LogClass: Error: StructProperty FFoo::Bar is not initialized properly",
            "[2026.10.20-17.56.27:001][ 12]LogScript: Warning: Accessed None trying to read property Panel",
            "[2026.10.20-17.56.27:002][ 13]LogScript: Warning: Accessed None trying to read property Panel",
        ]
        found = scan_log(lines)
        assert len(found) == 1 and found[0][2] == "functional"

    def test_performance_thresholds(self):
        o = Oracles(refresh_hz=72)
        slow = {"fps": 50.0, "refresh_hz": 72, "window_frames": 300, "frame_ms_p95": 30, "frame_ms_p99": 45}
        titles = [i.title for i in o.performance(slow, 3, 99)]
        assert any("Frame rate below" in t for t in titles) and any("Hitches" in t for t in titles)
        smooth = {"fps": 71.9, "refresh_hz": 72, "window_frames": 300, "frame_ms_p95": 14, "frame_ms_p99": 15}
        assert Oracles(refresh_hz=72).performance(smooth, 3, 99) == []


class TestMonkey:
    def test_same_seed_same_run(self):
        def run(seed):
            b = MonkeyBrain(seed=seed)
            b.start(_ctx())
            calls = [b.first(_obs())]
            calls += [b.next([], _obs(i, 10 - i)) for i in range(1, 8)]
            return [(c[0].name, c[0].args) for c in calls]

        assert run(3) == run(3)
        assert run(3) != run(4)

    def test_one_handed_never_uses_right(self):
        b = MonkeyBrain(seed=1)
        b.start(_ctx("one-handed"))
        for i in range(60):
            call = b.next([], _obs(i, 99))[0]
            assert call.args.get("hand", "left") == "left"

    def test_finishes_when_out_of_steps(self):
        b = MonkeyBrain(seed=0)
        b.start(_ctx())
        assert b.next([], _obs(10, 0))[0].name == "finish"


class FakeMessages:
    """Records requests and replays canned Claude responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _response(*blocks, stop="tool_use"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


def _tool(name, args, id_="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


class TestClaudeBrain:
    @pytest.fixture
    def fake(self, monkeypatch):
        import anthropic

        msgs = FakeMessages([
            _response(SimpleNamespace(type="text", text="The big button says Get Started."),
                      _tool("click", {"x": 480, "y": 660})),
            _response(_tool("report_issue", {"title": "Text too small", "severity": "minor",
                                             "category": "usability", "expected": "a", "actual": "b"}, "tu_2"),
                      _tool("finish", {"goal_achieved": True, "summary": "done", "rating": 7}, "tu_3")),
        ])
        client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
        return msgs

    def test_loop_shapes(self, fake):
        b = brains.ClaudeBrain()
        b.start(_ctx())
        first = b.first(_obs())
        assert [(c.name, c.args) for c in first] == [("click", {"x": 480, "y": 660})]
        assert first[0].thought == "The big button says Get Started."
        req = fake.requests[0]
        assert req["model"] == "claude-opus-5-5"
        assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
        assert req["thinking"] == {"type": "adaptive", "display": "summarized"}  # readable reasoning
        user = req["messages"][0]["content"]
        assert user[1]["type"] == "image" and user[1]["source"]["media_type"] == "image/jpeg"
        assert "First-timer" in req["system"][0]["text"]

        nxt = b.next([(first[0], "Clicked.")], _obs(1, 9))
        assert [c.name for c in nxt] == ["report_issue", "finish"]
        # Transcript is append-only: assistant turn, then tool_result carrying the new frame.
        sent = fake.requests[1]["messages"]
        assert sent[1]["role"] == "assistant"
        result = sent[2]["content"][0]
        assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
        assert any(block["type"] == "image" for block in result["content"])
        assert b.usage()["requests"] == 2

    def test_reasoning_in_thinking_blocks_becomes_the_thought(self, monkeypatch):
        """Opus 5.5 reasons between tool calls in thinking blocks, not text."""
        import anthropic

        msgs = FakeMessages([_response(SimpleNamespace(type="thinking", thinking="Get Started looks like the way in.",
                                                       signature="sig"),
                                       _tool("click", {"x": 1, "y": 2}))])
        monkeypatch.setattr(anthropic, "Anthropic",
                            lambda *a, **k: SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
        b = brains.ClaudeBrain()
        b.start(_ctx())
        assert b.first(_obs())[0].thought == "Get Started looks like the way in."

    def test_refusal_finishes_cleanly(self, monkeypatch):
        import anthropic

        msgs = FakeMessages([_response(stop="refusal")])
        monkeypatch.setattr(anthropic, "Anthropic",
                            lambda *a, **k: SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
        b = brains.ClaudeBrain()
        b.start(_ctx())
        assert b.first(_obs())[0].name == "finish"


def test_describe_controls_merges_duplicates():
    actions = [
        {"name": "ia_select", "type": "boolean", "bindings": [{"hand": "right", "input": "trigger", "component": "click"}]},
        {"name": "legacy_select", "type": "boolean", "bindings": [{"hand": "right", "input": "trigger", "component": "click"}]},
        {"name": "aim", "type": "pose", "bindings": [{"hand": "right", "input": "aim", "component": "pose"}]},
    ]
    text = describe_controls(actions)
    assert text.count("right trigger/click") == 1 and "aim" not in text


def test_report_html_renders(tmp_path):
    from visor.xr.playtest.report import write_html

    (tmp_path / "frames.json").write_text(json.dumps({"frame_rows": [[i, 0, 0, 0, i * 13_900_000, 0, 0, 1]
                                                                     for i in range(1, 200)]}))
    report = {
        "name": "pt", "created": "2026-10-21T10:00:00", "duration_s": 90,
        "app": {"name": "Game", "runtime": "Monado", "interaction_profile": "/interaction_profiles/oculus/touch_controller"},
        "persona": {"name": "First-timer", "tagline": "new"}, "goal": "play",
        "brain": {"kind": "claude", "model": "claude-opus-5-5", "requests": 3, "estimated_cost_usd": 0.12},
        "config": {"deterministic": True}, "verdict": {"goal_achieved": False, "summary": "<b>stuck</b>", "rating": 4},
        "issues": [{"id": "V01", "title": "Mic check blocks progress", "severity": "blocker", "category": "accessibility",
                    "description": "Expected a skip", "step": 3, "frame": 400, "source": "agent:first-timer",
                    "screenshot": "shots/003.jpg", "steps_to_reproduce": ["click {}"]}],
        "steps": [{"step": 1, "frame": 10, "action": "click", "args": {"x": 1}, "thought": "hm", "result": "ok",
                   "screenshot": "shots/001.jpg", "t": 0}],
        "perf": {"start": {"fps": 72, "refresh_hz": 72, "frame_ms_p50": 13.9, "frame_ms_p95": 14,
                           "frame_ms_p99": 15, "cpu_ms_p50": 2, "missed_frames": 0}},
        "paused_frames": [50, 150], "crash": None,
        "artifacts": {"video": "video.mp4", "frames": "frames.json"},
    }
    html = write_html(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Mic check blocks progress" in html and "4<small>/10</small>" in html
    assert "&lt;b&gt;stuck&lt;/b&gt;" in html  # agent text is escaped
    assert "real-time frames" in html


def test_fleet_merges_issues_by_signature(tmp_path, monkeypatch):
    from visor.xr.fleet import Fleet, FleetConfig

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    f = Fleet(FleetConfig(target="demo", count=3, mode="playtest"))
    for i, r in enumerate(f.results):
        (f.dir / r.name).mkdir(parents=True)
        issues = [{"title": f"Frame rate below the display rate ({60 + i} of 72 fps)", "severity": "major",
                   "category": "performance", "screenshot": None}]
        (f.dir / r.name / "report.json").write_text(json.dumps({"issues": issues}))
        r.report, r.status = f"{r.name}/report.html", "done"
    s = f.summary(10)
    assert len(s["issues"]) == 1 and len(s["issues"][0]["runs"]) == 3
    assert s["ok"] == 3


def test_mcp_registers_xr_tools():
    import asyncio

    from visor.mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"xr_launch", "xr_screenshot", "xr_click", "xr_press", "xr_step", "xr_pause", "xr_say", "xr_mic",
            "xr_hear"} <= names


class TestPlaytestHearing:
    def test_the_agent_is_told_and_the_sound_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        from array import array

        from visor.xr.hearing import Heard, Line, Sound
        from visor.xr.playtest.session import Playtest, PlaytestConfig, StepRecord

        t = Playtest(PlaytestConfig(target="demo", brain="monkey"), brain=MonkeyBrain())
        t.dir.mkdir(parents=True)

        class FakeEars:
            def hear(self, wait=0.0, step=None, expect=0.0):
                return Heard(start=0, end=2, sounds=[Sound(0.5, 1.5, -6, True)], lines=[Line(0.5, 1.5, "Hello!", "en")],
                             transcribed=True, clipped=0.01, samples=array("f", [0.1] * 1600))

        class FakeXr:
            def state(self):
                return {"render_frame": 99}

        t.xr, t.ears = FakeXr(), FakeEars()
        t.steps.append(StepRecord(step=1, frame=90, action="press", args={}, thought="", result="ok", screenshot="",
                                  t=0.0))
        note = t._listen(1)
        assert note == ' You heard the app say: "Hello!" The audio was distorted: 1.0% of it clipped at full volume.'
        assert t.steps[-1].heard_audio == "heard/001.wav" and (t.dir / "heard/001.wav").exists()
        assert t.heard[0]["said"] == "Hello!" and t.heard[0]["frame"] == 99
        assert [i.category for i in t.issues] == ["audio"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sleep")
class TestStalls:
    """An app that stops drawing (a level loading, shaders compiling) mustn't
    use up the agent's actions; it's an issue, and past a limit, a hang."""

    def _test(self, tmp_path, monkeypatch, step_seconds, drawn=lambda n: 609):
        import subprocess

        from visor.xr.playtest import session
        from visor.xr.playtest.session import Playtest, PlaytestConfig, StepRecord

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        monkeypatch.setattr(session, "STALL_POLL", 0.02)
        t = Playtest(PlaytestConfig(target="demo", brain="monkey"), brain=MonkeyBrain())
        t.dir.mkdir(parents=True)

        class Probe:
            calls = 0

            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def state(self):
                Probe.calls += 1
                return {"render_frame": drawn(Probe.calls)}

        class Xr:
            def step(self, frames, timeout):
                end = time.monotonic() + step_seconds
                while time.monotonic() < end and t.sim.proc.poll() is None:
                    time.sleep(0.01)
                if t.sim.proc.poll() is not None:
                    raise XrError("step: the app closed the connection (did it exit?)")
                return {"complete": True}

        class Sim:
            socket_path = "unused"
            proc = subprocess.Popen(["sleep", "30"])

            def alive(self):
                return self.proc.poll() is None

        monkeypatch.setattr(session, "XrClient", Probe)
        t.xr, t.sim = Xr(), Sim()
        t.steps.append(StepRecord(step=3, frame=600, action="click", args={}, thought="", result="ok", screenshot="",
                                  t=0.0))
        return t

    def test_a_long_freeze_is_measured(self, tmp_path, monkeypatch):
        t = self._test(tmp_path, monkeypatch, step_seconds=0.3)
        try:
            frozen, took = t._advance(72)
            assert 0.2 <= frozen <= took < 1.0
        finally:
            t.sim.proc.kill()

    def test_drawing_is_not_a_freeze(self, tmp_path, monkeypatch):
        t = self._test(tmp_path, monkeypatch, step_seconds=0.3, drawn=lambda n: n)
        try:
            frozen, took = t._advance(72)
            assert frozen < 0.1 and took >= 0.3
        finally:
            t.sim.proc.kill()

    def test_drawing_nothing_for_too_long_is_a_hang(self, tmp_path, monkeypatch):
        from visor.xr.playtest import session
        from visor.xr.sim import SimError

        monkeypatch.setattr(session, "STALL_LIMIT", 0.2)
        t = self._test(tmp_path, monkeypatch, step_seconds=5)
        with pytest.raises(SimError, match="closed it as hung"):
            t._advance(72)
        assert t.sim.proc.poll() is not None
        assert [i.source for i in t.issues] == ["oracle:hang"] and t.issues[0].severity == "blocker"
        assert "closed it as hung" in t.stopped_for

    def test_the_log_says_what_it_was_doing(self, tmp_path, monkeypatch):
        t = self._test(tmp_path, monkeypatch, step_seconds=0)
        t.sim.proc.kill()
        log = ["[2026.10.23-06.38.20:285][609]LogLoad: LoadMap: /Game/Environments/Voyager/Spaceship/INTERIOR/Int_Spaceship",
               "Failed to find game directory: /x/ShaderCompileWorker/Binaries"]
        issue, note = t._stall_issue(4, 609, "shots/004.jpg", 994.2, 1010.0, log)
        assert issue.title == "The view froze for 16 min 34 s while Int_Spaceship loaded"
        assert issue.severity == "major" and issue.category == "performance"
        assert "packaged build doesn't do" in issue.description and issue.extra["compiling_shaders"]
        assert note == " The app then drew nothing for 16 min 34 s while it loaded Int_Spaceship; it's drawing again now."
        quick, _ = t._stall_issue(4, 609, None, 12, 13, [])
        assert quick.title == "The view froze for 12 s after 'click'" and quick.severity == "minor"


def test_durations_read_naturally():
    from visor.xr.playtest.session import _duration

    assert [_duration(s) for s in (4.4, 59.6, 60, 994.2, 3600, 3725)] == \
        ["4 s", "1 min", "1 min", "16 min 34 s", "1 h", "1 h 2 min"]


def test_a_run_stopped_from_outside_says_so(tmp_path):
    from visor.xr.playtest.report import write_html

    report = {
        "name": "pt", "created": "2026-10-23T15:00:00", "duration_s": 1460, "app": {"name": "Game"},
        "persona": {"name": "First-timer", "tagline": "new"}, "goal": "play", "brain": {"kind": "claude"},
        "config": {"video": False}, "issues": [], "perf": {}, "artifacts": {}, "steps": [],
        "verdict": {"goal_achieved": False, "summary": "Stopped early: the computer was running out of memory."},
        "stopped_for": "the computer was running out of memory",
    }
    html = write_html(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Stopped early" in html and "Goal not reached" not in html


def test_report_shows_what_the_app_said(tmp_path):
    from visor.xr.playtest.report import write_html

    report = {
        "name": "pt", "created": "2026-10-22T10:00:00", "duration_s": 30, "app": {"name": "Game"},
        "persona": {"name": "First-timer", "tagline": "new"}, "goal": "play", "brain": {"kind": "monkey"},
        "config": {"video": False}, "verdict": {}, "issues": [], "perf": {}, "artifacts": {},
        "steps": [{"step": 1, "frame": 10, "action": "press", "args": {}, "thought": "", "t": 0, "result": "ok",
                   "screenshot": "shots/001.jpg", "heard": 'You heard the app say: "<Hola>"',
                   "heard_audio": "heard/001.wav"}],
        "heard": [{"step": 1, "said": "<Hola>", "language": "es", "audio": "heard/001.wav"}],
    }
    html = write_html(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "What the app said" in html and "<q>&lt;Hola&gt;</q>" in html and '<span class="lang">es</span>' in html
    assert html.count('src="heard/001.wav"') == 2 and "recording was off" in html


def test_call_dataclass_defaults():
    assert Call("wait", {"seconds": 1}).id == ""
