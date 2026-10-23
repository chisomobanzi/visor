"""Launching apps in the virtual headset: target resolution, environment, crashes."""

import json
import sys
import time

import pytest

from visor.xr import paths
from visor.xr.headsets import get_headset
from visor.xr.sim import AppSpec, SimConfig, SimError, SimInstance, detect_crash


@pytest.fixture
def xr_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path


class TestResolve:
    def test_demo_needs_setup(self, xr_home):
        with pytest.raises(SimError, match="visor sim setup"):
            AppSpec.resolve("demo")

    def test_demo(self, xr_home):
        exe = paths.hello_xr()
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        spec = AppSpec.resolve("demo")
        assert spec.kind == "hello_xr" and spec.keep_stdin_open
        assert spec.command[:3] == [str(exe), "-g", "Vulkan2"]

    def test_unreal_project(self, tmp_path, monkeypatch):
        engine = tmp_path / "UE_5.6"
        editor = engine / "Engine/Binaries/Linux/UnrealEditor"
        editor.parent.mkdir(parents=True)
        editor.write_text("")
        monkeypatch.setenv("UE_ROOT", str(engine))
        proj = tmp_path / "Game"
        (proj / "Plugins/UnrealMCP").mkdir(parents=True)
        (proj / "Game.uproject").write_text(json.dumps({"EngineAssociation": "5.6"}))
        spec = AppSpec.resolve(str(proj), map_name="/Game/Maps/Start", deterministic=True)
        assert spec.kind == "unreal-project"
        assert spec.command[0] == str(editor)
        assert spec.command[2] == "/Game/Maps/Start"
        assert "-vr" in spec.command and "-game" in spec.command
        assert "-NoMCP" in spec.command  # never fight the open editor for its MCP port
        assert "-UseFixedTimeStep" in spec.command and "-FPS=72" in spec.command

    def test_apk_explains_itself(self, tmp_path):
        apk = tmp_path / "game.apk"
        apk.write_text("")
        with pytest.raises(SimError, match="Android"):
            AppSpec.resolve(str(apk))

    def test_unknown_target(self):
        with pytest.raises(SimError, match="don't know how to run"):
            AppSpec.resolve("/definitely/not/here")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux runtime")
class TestEnvironment:
    def test_points_the_app_at_the_virtual_headset(self, xr_home):
        spec = AppSpec(name="app", command=["/bin/true"])
        sim = SimInstance(spec, SimConfig(headset="quest3", render_scale=0.5), name="t1")
        env = sim.environment()
        assert env["XR_RUNTIME_JSON"].endswith("openxr_monado.json")
        assert env["XR_ENABLE_API_LAYERS"].split(":")[0] == "XR_APILAYER_VISOR_agent"
        assert env["XRT_COMPOSITOR_NULL"] == "1"
        assert env["XRT_COMPOSITOR_NULL_REFRESH_RATES"] == "72,80,90,120"
        assert env["XRT_COMPOSITOR_NULL_VIEW_WIDTH"] == "840"  # Quest 3 at half scale
        assert env["VISOR_XR_SOCKET"].endswith("/visor-xr/t1.sock")
        assert env["VISOR_XR_SHM"] == "/visor-t1"
        # Its audio server is Visor's microphone, and PipeWire is out of reach: it can't hear the room.
        assert env["PULSE_SERVER"] == f"unix:{xr_home / 'run' / 'visor-xr' / 't1.pulse'}"
        assert env["PIPEWIRE_REMOTE"] == "/nonexistent"

    def test_no_microphone_at_all(self, xr_home):
        sim = SimInstance(AppSpec(name="a", command=["x"]), SimConfig(virtual_mic=False), name="t4")
        assert sim.environment()["PULSE_SERVER"] == "unix:/nonexistent"

    def test_microphone_opt_in(self, xr_home, monkeypatch):
        monkeypatch.delenv("PULSE_SERVER", raising=False)
        monkeypatch.delenv("PIPEWIRE_REMOTE", raising=False)
        sim = SimInstance(AppSpec(name="a", command=["x"]), SimConfig(microphone=True), name="t2")
        env = sim.environment()
        assert "PULSE_SERVER" not in env and "PIPEWIRE_REMOTE" not in env

    def test_unreal_plays_sound_only_into_visor_s_speakers(self, xr_home):
        spec = AppSpec(name="g", command=["UnrealEditor", "g.uproject", "-game", "-nosound"], kind="unreal-project")
        sim = SimInstance(spec, SimConfig(), name="g1")
        assert "-nosound" in sim.command()  # no Visor audio: it stays silent
        sim.mic_proc = object()
        assert "-nosound" not in sim.command()

    def test_start_refuses_without_setup(self, xr_home):
        sim = SimInstance(AppSpec(name="a", command=["/bin/true"]), name="t3")
        with pytest.raises(SimError, match="isn't installed"):
            sim.start()


