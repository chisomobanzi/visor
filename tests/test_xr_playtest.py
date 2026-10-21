"""Autonomous playtests: personas, oracles, brains, reports."""

import json
from types import SimpleNamespace

import pytest
from PIL import Image

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


def test_call_dataclass_defaults():
    assert Call("wait", {"seconds": 1}).id == ""
