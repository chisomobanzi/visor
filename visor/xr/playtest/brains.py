"""What decides the next move in a playtest.

``ClaudeBrain`` is the real playtester: Claude, in character, looking at each
frame and choosing what a person would do next, reporting what it finds.
``MonkeyBrain`` is a seeded chaos tester with no model behind it — cheap,
reproducible, and good at the bugs input spam finds (crashes, soft-locks).

Both speak the same tool vocabulary, so the session doesn't care which one
it is driving.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import random
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from visor.xr.playtest.personas import Persona

# One vocabulary for every brain. Pixel coordinates refer to the latest screenshot.
TOOLS: list[dict] = [
    {
        "name": "click",
        "description": (
            "Point a controller's laser at pixel (x, y) of the latest screenshot and pull the trigger. "
            "Use this for buttons, menus, panels — anything a VR player taps with a laser pointer."),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "pixel column in the latest screenshot"},
                "y": {"type": "number", "description": "pixel row in the latest screenshot"},
                "hand": {"type": "string", "enum": ["left", "right"]},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "point",
        "description": "Aim a controller's laser at pixel (x, y) without clicking — to hover, or to check where it lands.",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                           "hand": {"type": "string", "enum": ["left", "right"]}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "press",
        "description": ("Press a controller button: trigger, grip, a, b (right hand), x, y, menu (left hand), "
                        "stick (thumbstick click)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "button": {"type": "string", "enum": ["trigger", "grip", "a", "b", "x", "y", "menu", "stick"]},
                "hand": {"type": "string", "enum": ["left", "right"]},
                "hold_seconds": {"type": "number", "description": "how long to hold it (default 0.1)"},
            },
            "required": ["button"],
        },
    },
    {
        "name": "stick",
        "description": ("Push a thumbstick: x right(+)/left(-), y forward(+)/back(-), each -1..1. The left stick "
                        "usually moves you; the right stick usually turns or teleports."),
        "input_schema": {
            "type": "object",
            "properties": {"hand": {"type": "string", "enum": ["left", "right"]},
                           "x": {"type": "number"}, "y": {"type": "number"},
                           "seconds": {"type": "number", "description": "how long to hold it (default 0.5)"}},
            "required": ["hand", "x", "y"],
        },
    },
    {
        "name": "look",
        "description": ("Turn your head. yaw: degrees left (+) or right (-) of the direction you started facing; "
                        "pitch: up (+) or down (-)."),
        "input_schema": {
            "type": "object",
            "properties": {"yaw": {"type": "number"}, "pitch": {"type": "number"}},
            "required": ["yaw", "pitch"],
        },
    },
    {
        "name": "reach",
        "description": ("Move a hand to a spot relative to your head (metres): right, up (negative is below your "
                        "eyes), forward. Optionally hold grip to grab what's there."),
        "input_schema": {
            "type": "object",
            "properties": {"hand": {"type": "string", "enum": ["left", "right"]},
                           "right": {"type": "number"}, "up": {"type": "number"}, "forward": {"type": "number"},
                           "grab": {"type": "boolean", "description": "hold the grip button down (false lets go)"}},
            "required": ["hand", "right", "up", "forward"],
        },
    },
    {
        "name": "walk",
        "description": "Physically step around the room (metres, relative to where you face).",
        "input_schema": {
            "type": "object",
            "properties": {"forward": {"type": "number"}, "right": {"type": "number"}},
            "required": ["forward"],
        },
    },
    {
        "name": "wait",
        "description": "Let time pass without doing anything (e.g. for a character to finish talking).",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
    },
    {
        "name": "say",
        "description": (
            "Speak out loud, in character, into the headset's microphone: when the app asks you to talk, "
            "asks you a question, or takes voice commands. The app hears a natural voice, and the world "
            "runs while you speak. Pass a language code to speak another language."),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "exactly what you say, as you'd say it"},
                "language": {"type": "string", "description": "e.g. es, fr, de, pt, zh (default English)"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "report_issue",
        "description": (
            "Report a problem the moment you notice it — a bug, something confusing, uncomfortable, "
            "inaccessible, ugly or wrong. One problem per call. The current screenshot and your recent "
            "actions are attached automatically, so describe what you expected and what happened instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "short, specific: what is wrong, where"},
                "severity": {"type": "string", "enum": ["blocker", "major", "minor", "note"]},
                "category": {"type": "string", "enum": ["functional", "usability", "comfort", "accessibility",
                                                        "visual", "content", "performance", "audio", "crash"]},
                "expected": {"type": "string"},
                "actual": {"type": "string"},
            },
            "required": ["title", "severity", "category", "expected", "actual"],
        },
    },
    {
        "name": "finish",
        "description": ("End the playtest: when you've completed the goal, or you're stuck past your patience. "
                        "Give your honest verdict in character."),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_achieved": {"type": "boolean"},
                "summary": {"type": "string", "description": "what you did and how it went, 2-5 sentences"},
                "rating": {"type": "integer", "description": "1-10, how this persona would rate the experience"},
                "highlights": {"type": "array", "items": {"type": "string"}},
                "frustrations": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["goal_achieved", "summary", "rating"],
        },
    },
]

ACTION_TOOLS = {"click", "point", "press", "stick", "look", "reach", "walk", "wait", "say"}


@dataclass
class Call:
    """One tool call a brain wants made."""

    name: str
    args: dict
    id: str = ""
    thought: str = ""


@dataclass
class Observation:
    image: Image.Image  # what the agent sees (already scaled)
    text: str  # result of the last action(s), events, budget
    step: int
    steps_left: int


@dataclass
class BrainContext:
    persona: Persona
    goal: str
    app_name: str
    controls: str  # human-readable list of the app's bound actions
    image_size: tuple[int, int]
    max_steps: int
    headset: str = "Meta Quest 3"


class Brain:
    name = "brain"
    model = ""

    def start(self, ctx: BrainContext) -> None:
        self.ctx = ctx

    def first(self, obs: Observation) -> list[Call]:
        raise NotImplementedError

    def next(self, results: list[tuple[Call, str]], obs: Observation) -> list[Call]:
        raise NotImplementedError

    def usage(self) -> dict:
        return {}


# --------------------------------------------------------------------------
# Claude


def _jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=82)
    return base64.standard_b64encode(buf.getvalue()).decode()


def system_prompt(ctx: BrainContext) -> str:
    p = ctx.persona
    hands = "both controllers" if len(p.hands) == 2 else f"only the {p.hands[0]} controller (the other is off)"
    w, h = ctx.image_size
    return f"""You are playtesting a VR app, "{ctx.app_name}", in character. It runs on a virtual {ctx.headset}; you see what the headset shows (left eye) as a {w}x{h} screenshot after every action.