class TestHeadsets:
    def test_scaled_keeps_multiple_of_8(self):
        h = get_headset("quest3").scaled(0.5)
        assert (h.eye_width, h.eye_height) == (840, 880)
        assert h.refresh_rates[0] == 72

    def test_unknown(self):
        with pytest.raises(ValueError, match="unknown headset"):
            get_headset("vive")


class TestCrashes:
    def test_unreal_crash_folder(self, tmp_path):
        d = tmp_path / "ue/Saved/Crashes/crashinfo-Game-pid-1-ABC"
        d.mkdir(parents=True)
        (d / "Diagnostics.txt").write_text(
            'Exception was "SIGSEGV: invalid attempt to read memory at address 0x0"\n'
            "<CALLSTACK START>\nlibGame.so!AFoo::Tick() [Foo.cpp:12]\nlibc.so.6!start\n<CALLSTACK END>\n")
        c = detect_crash(tmp_path)
        assert c["message"].startswith("SIGSEGV")
        assert c["callstack"][0] == "libGame.so!AFoo::Tick() [Foo.cpp:12]"

    def test_log_patterns(self, tmp_path):
        (tmp_path / "app.log").write_text("starting\nall good\ndouble free or corruption (out)\n")
        assert "double free" in detect_crash(tmp_path)["message"]

    def test_clean_run(self, tmp_path):
        (tmp_path / "app.log").write_text("LogInit: hello\nLogExit: bye\n")
        assert detect_crash(tmp_path) is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc")
