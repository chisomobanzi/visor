"""Signing the Claude playtester in.

The Anthropic SDK finds credentials by itself in the environment
(ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN) or in an `ant auth login` profile.
For everyone else Visor keeps an API key of its own, entered once in the
dashboard or with `visor sim login`. It lives in ~/.config/visor/anthropic-key,
readable only by you; Visor only ever shows its last four characters, and
never writes it into logs, errors or reports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from visor.utils.host import config_home

ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CHECK_MODEL = "claude-opus-5-5"


class CredentialsError(ValueError):
    """The key was refused, or couldn't be stored. The message never holds the key."""


def key_path() -> Path:
    return config_home() / "visor" / "anthropic-key"


def stored_key() -> Optional[str]:
    try:
        key = key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _tail(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 12 else "…"


def _profile_dir() -> Path:
    if os.environ.get("ANTHROPIC_CONFIG_DIR"):
        return Path(os.environ["ANTHROPIC_CONFIG_DIR"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def _have_profile() -> bool:
    try:
        return any((_profile_dir() / "credentials").glob("*.json"))
    except OSError:
        return False


def source() -> Optional[dict]:
    """Where the playtester's credentials will come from, if anywhere:
    {kind: env | saved | profile, label}. Never the credential itself."""
    for var in ENV_VARS:
        if os.environ.get(var):
            return {"kind": "env", "label": f"{var} from the environment"}
    key = stored_key()
    if key:
        return {"kind": "saved", "label": f"your saved key {_tail(key)}"}
    if _have_profile():
        return {"kind": "profile", "label": "your `ant auth login` profile"}
    return None


def client_kwargs() -> dict:
    """Arguments for anthropic.Anthropic(): the saved key, unless the
    environment says otherwise (a one-off export wins, as it does in the SDK)."""
    if any(os.environ.get(v) for v in ENV_VARS):
        return {}
    key = stored_key()
    return {"api_key": key} if key else {}


def check(key: str) -> Optional[str]:
    """Ask Anthropic whether the key works (a model lookup: free). Raises
    CredentialsError if it was refused; returns a note if it couldn't be checked."""
    import anthropic

    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0)
    try:
        client.models.retrieve(CHECK_MODEL)
    except anthropic.AuthenticationError:
        raise CredentialsError("Anthropic didn't accept that key. Copy it again from console.anthropic.com → API Keys.") \
            from None
    except anthropic.PermissionDeniedError:
        raise CredentialsError(f"that key works but can't use {CHECK_MODEL}; check its workspace in the Anthropic Console.") \
            from None
    except anthropic.NotFoundError:
        return f"the key works, but {CHECK_MODEL} wasn't found for it"
    except anthropic.APIConnectionError:
        return "saved without checking it: Anthropic couldn't be reached"
    except anthropic.APIStatusError as e:
        return f"saved without checking it: Anthropic answered {e.status_code}"
    return None


def save(key: str, verify: bool = True) -> dict:
    """Check and keep a key; returns {label, path, note}."""
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise CredentialsError("that doesn't look like an API key (they start sk-ant-)")
    note = check(key) if verify else None
    path = key_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created owner-only rather than chmod-ed after, so it's never briefly readable by others.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        path.chmod(0o600)
    except OSError as e:
        raise CredentialsError(f"couldn't save the key in {path}: {e.strerror or e}") from None
    return {"label": f"your saved key {_tail(key)}", "path": str(path), "note": note}


def forget() -> bool:
    try:
        key_path().unlink()
        return True
    except FileNotFoundError:
        return False
