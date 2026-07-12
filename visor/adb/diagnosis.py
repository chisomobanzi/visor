"""Rule-based bottleneck diagnosis for performance sessions.

The VrApi stream contains enough signal to say *why* frames are missed,
not just that they were: CPU/GPU dynamic levels, utilization (average and
worst core), clocks, frame GPU time vs budget, thermal state, and free
memory. Each finding carries evidence and concrete recommendations so
the output is useful both to humans and to an AI assistant reasoning
about a session.
"""

from __future__ import annotations

from typing import Optional

from visor.adb.perf_session import PerfSession, summarize_session

# Verdict priority: physics first (throttling changes everything), then
# resource bounds, then unexplained hitches.
_VERDICT_PRIORITY = [
    "thermal_throttling",
    "memory_pressure",
    "gpu_bound",
    "cpu_bound",
    "hitching",
    "memory_growth",
]

_THROTTLED_STATUSES = {"SEVERE", "CRITICAL", "EMERGENCY", "SHUTDOWN"}

FREE_MEMORY_FLOOR_MB = 500
GRAPHICS_GROWTH_FLOOR_KB = 20_000

# Stale bursts in the first seconds are usually display-wake or app-load
# transients, not real performance problems
WARMUP_GRACE_S = 5.0


def _finding(kind, severity, summary, evidence, recommendations):
    return {
        "kind": kind,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
        "recommendations": recommendations,
    }