# Who you are
{p.name} — {p.tagline}
{p.behaviour}

Your body: eyes {p.eye_height:.2f} m above the floor, {hands}, you turn at most {p.max_turn_deg:.0f} degrees either way{'' if p.max_turn_speed >= 180 else f' and only slowly (about {p.max_turn_speed:.0f} degrees per second)'}.

# Your goal
{ctx.goal}

# What you're best placed to catch
{chr(10).join('- ' + w for w in p.watch_for)}

# How the session works
- The world is paused between your actions, so take your time. Each action plays out for a moment and you get a fresh screenshot.
- Pixel coordinates for click/point always refer to the latest screenshot. To press UI, click the middle of the button. If the laser lands off target, point first and adjust.
- The app's own controls (what it bound to each button) are below — use them to know what buttons do.
- You can talk: say speaks into the headset's microphone{'' if p.voice_volume >= 0.8 else ' (quietly — that is your voice)'}. When the app asks you to speak, answer out loud rather than looking for a button. You'll be told whether the app was listening.
- You can hear: what the app says is written out after each action ("You heard the app say: …"), and other sounds are described. The world runs on until it has finished talking. Speech recognition can mishear names; trust what you see when they disagree. Sound problems count too: no sound where you'd expect it, a voice you can't make out, audio that cuts off or distorts.
- Report every problem as soon as you notice it with report_issue: bugs, confusion, discomfort, accessibility barriers, visual or text problems, slowness. Be specific and honest; minor things count. Don't report the same thing twice.
- You have {ctx.max_steps} actions. If you've been stuck for about {p.patience_steps} actions, report it and try another way; if nothing works, finish.
- When the goal is done or you can't continue, call finish with your verdict.

