"""`visor audit` and `visor builds` — reading a build without uploading it.

These two exist so the GUI is not the only way to answer "will Meta take
this?". The upload path already audits, but it audits at the moment you have
decided to spend twenty minutes and 800 MB; a person planning a submission
wants the same answer while the cook is still worth redoing.

Both commands are read-only and network-free, so they work with no
credentials, no ovr-platform-util and no headset.
"""

import json

from typer.testing import CliRunner

from tests.apkbuild import DEBUG_CERT, make_apk, make_release_apk, signing_block, v2_value, V2_BLOCK_ID
from visor.cli.main import app

runner = CliRunner()


class TestAudit:
    def test_a_store_ready_build_reports_ready(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = runner.invoke(app, ["audit", str(apk)])
        assert result.exit_code == 0
        assert "READY" in result.stdout

    def test_a_debug_signed_build_is_refused_with_a_nonzero_exit(self, tmp_path):
        apk = make_release_apk(
            tmp_path / "debug.apk",
            signing_block=signing_block({V2_BLOCK_ID: v2_value(DEBUG_CERT)}),
        )
        result = runner.invoke(app, ["audit", str(apk)])
        assert result.exit_code == 1
        assert "debug" in result.stdout.lower()

    def test_the_exit_code_is_what_a_script_would_branch_on(self, tmp_path):
        """A blocker must fail the command, so `visor audit x && visor upload x` is safe."""
        ready = make_release_apk(tmp_path / "ok.apk")
        broken = make_apk(tmp_path / "unsigned.apk")
        assert runner.invoke(app, ["audit", str(ready)]).exit_code == 0
        assert runner.invoke(app, ["audit", str(broken)]).exit_code == 1

    def test_every_check_is_shown_not_just_the_failures(self, tmp_path):
        """The point is a submission checklist, not an error message."""
        apk = make_release_apk(tmp_path / "voyager.apk")
        out = runner.invoke(app, ["audit", str(apk)]).stdout
        for expected in ("Package name", "Size", "Signed", "Architecture", "Target SDK"):
            assert expected in out

    def test_json_output_is_machine_readable(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = runner.invoke(app, ["audit", str(apk), "--json"])
        payload = json.loads(result.stdout)
        assert payload["ready"] is True
        assert payload["blockers"] == 0
        assert any(c["id"] == "package" for c in payload["checks"])

    def test_the_expected_package_can_be_asserted(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = runner.invoke(app, ["audit", str(apk), "--package", "com.example.other"])
        assert result.exit_code == 1
        assert "com.example.other" in result.stdout

    def test_a_stale_version_code_is_caught_before_the_upload(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = runner.invoke(app, ["audit", str(apk), "--channel-version-code", "99"])
        assert result.exit_code == 1
        assert "99" in result.stdout

    def test_an_unreadable_file_says_so_rather_than_traceback(self, tmp_path):
        junk = tmp_path / "not-an.apk"
        junk.write_bytes(b"nope")
        result = runner.invoke(app, ["audit", str(junk)])
        assert result.exit_code == 1
        assert "Traceback" not in result.stdout


class TestBuilds:
    def test_it_lists_apks_newest_first(self, tmp_path):
        old = make_release_apk(tmp_path / "old.apk")
        new = make_release_apk(tmp_path / "new.apk")
        import os
        os.utime(old, (1_000_000, 1_000_000))
        out = runner.invoke(app, ["builds", str(tmp_path)]).stdout
        assert out.index("new.apk") < out.index("old.apk")
        assert new.name in out

    def test_it_shows_what_distinguishes_two_builds(self, tmp_path):
        make_release_apk(tmp_path / "voyager.apk")
        out = runner.invoke(app, ["builds", str(tmp_path)]).stdout
        assert "com.speakeasy3d.voyager" in out
        assert "Speakeasy3D" in out  # the signing key

    def test_an_empty_folder_says_so_and_still_succeeds(self, tmp_path):
        result = runner.invoke(app, ["builds", str(tmp_path)])
        assert result.exit_code == 0
        assert "No APK" in result.stdout

    def test_json_output_is_machine_readable(self, tmp_path):
        make_release_apk(tmp_path / "voyager.apk")
        result = runner.invoke(app, ["builds", str(tmp_path), "--json"])
        payload = json.loads(result.stdout)
        assert payload[0]["package"] == "com.speakeasy3d.voyager"
        assert payload[0]["debug_signed"] is False


class TestEntryPointsInAFreshInterpreter:
    """`--help` in a subprocess, because pytest's warm import graph hides bugs.

    Under pytest every submodule has already been imported by some other test,
    which repairs parent-package attributes that a real `visor` invocation never
    sets. A re-export in `visor.publish.__init__` that shadowed the
    `visor.publish.audit` submodule passed the whole suite and crashed on the
    first real command; only a clean interpreter catches that class of bug.
    """

    def _run(self, *args):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-m", "visor", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_cli_starts(self):
        result = self._run("--help")
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr

    def test_every_publish_command_is_reachable(self):
        out = self._run("--help").stdout
        for command in ("audit", "builds", "upload", "fetch-tool"):
            assert command in out

    def test_the_mcp_server_module_imports(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import visor.mcp.server"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    def test_the_dashboard_module_imports(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import visor.ui.server"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
