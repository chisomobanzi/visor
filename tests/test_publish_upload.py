"""Building and running the ovr-platform-util upload.

Two things are being pinned down here. The first is that a bad upload gets
caught *before* 771 MB goes over the wire — a rejection after a twenty-minute
push is the worst possible time to learn the APK was too big.

The second is that `store` is the live storefront. Meta's channel names are
case-insensitive, so anything that gates on the release channel has to be too;
a gate that waves through `Store` is not a gate.
"""

import pytest

from visor.publish import upload
from visor.publish.creds import Credentials


CREDS = Credentials(app_id="1234567890", app_secret="s3cr3t")


def make_apk(path, size=1024):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + b"\0" * (size - 4))
    return path


class TestReleaseChannel:
    """`store` is the live storefront; everything else is a test channel."""

    def test_store_is_a_release_channel(self):
        assert upload.is_release_channel("store")

    def test_case_does_not_matter(self):
        assert upload.is_release_channel("STORE")
        assert upload.is_release_channel("Store")

    def test_surrounding_space_does_not_matter(self):
        assert upload.is_release_channel("  store ")

    def test_alpha_is_not_a_release_channel(self):
        assert not upload.is_release_channel("ALPHA")

    def test_beta_and_rc_are_not_release_channels(self):
        assert not upload.is_release_channel("BETA")
        assert not upload.is_release_channel("RC")


