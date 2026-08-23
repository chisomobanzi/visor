"""Reading an APK well enough to refuse a bad upload.

An upload to Meta is ~800 MB and takes many minutes, and the failure modes that
matter are all visible in the file before any of that is spent: a debug-signed
build, the wrong package, a version code the channel already has, a 32-bit-only
library set, a manifest that never says it is a VR app. Meta rejects those after
the upload. This reads them out of the file first.

No Android SDK: `aapt2` and `apksigner` would answer all of this, and neither is
something a Quest developer necessarily has. An APK is a zip, its manifest is
AXML, and its signature lives in a documented block ahead of the central
directory — all reachable from the standard library.

Fixtures: `quest_manifest.axml` is the real compiled manifest from a shipped
Unreal Quest build, and `android_debug_cert.der` is the certificate every
Android install shares (`~/.android/debug.keystore`, `CN=Android Debug`) —
the exact thing an audit has to catch before it reaches the store.
"""

import pytest

from tests.apkbuild import (
    DEBUG_CERT,
    MANIFEST,
    V2_BLOCK_ID as _V2_BLOCK_ID,
    V3_BLOCK_ID as _V3_BLOCK_ID,
    lp as _lp,
    make_apk,
    signing_block as _signing_block,
    v2_value as _v2_value,
)
from visor.publish import apk


@pytest.fixture
def unsigned(tmp_path):
    return apk.inspect(make_apk(tmp_path / "unsigned.apk"))


class TestRefusingThingsThatAreNotApks:
    def test_missing_file(self, tmp_path):
        with pytest.raises(apk.ApkError):
            apk.inspect(tmp_path / "nope.apk")

    def test_not_a_zip(self, tmp_path):
        path = tmp_path / "text.apk"
        path.write_bytes(b"this is not a zip at all")
        with pytest.raises(apk.ApkError) as e:
            apk.inspect(path)
        assert "zip" in str(e.value).lower()

    def test_zip_without_a_manifest(self, tmp_path):
        path = make_apk(tmp_path / "bare.apk", manifest=None)
        with pytest.raises(apk.ApkError) as e:
            apk.inspect(path)
        assert "AndroidManifest.xml" in str(e.value)


class TestManifestFacts:
    """Everything the Meta dashboard shows about a build, read from the file."""

    def test_package_and_version(self, unsigned):
        assert unsigned.package == "com.speakeasy3d.voyager"
        assert unsigned.version_code == 2
        assert unsigned.version_name == "1.0"

    def test_sdk_levels(self, unsigned):
        assert unsigned.min_sdk == 32
        assert unsigned.target_sdk == 32

    def test_permissions(self, unsigned):
        assert "android.permission.RECORD_AUDIO" in unsigned.permissions
        assert "com.oculus.permission.HAND_TRACKING" in unsigned.permissions

    def test_features(self, unsigned):
        assert "android.hardware.vr.headtracking" in unsigned.features

    def test_vr_intent_category(self, unsigned):
        # Without this the headset treats the build as a 2D panel app.
        assert unsigned.is_vr_app

    def test_orientation(self, unsigned):
        assert unsigned.is_landscape

    def test_debuggable_is_read_from_the_activity_too(self, unsigned):
        # Unreal writes android:debuggable on the activity, not the
        # application — an audit that only looks at <application> reports
        # every Unreal build as clean whether it is or not.
        assert unsigned.debuggable is False

    def test_supported_devices_metadata(self, unsigned):
        assert unsigned.supported_devices


class TestPackagingFacts:
    def test_abis_come_from_the_lib_directory(self, tmp_path):
        info = apk.inspect(make_apk(tmp_path / "a.apk", abis=("arm64-v8a", "armeabi-v7a")))
        assert info.abis == ["arm64-v8a", "armeabi-v7a"]

    def test_size_is_the_file_on_disk(self, tmp_path):
        path = make_apk(tmp_path / "a.apk", padding=4096)
        assert apk.inspect(path).size_bytes == path.stat().st_size


class TestSignature:
    """Which schemes signed it, and — the point of all this — by whom."""

    def test_unsigned_apk_is_reported_as_unsigned(self, unsigned):
        assert unsigned.signature.schemes == []
        assert not unsigned.signature.signed

    def test_v2_scheme_is_detected(self, tmp_path):
        block = _signing_block({_V2_BLOCK_ID: _v2_value(DEBUG_CERT)})
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.schemes == ["v2"]
        assert info.signature.signed

    def test_v3_scheme_is_detected_alongside_v2(self, tmp_path):
        block = _signing_block(
            {_V2_BLOCK_ID: _v2_value(DEBUG_CERT), _V3_BLOCK_ID: _v2_value(DEBUG_CERT)}
        )
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.schemes == ["v2", "v3"]

    def test_certificate_subject_is_read(self, tmp_path):
        block = _signing_block({_V2_BLOCK_ID: _v2_value(DEBUG_CERT)})
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.common_name == "Android Debug"
        assert info.signature.subject["O"] == "Android"

    def test_certificate_digest_identifies_the_key(self, tmp_path):
        # The SHA-256 of the certificate is how you tell "signed with the same
        # key as last time" from "signed with a key the store will reject".
        block = _signing_block({_V2_BLOCK_ID: _v2_value(DEBUG_CERT)})
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.certificate_sha256 == (
            "d7adce8caadf7436566b3f69d9692afc2af6d912a8b183a490f5579c255438fc"
        )

    def test_the_debug_key_is_recognised(self, tmp_path):
        block = _signing_block({_V2_BLOCK_ID: _v2_value(DEBUG_CERT)})
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.debug_signed

    def test_jar_signing_alone_is_still_a_signature(self, tmp_path):
        info = apk.inspect(make_apk(tmp_path / "j.apk", jar_signed=True))
        assert info.signature.schemes == ["v1"]
        assert info.signature.common_name == "Android Debug"
        assert info.signature.debug_signed

    def test_an_unrecognised_block_id_is_ignored(self, tmp_path):
        block = _signing_block({0xDEADBEEF: b"\0" * 8})
        info = apk.inspect(make_apk(tmp_path / "s.apk", signing_block=block))
        assert info.signature.schemes == []
