"""MCP tools for a playtest with the calling agent as the player.

`visor sim playtest` asks Claude through the API, which needs an Anthropic
key. These tools give the same seat to whoever calls them (Claude Code on its
own plan, say). Visor boots the app, hands over a persona and a goal, shows
each frame, plays out each move with the oracles watching, and writes the same
report: issues with screenshots, a replay script, video, frame trace, crash log.
"""

from __future__ import annotations

import io
import json
import threading
from typing import Optional

from mcp.server.mcpserver import Context, Image

from visor.mcp.server import QuestError, mcp
from visor.xr.playtest.brains import ACTION_TOOLS, TOOLS, AgentBrain, Call, system_prompt, tool_guide

WAIT = 240.0  # longest a tool waits on the app before handing back; xr_playtest_look waits on
_runs: dict[str, dict] = {}  # name -> {test, brain, thread, status, note, awaiting, report, error}
_lock = threading.Lock()
_REQUIRED = {t["name"]: t["input_schema"].get("required", []) for t in TOOLS}


def _run(name: Optional[str]) -> dict:
    with _lock:
        if name:
            if name not in _runs:
                raise QuestError(f"no playtest '{name}' in this session (have: {', '.join(_runs) or 'none'})")
            return _runs[name]
        live = [r for r in _runs.values() if r["status"] != "done"]
        if len(live) == 1:
            return live[0]
        if not live:
            raise QuestError("no playtest running: start one with xr_playtest_start")
        raise QuestError(f"several playtests are running; name one: {', '.join(r['test'].name for r in live)}")


def _ended(run: dict) -> dict:
    test, report = run["test"], run.get("report") or {}
    if run.get("error") and not report:
        raise QuestError(f"the playtest failed: {run['error']}")
    return {
        "playtest": test.name,
        "verdict": report.get("verdict"),
        "stopped_for": report.get("stopped_for"),
        "issues": [{"id": i["id"], "severity": i["severity"], "title": i["title"]} for i in report.get("issues", [])],
        "report": str(test.dir / "report.html"),
        "video": str(test.dir / "video.mp4") if (report.get("artifacts") or {}).get("video") else None,
        "next": "open the report; repro.sh in the same folder replays the run",
    }


def _client_name(ctx: Optional[Context]) -> str:
    """Who's playing, as the MCP client introduced itself (claude-code, say)."""
    try:
        return ctx.session.client_params.client_info.name or ""  # type: ignore[union-attr]
    except AttributeError:
        return ""


def _done(run: dict) -> list:
    """The end of a playtest, as tool content (one tool can answer with a frame or with this)."""
    return [json.dumps(_ended(run), indent=1)]


def _show(run: dict, view) -> list:
    obs, results = view
    # The last move's result opens the observation, so it isn't repeated.
    lines = [f"{c.name}: {text}" for c, text in results[:-1]]
    lines.append(f"{results[-1][0].name}: {obs.text}" if results else obs.text)
    buf = io.BytesIO()
    obs.image.convert("RGB").save(buf, format="JPEG", quality=85)
    w, h = obs.image.size
    note = "\n".join(lines + [f"(playtest {run['test'].name}; this image is {w}x{h}: click and point "
                                        "use its pixel coordinates)"])
    out: list = []
    if not run.get("briefed"):  # the first frame comes with the briefing, however late
        run["briefed"] = True
        out.append(system_prompt(run["brain"].ctx) + f"""
# Playing through these tools
Make your moves with xr_playtest_act(moves=[...]): each move is the tool name plus its arguments, like
{{"tool": "click", "x": 412, "y": 610}}. Several moves per call are fine (report_issue, then an action);
put what you're thinking in `thought`, it goes in the report. Each call answers with the next frame.
If the app is busy (a level loading) the call hands back early: xr_playtest_look waits on.

Moves (? = optional):
{tool_guide()}
""")
    run["awaiting"] = True
    return out + [Image(data=buf.getvalue(), format="jpeg"), note]


def _next(run: dict, wait: float) -> list:
    view = run["brain"].view(timeout=max(1.0, float(wait)))
    if view is not None:
        return _show(run, view)
    if run["status"] == "done":
        return _done(run)
    return [f"Still busy: {run.get('note') or 'starting the app'}. Call xr_playtest_look to keep waiting "
            f"(playtest {run['test'].name})."]


