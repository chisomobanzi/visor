# Visor

[![CI](https://github.com/chisomobanzi/visor/actions/workflows/ci.yml/badge.svg)](https://github.com/chisomobanzi/visor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/visor-dev)](https://pypi.org/project/visor-dev/)
[![Python](https://img.shields.io/pypi/pyversions/visor-dev)](https://pypi.org/project/visor-dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Cross-platform Quest developer toolkit** — an open source alternative to Meta Quest Developer Hub (MQDH) that runs on Linux, macOS, and Windows (MQDH is Windows/Mac only, so Linux users had nothing), plus something MQDH doesn't have on any platform: an MCP server that lets AI coding agents — Claude Code and any other MCP-compatible client — see and control your headset.

And a **virtual headset**: any OpenXR app (Unreal, Unity, Godot — no plugin, no code changes) runs headless in a simulated Quest 3 on Linux, where you, a script, or a fleet of AI playtesters hold the head and both controllers, see every frame, freeze time, and get back bug reports with a one-command repro.

Five layers, one library:

| Layer | Command | What it does |
|-------|---------|--------------|
| Python library | `import visor.adb` | Quest-aware ADB wrapper: devices, apps, capture, logs, performance |
| MCP server | `visor-mcp` | 32 `quest_*` tools for any MCP client — screenshots the model can *see*, crash logs, perf monitoring — plus 23 `xr_*` tools that let it play VR apps in the virtual headset |
| CLI | `visor` | MQDH-like functionality from the terminal |
| Web dashboard | `visor ui` | Live instrument panel: perf charts, viewport, logs, app/file management — and a SIMULATOR you can drive with mouse and keyboard |
| Virtual headset | `visor sim` | Run OpenXR apps headless in a simulated Quest 3; drive, step, record, replay, playtest, run 20 at once |

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

### AppImage (Linux, no Python needed)

```bash
./packaging/appimage/build-appimage.sh --install   # builds, then installs to ~/Applications
```

Bundles its own CPython, so the only host requirement is `adb` (plus `ffmpeg`
for live video). Double-clicking opens the dashboard; the CLI passes straight
through — `./Visor-x86_64.AppImage devices`. Drop `--install` to just build
into `packaging/appimage/dist/`.

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

# Publishing (see "Publishing to the Meta Horizon Store" below)
visor builds ~/my-project         # find the APKs the packager wrote
visor audit build.apk             # check it against Meta's Store rules, offline
visor upload build.apk -c ALPHA   # upload to a release channel
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
- *"Is my latest build ready for the Meta Store?"* — `quest_audit_build` reads the APK; it needs no account and uploads nothing

Tools exposed: `quest_devices`, `quest_info`, `quest_screenshot`, `quest_install`, `quest_uninstall`, `quest_launch`, `quest_stop`, `quest_app_list`, `quest_app_info`, `quest_app_memory`, `quest_clear_data`, `quest_logs`, `quest_logs_stream`, `quest_crash_logs`, `quest_performance`, `quest_monitor`, `quest_thermal`, `quest_frame_timing`, `quest_perf_session`, `quest_perf_diff`, `quest_launch_benchmark`, `quest_trace`, `quest_screen_record`, `quest_push`, `quest_pull`, `quest_files`, `quest_connect_wireless`, `quest_set_refresh_rate`, `quest_tracking`, `quest_list_builds`, `quest_audit_build`, `quest_upload_build`.

Virtual headset (see [below](#virtual-headset)): `xr_launch`, `xr_list`, `xr_stop`, `xr_screenshot`, `xr_actions`, `xr_click`, `xr_point`, `xr_press`, `xr_hold`, `xr_stick`, `xr_look`, `xr_walk`, `xr_move_hand`, `xr_press_action`, `xr_pause`, `xr_resume`, `xr_step`, `xr_stats`, `xr_events`, `xr_system_event`, `xr_logs`, `xr_record`, `xr_replay`.

## Virtual headset

Visor can be the headset. `visor sim` runs any OpenXR app on this computer inside a simulated Quest 3 — no window, no HMD, nothing added to the app — and gives you (or an agent) the head, both Touch controllers, the app's frames, and a pause button:

```bash
visor sim setup                                   # one-time: builds the runtime + layer (~5 min, no root)
visor sim run demo                                # a test scene, to see it working
visor sim run ~/Projects/Game/Game.uproject --detach   # Unreal: launches the project in -game -vr

visor sim shot -o view.png                        # what the headset sees (left eye)
visor sim look 30 -10                             # turn the head: yaw 30° left, pitch 10° down
visor sim click 480 660                           # aim the right laser at that pixel and pull the trigger
visor sim press right a                           # press A
visor sim stick left 0 1 --frames 72              # push the left stick forward for a second
visor sim info                                    # the app's own controls: every action and its binding

visor sim pause                                   # freeze the world…
visor sim step 1                                  # …and advance exactly one frame
visor sim event focus_lost                        # open the Meta menu: the app should pause
visor sim record -t 20 --script run.json          # MP4 + a replay of every input
visor sim replay run.json                         # play the inputs back, frame for frame
```

Or open `visor ui` → **SIMULATOR**: the live view, where dragging looks around and clicking presses what's under the cursor (shift-click for the left hand, W A S D to walk, space for the trigger), both controllers with draggable thumbsticks, pause/step, the app's haptics as they happen, and its bound controls lighting up.

### AI agents play it

Every `xr_*` MCP tool returns what the headset sees after the action, and the pixel coordinates in that image are what `xr_click` takes — so an assistant can play a VR app the way it would use a web page. Ask Claude Code things like *"launch my project in the virtual headset and get through onboarding; tell me where you got stuck"*. `xr_pause` stops the world between turns, so slow thinking costs nothing.

### Playtests

```bash
visor sim playtest ~/Projects/Game --persona first-timer --steps 40
visor sim playtest demo --brain monkey --persona speedrunner --seed 7   # model-free, reproducible
visor sim personas
```

A playtester is Claude (`claude-opus-5-5`, needs `ANTHROPIC_API_KEY`) in character — a **first-timer** who doesn't read instructions, a **speedrunner** hunting soft-locks, a **seated** player who can't reach high shelves (the eye height and reach are enforced, not role-played), a **motion-sensitive** player, a **one-handed** player, a **thorough explorer** — or a seeded **monkey** that costs nothing. Alongside what the agent reports, the run watches for what's measurably wrong: crashes, frozen screens, black screens, input that stops doing anything, errors in the app's log, and frame rate below the display's. Each run writes, under `~/.local/share/visor/xr/runs/<name>/`:

- `report.html` — every issue with its screenshot and the actions before it, the verdict, the frame-time chart, every step
- `repro.sh` — relaunches the app frozen and replays every input at the same frame (Unreal runs with a fixed timestep, so game time lines up too)
- `video.mp4`, `frames.json` (per-frame timing), `app.log`, and the engine's crash report if there was one

The dashboard's **PLAYTESTS** page runs them and keeps the reports.

### Fleets

```bash
visor sim fleet demo --count 20 --seconds 30                       # soak 20 isolated copies at once
visor sim fleet ~/Projects/Game --count 4 --playtest --personas first-timer,speedrunner,seated
```

Each copy is its own runtime, socket and frame buffer, so they don't share anything but the GPU. Findings are merged across copies ("8 of 20 runs hit this"). On an RTX 5070 laptop, 20 copies of the demo hold 72 fps each with no missed frames; an Unreal project costs ~4 GB of RAM per copy.

### How it works

```
your app ──► OpenXR loader ──► Visor layer ──────────► Monado (in-process)
 (unchanged)                    poses, buttons,         null compositor shaped like a Quest 3:
                                actions, pause/step,    72/80/90/120 Hz, 1680×1760 per eye,
                                events, control socket  frames copied out to shared memory
                                        ▲                           │
                         visor sim / visor ui / visor-mcp ◄─────────┘
```

- **The layer** (`native/layer`, C++) is an OpenXR API layer: it answers `xrLocateViews`/`xrLocateSpace` from a virtual body, picks the Touch interaction profile, resolves every action the app defined against its own bindings, and gates `xrWaitFrame` for pause/step. It works over any OpenXR runtime, so it can drive a real headset's runtime too (`VISOR_XR_MODE=observe` watches without driving).
- **The runtime** is [Monado](https://monado.freedesktop.org) built in-process with a small patch (`native/monado`): Quest-shaped refresh rates and eye buffers, and frames copied out compositor-side, so capturing never touches the app's GPU queue.
- **Determinism**: inputs are applied at frame boundaries and recorded by frame number, so a recording replays to the same picture.

### Limits, for now

- Desktop Linux, Vulkan apps (Unreal and Godot default to Vulkan; Unity with the Vulkan graphics API). OpenGL apps can be driven but not watched.
- Android APKs don't run here — the same project built for Linux (or its `.uproject`) does. On a real Quest, the OpenXR loader bundled in Unreal APKs looks for layers in the APK's own `assets/openxr/1/api_layers/`, so a dev build can carry the layer without root; that path isn't built yet.
- Apps hear no microphone unless you pass `--mic` (then they hear yours); there's no synthetic speech input yet.

## Publishing to the Meta Horizon Store

Everything else Visor does talks to a headset. This is the one part that talks
to Meta, and it wraps `ovr-platform-util` — Meta's own upload utility, the same
one Meta Quest Developer Hub drives internally. On Linux, where MQDH does not
exist, that makes this the only route to a release channel.

`ovr-platform-util` is Meta's, not Visor's, so it can't be redistributed here.
It is fetched on request and kept alongside Visor's other user data:

```bash
visor fetch-tool
```

Credentials come from your app's Meta Horizon dashboard (API → App ID and App
Secret). Put them in the environment for a one-off, or in
`~/.config/visor/credentials` (written `0600`, `KEY=value` lines) to keep them:

```bash
OCULUS_APP_ID=… OCULUS_APP_SECRET=… visor upload build.apk --channel ALPHA
```

The secret is redacted from every command line, log line and error message
Visor produces. If you ever see it in output, that's a bug worth reporting.

### Read the build before you spend the upload

An 800 MB upload that Meta refuses on arrival costs twenty minutes and another
cook. `visor audit` answers the same question from the file, in under a second,
with no account and no headset:

```bash
visor builds ~/my-project          # what did the packager actually produce?
visor audit build.apk              # would Meta take this one?
visor audit build.apk --json       # same, for a script
```

It reads the APK directly — the Android binary manifest, the v2/v3 signing
block, the certificate subject — and checks what Meta checks: APK size against
the 1 GiB cap, `arm64-v8a` present (Quest is 64-bit only), target SDK against
Meta's floor, landscape orientation, the `com.oculus.intent.category.VR`
category, `com.oculus.supportedDevices`, `debuggable` off, and — the one that
catches most people — whether the build is signed with the Android debug key
rather than your release keystore.

Two things the file alone cannot settle, so pass them in and they become checks
rather than notes:

```bash
visor audit build.apk --package com.my.game --channel-version-code 7
```

`--channel-version-code` is what that channel already serves; Meta refuses a
build that does not exceed it, and refuses it *after* the upload. Find the
number on the Meta dashboard under **Distribution → Release Channels**. Visor
does not read it for you on purpose: Meta's only API for it
(`ovr-platform-util get-release-channel-data`) wants an interactive account
login — email, password, 2FA — rather than the app secret, and Visor will not
stand between you and a password prompt.

`visor audit` exits non-zero on any blocker, so
`visor audit build.apk && visor upload build.apk -c ALPHA` is safe to chain.

### Upload

```bash
visor upload build.apk --channel ALPHA --notes "fixed the hand ray"
visor upload build.apk -c ALPHA --dry-run     # Meta validates it and stops
```

`--dry-run` sends the build to Meta's validator without publishing it — the
rehearsal to do first. Channels are `ALPHA`, `BETA`, `RC` and `store`; the
first three are test channels, and they **start empty**: invite testers by
email in the dashboard or nobody can install what you uploaded.

`store` is the live storefront — everyone who owns the app gets that build. It
needs `--yes`, and the MCP tool refuses to publish there whatever it is asked, so
an assistant cannot publish to the public storefront by guessing (it can still
rehearse against it with `validate_only`).

Uploading also runs the audit first and refuses on blockers. If you believe a
check is wrong about your build, the upload is not the place to argue with it —
but nothing stops you: `visor upload` reports what it found before it spends
the bandwidth.

### The dashboard walks the whole thing

```bash
visor ui        # PUBLISH in the sidebar
```

Five steps: get the tool and store the credentials, scan for builds and pick
one, see every check with its blockers and warnings, send it (with a live log
you can cancel), and a persistent checklist of the half that only exists on
Meta's website — store listing, assets, age rating, privacy policy, Data Use
Checkup, IAP, review notes. Uploading a build is roughly half of a submission;
the checklist is there so the other half is not something you have to remember.

## Web dashboard

```bash
visor ui        # opens http://127.0.0.1:7700 in your browser
visor app       # same dashboard in a desktop window of its own
```

Overview (battery/thermal lens gauges, tracking status), viewport with **real-time live video** (H.264 off the device via `screenrecord`, transcoded to an MJPEG stream — requires ffmpeg), a **shot gallery** where every CAPTURE is saved as a PNG under `~/.visor/shots` and copied to your clipboard (capturing mid-stream grabs the on-screen frame without interrupting the video), performance charts sampled every 2.5s, filterable log viewer with crash reports, app management (install APK by upload, launch/stop/clear/uninstall), a device file browser, and a **publish runway** that takes a build from "which APK is this?" to a Meta release channel. Localhost-only by design.

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
