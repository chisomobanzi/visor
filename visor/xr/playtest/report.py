"""Render a playtest report as one self-contained HTML page.

The page sits next to its artefacts (shots/, video.mp4, repro.sh), so the run
directory can be zipped, attached to a ticket, or served as-is.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

SEV_ORDER = ("blocker", "major", "minor", "note")


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _audio(src: Optional[str]) -> str:
    return f'<audio controls preload="none" src="{_e(src)}"></audio>' if src else ""


def _frame_chart(frames_path: Path, refresh_hz: float, paused: Optional[list] = None) -> str:
    """Frame-interval sparkline (ms) of the real-time stretches of the run."""
    try:
        data = json.loads(frames_path.read_text())
    except (OSError, ValueError):
        return ""
    rows = data.get("frame_rows") or []
    lo, hi = (list(paused or []) + [None, None])[:2]
    intervals = []
    prev = None
    for row in rows:
        frame, end = row[0], row[4]
        stepped = lo is not None and hi is not None and lo <= frame <= hi + 1
        if not end or stepped:
            prev = None
            continue
        if prev is not None and end > prev:
            intervals.append((end - prev) / 1e6)
        prev = end
    if len(intervals) < 10:
        return ""
    budget = 1000.0 / (refresh_hz or 72.0)
    w, h = 900, 140
    top = max(budget * 3, min(max(intervals), budget * 6))
    step = w / max(1, len(intervals) - 1)
    pts = " ".join(f"{i * step:.1f},{h - min(v, top) / top * h:.1f}" for i, v in enumerate(intervals))
    by = h - budget / top * h
    over = sum(1 for v in intervals if v > budget * 1.5)
    return f"""
    <figure class="chart">
      <svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="frame times">
        <line x1="0" x2="{w}" y1="{by:.1f}" y2="{by:.1f}" class="budget"/>
        <polyline points="{pts}" class="series"/>
      </svg>
      <figcaption><span>{len(intervals)} real-time frames · budget {budget:.1f} ms (dashed) · {over} over 1.5× budget</span>
      <span>top of chart {top:.0f} ms</span></figcaption>
    </figure>"""


def _perf_row(label: str, s: dict) -> str:
    if not s:
        return ""
    fps, hz = s.get("fps", 0), s.get("refresh_hz", 0)
    cls = "good" if hz and fps >= hz * 0.95 else ("warn" if hz and fps >= hz * 0.85 else "bad")
    return (f"<tr><td>{_e(label)}</td><td class='{cls}'>{fps:.1f} / {hz:.0f}</td>"
            f"<td>{s.get('frame_ms_p50', 0):.1f}</td><td>{s.get('frame_ms_p95', 0):.1f}</td>"
            f"<td>{s.get('frame_ms_p99', 0):.1f}</td><td>{s.get('cpu_ms_p50', 0):.1f}</td>"
            f"<td>{int(s.get('missed_frames', 0))}</td></tr>")


def write_html(report: dict, out: Path) -> Path:
    persona = report.get("persona", {})
    verdict = report.get("verdict") or {}
    issues = report.get("issues", [])
    steps = report.get("steps", [])
    perf = report.get("perf", {})
    brain = report.get("brain", {})
    arts = report.get("artifacts", {})
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in SEV_ORDER}
    achieved = verdict.get("goal_achieved")
    rating = verdict.get("rating")
    refresh = (perf.get("start") or {}).get("refresh_hz", 72.0)

    chips = "".join(f'<span class="chip {s}">{counts[s]} {s}</span>' for s in SEV_ORDER if counts[s]) \
        or '<span class="chip good">no issues</span>'

    issue_cards = []
    for i in issues:
        shot = (f'<a class="shot" href="{_e(i["screenshot"])}" target="_blank">'
                f'<img src="{_e(i["screenshot"])}" loading="lazy" alt="frame {i["frame"]}"></a>') if i.get("screenshot") else ""
        repro = "".join(f"<li><code>{_e(s)}</code></li>" for s in i.get("steps_to_reproduce", []))
        source = i.get("source", "")
        who = "found by the oracles" if source.startswith("oracle") else f"reported by {persona.get('name', 'the agent')}"
        issue_cards.append(f"""
      <article class="issue {i['severity']}" id="{_e(i['id'])}">
        {shot}
        <div class="body">
          <header><span class="pill {i['severity']}">{_e(i['severity'])}</span>
            <span class="cat">{_e(i['category'])}</span><span class="id">{_e(i['id'])}</span></header>
          <h3>{_e(i['title'])}</h3>
          <p class="desc">{_e(i['description'])}</p>
          <footer>{_e(who)} · step {i['step']} · frame {i['frame']}</footer>
          {f'<details><summary>Last actions before this</summary><ol>{repro}</ol></details>' if repro else ''}
        </div>
      </article>""")

    film = []
    for s in steps:
        thumb = f'<img src="{_e(s["screenshot"])}" loading="lazy" alt="">' if s.get("screenshot") else '<div class="noimg"></div>'
        thought = f'<p class="thought">“{_e(s["thought"][:320])}”</p>' if s.get("thought") else ""
        said = (f'<p class="said"><span>🗣</span> “{_e(s["args"].get("text", ""))}”'
                f'<audio controls preload="none" src="{_e(s["audio"])}"></audio></p>') if s.get("audio") else ""
        heard = (f'<p class="heard"><span>🔊</span> {_e(s["heard"])}'
                 f'{_audio(s.get("heard_audio"))}</p>') if s.get("heard") else ""
        film.append(f"""
        <li><a href="{_e(s.get('screenshot') or '#')}" target="_blank">{thumb}</a>
          <div><b>{s['step']}. {_e(s['action'])}</b> <code>{_e(json.dumps(s['args']))}</code>
          {said}<p>{_e(s['result'])}</p>{heard}{thought}</div></li>""")

    # Everything the app said, as a script.
    script = []
    for h in report.get("heard") or []:
        if h.get("said"):
            lang = f' <span class="lang">{_e(h["language"])}</span>' if h.get("language") not in (None, "en") else ""
            script.append(f'<li><span class="when">step {h["step"]}</span><q>{_e(h["said"])}</q>{lang}'
                          f'{_audio(h.get("audio"))}</li>')
    heard_html = (f'<section><h2>What the app said</h2><ol class="script">{"".join(script)}</ol></section>'
                  if script else "")

    crash = report.get("crash")
    crash_html = ""
    if crash:
        stack = "\n".join(crash.get("callstack", [])[:40])
        crash_html = f"""
    <section><h2>Crash</h2><div class="crash"><p>{_e(crash.get('message'))}</p>
      {f'<pre>{_e(stack)}</pre>' if stack else ''}
      {f'<p class="muted">Engine crash report: <code>{_e(crash.get("report_dir"))}</code></p>' if crash.get('report_dir') else ''}</div></section>"""

    cost = brain.get("estimated_cost_usd")
    brain_line = (f"{_e(brain.get('model') or brain.get('kind'))} · {brain.get('requests', 0)} requests"
                  f"{f' · ≈ ${cost:.2f}' if cost is not None else ''}") if brain.get("kind") == "claude" \
        else f"scripted monkey · seed {brain.get('seed', 0)}"
    no_video = ("No video: recording was off for this run." if (report.get("config") or {}).get("video") is False
                else "No video (ffmpeg not found).")
    video = f'<video controls preload="metadata" src="{_e(arts["video"])}"></video>' if arts.get("video") else \
        f'<p class="muted">{no_video}</p>'
    chart = _frame_chart(out.parent / arts["frames"], refresh, report.get("paused_frames")) if arts.get("frames") else ""
    verdict_cls = "good" if achieved else ("bad" if achieved is False else "")
    scripted = brain.get("kind") != "claude"
    verdict_txt = "Goal reached" if achieved else ("Goal not reached" if achieved is False else
                                                   ("Scripted run" if scripted else "No verdict"))
    if report.get("stopped_for") and not achieved:  # cut short: the agent never got to judge
        verdict_cls, verdict_txt = "", "Stopped early"
    highlights = "".join(f"<li>{_e(h)}</li>" for h in verdict.get("highlights") or [])
    frustrations = "".join(f"<li>{_e(h)}</li>" for h in verdict.get("frustrations") or [])

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playtest · {_e(report['app'].get('name'))} · {_e(persona.get('name'))}</title>
<style>
:root {{
  color-scheme: dark;  /* native controls (the audio players) match the page */
  --bg:#101318; --surface:#171b22; --raised:#1d232d; --line:#2a303b;
  --ink:#e8eaf2; --muted:#8a93a6; --faint:#5a6272;
  --lens:#6bcbdf; --good:#2fa36b; --warn:#c98500; --bad:#e66767; --minor:#d0a54a; --note:#7f8ba3;
  --mono: ui-monospace, "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
  --body: "Inter", system-ui, -apple-system, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 var(--body); }}
main {{ max-width: 1120px; margin: 0 auto; padding: 40px 24px 80px; }}
a {{ color: var(--lens); }}
code, pre {{ font-family: var(--mono); font-size: 12.5px; }}
.muted {{ color: var(--muted); }}
.hero {{ display:grid; grid-template-columns: 1fr auto; gap: 24px; align-items: end;
  padding-bottom: 28px; border-bottom: 1px solid var(--line); }}
.eyebrow {{ color: var(--lens); font: 600 12px/1 var(--mono); letter-spacing: .14em; text-transform: uppercase; }}
h1 {{ font-size: 34px; line-height: 1.15; margin: 10px 0 8px; letter-spacing: -.01em; }}
.persona {{ display:inline-flex; gap:10px; align-items:center; color: var(--muted); }}
.persona b {{ color: var(--ink); }}
.verdict {{ text-align:right; }}
.verdict .big {{ font: 600 40px/1 var(--body); }}
.verdict .big small {{ font-size: 18px; color: var(--muted); }}
.badge {{ display:inline-block; margin-top:10px; padding: 5px 12px; border-radius: 999px; font-weight:600; font-size:13px;
  border: 1px solid var(--line); background: var(--surface); }}
.badge.good {{ color: var(--good); border-color: color-mix(in srgb, var(--good) 45%, transparent); }}
.badge.bad {{ color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }}
blockquote {{ margin: 22px 0 0; padding: 14px 18px; background: var(--surface); border-left: 3px solid var(--lens);
  border-radius: 0 10px 10px 0; color: #cfd4e0; }}
.stats {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 26px 0 8px; }}
.stat {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }}
.stat .k {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
.stat .v {{ font: 600 22px/1.3 var(--body); margin-top: 4px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; margin-top: 6px; }}
.chip {{ font: 600 12px/1 var(--mono); padding: 6px 9px; border-radius: 7px; background: var(--raised); }}
.chip.blocker, .pill.blocker {{ color:#fff; background: var(--bad); }}
.chip.major, .pill.major {{ color:#1b1406; background: #e8a33a; }}
.chip.minor, .pill.minor {{ color:#1b1406; background: var(--minor); }}
.chip.note, .pill.note {{ color: var(--ink); background: #3a4252; }}
.chip.good {{ color: var(--good); }}
section {{ margin-top: 42px; }}
h2 {{ font-size: 13px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin: 0 0 14px; }}
.issue {{ display:grid; grid-template-columns: 300px 1fr; gap: 18px; background: var(--surface);
  border: 1px solid var(--line); border-radius: 14px; padding: 14px; margin-bottom: 12px; }}
.issue.blocker {{ border-color: color-mix(in srgb, var(--bad) 60%, var(--line)); }}
.issue .shot img {{ width: 100%; border-radius: 9px; display:block; border: 1px solid var(--line); }}
.issue:not(:has(.shot)) {{ grid-template-columns: 1fr; }}
.issue header {{ display:flex; gap:8px; align-items:center; }}
.pill {{ font: 700 11px/1 var(--mono); text-transform: uppercase; letter-spacing: .06em; padding: 5px 8px; border-radius: 6px; }}
.cat {{ color: var(--muted); font-size: 13px; }}
.id {{ margin-left:auto; color: var(--faint); font: 12px var(--mono); }}
.issue h3 {{ margin: 10px 0 6px; font-size: 17px; line-height: 1.35; }}
.desc {{ white-space: pre-wrap; color: #c9cfdb; margin: 0 0 10px; }}
.issue footer {{ color: var(--faint); font-size: 12.5px; }}
details {{ margin-top: 8px; color: var(--muted); }}
details ol {{ margin: 6px 0 0; padding-left: 20px; }}
video {{ display:block; max-width: 100%; max-height: 62vh; margin: 0 auto; border-radius: 14px;
  border: 1px solid var(--line); background: #000; }}
.chart {{ margin: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 14px; }}
.chart svg {{ width: 100%; height: 140px; display: block; }}
.chart .series {{ fill: none; stroke: var(--lens); stroke-width: 1.4; vector-effect: non-scaling-stroke; }}
.chart .budget {{ stroke: var(--warn); stroke-dasharray: 5 5; stroke-width: 1; vector-effect: non-scaling-stroke; }}
.chart figcaption {{ display:flex; justify-content: space-between; color: var(--muted); font-size: 12.5px; margin-top: 8px; }}
table {{ width:100%; border-collapse: collapse; margin-top: 12px; font: 13px var(--mono); }}
th, td {{ text-align:left; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
th {{ color: var(--muted); font-weight: 500; }}
td.good {{ color: var(--good); }} td.warn {{ color: var(--warn); }} td.bad {{ color: var(--bad); }}
.film {{ list-style:none; padding:0; margin:0; display:grid; gap: 10px; }}
.film li {{ display:grid; grid-template-columns: 160px 1fr; gap: 14px; background: var(--surface);
  border:1px solid var(--line); border-radius: 12px; padding: 10px; }}
.film img, .film .noimg {{ width: 160px; aspect-ratio: 1/1.05; object-fit: cover; border-radius: 8px; background: var(--raised); }}
.film p {{ margin: 4px 0 0; color: #c9cfdb; }}
.film .thought {{ color: var(--muted); font-style: italic; }}
.film .said {{ display:flex; flex-wrap: wrap; align-items:center; gap: 6px 12px; color: var(--ink); font-size: 15px; }}
.film audio, .script audio {{ height: 30px; max-width: 100%; }}
.film .heard {{ display:flex; flex-wrap: wrap; align-items:center; gap: 6px 12px; color: var(--lens); }}
.script {{ list-style:none; padding:0; margin:0; display:grid; gap: 8px; }}
.script li {{ display:flex; flex-wrap: wrap; align-items:center; gap: 6px 14px; background: var(--surface);
  border:1px solid var(--line); border-radius: 12px; padding: 10px 14px; }}
.script .when {{ font: 12px var(--mono); color: var(--muted); min-width: 52px; }}
.script q {{ flex: 1 1 320px; font-size: 15px; }}
.script .lang {{ font: 11px var(--mono); color: var(--lens); border: 1px solid var(--line); border-radius: 5px; padding: 1px 6px; }}
.crash {{ background: color-mix(in srgb, var(--bad) 10%, var(--surface)); border: 1px solid color-mix(in srgb, var(--bad) 45%, var(--line));
  border-radius: 14px; padding: 16px; }}
.crash pre {{ overflow:auto; max-height: 360px; background: #0c0f13; padding: 12px; border-radius: 9px; }}
.repro pre {{ background: #0c0f13; border: 1px solid var(--line); padding: 14px 16px; border-radius: 12px; overflow:auto; }}
.cols {{ display:grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
.cols ul {{ margin: 6px 0 0; padding-left: 18px; color: #c9cfdb; }}
footer.page {{ margin-top: 54px; color: var(--faint); font-size: 12.5px; }}
@media (max-width: 760px) {{
  .hero {{ grid-template-columns: 1fr; }} .verdict {{ text-align:left; }}
  .issue, .film li, .cols {{ grid-template-columns: 1fr; }}
  .film img, .film .noimg {{ width: 100%; }}
}}
</style></head>
<body><main>
  <div class="hero">
    <div>
      <div class="eyebrow">Visor playtest · {_e(report.get('created'))}</div>
      <h1>{_e(report['app'].get('name'))}</h1>
      <div class="persona"><b>{_e(persona.get('name'))}</b> — {_e(persona.get('tagline'))}</div>
    </div>
    <div class="verdict">
      <div class="big">{_e(rating) if rating is not None else '—'}<small>/10</small></div>
      <div class="badge {verdict_cls}">{verdict_txt}</div>
    </div>
  </div>
  {f'<blockquote>{_e(verdict.get("summary"))}</blockquote>' if verdict.get('summary') else ''}
  <div class="stats">
    <div class="stat"><div class="k">Issues</div><div class="chips">{chips}</div></div>
    <div class="stat"><div class="k">Actions</div><div class="v">{len(steps)}</div></div>
    <div class="stat"><div class="k">Duration</div><div class="v">{report.get('duration_s', 0) / 60:.1f} min</div></div>
    <div class="stat"><div class="k">Frame rate</div><div class="v">{(perf.get('start') or {}).get('fps', 0):.0f}<small class="muted"> / {refresh:.0f}</small></div></div>
    <div class="stat"><div class="k">Played by</div><div class="v" style="font-size:14px">{brain_line}</div></div>
  </div>
  {f'<div class="cols"><div><h2>Highlights</h2><ul>{highlights}</ul></div><div><h2>Frustrations</h2><ul>{frustrations}</ul></div></div>' if highlights or frustrations else ''}
  <section><h2>Goal</h2><p>{_e(report.get('goal'))}</p></section>
  <section><h2>Issues</h2>{''.join(issue_cards) or '<p class="muted">Nothing found.</p>'}</section>
  {crash_html}
  <section><h2>Reproduce</h2><div class="repro">
    <p class="muted">Launches the app in Visor's virtual headset frozen at its first frame, then plays back every input from this run, frame for frame{' (fixed timestep, so it plays out the same way)' if (report.get('config') or {}).get('deterministic') else ''}.</p>
    <pre>cd {_e(out.parent)}
./repro.sh</pre></div></section>
  <section><h2>Recording</h2>{video}</section>
  <section><h2>Performance</h2>{chart}
    <table><thead><tr><th>window</th><th>fps</th><th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>cpu ms</th><th>missed</th></tr></thead>
    <tbody>{_perf_row('idle at start', perf.get('start'))}{_perf_row('after playing', perf.get('end'))}</tbody></table>
  </section>
  {heard_html}
  <section><h2>Every action</h2><ol class="film">{''.join(film)}</ol></section>
  <footer class="page">{_e(report.get('name'))} · {_e(report['app'].get('runtime'))} · {_e(report['app'].get('interaction_profile'))}
    · <a href="report.json">report.json</a> · <a href="app.log">app.log</a></footer>
</main></body></html>"""
    out.write_text(page, encoding="utf-8")  # agent text is any Unicode; Windows defaults to cp1252
    return out