# The app's controls
{ctx.controls or '(the app defined no controller actions)'}
"""


class ClaudeBrain(Brain):
    """Claude as the playtester, through the Messages API with tool use.

    The transcript is append-only (thinking blocks stay valid); old
    screenshots are pruned server-side by context editing rather than by
    rewriting history.
    """

    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_tokens: int = 16000):
        import anthropic

        from visor.xr.playtest import credentials

        self.client = anthropic.Anthropic(**credentials.client_kwargs())
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.messages: list = []
        self.totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "requests": 0}
        self.stopped: Optional[str] = None

    def start(self, ctx: BrainContext) -> None:
        super().start(ctx)
        self.system = system_prompt(ctx)

    def _image_block(self, img: Image.Image) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _jpeg_b64(img)}}

    def _ask(self) -> list[Call]:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                tools=TOOLS,
                messages=self.messages,
                # Opus 5.5 reasons between tool calls in thinking blocks; "summarized" makes them
                # readable for the report (it changes visibility only, not what's billed).
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
                betas=["server-side-fallback-2026-07-01", "context-management-2025-06-27"],
                fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            self.stopped = f"Claude API error {e.status_code}: {e.message}"
            return [Call("finish", {"goal_achieved": False, "summary": self.stopped, "rating": 0})]
        except anthropic.APIConnectionError as e:
            self.stopped = f"couldn't reach the Claude API: {e}"
            return [Call("finish", {"goal_achieved": False, "summary": self.stopped, "rating": 0})]
        u = response.usage
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            self.totals[k] += getattr(u, k, 0) or 0
        self.totals["requests"] += 1
        self.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "refusal":
            self.stopped = "the model declined to continue"
            return [Call("finish", {"goal_achieved": False, "summary": self.stopped, "rating": 0})]
        # What it said, or failing that what it thought (thinking blocks are kept, unchanged, in
        # the transcript above: they're bound to this conversation and must go back as received).
        thought = " ".join(b.text for b in response.content if b.type == "text").strip() or " ".join(
            (getattr(b, "thinking", "") or "") for b in response.content if b.type == "thinking").strip()
        calls = [Call(b.name, dict(b.input), id=b.id, thought=thought)
                 for b in response.content if b.type == "tool_use"]
        if not calls:
            # It answered in prose; nudge it back to acting (keeps the transcript append-only).
            self.messages.append({"role": "user", "content": "Keep playing with your tools, or call finish."})
            return self._ask()
        return calls

    def first(self, obs: Observation) -> list[Call]:
        self.messages = [{"role": "user", "content": [
            {"type": "text", "text": f"You've just put the headset on. {obs.text}\nWhat do you do?"},
            self._image_block(obs.image),
        ]}]
        return self._ask()

    def next(self, results: list[tuple[Call, str]], obs: Observation) -> list[Call]:
        blocks = []
        for i, (call, text) in enumerate(results):
            content: list = [{"type": "text", "text": text}]
            if i == len(results) - 1:
                content.append(self._image_block(obs.image))
                content.append({"type": "text", "text": obs.text})
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
        self.messages.append({"role": "user", "content": blocks})
        return self._ask()

    def usage(self) -> dict:
        t = dict(self.totals)
        # Claude Opus 5.5 list prices per million tokens.
        cost = (t["input_tokens"] * 4.0 + t["output_tokens"] * 20.0 + t["cache_read_input_tokens"] * 0.20
                + t["cache_creation_input_tokens"] * 5.0) / 1e6
        t["estimated_cost_usd"] = round(cost, 4) if self.model == "claude-opus-5-5" else None
        t["model"] = self.model
        return t


# --------------------------------------------------------------------------
# Another agent, over MCP


class AgentBrain(Brain):
    """Another agent plays: Claude Code, or any MCP client, through the
    xr_playtest_* tools, on its own model and plan (no API key here). The
    playtest runs in a thread of its own and waits in first()/next() for the
    agent's moves; the agent waits in view() for what they led to."""

    name = "agent"

    def __init__(self, patience: float = 1800.0, client: str = ""):
        self.patience = patience  # seconds to wait for a move before calling the playtest off
        self.client = client  # who's playing, as the MCP client named itself
        self._moves: queue.Queue = queue.Queue()
        self._views: queue.Queue = queue.Queue()
        self.last: Optional[tuple[Observation, list]] = None
        self.moves = 0

    def first(self, obs: Observation) -> list[Call]:
        return self._turn(obs, [])

    def next(self, results: list[tuple[Call, str]], obs: Observation) -> list[Call]:
        return self._turn(obs, results)

    def _turn(self, obs: Observation, results: list) -> list[Call]:
        self._views.put((obs, results))
        try:
            calls = self._moves.get(timeout=self.patience)
        except queue.Empty:
            calls = None
        if calls is None:
            why = "the playtester called it off" if not self.patience else "the playtester stopped responding"
            return [Call("finish", {"goal_achieved": False, "summary": f"Stopped early: {why}."})]
        self.moves += 1
        return calls

    # the agent's side

    def view(self, timeout: float) -> Optional[tuple[Observation, list]]:
        """The next thing to look at, once the app has played out the last move."""
        try:
            view = self._views.get(timeout=timeout)
        except queue.Empty:
            return None
        if view is not None:  # None: the playtest is over
            self.last = view
        return view

    def move(self, calls: list[Call]) -> None:
        self._moves.put(calls)

    def stop(self) -> None:
        self.patience = 0.0
        self._moves.put(None)

    def usage(self) -> dict:
        return {"client": self.client, "moves": self.moves}


