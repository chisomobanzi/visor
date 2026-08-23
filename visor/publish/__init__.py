"""Publishing builds to Meta Horizon Store release channels.

Everything else in Visor talks to a headset over ADB. This package is the one
part that talks to Meta's servers, and it does so by driving `ovr-platform-util`
— the same command line utility Meta Quest Developer Hub drives internally.
That is deliberate: the upload protocol is undocumented, so wrapping Meta's own
tool is the only route that won't rot.
"""

# Types and small helpers are re-exported; `audit.run` and `upload.upload`
# deliberately are not, because binding those names here would shadow the
# `visor.publish.audit` / `visor.publish.upload` submodules for anyone doing
# `from visor.publish import audit`.
from visor.publish.apk import ApkError, ApkInfo, inspect
from visor.publish.audit import Check, Report
from visor.publish.builds import Build, describe, discover, discover_all
from visor.publish.creds import Credentials, MissingCredentialsError
from visor.publish.ovrtool import OvrToolError, OvrToolNotFoundError
from visor.publish.upload import (
    ConfirmationRequired,
    PreflightError,
    UploadError,
    UploadResult,
)

__all__ = [
    "ApkError",
    "ApkInfo",
    "Build",
    "Check",
    "ConfirmationRequired",
    "Credentials",
    "MissingCredentialsError",
    "OvrToolError",
    "OvrToolNotFoundError",
    "PreflightError",
    "Report",
    "UploadError",
    "UploadResult",
    "describe",
    "discover",
    "discover_all",
    "inspect",
]
