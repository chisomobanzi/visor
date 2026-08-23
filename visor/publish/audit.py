"""Meta Horizon Store rules an APK can be judged against before it is uploaded.

Every rule here corresponds to something Meta checks — some at upload, some at
review — and every one of them is visible in the file beforehand. Learning that
a build is debug-signed after pushing 800 MB and waiting for review is the
expensive way to find out, and the fix is always another cook.

Two levels do the work. A **blocker** is not a judgement call: the upload will be
refused or the build rejected, and shipping it is not an option the developer
has. A **warning** is a build that will go up and may still be wrong for reasons
only its author knows — so warnings inform and never stop anything. Anything the
file cannot settle alone (is this version code higher than the channel's? is
this the app you meant?) is reported as **info** until the caller supplies the
missing half.

The rules are about Quest, not about any one app. Where Meta's requirements have
a moving floor — the target SDK minimum rises every year or so — the number is
named here with a comment saying so, because a stale floor that silently passes
is worse than no check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from visor.publish.apk import ApkInfo

#: Meta's hard ceiling on a single Quest APK.
MAX_APK_BYTES = 1024**3

#: Meta's minimum target SDK for new Quest uploads. Meta raises this
#: periodically; when an upload is rejected for target SDK despite this check
#: passing, this is the number that went stale.
MIN_TARGET_SDK = 32

#: Quest runs 64-bit only. A build without this ABI has no code to run.
REQUIRED_ABI = "arm64-v8a"

#: Permissions that are fine to hold and still oblige you to answer for them on
#: the dashboard — Data Use Checkup, and a privacy policy that names them.
SENSITIVE_PERMISSIONS = {
    "android.permission.RECORD_AUDIO": "microphone",
    "android.permission.CAMERA": "camera",
    "android.permission.ACCESS_FINE_LOCATION": "precise location",
    "android.permission.ACCESS_COARSE_LOCATION": "location",
    "android.permission.READ_CONTACTS": "contacts",
    "com.oculus.permission.EYE_TRACKING": "eye tracking",
    "com.oculus.permission.FACE_TRACKING": "face tracking",
    "com.oculus.permission.BODY_TRACKING": "body tracking",
    "com.oculus.permission.USE_SCENE": "scene / spatial data",
}

BLOCKER = "blocker"
WARN = "warn"
INFO = "info"
OK = "ok"

_ORDER = {BLOCKER: 0, WARN: 1, INFO: 2, OK: 3}


@dataclass(frozen=True)
class Check:
    """One rule's verdict on one build."""

    id: str
    level: str
    title: str
    detail: str
    hint: str = ""

    def summary(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class Report:
    """What the audit concluded, worst news first."""

    checks: list[Check] = field(default_factory=list)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.level == BLOCKER]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == WARN]

    @property
    def ready(self) -> bool:
        """True when nothing here stands between this build and an upload."""
        return not self.blockers

    def summary(self) -> dict:
        ordered = sorted(self.checks, key=lambda c: _ORDER.get(c.level, 9))
        return {
            "ready": self.ready,
            "blockers": len(self.blockers),
            "warnings": len(self.warnings),
            "checks": [c.summary() for c in ordered],
        }


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def run(
    info: ApkInfo,
    *,
    expect_package: Optional[str] = None,
    channel_version_code: Optional[int] = None,
) -> Report:
    """Audit a build. `expect_package` and `channel_version_code` are optional
    facts the file cannot know: which app this is meant to be, and what the
    target channel already serves."""
    checks: list[Check] = [
        _package(info, expect_package),
        _version_code(info, channel_version_code),
        _size(info),
        _signed(info),
        _signing_key(info),
        _signature_scheme(info),
        _abi(info),
        _target_sdk(info),
        _orientation(info),
        _vr_category(info),
        _debuggable(info),
        _supported_devices(info),
        _headtracking(info),
        _sensitive_permissions(info),
    ]
    return Report(checks=checks)


