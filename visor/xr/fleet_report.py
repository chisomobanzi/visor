"""Fleet summary page: every copy as a tile, findings merged across copies."""

from __future__ import annotations

import html
from pathlib import Path


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def write_fleet_html(summary: dict, out: Path) -> Path:
    cfg = summary.get("config", {})
    runs = summary.get("runs", [])
    tiles = []
    for r in runs:
        img = f'<img src="{_e(r["shot"])}" loading="lazy" alt="">' if r.get("shot") else '<div class="noimg"></div>'
        hz = r.get("refresh_hz") or 72
        fps_cls = "good" if r.get("fps", 0) >= hz * 0.95 else ("warn" if r.get("fps", 0) >= hz * 0.85 else "bad")
        link_open = f'<a href="{_e(r["report"])}">' if r.get("report") else "<div>"
        link_close = "</a>" if r.get("report") else "</div>"
        meta = []
        if r.get("persona"):
            meta.append(f"{_e(r['persona'])} · seed {r.get('seed', 0)}")
        if r.get("fps"):
            meta.append(f'<span class="{fps_cls}">{r["fps"]:.0f}/{hz:.0f} fps</span> · p99 {r.get("p99_ms", 0):.0f} ms')
        if r.get("issues"):
            meta.append(f"{r['issues']} issue{'s' if r['issues'] != 1 else ''}")
        if r.get("error"):
            meta.append(f'<span class="bad">{_e(r["error"][:80])}</span>')
        tiles.append(f"""
      <li class="tile {r['status']}">{link_open}{img}
        <div class="cap"><b>#{r['index']:02d}</b><span class="st {r['status']}">{_e(r['status'])}</span></div>
        <div class="meta">{'<br>'.join(meta)}</div>{link_close}</li>""")
    total = len(runs) or 1
    rows = []
    for m in summary.get("issues", []):
        share = len(m["runs"]) / total
        ex = f'<a href="{_e(m["example"])}"><img src="{_e(m["example"])}" alt=""></a>' if m.get("example") else ""
        rows.append(f"""
      <tr><td>{ex}</td><td><span class="pill {m['severity']}">{_e(m['severity'])}</span></td>
        <td><b>{_e(m['title'])}</b><div class="muted">{_e(m['category'])}</div></td>
        <td class="share"><div class="bar"><i style="width:{share * 100:.0f}%"></i></div>{len(m['runs'])}/{total} runs</td></tr>""")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet · {_e(cfg.get('target'))}</title>
<style>
:root {{ --bg:#101318; --surface:#171b22; --raised:#1d232d; --line:#2a303b; --ink:#e8eaf2; --muted:#8a93a6;
  --faint:#5a6272; --lens:#6bcbdf; --good:#2fa36b; --warn:#c98500; --bad:#e66767; --minor:#d0a54a;
  --mono: ui-monospace, "JetBrains Mono", "Fira Code", monospace; --body: "Inter", system-ui, sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--body); }}
main {{ max-width: 1240px; margin: 0 auto; padding: 40px 24px 80px; }}
a {{ color: inherit; text-decoration: none; }}
.eyebrow {{ color: var(--lens); font: 600 12px/1 var(--mono); letter-spacing: .14em; text-transform: uppercase; }}
h1 {{ font-size: 32px; margin: 10px 0 6px; }}
.muted {{ color: var(--muted); font-size: 13px; }}
.kpis {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 24px 0 36px; }}
.kpi {{ background: var(--surface); border:1px solid var(--line); border-radius: 12px; padding: 14px 16px; }}
.kpi .k {{ color: var(--muted); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
.kpi .v {{ font: 600 24px/1.3 var(--body); }}
h2 {{ font-size: 13px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin: 0 0 14px; }}
.grid {{ list-style: none; padding: 0; margin: 0 0 40px; display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; }}
.tile {{ background: var(--surface); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
.tile.crashed, .tile.failed {{ border-color: color-mix(in srgb, var(--bad) 55%, var(--line)); }}
.tile img, .tile .noimg {{ width: 100%; aspect-ratio: 1 / 1.05; object-fit: cover; display:block; background: var(--raised); }}
.cap {{ display:flex; justify-content: space-between; padding: 8px 10px 0; font: 13px var(--mono); }}
.st {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }}
.st.done {{ color: var(--good); }} .st.crashed, .st.failed {{ color: var(--bad); }} .st.running {{ color: var(--lens); }}
.meta {{ padding: 4px 10px 10px; font-size: 12.5px; color: var(--muted); }}
.good {{ color: var(--good); }} .warn {{ color: var(--warn); }} .bad {{ color: var(--bad); }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }}
td img {{ width: 96px; border-radius: 6px; display:block; }}
.pill {{ font: 700 11px/1 var(--mono); text-transform: uppercase; padding: 5px 8px; border-radius: 6px; }}
.pill.blocker {{ background: var(--bad); color: #fff; }} .pill.major {{ background: #e8a33a; color: #1b1406; }}
.pill.minor {{ background: var(--minor); color: #1b1406; }} .pill.note {{ background: #3a4252; }}
.share {{ width: 220px; font: 12.5px var(--mono); color: var(--muted); }}
.bar {{ height: 6px; background: var(--raised); border-radius: 3px; margin-bottom: 6px; overflow: hidden; }}
.bar i {{ display:block; height: 100%; background: var(--lens); }}
</style></head><body><main>
  <div class="eyebrow">Visor fleet · {_e(summary.get('name'))}</div>
  <h1>{_e(cfg.get('target'))}</h1>
  <div class="muted">{_e(cfg.get('mode'))} · {cfg.get('count')} copies · eye scale {cfg.get('scale')} · {_e(cfg.get('headset'))}</div>
  <div class="kpis">
    <div class="kpi"><div class="k">Completed</div><div class="v">{summary.get('ok')}/{len(runs)}</div></div>
    <div class="kpi"><div class="k">Crashed</div><div class="v {'bad' if summary.get('crashed') else ''}">{summary.get('crashed')}</div></div>
    <div class="kpi"><div class="k">Mean fps</div><div class="v">{summary.get('mean_fps')}</div></div>
    <div class="kpi"><div class="k">Distinct issues</div><div class="v">{len(summary.get('issues', []))}</div></div>
    <div class="kpi"><div class="k">Wall clock</div><div class="v">{summary.get('elapsed_s', 0) / 60:.1f} min</div></div>
  </div>
  <h2>Copies</h2><ul class="grid">{''.join(tiles)}</ul>
  <h2>Findings across the fleet</h2>
  {'<table>' + ''.join(rows) + '</table>' if rows else '<p class="muted">No issues reported.</p>'}
</main></body></html>"""
    out.write_text(page, encoding="utf-8")
    return out
