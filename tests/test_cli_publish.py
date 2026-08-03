"""The publish commands at the terminal (typer runner, mocked publish layer).

The library refuses dangerous uploads; these tests are about whether the
person at the keyboard can tell what is about to happen. An `upload` that
prints nothing until it either succeeds or explodes twenty minutes later is
technically correct and practically unusable.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from visor.cli.main import app
from visor.publish import upload as upload_mod
from visor.publish.creds import Credentials, MissingCredentialsError

runner = CliRunner()

CREDS = Credentials(app_id="1234567890", app_secret="s3cr3t")


@pytest.fixture
def apk(tmp_path):
    path = tmp_path / "voyager.apk"
    path.write_bytes(b"PK\x03\x04" + b"\0" * 1020)
    return path


@pytest.fixture(autouse=True)
def stub_publish(monkeypatch):
    """Credentials resolve, the tool is present, nothing actually uploads."""
    calls = {}

    def fake_upload(apk, channel, credentials, **kwargs):
        calls["apk"] = Path(apk)
        calls["channel"] = channel
        calls.update(kwargs)
        on_line = kwargs.get("on_line")
        if on_line:
            on_line("Uploading 100%")
        return upload_mod.UploadResult(
            succeeded=True,
            command="ovr-platform-util upload-quest-build ...",
            output="Uploading 100%",
            preflight_notes=(),
        )

    monkeypatch.setattr("visor.cli.main.publish_upload", fake_upload)
    monkeypatch.setattr("visor.cli.main.load_credentials", lambda: CREDS)
    return calls


class TestUploadCommand:
    def test_uploads_to_the_named_channel(self, apk, stub_publish):
        result = runner.invoke(app, ["upload", str(apk), "--channel", "ALPHA"])
        assert result.exit_code == 0
        assert stub_publish["channel"] == "ALPHA"
        assert stub_publish["apk"] == apk

    def test_defaults_to_alpha(self, apk, stub_publish):
        """The safe channel is the one you get for free."""
        result = runner.invoke(app, ["upload", str(apk)])
        assert result.exit_code == 0
        assert stub_publish["channel"].upper() == "ALPHA"

    def test_passes_age_group_and_notes(self, apk, stub_publish):
        runner.invoke(
            app,
            [
                "upload",
                str(apk),
                "--age-group",
                "TEENS_AND_ADULTS",
                "--notes",
                "StoreVersion 7",
            ],
        )
        assert stub_publish["age_group"] == "TEENS_AND_ADULTS"
        assert stub_publish["notes"] == "StoreVersion 7"

    def test_streams_progress_to_the_terminal(self, apk):
        result = runner.invoke(app, ["upload", str(apk)])
        assert "100%" in result.output

    def test_reports_success(self, apk):
        result = runner.invoke(app, ["upload", str(apk)])
        assert "ALPHA" in result.output

    def test_shows_preflight_notes(self, apk, monkeypatch):
        def noted(apk, channel, credentials, **kwargs):
            return upload_mod.UploadResult(
                succeeded=True,
                command="...",
                output="",
                preflight_notes=("No age group given, so Meta will hold this "
                                 "build in draft status.",),
            )

        monkeypatch.setattr("visor.cli.main.publish_upload", noted)
        result = runner.invoke(app, ["upload", str(apk)])
        assert "draft" in result.output


class TestDryRun:
    """`--dry-run` is the rehearsal: real credentials, real network, real
    validation by Meta, and no build in front of testers afterwards."""

    def test_asks_the_library_to_validate_only(self, apk, stub_publish):
        result = runner.invoke(app, ["upload", str(apk), "--dry-run"])
        assert result.exit_code == 0
        assert stub_publish["validate_only"] is True

    def test_is_off_by_default(self, apk, stub_publish):
        runner.invoke(app, ["upload", str(apk)])
        assert stub_publish.get("validate_only") is not True

    def test_says_nothing_was_uploaded(self, apk, monkeypatch):
        """Otherwise a green 'Uploaded to ALPHA' would be a lie."""
        def validated(apk, channel, credentials, **kwargs):
            return upload_mod.UploadResult(
                succeeded=True,
                command="...",
                output="",
                validated_only=True,
            )

        monkeypatch.setattr("visor.cli.main.publish_upload", validated)
        result = runner.invoke(app, ["upload", str(apk), "--dry-run"])
        assert "not uploaded" in result.output.lower()

    def test_can_rehearse_the_store_without_the_yes_flag(self, apk, stub_publish):
        """Nothing is published, so the gate has nothing left to guard."""
        result = runner.invoke(
            app, ["upload", str(apk), "--channel", "store", "--dry-run"]
        )
        assert result.exit_code == 0
        assert stub_publish["validate_only"] is True


class TestUploadRefusals:
    def test_missing_apk_exits_nonzero(self, tmp_path):
        result = runner.invoke(app, ["upload", str(tmp_path / "nope.apk")])
        assert result.exit_code != 0

    def test_missing_credentials_explains_how_to_set_them(self, apk, monkeypatch):
        def no_creds():
            raise MissingCredentialsError(
                "Meta app credentials incomplete (missing: OCULUS_APP_SECRET)."
            )

        monkeypatch.setattr("visor.cli.main.load_credentials", no_creds)
        result = runner.invoke(app, ["upload", str(apk)])
        assert result.exit_code != 0
        assert "OCULUS_APP_SECRET" in result.output

    def test_store_channel_needs_the_yes_flag(self, apk, monkeypatch):
        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("CLI uploaded to store without --yes")

        monkeypatch.setattr("visor.cli.main.publish_upload", explode)
        result = runner.invoke(app, ["upload", str(apk), "--channel", "store"])
        assert result.exit_code != 0
        assert "--yes" in result.output

    def test_store_channel_proceeds_with_the_yes_flag(self, apk, stub_publish):
        result = runner.invoke(
            app, ["upload", str(apk), "--channel", "store", "--yes"]
        )
        assert result.exit_code == 0
        assert stub_publish["confirm_release"] is True

    def test_upload_failure_exits_nonzero(self, apk, monkeypatch):
        def fail(*a, **k):
            raise upload_mod.UploadError("upload-quest-build failed.\nInvalid APK")

        monkeypatch.setattr("visor.cli.main.publish_upload", fail)
        result = runner.invoke(app, ["upload", str(apk)])
        assert result.exit_code != 0
        assert "Invalid APK" in result.output

    def test_secret_never_appears_in_output(self, apk):
        result = runner.invoke(app, ["upload", str(apk)])
        assert "s3cr3t" not in result.output


class TestFetchToolCommand:
    def test_downloads_when_asked(self, monkeypatch, tmp_path):
        got = tmp_path / "ovr-platform-util"
        monkeypatch.setattr("visor.cli.main.ovrtool_fetch", lambda: got)
        monkeypatch.setattr("visor.cli.main.ovrtool_version", lambda p: "1.15.0")
        result = runner.invoke(app, ["fetch-tool"])
        assert result.exit_code == 0
        assert str(got) in result.output

    def test_reports_the_version(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "visor.cli.main.ovrtool_fetch", lambda: tmp_path / "ovr-platform-util"
        )
        monkeypatch.setattr("visor.cli.main.ovrtool_version", lambda p: "1.15.0")
        result = runner.invoke(app, ["fetch-tool"])
        assert "1.15.0" in result.output

    def test_names_metas_licence_before_downloading(self, monkeypatch, tmp_path):
        """It's Meta's binary, not ours — say so rather than pretending it's
        part of Visor."""
        monkeypatch.setattr(
            "visor.cli.main.ovrtool_fetch", lambda: tmp_path / "ovr-platform-util"
        )
        monkeypatch.setattr("visor.cli.main.ovrtool_version", lambda p: None)
        result = runner.invoke(app, ["fetch-tool"])
        assert "Meta" in result.output