def diagnose_session(session: PerfSession) -> dict:
    """Analyze a recorded session and explain its performance.

    Returns {"verdict", "headline", "findings": [...]} where findings are
    ordered most-significant first.
    """
    summary = summarize_session(session)

    if not session.vrapi:
        return {
            "verdict": "no_vr_data",
            "headline": summary.get("note", "No VR frame data captured."),
            "findings": [],
        }

    fps = summary["fps"]
    stale = summary["stale_frames"]
    gpu = summary["gpu"]
    cpu = summary["cpu"]
    target = fps.get("target")
    budget_ms = round(1000 / target, 2) if target else None

    stale_pct = stale.get("percent_of_frames") or 0.0
    missed_frames = stale_pct > 1.0 or (
        target and fps["avg"] is not None and fps["avg"] < target - 2
    )
    burst_seconds = [
        s for s in session.vrapi
        if (s.get("stale_frames") or 0) >= 5
        and s.get("elapsed_s", 0) >= WARMUP_GRACE_S
    ]

    findings = []

    # ---- thermal throttling
    statuses = {t.get("status_label") for t in session.thermal}
    throttled = bool(statuses & _THROTTLED_STATUSES)
    clock_drop = _sustained_clock_drop(session.vrapi)
    if throttled or (clock_drop and missed_frames):
        evidence = []
        if throttled:
            evidence.append(
                f"thermal status reached {sorted(statuses & _THROTTLED_STATUSES)[0]}"
            )
        if temp := summary.get("thermal", {}).get("max_skin_temp_c"):
            evidence.append(f"max skin temperature {temp}°C")
        if clock_drop:
            evidence.append(
                f"GPU clock fell {clock_drop['drop_percent']}% "
                f"({clock_drop['from_mhz']} → {clock_drop['to_mhz']}MHz) "
                "without the level being lowered — the OS is clamping clocks"
            )
        findings.append(_finding(
            "thermal_throttling", "critical",
            "The headset is thermally throttling — clocks are being cut, "
            "so current frame rates don't reflect your app's real cost.",
            evidence,
            [
                "Reduce sustained GPU/CPU load — throttling is cumulative heat, "
                "not an instant threshold.",
                "Re-test from a cool start before trusting any numbers.",
                "If this happens in normal play sessions, target lower "
                "CPU/GPU levels or a lower refresh rate.",
            ],
        ))

    # ---- memory pressure
    free_min = (summary.get("free_memory_mb") or {}).get("min")
    lmkd = [e for e in session.events if e["kind"] == "lmkd_kill"]
    if (free_min is not None and free_min < FREE_MEMORY_FLOOR_MB) or lmkd:
        evidence = []
        if free_min is not None and free_min < FREE_MEMORY_FLOOR_MB:
            evidence.append(f"free memory bottomed out at {free_min}MB")
        evidence += [f"{e['detail']} at {e['elapsed_s']}s" for e in lmkd[:3]]
        killed_target = any(
            session.package and session.package in e["detail"] for e in lmkd
        )
        findings.append(_finding(
            "memory_pressure",
            "critical" if killed_target else "warning",
            "The system is under memory pressure"
            + (" and killed your app." if killed_target
               else " — background kills and hitches are likely."),
            evidence,
            [
                "Reduce resident memory: texture resolution, audio banks, "
                "preloaded assets.",
                "Check the memory anatomy — graphics allocations dominate "
                "VR apps (see quest_app_memory / the session memory series).",
            ],
        ))

    # ---- GPU bound
    p95_gpu = gpu.get("p95_util")
    if missed_frames and p95_gpu is not None and (
        p95_gpu >= 0.9 or ((gpu.get("avg_util") or 0) >= 0.85 and gpu["max_level"] >= 5)
    ):
        evidence = [
            f"GPU utilization avg {gpu['avg_util']}, p95 {p95_gpu}",
            f"GPU dynamic level reached {gpu['max_level']}/5"
            + (" (maxed — the OS has no more clock to give)"
               if gpu["max_level"] >= 5 else ""),
        ]
        if gpu.get("p95_app_gpu_ms") and budget_ms:
            pct = round(gpu["p95_app_gpu_ms"] / budget_ms * 100)
            evidence.append(
                f"app GPU time p95 {gpu['p95_app_gpu_ms']}ms of "
                f"{budget_ms}ms frame budget ({pct}%)"
            )
        findings.append(_finding(
            "gpu_bound", "critical" if stale_pct > 5 else "warning",
            f"The app is GPU-bound: rendering costs more than the "
            f"{target}Hz frame budget allows.",
            evidence,
            [
                "Reduce eye-buffer resolution or enable/increase fixed "
                "foveated rendering.",
                "Cut fragment cost: overdraw, transparency layers, expensive "
                "shaders, MSAA level.",
                "Consider Application SpaceWarp (render at half rate) if "
                "content suits it.",
                "Capture a GPU-focused trace (visor perf trace) to find the "
                "expensive passes.",
            ],
        ))

    # ---- CPU bound
    p95_worst = cpu.get("p95_worst_core")
    if missed_frames and p95_worst is not None and p95_worst >= 0.9:
        single_thread = (cpu.get("avg_util") or 0) <= p95_worst - 0.3
        summary_text = (
            "The app is CPU-bound on a single thread — one core is "
            "saturated while the rest idle."
            if single_thread else
            "The app is CPU-bound — cores are saturated."
        )
        findings.append(_finding(
            "cpu_bound", "critical" if stale_pct > 5 else "warning",
            summary_text,
            [
                f"worst-core utilization p95 {p95_worst}, average across "
                f"cores {cpu.get('avg_util')}",
                f"CPU dynamic level reached {cpu['max_level']}/5",
            ],
            [
                "Profile the hot thread (visor perf trace / simpleperf) — "
                "for engines it's usually the game or render thread.",
                "Move work off the hot thread: jobs/worker threads, async "
                "loading.",
                "If it's the render thread: batch draw calls, reduce "
                "per-object state changes.",
            ],
        ))

    # ---- unexplained hitches
    bound_kinds = {f["kind"] for f in findings}
    if burst_seconds and not (bound_kinds & {"gpu_bound", "cpu_bound",
                                             "thermal_throttling"}):
        moments = ", ".join(f"{s['elapsed_s']}s" for s in burst_seconds[:5])
        findings.append(_finding(
            "hitching", "warning",
            "Frames drop in short bursts (hitches) while CPU and GPU have "
            "headroom — something is blocking the frame loop.",
            [
                f"{len(burst_seconds)} stale-frame burst(s) at {moments}",
                f"yet GPU util p95 {p95_gpu} and worst-core p95 {p95_worst} "
                "are below saturation",
            ],
            [
                "Usual suspects: GC spikes, synchronous asset loads, shader "
                "compilation, main-thread I/O.",
                "Correlate the burst timestamps with your app's logs.",
                "Capture a trace around a hitch (visor perf trace) to see "
                "exactly what blocked.",
            ],
        ))

    # ---- memory growth (leak suspicion) — informational
    mem = summary.get("memory") or {}
    growth = mem.get("graphics_growth_kb") or 0
    start_graphics = None
    if session.memory:
        start_graphics = session.memory[0].get("graphics_kb")
    if growth > GRAPHICS_GROWTH_FLOOR_KB and start_graphics and \
            growth > 0.1 * start_graphics:
        findings.append(_finding(
            "memory_growth", "info",
            f"Graphics memory grew {growth // 1024}MB during the session "
            "without leveling off — possible leak or unbounded streaming.",
            [
                f"graphics PSS {start_graphics // 1024}MB → "
                f"{(start_graphics + growth) // 1024}MB over "
                f"{session.duration_s}s",
            ],
            [
                "Record a longer session — leaks keep climbing, streaming "
                "systems plateau.",
                "Audit texture/render-target/buffer lifetime.",
            ],
        ))

    findings.sort(key=lambda f: _VERDICT_PRIORITY.index(f["kind"]))
    verdict = findings[0]["kind"] if findings else "healthy"

    if verdict == "healthy":
        headline = (
            f"Healthy: averaging {fps['avg']} fps against a {target}Hz "
            f"target with {stale_pct}% stale frames."
            if target else
            f"Healthy: averaging {fps['avg']} fps with {stale_pct}% stale frames."
        )
    else:
        headline = findings[0]["summary"]

    result = {"verdict": verdict, "headline": headline, "findings": findings}
    if len(session.vrapi) < 10:
        result["note"] = (
            f"Only {len(session.vrapi)} seconds of VR metrics — results may "
            "be dominated by app launch/wake transients. Record 30s+ of "
            "actual gameplay for a trustworthy diagnosis."
        )
    return result


