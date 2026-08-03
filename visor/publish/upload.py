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
) -> list[str]:
    """Check what can be checked locally. Returns notes worth showing the user.

    Hard problems raise; soft ones come back as notes, because they are things
    a user may genuinely have meant (a deliberate draft, a deliberate release).
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
    return notes


def build_argv(
    binary: str | Path,
    credentials: Credentials,
    apk: Path,
    channel: str,
    age_group: Optional[str] = None,
    notes: Optional[str] = None,
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
) -> UploadResult:
    """Upload a build, streaming redacted progress lines to `on_line`.

    `confirm_release` only unlocks the live store channel; it relaxes nothing
    else, so a caller that sets it still gets every other preflight check.
    """
    apk = Path(apk)
    preflight_notes = preflight(apk, channel, age_group)

    if is_release_channel(channel) and not confirm_release:
        raise ConfirmationRequired(
            f"Channel {channel!r} is the live Meta Horizon store. "
            "Re-run with explicit confirmation if you meant to publish "
            "to everyone who owns the app."
        )

    resolved = Path(binary) if binary else require()
    argv = build_argv(resolved, credentials, apk, channel, age_group, notes)
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
    )


def _stream(process) -> Iterable[str]:
    return process.stdout or ()
