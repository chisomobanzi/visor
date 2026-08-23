"""What is actually inside an .apk, without the Android SDK.

`aapt2 dump badging` and `apksigner verify` answer everything here, and neither
ships with anything a Quest developer needs for sideloading — they live in the
Android SDK build-tools. Visor asks for `adb` and nothing else, so it reads the
file itself: an APK is a zip, its manifest is Android binary XML (see
`visor.publish.axml`), and its signature sits in a documented block just ahead of
the zip central directory.

The reason to bother: an upload to Meta is most of a gigabyte and many minutes,
and every rejection that matters is legible in the file beforehand. A build
signed with the debug key, a package name from the other app in the project, a
version code the channel already carries, no VR intent category — Meta finds all
of those after the upload. This finds them first.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from visor.adb.wrapper import VisorError
from visor.publish import axml

#: APK Signature Scheme block IDs (Android's `ApkSigningBlockUtils`).
_SCHEME_IDS = {
    0x7109871A: "v2",
    0xF05368C0: "v3",
    0x1B93AD61: "v4",
}
_SIG_BLOCK_MAGIC = b"APK Sig Block 42"

#: `ActivityInfo.SCREEN_ORIENTATION_*` values Meta accepts — the store rejects a
#: build whose activity is not landscape, and it does so only after the upload.
_LANDSCAPE = {0, 6, 8, 11}

#: The certificate every Android SDK install signs debug builds with. Meta
#: rejects it; so does every other store.
_DEBUG_COMMON_NAME = "Android Debug"

_DN_OIDS = {
    "2.5.4.3": "CN",
    "2.5.4.6": "C",
    "2.5.4.7": "L",
    "2.5.4.8": "ST",
    "2.5.4.10": "O",
    "2.5.4.11": "OU",
}
_DER_STRING_TAGS = {0x0C, 0x13, 0x14, 0x16, 0x1E}

_VR_CATEGORY = "com.oculus.intent.category.VR"


class ApkError(VisorError):
    """The file cannot be read as an APK."""


@dataclass(frozen=True)
class Signature:
    """Who signed the build, and under which schemes."""

    schemes: list[str] = field(default_factory=list)
    subject: dict[str, str] = field(default_factory=dict)
    certificate_sha256: Optional[str] = None

    @property
    def signed(self) -> bool:
        return bool(self.schemes)

    @property
    def common_name(self) -> Optional[str]:
        return self.subject.get("CN")

    @property
    def debug_signed(self) -> bool:
        """Signed with the Android debug key — an automatic store rejection."""
        return self.common_name == _DEBUG_COMMON_NAME

    @property
    def subject_dn(self) -> str:
        order = ("CN", "OU", "O", "L", "ST", "C")
        return ", ".join(
            f"{k}={self.subject[k]}" for k in order if k in self.subject
        )


@dataclass(frozen=True)
class ApkInfo:
    """Everything about a build that can be read off disk."""

    path: Path
    size_bytes: int
    package: Optional[str]
    version_code: Optional[int]
    version_name: Optional[str]
    min_sdk: Optional[int]
    target_sdk: Optional[int]
    abis: list[str]
    permissions: list[str]
    features: dict[str, bool]
    metadata: dict[str, Any]
    orientation: Optional[int]
    debuggable: bool
    is_vr_app: bool
    signature: Signature

    @property
    def is_landscape(self) -> bool:
        return self.orientation in _LANDSCAPE

    @property
    def supported_devices(self) -> Optional[str]:
        """The `com.oculus.supportedDevices` declaration, if the build makes one."""
        value = self.metadata.get("com.oculus.supportedDevices")
        return str(value) if value not in (None, "") else None

    def summary(self) -> dict:
        """A flat, JSON-safe view — what the dashboard and MCP hand back."""
        return {
            "path": str(self.path),
            "name": self.path.name,
            "size_bytes": self.size_bytes,
            "package": self.package,
            "version_code": self.version_code,
            "version_name": self.version_name,
            "min_sdk": self.min_sdk,
            "target_sdk": self.target_sdk,
            "abis": self.abis,
            "permissions": self.permissions,
            "features": self.features,
            "orientation": self.orientation,
            "landscape": self.is_landscape,
            "debuggable": self.debuggable,
            "vr": self.is_vr_app,
            "supported_devices": self.supported_devices,
            "signed": self.signature.signed,
            "signature_schemes": self.signature.schemes,
            "signer": self.signature.subject_dn or None,
            "certificate_sha256": self.signature.certificate_sha256,
            "debug_signed": self.signature.debug_signed,
        }


def inspect(path: str | Path) -> ApkInfo:
    """Read an APK's manifest, native libraries and signature."""
    path = Path(path)
    if not path.exists():
        raise ApkError(f"APK does not exist: {path}")
    if not path.is_file():
        raise ApkError(f"Not a file: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "AndroidManifest.xml" not in names:
                raise ApkError(
                    f"{path.name} is a zip but has no AndroidManifest.xml — "
                    "it is not an APK."
                )
            manifest_bytes = archive.read("AndroidManifest.xml")
    except zipfile.BadZipFile:
        raise ApkError(
            f"{path.name} is not a zip archive, so it is not an APK. "
            "A truncated download or an interrupted cook looks exactly like this."
        ) from None

    manifest = axml.parse(manifest_bytes)
    application = manifest.find("application")
    sdk = manifest.find("uses-sdk")

    abis = sorted(
        {n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2}
    )
    permissions = sorted(
        {
            str(e.get("name"))
            for e in manifest.findall("uses-permission")
            if e.get("name")
        }
    )
    features = {
        str(e.get("name")): bool(e.get("required", True))
        for e in manifest.findall("uses-feature")
        if e.get("name")
    }
    metadata = {
        str(e.get("name")): e.get("value")
        for e in manifest.findall("meta-data")
        if e.get("name")
    }

    activity = manifest.find("activity")
    return ApkInfo(
        path=path,
        size_bytes=path.stat().st_size,
        package=manifest.get("package"),
        version_code=_as_int(manifest.get("versionCode")),
        version_name=_as_str(manifest.get("versionName")),
        min_sdk=_as_int(sdk.get("minSdkVersion")) if sdk else None,
        target_sdk=_as_int(sdk.get("targetSdkVersion")) if sdk else None,
        abis=abis,
        permissions=permissions,
        features=features,
        metadata=metadata,
        orientation=_as_int(activity.get("screenOrientation")) if activity else None,
        # Unreal writes android:debuggable on the activity rather than the
        # application, so looking only at <application> passes every Unreal
        # build regardless of what it actually is.
        debuggable=any(
            e.get("debuggable") is True
            for e in ([application] if application else []) + manifest.findall("activity")
        ),
        is_vr_app=any(
            e.get("name") == _VR_CATEGORY for e in manifest.findall("category")
        ),
        signature=read_signature(path),
    )


def _as_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def read_signature(path: str | Path) -> Signature:
    """Which signature schemes cover this APK, and whose certificate they carry.

    This reports what is *present*, not that the signature verifies — checking
    the digests would mean re-hashing a gigabyte to learn something Meta will
    check anyway. The question worth answering cheaply is whose key it is.
    """
    path = Path(path)
    schemes: list[str] = []
    cert: Optional[bytes] = None

    block = _find_signing_block(path)
    if block is not None:
        for scheme_id, value in _signing_block_pairs(block):
            name = _SCHEME_IDS.get(scheme_id)
            if not name:
                continue
            schemes.append(name)
            if cert is None:
                cert = _first_certificate(value)

    if not schemes:
        # A v1 (JAR) signature is the older scheme: a PKCS#7 blob in META-INF.
        cert = _jar_certificate(path)
        if cert is not None:
            schemes.append("v1")

    if cert is None:
        return Signature(schemes=sorted(set(schemes)))
    return Signature(
        schemes=sorted(set(schemes)),
        subject=_certificate_subject(cert),
        certificate_sha256=hashlib.sha256(cert).hexdigest(),
    )


def _find_signing_block(path: Path) -> Optional[bytes]:
    """The APK Signing Block, which sits between the entries and the directory."""
    with open(path, "rb") as handle:
        size = path.stat().st_size
        # The end-of-central-directory record is last, and at most 64 KiB + 22
        # bytes because of its trailing comment field.
        tail_size = min(size, 65536 + 22)
        handle.seek(size - tail_size)
        tail = handle.read(tail_size)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0:
            return None
        try:
            (cd_offset,) = struct.unpack_from("<I", tail, marker + 16)
        except struct.error:
            return None
        if cd_offset < 32 or cd_offset > size:
            return None

        handle.seek(cd_offset - 24)
        footer = handle.read(24)
        if footer[8:] != _SIG_BLOCK_MAGIC:
            return None
        (block_size,) = struct.unpack_from("<Q", footer, 0)
        start = cd_offset - block_size - 8
        if start < 0 or block_size > cd_offset:
            return None
        handle.seek(start)
        return handle.read(cd_offset - start)


def _signing_block_pairs(block: bytes):
    """Walk the block's uint64-length / uint32-id / value triples."""
    at, end = 8, len(block) - 24
    while at + 12 <= end:
        (length,) = struct.unpack_from("<Q", block, at)
        if length < 4 or at + 8 + length > len(block):
            return
        (scheme_id,) = struct.unpack_from("<I", block, at + 8)
        yield scheme_id, block[at + 12 : at + 8 + length]
        at += 8 + length


def _length_prefixed(buffer: bytes, offset: int) -> tuple[bytes, int]:
    (length,) = struct.unpack_from("<I", buffer, offset)
    return buffer[offset + 4 : offset + 4 + length], offset + 4 + length


def _first_certificate(value: bytes) -> Optional[bytes]:
    """signers -> signer -> signed data -> certificates -> the first one."""
    try:
        signers, _ = _length_prefixed(value, 0)
        signer, _ = _length_prefixed(signers, 0)
        signed_data, _ = _length_prefixed(signer, 0)
        _digests, offset = _length_prefixed(signed_data, 0)
        certificates, _ = _length_prefixed(signed_data, offset)
        certificate, _ = _length_prefixed(certificates, 0)
    except (struct.error, IndexError):
        return None
    return certificate or None


def _jar_certificate(path: Path) -> Optional[bytes]:
    """The PKCS#7 blob from a v1 signature — enough to read the subject from."""
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                upper = name.upper()
                if upper.startswith("META-INF/") and upper.endswith(
                    (".RSA", ".DSA", ".EC")
                ):
                    return archive.read(name)
    except (zipfile.BadZipFile, KeyError):
        return None
    return None


def _oid(raw: bytes) -> str:
    parts = [str(raw[0] // 40), str(raw[0] % 40)]
    value = 0
    for byte in raw[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return ".".join(parts)


def _certificate_subject(der: bytes) -> dict[str, str]:
    """Pull CN/O/OU/L/ST/C out of a DER certificate.

    A scan for the known attribute OIDs rather than a full ASN.1 walk: the whole
    question is whose name is on the certificate, and a signing certificate is
    self-signed, so issuer and subject carry the same values. Later occurrences
    win, which is the subject — it follows the issuer in the encoding.
    """
    found: dict[str, str] = {}
    i = 0
    limit = len(der) - 2
    while i < limit:
        if der[i] != 0x06:
            i += 1
            continue
        length = der[i + 1]
        if length >= 0x80 or i + 2 + length >= len(der):
            i += 1
            continue
        name = _DN_OIDS.get(_oid(der[i + 2 : i + 2 + length]))
        after = i + 2 + length
        if name is None or after + 1 >= len(der) or der[after] not in _DER_STRING_TAGS:
            i += 1
            continue
        value_length = der[after + 1]
        found[name] = der[after + 2 : after + 2 + value_length].decode(
            "utf-8", errors="replace"
        )
        i = after + 2 + value_length
    return found