@mcp.tool()
def xr_playtest_start(target: str, persona: str = "first-timer", goal: Optional[str] = None, steps: int = 40,
                      map: Optional[str] = None, wait_seconds: float = WAIT,
                      ctx: Optional[Context] = None) -> list:
    """Playtest a VR app yourself, in character, with Visor keeping score.
    No Anthropic key needed: you are the player. Visor boots the app in the
    virtual headset (a .uproject, a Linux build, an OpenXR executable or
    "demo"), gives you a persona (first-timer, speedrunner, explorer, seated,
    one-handed, soft-spoken...) and a goal, and returns your briefing and the
    first frame. Then loop xr_playtest_act. At the end Visor writes the
    report: your issues and the oracles' (freezes, crashes, frame drops, log
    errors, audio), a replay script, video and frame trace. Unreal projects
    can take a few minutes to boot: if this hands back early, call
    xr_playtest_look."""
    from visor.xr.playtest.session import DEFAULT_GOAL, Playtest, PlaytestConfig

    run: dict = {"status": "starting", "note": "", "awaiting": False, "report": None, "error": None}

    def on_event(kind: str, data: dict) -> None:
        if kind in ("status", "error"):
            run["note"] = data.get("message", "")

    brain = AgentBrain(client=_client_name(ctx))
    cfg = PlaytestConfig(target=target, persona=persona, goal=goal or DEFAULT_GOAL, brain="agent",
                         max_steps=max(1, int(steps)), map=map)
    try:
        test = Playtest(cfg, on_event=on_event, brain=brain)
    except ValueError as e:
        raise QuestError(str(e)) from None
    run.update(test=test, brain=brain)

    def play() -> None:
        try:
            run["report"] = test.run()
        except Exception as e:  # the report says what it can; the agent hears the rest
            run["error"] = str(e)
        finally:
            run["status"] = "done"
            brain._views.put(None)  # wake an agent waiting for a frame that won't come

    with _lock:
        _runs[test.name] = run
    run["thread"] = threading.Thread(target=play, name=f"playtest-{test.name}", daemon=True)
    run["thread"].start()
    return _next(run, wait_seconds)


@mcp.tool()
def xr_playtest_act(moves: list[dict], thought: str = "", playtest: Optional[str] = None,
                    wait_seconds: float = WAIT) -> list:
    """Your next move(s) in an xr_playtest_start playtest, e.g.
    moves=[{"tool": "click", "x": 412, "y": 610}] or
    [{"tool": "report_issue", "title": "...", "severity": "minor", "category": "usability",
    "expected": "...", "actual": "..."}, {"tool": "wait", "seconds": 3}].
    Answers with the next frame, what each move did, and what the app said.
    {"tool": "finish", "goal_achieved": ..., "summary": "...", "rating": 1-10}
    ends the playtest and returns the report."""
    run = _run(playtest)
    if run["status"] == "done":
        return _done(run)
    if not run["awaiting"]:
        raise QuestError("the app is still playing out your last move: xr_playtest_look waits for it")
    calls = []
    for i, move in enumerate(moves):
        move = dict(move)
        name = move.pop("tool", None) or move.pop("name", None)
        if isinstance(move.get("args"), dict):  # {"name": ..., "args": {...}} reads naturally too
            move = move["args"]
        if name not in ACTION_TOOLS | {"report_issue", "finish"}:
            raise QuestError(f"unknown move {name!r}; moves are {', '.join(t['name'] for t in TOOLS)}")
        missing = [a for a in _REQUIRED[name] if a not in move]
        if missing:
            raise QuestError(f"{name} needs {', '.join(missing)}")
        calls.append(Call(name, move, id=f"m{run['brain'].moves}.{i}", thought=thought))
    if not calls:
        raise QuestError("no moves: pass at least one, or finish")
    run["awaiting"] = False
    run["brain"].move(calls)
    if any(c.name == "finish" for c in calls):
        run["thread"].join(timeout=180)  # Visor measures the end state and writes the report
        if run["status"] == "done":
            return _done(run)
    return _next(run, wait_seconds)


@mcp.tool()
def xr_playtest_look(playtest: Optional[str] = None, wait_seconds: float = WAIT) -> list:
    """See the current frame of a playtest again, or wait for the app when a
    move handed back early (a level still loading, the app still booting)."""
    run = _run(playtest)
    if run["status"] == "done":
        return _done(run)
    if run["awaiting"] and run["brain"].last is not None:
        return _show(run, run["brain"].last)
    return _next(run, wait_seconds)


@mcp.tool()
def xr_playtest_stop(playtest: Optional[str] = None) -> dict:
    """Call a playtest off early. Visor still stops the app and writes the
    report with everything found so far."""
    run = _run(playtest)
    if run["status"] != "done":
        run["brain"].stop()
        run["thread"].join(timeout=180)
    return _ended(run)
