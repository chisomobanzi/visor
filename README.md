# Visor

[![CI](https://github.com/chisomobanzi/visor/actions/workflows/ci.yml/badge.svg)](https://github.com/chisomobanzi/visor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/visor-dev)](https://pypi.org/project/visor-dev/)
[![Python](https://img.shields.io/pypi/pyversions/visor-dev)](https://pypi.org/project/visor-dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Cross-platform Quest developer toolkit** — an open source alternative to Meta Quest Developer Hub (MQDH) that runs on Linux, macOS, and Windows (MQDH is Windows/Mac only, so Linux users had nothing), plus something MQDH doesn't have on any platform: an MCP server that lets AI coding agents — Claude Code and any other MCP-compatible client — see and control your headset.

Four layers, one library:

| Layer | Command | What it does |
|-------|---------|--------------|
| Python library | `import visor.adb` | Quest-aware ADB wrapper: devices, apps, capture, logs, performance |
| MCP server | `visor-mcp` | 29 `quest_*` tools for any MCP client — screenshots the model can *see*, crash logs, perf monitoring |
| CLI | `visor` | MQDH-like functionality from the terminal |
| Web dashboard | `visor ui` | Live instrument panel: perf charts, viewport, logs, app/file management |

## Requirements

- Python 3.10+ on Linux, macOS, or Windows
- `adb` on your PATH:
  - Linux: `sudo apt install android-sdk-platform-tools` (or `sudo pacman -S android-tools`)
  - macOS: `brew install android-platform-tools`
  - Windows: `winget install --id Google.PlatformTools` (or [download platform-tools](https://developer.android.com/tools/releases/platform-tools))
- A Quest with [developer mode enabled](https://developers.meta.com/horizon/documentation/native/android/mobile-device-setup/), connected via USB (accept the debugging prompt in the headset) or wireless ADB
- Optional: `ffmpeg` for the dashboard's live video view, `scrcpy` for casting (both available via the same package managers)

## Install

```bash
pip install visor-dev    # provides the `visor` command; from source:
git clone https://github.com/chisomobanzi/visor && cd visor && pip install .
```

## CLI quickstart

```bash
visor devices                     # list connected headsets
visor info                        # battery, storage, firmware, thermal
visor connect-wireless            # unplug the cable, stay connected

visor install ./my-game.apk       # sideload a build
visor launch com.my.game
visor apps                        # sideloaded apps

visor screenshot                  # auto-wakes a sleeping headset
visor screenshot --clean          # de-fisheyed single eye (rectangular; needs ffmpeg)
visor record 30                   # 30s video
visor cast                        # scrcpy casting

visor logs --tag Unity --level D  # filtered logcat
visor logs --grep NullReference -f  # follow live
visor crash-logs com.my.game      # parsed crash/ANR reports

visor perf                        # CPU/memory/thermal/battery snapshot
visor perf --monitor 30           # timeseries
visor thermal                     # per-sensor temps with throttle warnings

# Profiling (see "Performance profiling" below)
visor perf record -t 60           # record a session while you play
visor perf report session.json    # summary + bottleneck diagnosis
visor perf diff before.json after.json
visor bench com.my.game           # cold-start benchmark
visor trace -t 10                 # perfetto trace -> ui.perfetto.dev

visor push ./file /sdcard/  ·  visor pull /sdcard/f ./  ·  visor ls /sdcard/
```

Multiple devices? Add `--device <serial>`.

## AI assistant integration (MCP)

`visor-mcp` is a standard [Model Context Protocol](https://modelcontextprotocol.io) server over stdio, so it works with any MCP-compatible client — Claude Code, OpenAI's Codex CLI, Cline, Continue, Cursor, and agent frameworks built on the MCP SDKs (including ones driving open-source models). It's primarily developed and tested against Claude Code, but nothing in it is Claude-specific: it exposes plain MCP tools and standard content blocks.

The server is a single command, `visor-mcp`, that speaks MCP over stdio and takes no arguments — register it however your client expects.

Claude Code:

```bash
claude mcp add quest -- visor-mcp
```

Most other clients (Cursor, Cline, Continue, Claude Desktop, …) use a JSON config:

```json
{
  "mcpServers": {
    "quest": {
      "command": "visor-mcp"
    }
  }
}
```

Codex and some others use their own config file, but the essentials are the same everywhere: run `visor-mcp`, no args, stdio transport.

Then ask your assistant things like:

- *"Take a screenshot of my headset — what's on screen?"* — clients that support image tool results (like Claude Code) display the actual image; text-only clients still get every other tool
- *"Install this build, launch it, and watch the logs for exceptions while I test"*
- *"Why did my app crash? Pull the crash log and explain the stack trace"*
- *"Monitor performance for 60 seconds while I play, then tell me if I'm thermal throttling"*

Tools exposed: `quest_devices`, `quest_info`, `quest_screenshot`, `quest_install`, `quest_uninstall`, `quest_launch`, `quest_stop`, `quest_app_list`, `quest_app_info`, `quest_app_memory`, `quest_clear_data`, `quest_logs`, `quest_logs_stream`, `quest_crash_logs`, `quest_performance`, `quest_monitor`, `quest_thermal`, `quest_frame_timing`, `quest_perf_session`, `quest_perf_diff`, `quest_launch_benchmark`, `quest_trace`, `quest_screen_record`, `quest_push`, `quest_pull`, `quest_files`, `quest_connect_wireless`, `quest_set_refresh_rate`, `quest_tracking`.

## Web dashboard

```bash
visor ui        # opens http://127.0.0.1:7700 in your browser
visor app       # same dashboard in a native desktop window (pip install visor-dev[app])
```

Overview (battery/thermal lens gauges, tracking status), viewport with **real-time live video** (H.264 off the device via `screenrecord`, transcoded to an MJPEG stream — requires ffmpeg), performance charts sampled every 2.5s, filterable log viewer with crash reports, app management (install APK by upload, launch/stop/clear/uninstall), and a device file browser. Localhost-only by design.

On Linux, `visor install-desktop` adds an app-menu launcher and icon so Visor opens like any other desktop app (`visor install-desktop --uninstall` removes it).

## Library

```python
from visor import adb

quest = adb.discover_devices()[0]
adb.install_apk("build.apk", quest.serial)
adb.launch_app("com.my.game", quest.serial)
for entry in adb.logcat_stream(quest.serial, level="E"):
    print(entry.tag, entry.message)
```

All functions raise typed errors (`DeviceUnauthorizedError`, `DeviceNotFoundError`, …) with actionable messages instead of raw ADB stderr.

## Performance profiling

Visor is a full VR profiler built on the per-second `VrApi` metrics the
Quest runtime logs for any rendering VR app: FPS vs target, stale frames,
CPU/GPU dynamic levels + clocks + utilization (including worst core), app
GPU time vs frame budget, and free memory — plus periodic memory anatomy
(`dumpsys meminfo`, where graphics allocations dominate) and thermal
snapshots.

**Sessions** are the core primitive: record one while you play
(`visor perf record`, the dashboard's ● RECORD, or the `quest_perf_session`
MCP tool), get a summary and an evidence-based **bottleneck diagnosis** —
GPU-bound, CPU-bound (single-thread vs parallel), thermal throttling
(including silent clock clamping), memory pressure (lmkd kills), hitching
(bursts with headroom), or memory growth — each with concrete
recommendations. Sessions are JSON files; diff two of them to verify an
optimization (`visor perf diff` / `quest_perf_diff`).

The dashboard's Performance tab is a live workspace: FPS chart with target
line and stale-frame dots, app GPU time vs budget, utilization, clock
levels, memory series, skin temperature with throttle threshold, and event
annotations (level changes, thermal transitions, lmkd kills).

For the "what exactly blocked this frame" tier: `visor trace` captures a
perfetto system trace (scheduling/clocks/graphics; open at ui.perfetto.dev),
and `visor bench` measures cold-start times.

Notes: VR frame metrics only flow while the app is *rendering* — a
sleeping display pauses everything (visor auto-wakes it), and 2D panel
apps fall back to `gfxinfo`. Battery current draw isn't readable on OS
v14+, so drain is inferred from level over longer sessions.

## Notes on Quest OS v14+

- `/sys/class/thermal` and battery `current_now` are permission-denied over ADB; visor reads thermals from `dumpsys thermalservice` instead (45 sensors on Quest 3).
- Screenshots of a sleeping headset return nothing; visor auto-wakes the display via the `prox_close` broadcast and restores the proximity sensor afterwards.
- Screenshots and the live viewport are the per-eye display buffer, which is barrel-distorted for the headset lens (a fisheye look). Visor crops to the left eye and applies ffmpeg lens-correction so the MCP/UI output reads as a flat rectangular image (needs ffmpeg; the raw full-stereo PNG is still kept on disk).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests use captured real-device output as fixtures — no headset needed.

## License

MIT
