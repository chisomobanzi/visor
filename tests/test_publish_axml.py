"""Reading a compiled AndroidManifest.xml without the Android SDK.

An APK's manifest is not XML text — it is Android's binary AXML: a string pool
followed by element chunks. `aapt2` reads it, but aapt2 ships inside the Android
SDK build-tools, which a Quest developer who only sideloads builds has no reason
to have installed. Visor's whole premise is that `adb` is the only host tool you
need, so the parser here is pure Python.

The fixture is the real manifest out of a shipped Unreal Quest build, and every
expectation below was taken from `aapt2 dump badging` / `aapt2 dump xmltree` on
that same APK. That is the point: aapt2 is the authority, this parser has to
agree with it.
"""

from pathlib import Path

import pytest

from visor.publish import axml

FIXTURE = Path(__file__).parent / "fixtures" / "quest_manifest.axml"


@pytest.fixture(scope="module")
def manifest():
    return axml.parse(FIXTURE.read_bytes())


class TestRejectingThingsThatAreNotAxml:
    """A helpful error beats a struct.error traceback from 40 frames down."""

    def test_empty_input(self):
        with pytest.raises(axml.AxmlError):
            axml.parse(b"")

    def test_plain_text_xml(self):
        # The mistake someone actually makes: handing it the source manifest
        # from the project tree instead of the one inside the APK.
        with pytest.raises(axml.AxmlError) as e:
            axml.parse(b'<?xml version="1.0"?><manifest/>')
        assert "binary" in str(e.value).lower()

    def test_truncated_after_the_header(self):
        with pytest.raises(axml.AxmlError):
            axml.parse(bytes.fromhex("03000800b85b0000"))


class TestElementTree:
    """The shape aapt2's xmltree shows, minus the formatting."""

    def test_root_is_manifest(self, manifest):
        assert manifest.name == "manifest"

    def test_package_is_an_attribute_of_the_root(self, manifest):
        assert manifest.get("package") == "com.speakeasy3d.voyager"

    def test_android_namespaced_attributes_are_resolved_by_bare_name(self, manifest):
        # Callers should not have to spell the schema URL to ask for
        # android:versionCode.
        assert manifest.get("versionCode") == 2

    def test_find_all_walks_the_whole_tree(self, manifest):
        permissions = {
            e.get("name") for e in manifest.findall("uses-permission")
        }
        assert "android.permission.RECORD_AUDIO" in permissions
        assert "com.oculus.permission.HAND_TRACKING" in permissions

    def test_children_are_only_direct_children(self, manifest):
        names = {c.name for c in manifest.children}
        assert "application" in names
        # A <category> is nested three levels down, not a child of <manifest>.
        assert "category" not in names

    def test_missing_attribute_returns_the_default(self, manifest):
        assert manifest.get("nonexistent") is None
        assert manifest.get("nonexistent", "fallback") == "fallback"


class TestValueTypes:
    """AXML stores typed values, and the type changes what a caller can do."""

    def test_integers_come_back_as_int(self, manifest):
        sdk = manifest.find("uses-sdk")
        assert sdk.get("minSdkVersion") == 32
        assert sdk.get("targetSdkVersion") == 32

    def test_strings_come_back_as_str(self, manifest):
        assert manifest.get("versionName") == "1.0"

    def test_booleans_come_back_as_bool(self, manifest):
        # Shipping builds must not be debuggable; the audit layer leans on this
        # being a real False and not the string "false". Unreal writes the flag
        # on the activity rather than the application, which is why the audit
        # has to look in both places.
        assert manifest.find("activity").get("debuggable") is False
        assert manifest.find("application").get("extractNativeLibs") is True

    def test_enum_backed_attributes_stay_numeric(self, manifest):
        activity = manifest.find("activity")
        # screenOrientation is an ActivityInfo constant: 0 == landscape.
        assert activity.get("screenOrientation") == 0


class TestAttributeNamesTakenFromTheResourceMap:
    """Some builds leave attribute names blank and only ship resource IDs.

    aapt2 can emit a manifest whose attribute name strings are empty, with the
    real identity carried in the parallel RES_XML_RESOURCE_MAP chunk. A parser
    that trusts the string pool alone reads such a manifest as a pile of
    unnamed attributes and silently reports nothing at all — the failure mode
    that makes an audit say "clean" about a build it could not read.
    """

    def test_known_ids_map_back_to_names(self):
        assert axml.attribute_name_for_id(0x0101021B) == "versionCode"
        assert axml.attribute_name_for_id(0x01010270) == "targetSdkVersion"
        assert axml.attribute_name_for_id(0x0101001E) == "screenOrientation"

    def test_unknown_id_is_reported_as_hex(self):
        assert axml.attribute_name_for_id(0x7F123456) == "0x7f123456"
