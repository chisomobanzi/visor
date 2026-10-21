"""Turning what's in the APK into a verdict about uploading it.

The rules encode Meta Horizon Store requirements that a file can be judged
against on its own: signing, 64-bit, target SDK floor, landscape, the VR intent
category, the 1 GiB ceiling. Each one is something Meta checks *after* an
800 MB upload, and each one costs another cook to fix.

Two things the split matters for. A blocker means the upload will be refused or
the build rejected — there is no judgement call. A warning means the build will
go up and might still be wrong, which is a question only the developer can
answer, so warnings never stop anything. The audit is also deliberately generic:
it knows about Quest, not about any particular app.
"""

from visor.publish import audit
from visor.publish.apk import ApkInfo, Signature
from pathlib import Path


def make_info(**overrides) -> ApkInfo:
    """A build with nothing wrong with it, plus whatever the test breaks."""
    fields = dict(
        path=Path("/builds/game.apk"),
        size_bytes=700 * 1024 * 1024,
        package="com.example.game",
        version_code=7,
        version_name="1.2",
        min_sdk=32,
        target_sdk=32,
        abis=["arm64-v8a"],
        permissions=["android.permission.INTERNET"],
        features={"android.hardware.vr.headtracking": True},
        metadata={"com.oculus.supportedDevices": "quest2|quest3"},
        orientation=0,
        debuggable=False,
        is_vr_app=True,
        signature=Signature(
            schemes=["v2"],
            subject={"CN": "Example Studio"},
            certificate_sha256="ab" * 32,
        ),
    )
    fields.update(overrides)
    return ApkInfo(**fields)


def levels(report, check_id):
    return next(c.level for c in report.checks if c.id == check_id)


class TestACleanBuild:
    def test_no_blockers(self):
        assert audit.run(make_info()).blockers == []

    def test_is_reported_as_ready(self):
        assert audit.run(make_info()).ready

    def test_every_rule_still_reports_itself(self):
        # A rule that only speaks up when it fails is a rule nobody can tell is
        # running. Passing checks stay in the report as `ok`.
        report = audit.run(make_info())
        assert len(report.checks) > 8
        assert all(c.level == "ok" for c in report.checks if c.id != "version-code")


class TestSigning:
    """The single most expensive mistake: it uploads fine and is then rejected."""

    def test_unsigned_is_a_blocker(self):
        report = audit.run(make_info(signature=Signature()))
        assert levels(report, "signed") == "blocker"

    def test_debug_signed_is_a_blocker(self):
        report = audit.run(
            make_info(signature=Signature(schemes=["v2"], subject={"CN": "Android Debug"}))
        )
        assert levels(report, "signing-key") == "blocker"
        assert "debug" in report.blockers[0].detail.lower()

    def test_v1_only_is_a_warning_not_a_blocker(self):
        report = audit.run(
            make_info(signature=Signature(schemes=["v1"], subject={"CN": "Example"}))
        )
        assert levels(report, "signature-scheme") == "warn"
        assert report.blockers == []


class TestArchitecture:
    def test_missing_arm64_is_a_blocker(self):
        report = audit.run(make_info(abis=["armeabi-v7a"]))
        assert levels(report, "abi") == "blocker"

    def test_no_native_libraries_at_all_is_a_blocker(self):
        report = audit.run(make_info(abis=[]))
        assert levels(report, "abi") == "blocker"

    def test_a_32_bit_slice_alongside_arm64_is_only_dead_weight(self):
        report = audit.run(make_info(abis=["arm64-v8a", "armeabi-v7a"]))
        assert levels(report, "abi") == "warn"
        assert report.blockers == []


class TestManifestRules:
    def test_target_sdk_below_the_floor_is_a_blocker(self):
        report = audit.run(make_info(target_sdk=29))
        assert levels(report, "target-sdk") == "blocker"

    def test_a_missing_target_sdk_cannot_be_waved_through(self):
        report = audit.run(make_info(target_sdk=None))
        assert levels(report, "target-sdk") == "blocker"

    def test_portrait_is_a_blocker(self):
        # Meta's own upload validation rejects this one.
        report = audit.run(make_info(orientation=1))
        assert levels(report, "orientation") == "blocker"

    def test_not_declaring_the_vr_category_is_a_blocker(self):
        report = audit.run(make_info(is_vr_app=False))
        assert levels(report, "vr-category") == "blocker"

    def test_debuggable_is_a_blocker(self):
        report = audit.run(make_info(debuggable=True))
        assert levels(report, "debuggable") == "blocker"

    def test_missing_supported_devices_is_a_warning(self):
        report = audit.run(make_info(metadata={}))
        assert levels(report, "supported-devices") == "warn"


class TestSize:
    def test_over_a_gibibyte_is_a_blocker(self):
        report = audit.run(make_info(size_bytes=1024**3 + 1))
        assert levels(report, "size") == "blocker"
        assert "GiB" in report.blockers[0].detail

    def test_just_under_the_limit_passes(self):
        assert audit.run(make_info(size_bytes=1024**3 - 1)).blockers == []


class TestPermissionsWorthMentioning:
    """Not wrong — but each one is a question Meta will ask on the dashboard."""

    def test_microphone_raises_the_data_use_question(self):
        report = audit.run(
            make_info(permissions=["android.permission.RECORD_AUDIO"])
        )
        check = next(c for c in report.checks if c.id == "sensitive-permissions")
        assert check.level == "warn"
        assert "RECORD_AUDIO" in check.detail

    def test_ordinary_permissions_say_nothing(self):
        report = audit.run(make_info(permissions=["android.permission.INTERNET"]))
        assert levels(report, "sensitive-permissions") == "ok"


class TestThingsOnlyTheCallerKnows:
    """Facts the file cannot settle by itself, passed in when they are known."""

    def test_version_code_is_reported_but_unjudged_when_nothing_to_compare(self):
        report = audit.run(make_info(version_code=7))
        assert levels(report, "version-code") == "info"

    def test_a_version_code_the_channel_already_has_is_a_blocker(self):
        report = audit.run(make_info(version_code=7), channel_version_code=7)
        assert levels(report, "version-code") == "blocker"

    def test_a_higher_version_code_passes(self):
        report = audit.run(make_info(version_code=8), channel_version_code=7)
        assert levels(report, "version-code") == "ok"

    def test_the_wrong_package_is_a_blocker(self):
        report = audit.run(make_info(), expect_package="com.example.other")
        assert levels(report, "package") == "blocker"

    def test_the_expected_package_passes(self):
        report = audit.run(make_info(), expect_package="com.example.game")
        assert levels(report, "package") == "ok"


class TestReportShape:
    def test_checks_are_json_safe(self):
        import json

        json.dumps(audit.run(make_info()).summary())

    def test_blockers_come_first_in_the_summary(self):
        report = audit.run(make_info(debuggable=True, abis=[]))
        ids = [c["id"] for c in report.summary()["checks"]]
        assert ids[0] in ("abi", "debuggable")
        assert report.summary()["ready"] is False