def _sustained_clock_drop(vrapi: list) -> Optional[dict]:
    """Detect the OS clamping GPU clocks: frequency falls meaningfully
    between the first and last thirds without the dynamic level dropping."""
    clocked = [s for s in vrapi
               if s.get("gpu_mhz") and s.get("gpu_level") is not None]
    if len(clocked) < 6:
        return None
    third = len(clocked) // 3
    head, tail = clocked[:third], clocked[-third:]
    head_mhz = sum(s["gpu_mhz"] for s in head) / len(head)
    tail_mhz = sum(s["gpu_mhz"] for s in tail) / len(tail)
    head_level = max(s["gpu_level"] for s in head)
    tail_level = max(s["gpu_level"] for s in tail)
    if tail_level >= head_level and tail_mhz < head_mhz * 0.85:
        return {
            "from_mhz": round(head_mhz),
            "to_mhz": round(tail_mhz),
            "drop_percent": round((1 - tail_mhz / head_mhz) * 100),
        }
    return None


# ------------------------------------------------------------------ diff


def diff_sessions(before: PerfSession, after: PerfSession) -> dict:
    """Compare two sessions (e.g. before/after an optimization)."""
    s_before, s_after = summarize_session(before), summarize_session(after)
    d_before, d_after = diagnose_session(before), diagnose_session(after)

    def metric(path, digits=2):
        def dig(summary):
            node = summary
            for key in path:
                node = (node or {}).get(key)
                if node is None:
                    return None
            return node
        b, a = dig(s_before), dig(s_after)
        delta = round(a - b, digits) if a is not None and b is not None else None
        return {"before": b, "after": a, "delta": delta}

    diff = {
        "fps_avg": metric(("fps", "avg")),
        "fps_p5": metric(("fps", "p5")),
        "stale_percent": metric(("stale_frames", "percent_of_frames")),
        "gpu_util_avg": metric(("gpu", "avg_util"), 3),
        "app_gpu_p95_ms": metric(("gpu", "p95_app_gpu_ms")),
        "cpu_worst_core_p95": metric(("cpu", "p95_worst_core"), 3),
        "graphics_growth_kb": metric(("memory", "graphics_growth_kb"), 0),
        "verdict": {"before": d_before["verdict"], "after": d_after["verdict"]},
    }

    fps_delta = diff["fps_avg"]["delta"] or 0
    stale_delta = diff["stale_percent"]["delta"] or 0
    if fps_delta > 1 or stale_delta < -0.5:
        trend = "improved"
    elif fps_delta < -1 or stale_delta > 0.5:
        trend = "regressed"
    else:
        trend = "unchanged"
    diff["headline"] = (
        f"Performance {trend}: {diff['fps_avg']['before']} → "
        f"{diff['fps_avg']['after']} avg fps, "
        f"{diff['stale_percent']['before']}% → "
        f"{diff['stale_percent']['after']}% stale frames "
        f"(verdict {diff['verdict']['before']} → {diff['verdict']['after']})."
    )
    return diff