class TestPreflightRejects:
    def test_missing_apk(self, tmp_path):
        with pytest.raises(upload.PreflightError, match="does not exist"):
            upload.preflight(tmp_path / "nope.apk", channel="ALPHA")

    def test_directory_instead_of_file(self, tmp_path):
        with pytest.raises(upload.PreflightError):
            upload.preflight(tmp_path, channel="ALPHA")

    def test_apk_over_one_gigabyte(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "big.apk")
        monkeypatch.setattr(upload, "MAX_APK_BYTES", 512)
        with pytest.raises(upload.PreflightError, match="1 KiB|too large|exceeds"):
            upload.preflight(apk, channel="ALPHA")

    def test_error_names_the_actual_size(self, tmp_path, monkeypatch):
        """'Too large' without a number leaves you guessing how much to cut."""
        apk = make_apk(tmp_path / "big.apk", size=4096)
        monkeypatch.setattr(upload, "MAX_APK_BYTES", 512)
        with pytest.raises(upload.PreflightError) as excinfo:
            upload.preflight(apk, channel="ALPHA")
        assert "4" in str(excinfo.value)

    def test_not_an_apk(self, tmp_path):
        obb = tmp_path / "main.obb"
        obb.write_bytes(b"x")
        with pytest.raises(upload.PreflightError, match=".apk"):
            upload.preflight(obb, channel="ALPHA")

    def test_blank_channel(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        with pytest.raises(upload.PreflightError, match="channel"):
            upload.preflight(apk, channel="   ")

    def test_unknown_age_group(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        with pytest.raises(upload.PreflightError, match="TEENS_AND_ADULTS"):
            upload.preflight(apk, channel="ALPHA", age_group="EVERYONE")

    def test_age_group_is_case_insensitive(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        upload.preflight(apk, channel="ALPHA", age_group="teens_and_adults")


class TestPreflightWarns:
    """Soft problems come back as notes rather than exceptions — they are
    things the user may well have meant."""

    def test_clean_upload_has_no_notes(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        assert upload.preflight(apk, channel="ALPHA", age_group="MIXED_AGES") == []

    def test_missing_age_group_warns_about_draft(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        notes = upload.preflight(apk, channel="ALPHA")
        assert any("draft" in n.lower() for n in notes)

    def test_release_channel_is_called_out(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        notes = upload.preflight(apk, channel="store", age_group="MIXED_AGES")
        assert any("store" in n.lower() for n in notes)


class TestBuildArgv:
    def test_uses_metas_flag_spelling(self, tmp_path):
        """Meta spells these with hyphens; underscores are silently rejected."""
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "--app-id" in argv
        assert "--app-secret" in argv
        assert "--apk" in argv
        assert "--channel" in argv

    def test_subcommand_comes_first(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert argv[0] == "/bin/ovr"
        assert argv[1] == "upload-quest-build"

    def test_carries_the_values(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert argv[argv.index("--app-id") + 1] == "1234567890"
        assert argv[argv.index("--app-secret") + 1] == "s3cr3t"
        assert argv[argv.index("--apk") + 1] == str(apk)
        assert argv[argv.index("--channel") + 1] == "ALPHA"

    def test_age_group_included_when_given(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv(
            "/bin/ovr", CREDS, apk, channel="ALPHA", age_group="teens_and_adults"
        )
        assert argv[argv.index("--age-group") + 1] == "TEENS_AND_ADULTS"

    def test_age_group_omitted_entirely_when_absent(self, tmp_path):
        """Passing an empty --age-group is not the same as omitting it."""
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "--age-group" not in argv

    def test_notes_included_when_given(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv(
            "/bin/ovr", CREDS, apk, channel="ALPHA", notes="StoreVersion 7"
        )
        assert argv[argv.index("--notes") + 1] == "StoreVersion 7"

    def test_progress_bar_disabled(self, tmp_path):
        """We're not on a TTY; the animated bar would arrive as line noise."""
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "--disable-progress-bar" in argv


class TestValidateOnly:
    """`--validate` runs the whole upload path against Meta and stops short of
    publishing — the only way to prove credentials and an APK are good without
    putting a build in front of testers."""

    def test_flag_absent_by_default(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "--validate" not in argv

    def test_flag_present_when_asked(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv(
            "/bin/ovr", CREDS, apk, channel="ALPHA", validate_only=True
        )
        assert "--validate" in argv

    def test_upload_passes_it_through(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        seen = {}

        def record(argv):
            seen["argv"] = argv
            return FakeProcess(["ok\n"])

        monkeypatch.setattr(upload, "_spawn", record)
        upload.upload(
            apk,
            channel="ALPHA",
            credentials=CREDS,
            binary="/bin/ovr",
            validate_only=True,
        )
        assert "--validate" in seen["argv"]

    def test_validating_against_store_needs_no_consent(self, tmp_path, monkeypatch):
        """Nothing is published, so the live-store gate has nothing to protect."""
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(upload, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        result = upload.upload(
            apk,
            channel="store",
            credentials=CREDS,
            binary="/bin/ovr",
            validate_only=True,
            age_group="MIXED_AGES",
        )
        assert result.succeeded

    def test_result_says_nothing_was_uploaded(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(upload, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        result = upload.upload(
            apk,
            channel="ALPHA",
            credentials=CREDS,
            binary="/bin/ovr",
            validate_only=True,
        )
        assert result.validated_only


class TestPostProcessing:
    """Meta runs malware and security checks after the bytes land. A build that
    fails them is not uploaded, whatever the upload step said."""

    def test_waits_by_default(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "--wait-for-postprocessing" in argv

    def test_can_be_turned_off(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv(
            "/bin/ovr", CREDS, apk, channel="ALPHA", wait_for_postprocessing=False
        )
        assert "--wait-for-postprocessing" not in argv

    def test_not_requested_when_only_validating(self, tmp_path):
        """There is no build to post-process; asking would just be noise."""
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv(
            "/bin/ovr", CREDS, apk, channel="ALPHA", validate_only=True
        )
        assert "--wait-for-postprocessing" not in argv


class TestRedactedArgv:
    """What gets logged, printed, or handed back to an MCP client."""

    def test_secret_is_gone(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "s3cr3t" not in " ".join(upload.redacted_argv(argv, CREDS))

    def test_app_id_survives(self, tmp_path):
        """The app id is how you tell two uploads apart; it is not a secret."""
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert "1234567890" in " ".join(upload.redacted_argv(argv, CREDS))

    def test_shape_is_unchanged(self, tmp_path):
        apk = make_apk(tmp_path / "a.apk")
        argv = upload.build_argv("/bin/ovr", CREDS, apk, channel="ALPHA")
        assert len(upload.redacted_argv(argv, CREDS)) == len(argv)


class FakeProcess:
    """Stands in for the ovr-platform-util subprocess."""

    def __init__(self, lines=(), returncode=0):
        self._lines = list(lines)
        self.returncode = returncode
        self.stdout = self

    def __iter__(self):
        return iter(self._lines)

    def wait(self):
        return self.returncode


class TestUpload:
    def test_returns_success_on_zero_exit(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload, "_spawn", lambda argv: FakeProcess(["Uploading...\n", "Done\n"])
        )
        result = upload.upload(
            apk, channel="ALPHA", credentials=CREDS, binary="/bin/ovr"
        )
        assert result.succeeded

    def test_nonzero_exit_raises(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload, "_spawn", lambda argv: FakeProcess(["boom\n"], returncode=1)
        )
        with pytest.raises(upload.UploadError):
            upload.upload(apk, channel="ALPHA", credentials=CREDS, binary="/bin/ovr")

    def test_failure_quotes_the_tool_output(self, tmp_path, monkeypatch):
        """Meta's own message is more useful than anything we could invent."""
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload,
            "_spawn",
            lambda argv: FakeProcess(["Invalid app secret\n"], returncode=1),
        )
        with pytest.raises(upload.UploadError, match="Invalid app secret"):
            upload.upload(apk, channel="ALPHA", credentials=CREDS, binary="/bin/ovr")

    def test_progress_lines_are_streamed(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload, "_spawn", lambda argv: FakeProcess(["10%\n", "50%\n", "100%\n"])
        )
        seen = []
        upload.upload(
            apk,
            channel="ALPHA",
            credentials=CREDS,
            binary="/bin/ovr",
            on_line=seen.append,
        )
        assert seen == ["10%", "50%", "100%"]

    def test_streamed_lines_are_redacted(self, tmp_path, monkeypatch):
        """ovr-platform-util has been known to echo its own argv on error."""
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload,
            "_spawn",
            lambda argv: FakeProcess(["running --app-secret s3cr3t\n"]),
        )
        seen = []
        upload.upload(
            apk,
            channel="ALPHA",
            credentials=CREDS,
            binary="/bin/ovr",
            on_line=seen.append,
        )
        assert "s3cr3t" not in " ".join(seen)

    def test_failure_message_is_redacted(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(
            upload,
            "_spawn",
            lambda argv: FakeProcess(["failed: --app-secret s3cr3t\n"], returncode=1),
        )
        with pytest.raises(upload.UploadError) as excinfo:
            upload.upload(apk, channel="ALPHA", credentials=CREDS, binary="/bin/ovr")
        assert "s3cr3t" not in str(excinfo.value)

    def test_preflight_runs_before_spawning(self, tmp_path, monkeypatch):
        """The whole point of preflight is not paying for the upload first."""

        def explode(argv):  # pragma: no cover - must never run
            raise AssertionError("upload() spawned the tool despite a bad APK")

        monkeypatch.setattr(upload, "_spawn", explode)
        with pytest.raises(upload.PreflightError):
            upload.upload(
                tmp_path / "missing.apk",
                channel="ALPHA",
                credentials=CREDS,
                binary="/bin/ovr",
            )

    def test_release_channel_refused_without_consent(self, tmp_path, monkeypatch):
        """Publishing to the live storefront is never an accident."""

        def explode(argv):  # pragma: no cover - must never run
            raise AssertionError("upload() went to store without confirmation")

        monkeypatch.setattr(upload, "_spawn", explode)
        apk = make_apk(tmp_path / "a.apk")
        with pytest.raises(upload.ConfirmationRequired):
            upload.upload(apk, channel="store", credentials=CREDS, binary="/bin/ovr")

    def test_release_channel_allowed_with_consent(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(upload, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        result = upload.upload(
            apk,
            channel="store",
            credentials=CREDS,
            binary="/bin/ovr",
            confirm_release=True,
            age_group="MIXED_AGES",
        )
        assert result.succeeded

    def test_consent_does_not_leak_to_test_channels(self, tmp_path, monkeypatch):
        """confirm_release is about `store`; it must not relax anything else."""
        monkeypatch.setattr(upload, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        with pytest.raises(upload.PreflightError):
            upload.upload(
                tmp_path / "gone.apk",
                channel="ALPHA",
                credentials=CREDS,
                binary="/bin/ovr",
                confirm_release=True,
            )

    def test_result_carries_the_redacted_command(self, tmp_path, monkeypatch):
        apk = make_apk(tmp_path / "a.apk")
        monkeypatch.setattr(upload, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        result = upload.upload(
            apk, channel="ALPHA", credentials=CREDS, binary="/bin/ovr"
        )
        assert "s3cr3t" not in result.command
        assert "upload-quest-build" in result.command