def _package(info: ApkInfo, expected: Optional[str]) -> Check:
    found = info.package or "(none)"
    if not info.package:
        return Check(
            "package",
            BLOCKER,
            "Package name",
            "The manifest declares no package name.",
            "This is not a build any store can accept — re-cook it.",
        )
    if expected and expected != info.package:
        return Check(
            "package",
            BLOCKER,
            "Package name",
            f"This APK is {found}, not {expected}.",
            "A project with more than one app configuration cooks whichever one "
            "is currently selected. Switch it and cook again.",
        )
    return Check("package", OK, "Package name", found)


def _version_code(info: ApkInfo, channel_version_code: Optional[int]) -> Check:
    code = info.version_code
    name = info.version_name or "?"
    if code is None:
        return Check(
            "version-code",
            BLOCKER,
            "Version code",
            "The manifest declares no versionCode.",
        )
    label = f"versionCode {code} (versionName {name})"
    if channel_version_code is None:
        return Check(
            "version-code",
            INFO,
            "Version code",
            label,
            "Meta refuses an upload whose version code is not higher than the "
            "one the channel already serves. Check the channel before uploading.",
        )
    if code <= channel_version_code:
        return Check(
            "version-code",
            BLOCKER,
            "Version code",
            f"{label}, but the channel already serves {channel_version_code}.",
            "Raise the version code in your project settings and cook again.",
        )
    return Check(
        "version-code",
        OK,
        "Version code",
        f"{label}, above the channel's {channel_version_code}.",
    )


def _size(info: ApkInfo) -> Check:
    size = _human_bytes(info.size_bytes)
    if info.size_bytes > MAX_APK_BYTES:
        over = _human_bytes(info.size_bytes - MAX_APK_BYTES)
        return Check(
            "size",
            BLOCKER,
            "Size",
            f"{size}, over Meta's {_human_bytes(MAX_APK_BYTES)} APK limit by {over}.",
            "Move assets into an OBB or ship them as downloadable content.",
        )
    return Check("size", OK, "Size", f"{size} of {_human_bytes(MAX_APK_BYTES)}")


def _signed(info: ApkInfo) -> Check:
    if not info.signature.signed:
        return Check(
            "signed",
            BLOCKER,
            "Signed",
            "This APK carries no signature at all.",
            "An unsigned build cannot be installed or uploaded. Package it with "
            "your release keystore — for Unreal, that is a distribution build.",
        )
    return Check(
        "signed", OK, "Signed", f"{', '.join(info.signature.schemes)} signature present"
    )


def _signing_key(info: ApkInfo) -> Check:
    signature = info.signature
    if not signature.signed:
        return Check("signing-key", OK, "Signing key", "—")
    if signature.debug_signed:
        return Check(
            "signing-key",
            BLOCKER,
            "Signing key",
            "Signed with the Android debug key (CN=Android Debug).",
            "Every store rejects the debug key. Build for distribution so the "
            "release keystore is used — a Shipping configuration alone does not "
            "do it.",
        )
    who = signature.subject_dn or "unknown subject"
    digest = (signature.certificate_sha256 or "")[:16]
    return Check(
        "signing-key",
        OK,
        "Signing key",
        who,
        f"Certificate SHA-256 starts {digest}… — an update must be signed with "
        "this same key or the store will not accept it.",
    )


def _signature_scheme(info: ApkInfo) -> Check:
    schemes = info.signature.schemes
    if not schemes:
        return Check("signature-scheme", OK, "Signature scheme", "—")
    if schemes == ["v1"]:
        return Check(
            "signature-scheme",
            WARN,
            "Signature scheme",
            "Only the old v1 (JAR) signature is present.",
            "Modern Android wants APK Signature Scheme v2 or later. It will "
            "probably still install, but sign with v2 as well.",
        )
    return Check("signature-scheme", OK, "Signature scheme", ", ".join(schemes))


def _abi(info: ApkInfo) -> Check:
    if not info.abis:
        return Check(
            "abi",
            BLOCKER,
            "Architecture",
            "No native libraries in the APK.",
            "A Quest build ships arm64-v8a native code. An APK without it has "
            "nothing to run.",
        )
    if REQUIRED_ABI not in info.abis:
        return Check(
            "abi",
            BLOCKER,
            "Architecture",
            f"{', '.join(info.abis)} — no {REQUIRED_ABI}.",
            "Quest is 64-bit only; a 32-bit build will not install.",
        )
    extra = [a for a in info.abis if a != REQUIRED_ABI]
    if extra:
        return Check(
            "abi",
            WARN,
            "Architecture",
            f"{REQUIRED_ABI} plus {', '.join(extra)}.",
            "Quest never loads the 32-bit slice — it is size you are paying for "
            "and uploading for nothing.",
        )
    return Check("abi", OK, "Architecture", REQUIRED_ABI)


