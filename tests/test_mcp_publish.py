"""The upload tool as an assistant sees it.

Everything else Visor exposes over MCP reads a headset. This one reaches
Meta's servers and cannot be undone by running it again, which changes what
"a good tool" means: the model must not be able to reach the live store by
guessing a plausible argument, and nothing that comes back — success, failure,
or traceback — may carry the app secret into a transcript.
"""

import pytest

from visor.mcp import server
from visor.publish import upload as upload_mod
from visor.publish.creds import Credentials, MissingCredentialsError

CREDS = Credentials(app_id="1234567890", app_secret="s3cr3t")


@pytest.fixture
def apk(tmp_path):
    path = tmp_path / "voyager.apk"
    path.write_bytes(b"PK\x03\x04" + b"\0" * 1020)
    return path


@pytest.fixture
def uploads(monkeypatch):
    """Record what would have been uploaded, without uploading."""
    calls = []

    def fake_upload(apk, channel, credentials, **kwargs):
        calls.append({"apk": str(apk), "channel": channel, **kwargs})
        return upload_mod.UploadResult(
            succeeded=True,
            command="ovr-platform-util upload-quest-build --app-secret <REDACTED>",
            output="Uploading 100%\nBuild uploaded.",
            preflight_notes=("No age group given, so Meta will hold this build "
                             "in draft status.",),
        )

    monkeypatch.setattr(server, "publish_upload", fake_upload)
    monkeypatch.setattr(server, "load_credentials", lambda: CREDS)
    return calls


class TestConfirmGate:
    """The tool takes an explicit confirmation because a model that is 80%
    sure the user meant "upload it" would otherwise just upload it."""

    def test_refuses_without_confirmation(self, apk, uploads):
        with pytest.raises(server.QuestError):
            server.quest_upload_build(apk=str(apk), channel="ALPHA")
        assert uploads == []

    def test_refusal_says_what_to_pass(self, apk, uploads):
        with pytest.raises(server.QuestError, match="confirm"):
            server.quest_upload_build(apk=str(apk), channel="ALPHA")

    def test_proceeds_when_confirmed(self, apk, uploads):
        server.quest_upload_build(apk=str(apk), channel="ALPHA", confirm=True)
        assert len(uploads) == 1


class TestStoreIsOffLimits:
    """Confirmation is enough for a test channel. It is not enough to publish
    to everyone who owns the app — that stays a human action at a terminal."""

    def test_store_is_refused_even_when_confirmed(self, apk, uploads):
        with pytest.raises(server.QuestError):
            server.quest_upload_build(apk=str(apk), channel="store", confirm=True)
        assert uploads == []

    def test_store_refusal_is_case_insensitive(self, apk, uploads):
        for spelling in ("STORE", "Store", " store "):
            with pytest.raises(server.QuestError):
                server.quest_upload_build(
                    apk=str(apk), channel=spelling, confirm=True
                )
        assert uploads == []

    def test_refusal_points_at_the_cli(self, apk, uploads):
        with pytest.raises(server.QuestError, match="visor upload"):
            server.quest_upload_build(apk=str(apk), channel="store", confirm=True)

    def test_confirm_release_is_never_forwarded(self, apk, uploads):
        """Not just refused at this layer — the flag that would unlock the
        library gate must never be set from here at all."""
        server.quest_upload_build(apk=str(apk), channel="ALPHA", confirm=True)
        assert uploads[0].get("confirm_release") is not True


class TestValidateOnly:
    """Validating is the one thing here that changes nothing, so it is the one
    thing that should not need a human to unlock it."""

    def test_needs_no_confirmation(self, apk, uploads):
        server.quest_upload_build(apk=str(apk), channel="ALPHA", validate_only=True)
        assert uploads[0]["validate_only"] is True

    def test_allowed_against_the_store(self, apk, uploads):
        server.quest_upload_build(apk=str(apk), channel="store", validate_only=True)
        assert len(uploads) == 1

    def test_still_refuses_a_bad_apk(self, tmp_path, uploads):
        with pytest.raises(server.QuestError):
            server.quest_upload_build(
                apk=str(tmp_path / "nope.apk"), channel="ALPHA", validate_only=True
            )
        assert uploads == []


class TestResult:
    def test_reports_success_and_channel(self, apk, uploads):
        result = server.quest_upload_build(
            apk=str(apk), channel="ALPHA", confirm=True
        )
        assert result["succeeded"] is True
        assert result["channel"] == "ALPHA"

    def test_surfaces_preflight_notes(self, apk, uploads):
        result = server.quest_upload_build(
            apk=str(apk), channel="ALPHA", confirm=True
        )
        assert any("draft" in n.lower() for n in result["notes"])

    def test_says_the_channel_starts_empty(self, apk, uploads):
        """An upload nobody was invited to reaches nobody; the model should
        be able to tell the user that without being asked."""
        result = server.quest_upload_build(
            apk=str(apk), channel="ALPHA", confirm=True
        )
        assert "invite" in str(result).lower()

    def test_passes_age_group_and_notes(self, apk, uploads):
        server.quest_upload_build(
            apk=str(apk),
            channel="ALPHA",
            confirm=True,
            age_group="TEENS_AND_ADULTS",
            notes="StoreVersion 7",
        )
        assert uploads[0]["age_group"] == "TEENS_AND_ADULTS"
        assert uploads[0]["notes"] == "StoreVersion 7"


class TestNothingLeaks:
    def test_success_payload_has_no_secret(self, apk, uploads):
        result = server.quest_upload_build(
            apk=str(apk), channel="ALPHA", confirm=True
        )
        assert "s3cr3t" not in str(result)

    def test_failure_message_has_no_secret(self, apk, monkeypatch):
        monkeypatch.setattr(server, "load_credentials", lambda: CREDS)

        def fail(*a, **k):
            raise upload_mod.UploadError("failed: --app-secret s3cr3t")

        monkeypatch.setattr(server, "publish_upload", fail)
        with pytest.raises(server.QuestError) as excinfo:
            server.quest_upload_build(apk=str(apk), channel="ALPHA", confirm=True)
        assert "s3cr3t" not in str(excinfo.value)

    def test_credential_failure_is_actionable(self, apk, monkeypatch):
        def no_creds():
            raise MissingCredentialsError("missing: OCULUS_APP_SECRET")

        monkeypatch.setattr(server, "load_credentials", no_creds)
        with pytest.raises(server.QuestError, match="OCULUS_APP_SECRET"):
            server.quest_upload_build(apk=str(apk), channel="ALPHA", confirm=True)

    def test_missing_apk_is_a_clean_error(self, tmp_path, uploads):
        with pytest.raises(server.QuestError):
            server.quest_upload_build(
                apk=str(tmp_path / "nope.apk"), channel="ALPHA", confirm=True
            )
        assert uploads == []
