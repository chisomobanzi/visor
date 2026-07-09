# Visor

Open source Quest developer toolkit for Linux — a replacement for the Windows/Mac-only Meta Quest Developer Hub (MQDH).

Three layers:

1. **Python library** (`visor.adb`) — Quest-aware ADB wrapper: device discovery, app management, capture, logcat parsing, performance/thermal monitoring.
2. **MCP server** (`visor-mcp`) — lets Claude Code see your headset: screenshots, crash logs, performance data, APK installs, all as MCP tools.
3. **CLI** (`visor`) — MQDH-like functionality from the terminal.

Docs: see below (work in progress — full docs land with v0.1.0).