def tool_guide() -> str:
    """The move vocabulary as short lines, for an agent that plays through MCP."""
    lines = []
    for t in TOOLS:
        props = t["input_schema"].get("properties", {})
        need = t["input_schema"].get("required", [])
        args = ", ".join(a if a in need else f"{a}?" for a in props)
        lines.append(f"- {t['name']}({args}): {t['description']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Monkey


MONKEY_LINES = ("hello", "yes", "no", "can you hear me", "testing one two three", "next", "start", "help",
                "um", "what")


@dataclass
class MonkeyBrain(Brain):
    """Seeded random input — no model, fully reproducible from its seed."""

    seed: int = 0
    name: str = "monkey"
    _rng: random.Random = field(default=None, repr=False)  # type: ignore[assignment]
    _steps: int = 0

    def start(self, ctx: BrainContext) -> None:
        super().start(ctx)
        self._rng = random.Random(self.seed)
        self._steps = 0

    def _pick(self) -> Call:
        r = self._rng
        p = self.ctx.persona
        w, h = self.ctx.image_size
        hand = r.choice(list(p.hands))
        spam = p.key == "speedrunner"
        roll = r.random()
        if roll < (0.45 if spam else 0.35):
            # Most UI sits in the middle of the view; aim there more often.
            x = min(w - 1, max(0, r.gauss(w * 0.5, w * 0.2)))
            y = min(h - 1, max(0, r.gauss(h * 0.58, h * 0.18)))
            return Call("click", {"x": round(x), "y": round(y), "hand": hand})
        if roll < 0.55:
            buttons = ["trigger", "grip", "a", "b"] if hand == "right" else ["trigger", "grip", "x", "y", "menu"]
            return Call("press", {"button": r.choice(buttons), "hand": hand,
                                  "hold_seconds": 0.05 if spam else r.choice([0.1, 0.1, 0.5])})
        if roll < 0.70:
            lim = min(p.max_turn_deg, 70.0)
            return Call("look", {"yaw": round(r.uniform(-lim, lim)), "pitch": round(r.uniform(-30, 20))})
        if roll < 0.80:
            return Call("stick", {"hand": hand, "x": round(r.uniform(-1, 1), 2), "y": round(r.uniform(-1, 1), 2),
                                  "seconds": round(r.uniform(0.2, 1.0), 1)})
        if roll < 0.88:
            return Call("wait", {"seconds": 0.5 if spam else r.choice([1.0, 2.0])})
        if roll < 0.93:
            return Call("point", {"x": round(r.uniform(0, w)), "y": round(r.uniform(0, h)), "hand": hand})
        if roll < 0.95:
            return Call("say", {"text": r.choice(MONKEY_LINES)})
        return Call("reach", {"hand": hand, "right": round(r.uniform(-0.3, 0.3), 2), "up": round(r.uniform(-0.5, 0.1), 2),
                              "forward": round(r.uniform(0.2, 0.6), 2), "grab": r.random() < 0.5})

    def first(self, obs: Observation) -> list[Call]:
        return self.next([], obs)

    def next(self, results: list[tuple[Call, str]], obs: Observation) -> list[Call]:
        self._steps += 1
        if obs.steps_left <= 0:
            return [Call("finish", {"goal_achieved": None, "rating": None,
                                    "summary": f"Random input for {self._steps - 1} actions (seed {self.seed})."})]
        return [self._pick()]

    def usage(self) -> dict:
        return {"seed": self.seed}


def make_brain(kind: str, seed: int = 0, model: str = "claude-opus-5-5", effort: str = "medium") -> Brain:
    if kind == "claude":
        try:
            return ClaudeBrain(model=model, effort=effort)
        except ImportError:
            from visor.xr.paths import install_hint

            raise ValueError(f"the Claude playtester needs the anthropic package: {install_hint('agent')}") from None
    if kind == "monkey":
        return MonkeyBrain(seed=seed)
    raise ValueError(f"unknown brain '{kind}' (claude | monkey)")


def describe_controls(actions: list[dict], limit: int = 40) -> str:
    """The app's bound actions as short lines, merged across legacy duplicates."""
    lines: dict[str, list[str]] = {}
    for a in actions:
        if not a.get("bindings") or a.get("type") in ("pose", "haptic"):
            continue
        for b in a["bindings"]:
            key = f"{b['hand']} {b['input']}" + (f"/{b['component']}" if b.get("component") else "")
            lines.setdefault(key, []).append(a["name"])
    out = []
    for key in sorted(lines):
        names = sorted(set(lines[key]), key=len)[:3]
        out.append(f"- {key}: {', '.join(names)}")
    return "\n".join(out[:limit])


def to_json(obj) -> str:
    return json.dumps(obj, default=str)
