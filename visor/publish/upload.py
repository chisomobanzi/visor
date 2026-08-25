"""Driving `ovr-platform-util upload-quest-build`.

An upload is the one thing Visor does that reaches outside the machine and
cannot be undone by running it again — a build that lands on `store` is on the
live storefront. So the shape here is: check everything cheap before spending
the upload, name the live channel explicitly rather than trusting the caller to
have meant it, and never let the app secret out of the process in readable form.

Flag spellings come from Meta's platform command line utility reference:
https://developers.meta.com/horizon/resources/publish-reference-platform-command-line-utility/
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from visor.adb.wrapper import VisorError
from visor.publish import apk as apk_reader
from visor.publish import audit as store_audit
from visor.publish.creds import Credentials
from visor.publish.ovrtool import require

SUBCOMMAND = "upload-quest-build"

#: Meta's hard limit on a single Quest APK. Anything larger has to move assets
#: into an OBB or a downloadable content chunk.
MAX_APK_BYTES = 1024**3

#: Meta's name for the live storefront channel. Case-insensitive on their side,
#: so every comparison against it here is folded.
RELEASE_CHANNEL = "store"

AGE_GROUPS = ("TEENS_AND_ADULTS", "MIXED_AGES", "CHILDREN")


class PreflightError(VisorError):
    """The upload was refused before anything was sent."""


class ConfirmationRequired(VisorError):
    """A live-storefront upload was requested without explicit consent."""


class UploadError(VisorError):
    """ovr-platform-util ran and failed."""


@dataclass(frozen=True)
class UploadResult:
    succeeded: bool
    command: str
    output: str
    #: Soft warnings raised by preflight. Kept separate from `output` so a
    #: caller can never mistake one of our notes for something Meta said.
    preflight_notes: tuple[str, ...] = ()
    #: True when Meta checked the build and nothing was published.
    validated_only: bool = False


def is_release_channel(channel: str) -> bool:
    return (channel or "").strip().lower() == RELEASE_CHANNEL


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - loop always returns


def _normalise_age_group(age_group: Optional[str]) -> Optional[str]:
    if not age_group:
        return None
    normalised = age_group.strip().upper()
    if normalised not in AGE_GROUPS:
        raise PreflightError(
            f"Unknown age group {age_group!r}. Meta accepts: {', '.join(AGE_GROUPS)}."
        )
    return normalised


def preflight(
    apk: Path,
    channel: str,
    age_group: Optional[str] = None,
    *,
    audit: bool = True,
    expect_package: Optional[str] = None,
    channel_version_code: Optional[int] = None,
) -> list[str]:
    """Check what can be checked locally. Returns notes worth showing the user.

    Hard problems raise; soft ones come back as notes, because they are things
    a user may genuinely have meant (a deliberate draft, a deliberate release).

    The second half of this opens the APK. Everything Meta rejects a build for
    is already written in the file — the signing key, the architectures, the
    orientation, the version code — and Meta only says so once the whole ~800 MB
    has gone over the wire. `audit=False` waives it, because Meta's rules move
    and a stale check of ours must never be the thing that stops a release.
    """
    apk = Path(apk)

    if not apk.exists():
        raise PreflightError(f"APK does not exist: {apk}")
    if not apk.is_file():
        raise PreflightError(f"Not a file: {apk}")
    if apk.suffix.lower() != ".apk":
        raise PreflightError(f"Expected a .apk, got {apk.name}")

    size = apk.stat().st_size
    if size > MAX_APK_BYTES:
        raise PreflightError(
            f"APK is {_human_bytes(size)}, which exceeds Meta's "
            f"{_human_bytes(MAX_APK_BYTES)} limit by "
            f"{_human_bytes(size - MAX_APK_BYTES)}.\n"
            "Move assets into an OBB or ship them as downloadable chunks."
        )

    if not (channel or "").strip():
        raise PreflightError("No release channel given (e.g. ALPHA, BETA, RC, store).")

    normalised_age = _normalise_age_group(age_group)

    notes: list[str] = []
    if normalised_age is None:
        notes.append(
            "No age group given, so Meta will hold this build in draft status "
            f"until you set one ({', '.join(AGE_GROUPS)})."
        )
    if is_release_channel(channel):
        notes.append(
            f"Channel is {channel!r} — this is the live store, visible to "
            "everyone who owns the app."
        )

    if audit:
        notes.extend(
            _audit_notes(
                apk,
                expect_package=expect_package,
                channel_version_code=channel_version_code,
            )
        )
    return notes


def _audit_notes(
    apk: Path,
    *,
    expect_package: Optional[str],
    channel_version_code: Optional[int],
) -> list[str]:
    """Run the store audit, raising on blockers and returning its warnings.

    A file this reader cannot parse is a note, never a refusal: an APK we choke
    on may be perfectly good, and refusing it would make our reader the gate
    rather than Meta's rules.
    """
    try:
        info = apk_reader.inspect(apk)
    except apk_reader.ApkError as error:
        return [f"Could not read the APK, so the store audit was skipped: {error}"]

    report = store_audit.run(
        info,
        expect_package=expect_package,
        channel_version_code=channel_version_code,
    )
    if report.blockers:
        detail = "\n".join(
            f"  - {check.title}: {check.detail}"
            + (f"\n    {check.hint}" if check.hint else "")
            for check in report.blockers
        )
        plural = "" if len(report.blockers) == 1 else "s"
        raise PreflightError(
            f"Meta will reject this build ({len(report.blockers)} blocker{plural}):\n"
            f"{detail}\n"
            "Fix these, or re-run with the store audit waived if you believe "
            "the check is wrong."
        )
    return [
        f"{check.title}: {check.detail}" + (f" {check.hint}" if check.hint else "")
        for check in report.warnings
    ]


def build_argv(
    binary: str | Path,
    credentials: Credentials,
    apk: Path,
    channel: str,
    age_group: Optional[str] = None,
    notes: Optional[str] = None,
    validate_only: bool = False,
    wait_for_postprocessing: bool = True,
) -> list[str]:
    """The exact command line, secret included — see `redacted_argv` for logging."""
    argv = [
        str(binary),
        SUBCOMMAND,
        "--app-id",
        credentials.app_id,
        "--app-secret",
        credentials.app_secret,
        "--apk",
        str(apk),
        "--channel",
        channel.strip(),
        # We are never on a TTY here; the animated bar would arrive as a wall of
        # carriage returns instead of progress lines.
        "--disable-progress-bar",
    ]
    normalised_age = _normalise_age_group(age_group)
    if normalised_age:
        argv += ["--age-group", normalised_age]
    if notes:
        argv += ["--notes", notes]
    if validate_only:
        # Meta checks the build and stops. Nothing is published, so there is
        # also nothing to post-process.
        argv.append("--validate")
    elif wait_for_postprocessing:
        # Malware and security checks run after the bytes land, and a build
        # that fails them was not really uploaded. Without this the utility
        # exits 0 long before anyone knows that.
        argv.append("--wait-for-postprocessing")
    return argv


def redacted_argv(argv: Sequence[str], credentials: Credentials) -> list[str]:
    """The same command with the secret removed, safe to log or return."""
    return [credentials.redact(arg) for arg in argv]


def _spawn(argv: Sequence[str]) -> subprocess.Popen:
    """Start the utility with stderr folded into stdout, line-buffered.

    Split out so tests can stand in for the subprocess without patching
    subprocess itself.
    """
    return subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def upload(
    apk: Path,
    channel: str,
    credentials: Credentials,
    age_group: Optional[str] = None,
    notes: Optional[str] = None,
    binary: Optional[str | Path] = None,
    confirm_release: bool = False,
    on_line: Optional[Callable[[str], None]] = None,
    validate_only: bool = False,
    audit: bool = True,
    expect_package: Optional[str] = None,
    channel_version_code: Optional[int] = None,
) -> UploadResult:
    """Upload a build, streaming redacted progress lines to `on_line`.

    `confirm_release` only unlocks the live store channel; it relaxes nothing
    else, so a caller that sets it still gets every other preflight check.

    `validate_only` asks Meta to check the build and stop. It exercises the
    credentials, the network and Meta's own validation without putting a build
    in front of testers, which makes it the honest way to rehearse an upload.
    """
    apk = Path(apk)
    preflight_notes = preflight(
        apk,
        channel,
        age_group,
        audit=audit,
        expect_package=expect_package,
        channel_version_code=channel_version_code,
    )

    if is_release_channel(channel) and not confirm_release and not validate_only:
        raise ConfirmationRequired(
            f"Channel {channel!r} is the live Meta Horizon store. "
            "Re-run with explicit confirmation if you meant to publish "
            "to everyone who owns the app."
        )

    resolved = Path(binary) if binary else require()
    argv = build_argv(
        resolved,
        credentials,
        apk,
        channel,
        age_group,
        notes,
        validate_only=validate_only,
    )
    shown = " ".join(redacted_argv(argv, credentials))

    process = _spawn(argv)
    collected: list[str] = []
    for raw in _stream(process):
        line = credentials.redact(raw.rstrip("\n"))
        collected.append(line)
        if on_line:
            on_line(line)

    if process.wait() != 0:
        tail = "\n".join(collected[-20:]) or "(no output)"
        raise UploadError(f"{SUBCOMMAND} failed.\n{tail}")

    return UploadResult(
        succeeded=True,
        command=shown,
        output="\n".join(collected),
        preflight_notes=tuple(preflight_notes),
        validated_only=validate_only,
    )


def _stream(process) -> Iterable[str]:
    return process.stdout or ()


# ---------------------------------------------------------------------------
# Reading the utility's mind.
#
# ovr-platform-util is chatty but unstructured. Two moments matter enough to
# classify: the long silence after the bytes have left (Meta postprocessing —
# which reads exactly like a hang unless named), and the credential failure
# whose headline blames the App ID when the Graph error underneath says the
# SECRET went stale (that misdirection cost a real upload on 2026-08-23).
# ---------------------------------------------------------------------------

_POSTPROCESS_MARKERS = ("POSTPRO", "post-process", "postprocess", "Processing build")
_UPLOAD_MARKERS = ("Uploading", "upload progress", "%", "retried")
_DONE_MARKERS = ("Successfully", "Uploaded to")


def phase_of(lines: "list[str]") -> str:
    """Which stage the utility is in, judged from its most recent output."""
    for line in reversed(lines):
        if any(m in line for m in _DONE_MARKERS):
            return "finishing"
        if any(m in line for m in _POSTPROCESS_MARKERS):
            return "postprocessing"
        if any(m in line for m in _UPLOAD_MARKERS):
            return "uploading"
    return "starting"


def diagnose_failure(output: str) -> Optional[str]:
    """A targeted hint for the failures whose error text points the wrong way."""
    if ("OAuthException" in output and "190" in output) or (
        "App ID or App Secret must be invalid" in output
    ):
        return (
            "This headline usually means the App SECRET went stale, not the App "
            "ID — re-copy the secret from developers.meta.com/horizon/manage → "
            "your app → Development → API, and leave the App ID alone. (The "
            "Graph error underneath — code 190, token verification — is the "
            "honest half of the message.)"
        )
    return None