def test_simulated_apps_go_first_when_memory_runs_out():
    import subprocess

    from visor.xr.sim import _first_to_go

    p = subprocess.Popen(["sleep", "5"])
    try:
        _first_to_go(p.pid)
        assert open(f"/proc/{p.pid}/oom_score_adj").read().strip() == "1000"
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc")
class TestMemory:
    """Running out of memory must cost only the simulated app, never what the
    user started it from (an IDE's terminal) or the run's report."""

    def _app(self, tmp_path, *cmd):
        import subprocess

        inst = SimInstance(AppSpec(name="sleeper", command=list(cmd)), run_dir=tmp_path)
        inst.proc = subprocess.Popen(list(cmd))
        return inst

    def test_apps_get_a_scope_of_their_own(self):
        """Otherwise an OOM kill of the app makes systemd stop the scope it
        shares with whatever launched Visor: Rider went down with one."""
        import subprocess

        from visor.xr import sim

        sim._can_scope.cache_clear()
        if not sim._can_scope():
            pytest.skip("no systemd user manager")
        p = subprocess.Popen(sim._own_scope("test") + ["sleep", "5"])
        try:
            for _ in range(50):  # systemd-run execs the app once the scope exists
                if open(f"/proc/{p.pid}/comm").read().strip() == "sleep":
                    break
                time.sleep(0.05)
            ours = open("/proc/self/cgroup").read()
            theirs = open(f"/proc/{p.pid}/cgroup").read()
            assert theirs != ours and theirs.strip().endswith(".scope")
        finally:
            p.kill()
            p.wait()

    def test_no_scope_without_systemd(self, monkeypatch):
        from visor.xr import sim

        monkeypatch.setenv("VISOR_XR_NO_SCOPE", "1")
        sim._can_scope.cache_clear()
        try:
            assert sim._own_scope("test") == []
        finally:
            sim._can_scope.cache_clear()

    def test_low_memory_closes_the_app_and_says_why(self, tmp_path, monkeypatch):
        from visor.xr import sim

        monkeypatch.setattr(sim, "mem_available_mb", lambda: 400)
        monkeypatch.setattr(sim, "LOW_MEMORY_SECONDS", 0)
        monkeypatch.setattr(sim, "_memory_closed_at", [-sim.LOW_MEMORY_SETTLE])
        inst = self._app(tmp_path, "sleep", "30")
        inst._watch_memory()
        assert inst.proc.poll() is not None
        assert "running out of memory (400 MB left)" in inst.ended_by()

    def test_a_fleet_loses_one_copy_at_a_time(self, tmp_path, monkeypatch):
        """Twenty copies see the same low; closing one may be enough."""
        import threading

        from visor.xr import sim

        monkeypatch.setattr(sim, "mem_available_mb", lambda: 400)
        monkeypatch.setattr(sim, "LOW_MEMORY_SECONDS", 0)
        monkeypatch.setattr(sim, "_memory_closed_at", [-sim.LOW_MEMORY_SETTLE])
        copies = [self._app(tmp_path / str(n), "sleep", "30") for n in range(3)]
        watchers = [threading.Thread(target=c._watch_memory, daemon=True) for c in copies]
        try:
            for w in watchers:
                w.start()
            time.sleep(3)
            assert sum(c.proc.poll() is not None for c in copies) == 1
        finally:
            for c in copies:
                c.proc.kill()
                c.proc.wait()

    def test_enough_memory_leaves_it_alone(self, tmp_path, monkeypatch):
        from visor.xr import sim

        monkeypatch.setattr(sim, "mem_available_mb", lambda: 8000)
        inst = self._app(tmp_path, "sleep", "1.5")
        inst._watch_memory()  # returns when the app ends on its own
        assert inst.proc.returncode == 0 and inst.ended_by() is None

    def test_a_kill_without_a_crash_report_is_the_kernel(self, tmp_path):
        inst = self._app(tmp_path, "sleep", "30")
        inst.proc.kill()
        inst.proc.wait()
        assert "ran out of memory" in inst.ended_by()

    def test_floor_is_configurable(self, monkeypatch):
        from visor.xr import sim

        monkeypatch.setenv("VISOR_XR_MIN_FREE_MB", "0")
        assert sim.min_free_mb() == 0
        monkeypatch.setenv("VISOR_XR_MIN_FREE_MB", "lots")
        assert sim.min_free_mb() == sim.MIN_FREE_MB
        assert sim.mem_available_mb() > 0


class TestInstallHints:
    def test_pip_by_default(self, monkeypatch):
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(paths.sys, "prefix", "/home/u/project/.venv")
        assert paths.install_hint("agent") == "pip install 'visor-dev[agent]'"

    def test_pipx_installs_inject(self, monkeypatch):
        """A pip install wouldn't reach pipx's private environment."""
        monkeypatch.delenv("APPIMAGE", raising=False)
        monkeypatch.setattr(paths.sys, "prefix", "/home/u/.local/share/pipx/venvs/visor-dev")
        assert paths.install_hint("voice") == "pipx inject visor-dev piper-tts faster-whisper"
        monkeypatch.setattr(paths.sys, "prefix", r"C:\Users\u\pipx\venvs\visor-dev")  # Windows
        assert paths.install_hint("agent") == "pipx inject visor-dev anthropic"

    def test_cli_errors_keep_their_square_brackets(self, capsys):
        """Rich read [agent] as markup and printed `pip install 'visor-dev'`."""
        import typer

        from visor.cli import sim as cli

        with pytest.raises(typer.Exit):
            cli._die("needs the anthropic package: pip install 'visor-dev[agent]'")
        assert "'visor-dev[agent]'" in capsys.readouterr().err.replace("\n", "")
