"""Meta app credentials: where they come from, and keeping them out of sight.

An App ID identifies your app and is not especially sensitive. The App Secret
is — it is enough to publish a build to any of your release channels.

Meta's utility only accepts the secret on its command line, so it is inherently
visible to `ps` on a shared machine. Visor cannot fix that. What it can do is
guarantee that Visor itself never writes the secret anywhere: not into a
traceback, not into a log line, not into an error surfaced to an MCP client.
Hence a Credentials type whose repr is redacted by construction, rather than a
bare string that any f-string could spill.
"""

from __future__ import annotations

import os
import stat
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from visor.adb.wrapper import VisorError
from visor.utils.host import config_home

ID_VAR = "OCULUS_APP_ID"
SECRET_VAR = "OCULUS_APP_SECRET"

_PLACEHOLDER = "<REDACTED>"

_DASHBOARD_URL = "https://developers.meta.com/horizon/manage"


class MissingCredentialsError(VisorError):
    """No App ID/Secret in the environment or the config file."""


@dataclass(frozen=True)
class Credentials:
    app_id: str
    app_secret: str

    def __repr__(self) -> str:
        # Named explicitly rather than relying on the dataclass repr, which
        # would happily print the secret into any traceback.
        return f"Credentials(app_id={self.app_id!r}, app_secret={_PLACEHOLDER})"

    __str__ = __repr__

    def redact(self, text: Any) -> Any:
        """Scrub the secret out of arbitrary text.

        Non-strings pass through untouched, and an empty secret is a no-op —
        ``"".replace("", x)`` would otherwise insert the placeholder between
        every character and mangle output that had nothing to hide.
        """
        if not isinstance(text, str) or not self.app_secret:
            return text
        return text.replace(self.app_secret, _PLACEHOLDER)


def config_path() -> Path:
    return config_home() / "visor" / "credentials"


def _parse(text: str) -> dict[str, str]:
    """Read KEY=value lines, tolerating comments, blanks, spacing and quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _warn_if_readable(path: Path) -> None:
    if sys.platform == "win32":
        # Windows guards files with ACLs, which st_mode doesn't show: it
        # reports 0666 for any writable file, so this would cry wolf every time.
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:  # pragma: no cover - unreadable file fails later anyway
        return
    if mode & 0o077:
        warnings.warn(
            f"{path} is readable by other users (mode {mode:04o}). "
            f"Your Meta app secret is in it — run: chmod 0600 {path}",
            UserWarning,
            stacklevel=3,
        )


def _from_file(path: Optional[Path]) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    _warn_if_readable(path)
    return _parse(path.read_text())


def load(
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
) -> Credentials:
    """Resolve credentials from the environment, then the config file.

    The environment wins so a one-off `OCULUS_APP_ID=… visor upload` can
    override a stored default. Blank values are treated as absent: an
    exported-but-empty variable is a CI mistake, not an instruction to upload
    with no secret.
    """
    env = os.environ if env is None else env
    path = config_path() if path is None else path

    from_file = _from_file(path)

    def pick(key: str) -> str:
        return (env.get(key) or "").strip() or from_file.get(key, "").strip()

    app_id, app_secret = pick(ID_VAR), pick(SECRET_VAR)

    if not app_id or not app_secret:
        missing = [n for n, v in ((ID_VAR, app_id), (SECRET_VAR, app_secret)) if not v]
        raise MissingCredentialsError(
            f"Meta app credentials incomplete (missing: {', '.join(missing)}).\n"
            f"Set {ID_VAR} and {SECRET_VAR} in the environment, or write them to\n"
            f"    {path}\n"
            f"as {ID_VAR}=... / {SECRET_VAR}=... then chmod 0600 it.\n"
            f"Both live at {_DASHBOARD_URL} -> your app -> Development -> API."
        )

    return Credentials(app_id=app_id, app_secret=app_secret)


def save(credentials: Credentials, path: Optional[Path] = None) -> Path:
    """Write credentials to disk, owner-readable only."""
    path = config_path() if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive perms rather than writing then chmod-ing, so the
    # secret is never briefly world-readable on disk.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(
            f"{ID_VAR}={credentials.app_id}\n"
            f"{SECRET_VAR}={credentials.app_secret}\n"
        )
    path.chmod(0o600)
    return path