def _target_sdk(info: ApkInfo) -> Check:
    target = info.target_sdk
    if target is None:
        return Check(
            "target-sdk",
            BLOCKER,
            "Target SDK",
            "The manifest declares no targetSdkVersion.",
            f"Meta requires at least {MIN_TARGET_SDK}.",
        )
    if target < MIN_TARGET_SDK:
        return Check(
            "target-sdk",
            BLOCKER,
            "Target SDK",
            f"targetSdkVersion {target} is below Meta's minimum of {MIN_TARGET_SDK}.",
            "Raise the target SDK in your project's Android settings and cook again.",
        )
    return Check(
        "target-sdk",
        OK,
        "Target SDK",
        f"targetSdkVersion {target} (min {info.min_sdk or '?'})",
        "Meta raises this floor periodically — worth re-reading their current "
        "requirement before a release.",
    )


def _orientation(info: ApkInfo) -> Check:
    if info.orientation is None:
        return Check(
            "orientation",
            WARN,
            "Orientation",
            "No screenOrientation on the main activity.",
            "Meta expects landscape and validates it at upload.",
        )
    if not info.is_landscape:
        return Check(
            "orientation",
            BLOCKER,
            "Orientation",
            f"screenOrientation is {info.orientation}, which is not landscape.",
            "Meta's upload validation rejects this outright.",
        )
    return Check("orientation", OK, "Orientation", "landscape")


def _vr_category(info: ApkInfo) -> Check:
    if not info.is_vr_app:
        return Check(
            "vr-category",
            BLOCKER,
            "VR app",
            "No com.oculus.intent.category.VR on the launch activity.",
            "Without it the headset runs the build as a flat panel app and the "
            "store will not take it as a VR title.",
        )
    return Check("vr-category", OK, "VR app", "declares com.oculus.intent.category.VR")


def _debuggable(info: ApkInfo) -> Check:
    if info.debuggable:
        return Check(
            "debuggable",
            BLOCKER,
            "Debuggable",
            "android:debuggable is true.",
            "A debuggable build is rejected by the store and lets anyone attach "
            "to the running app. Cook a Shipping/distribution build.",
        )
    return Check("debuggable", OK, "Debuggable", "false")


def _supported_devices(info: ApkInfo) -> Check:
    devices = info.supported_devices
    if not devices:
        return Check(
            "supported-devices",
            WARN,
            "Supported devices",
            "No com.oculus.supportedDevices declared.",
            "Meta uses this to decide which headsets the store offers the build "
            "to. Undeclared, it can be offered to hardware you never tested.",
        )
    unique = sorted(set(filter(None, devices.split("|"))))
    return Check("supported-devices", OK, "Supported devices", ", ".join(unique))


def _headtracking(info: ApkInfo) -> Check:
    if "android.hardware.vr.headtracking" not in info.features:
        return Check(
            "headtracking",
            WARN,
            "Head tracking",
            "No android.hardware.vr.headtracking feature declared.",
            "Standard for a 6DoF Quest title; its absence usually means a "
            "manifest that was never set up for VR.",
        )
    return Check("headtracking", OK, "Head tracking", "declared")


def _sensitive_permissions(info: ApkInfo) -> Check:
    found = [p for p in info.permissions if p in SENSITIVE_PERMISSIONS]
    if not found:
        return Check(
            "sensitive-permissions",
            OK,
            "Sensitive permissions",
            "none requested",
        )
    described = ", ".join(f"{p} ({SENSITIVE_PERMISSIONS[p]})" for p in found)
    return Check(
        "sensitive-permissions",
        WARN,
        "Sensitive permissions",
        described,
        "Not a problem, but each one has to be declared in Data Use Checkup and "
        "covered by the privacy policy on your store listing. Review takes the "
        "difference personally.",
    )
