"""The dashboard's publish surface: pick a build, audit it, send it to Meta.

Publishing is the one thing Visor does that leaves this machine and cannot be
undone by running it again, and it is also the thing a solo developer does
rarely enough to forget the order of. So this is written as a runway rather
than a button: what the tool needs, which build, what is wrong with it, a
rehearsal against Meta's own validator, then the upload — and afterwards a
checklist of the parts only the Meta dashboard can do, kept on disk because
the person doing them will close this window and come back tomorrow.

Two rules shape the code. The app secret never leaves this process in readable
form: it is stored 0600, redacted out of every line before it reaches a
browser, and never returned by any route. And no route ever guesses — an
upload names its channel explicitly and the live storefront needs a separate,
deliberate confirmation.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from visor.adb.wrapper import VisorError
from visor.publish import apk as apk_reader
from visor.publish import audit as store_audit
from visor.publish import builds as build_finder
from visor.publish import creds as creds_module
from visor.publish import ovrtool, upload as uploader
from visor.utils.host import config_home

router = APIRouter(prefix="/api/publish")

#: Meta's own channel names. `store` is the live storefront; the rest are test
#: channels you can push to freely.
CHANNELS = ("ALPHA", "BETA", "RC", "store")

_STATE_LOCK = threading.Lock()


def state_path() -> Path:
    """Where the runway remembers itself between sessions."""
    return config_home() / "visor" / "publish-ui.json"


def _load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _update_state(**changes) -> dict:
    with _STATE_LOCK:
        state = _load_state()
        state.update({k: v for k, v in changes.items() if v is not None})
        _save_state(state)
        return state


# --------------------------------------------------------------------------
# The checklist of things only the Meta dashboard can do.
#
# Uploading a build is roughly half of a submission. The other half is on
# Meta's website, is easy to half-finish, and is the reason a build sits in
# limbo for a week. Listing it here — with state that survives a restart —
# turns "did I do that?" into something answerable.
# --------------------------------------------------------------------------

SUBMISSION_CHECKLIST = [
    {
        "id": "channel",
        "title": "Point the release channel at this build",
        "detail": "An uploaded build is not live until a channel points at it. "
        "Meta Horizon dashboard → Distribution → Release Channels.",
    },
    {
        "id": "data-use",
        "title": "Data Use Checkup approved and unexpired",
        "detail": "Required for microphone, user ID, IAP and tracking permissions. "
        "It expires 12 months after approval, and adding a platform feature "
        "needs a fresh submission.",
    },
    {
        "id": "listing",
        "title": "Store listing complete",
        "detail": "Name, short and long description, keywords, category, age rating, "
        "and the language variants you intend to ship.",
    },
    {
        "id": "assets",
        "title": "Store assets uploaded",
        "detail": "Cover art, icon, hero image, and at least the minimum number of "
        "screenshots or a trailer, at the sizes Meta specifies.",
    },
    {
        "id": "privacy",
        "title": "Privacy policy and data safety answered",
        "detail": "A reachable public privacy-policy URL, plus the data-collection "
        "questionnaire.",
    },
    {
        "id": "iap",
        "title": "Add-ons and subscriptions published",
        "detail": "Every SKU the app queries must exist and be Published, or the "
        "purchase flow fails on a reviewer's headset.",
    },
    {
        "id": "review-notes",
        "title": "Review instructions written",
        "detail": "Test account, how to reach the paid surfaces, and anything a "
        "reviewer would otherwise never find.",
    },
    {
        "id": "submit",
        "title": "Submit for review",
        "detail": "The final button. Everything above has to be green first.",
    },
]


# --------------------------------------------------------------------------
# Upload jobs.
#
# An upload runs for many minutes and streams progress, so it cannot be a
# request. It is a background thread whose lines a browser polls; the process
# handle is kept so the job can actually be stopped, not merely forgotten.
# --------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    channel: str
    apk: str
    validate_only: bool
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    status: str = "running"
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None
    command: Optional[str] = None
    cancelled: bool = False
    _process: Any = None

    def summary(self, since: int = 0) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "channel": self.channel,
            "apk": self.apk,
            "validate_only": self.validate_only,
            "started": self.started,
            "finished": self.finished,
            "elapsed": (self.finished or time.time()) - self.started,
            "lines": self.lines[since:],
            "total_lines": len(self.lines),
            "notes": self.notes,
            "error": self.error,
            "command": self.command,
        }


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def _remember_process(job: Job):
    """Hand `_spawn` a hook so a running upload can be cancelled."""
    original = uploader._spawn

    def spawn(argv):
        process = original(argv)
        job._process = process
        return process

    return original, spawn


def _run_job(job: Job, **kwargs) -> None:
    original, patched = _remember_process(job)
    uploader._spawn = patched
    try:
        result = uploader.upload(on_line=job.lines.append, **kwargs)
        job.notes = list(result.preflight_notes)
        job.command = result.command
        job.status = "validated" if result.validated_only else "succeeded"
    except (uploader.PreflightError, uploader.ConfirmationRequired) as error:
        # Nothing ran and nothing was sent, which is a different thing from a
        # failed upload and should not read like one.
        job.status = "refused"
        job.error = str(error)
    except VisorError as error:
        job.status = "cancelled" if job.cancelled else "failed"
        job.error = str(error)
    except Exception as error:  # a crash here must not hang the browser
        job.status = "failed"
        job.error = f"{type(error).__name__}: {error}"
    finally:
        uploader._spawn = original
        job.finished = time.time()
        job._process = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/status")
def status():
    """Everything the runway needs before it can start: tool, credentials."""
    binary = ovrtool.find()
    version = None
    if binary:
        try:
            version = ovrtool.version(binary)
        except VisorError:
            version = None

    stored = None
    source = None
    try:
        loaded = creds_module.load()
        stored = loaded.app_id
        source = "environment" if creds_module.ID_VAR in _environ() else "file"
    except creds_module.MissingCredentialsError:
        pass

    state = _load_state()
    return {
        "tool": {
            "found": binary is not None,
            "path": str(binary) if binary else None,
            "version": version,
            "cache_dir": str(ovrtool.cache_dir()),
        },
        "credentials": {
            "configured": stored is not None,
            "app_id": stored,
            "source": source,
            "path": str(creds_module.config_path()),
        },
        "channels": list(CHANNELS),
        "age_groups": list(uploader.AGE_GROUPS),
        "state": state,
        "checklist": _checklist_view(state),
    }


def _environ() -> dict:
    import os

    return dict(os.environ)


@router.post("/tool/fetch")
def fetch_tool():
    """Download Meta's utility into Visor's cache. Never automatic."""
    try:
        path = ovrtool.fetch()
    except VisorError as error:
        raise HTTPException(502, str(error)) from None
    return {"path": str(path), "version": ovrtool.version(path)}


class CredentialsIn(BaseModel):
    app_id: str
    app_secret: str


@router.post("/credentials")
def save_credentials(body: CredentialsIn):
    """Store the App ID and Secret 0600 in the user's config directory."""
    app_id = body.app_id.strip()
    secret = body.app_secret.strip()
    if not app_id or not secret:
        raise HTTPException(400, "Both an App ID and an App Secret are required.")
    try:
        creds_module.save(creds_module.Credentials(app_id=app_id, app_secret=secret))
    except VisorError as error:
        raise HTTPException(500, str(error)) from None
    # Deliberately returns the ID only. The secret never comes back out.
    return {"app_id": app_id, "path": str(creds_module.config_path())}


@router.get("/builds")
def list_builds(root: Optional[str] = None, limit: int = 25):
    """The APKs on this machine, newest first."""
    if root:
        roots = build_finder.default_roots(Path(root).expanduser())
        if not roots:
            expanded = Path(root).expanduser()
            roots = [expanded] if expanded.exists() else []
            if not roots:
                raise HTTPException(404, f"No such directory: {root}")
        _update_state(root=str(Path(root).expanduser()))
    else:
        remembered = _load_state().get("root")
        if not remembered:
            return {"root": None, "builds": []}
        roots = build_finder.default_roots(Path(remembered)) or [Path(remembered)]

    found = build_finder.discover_all(roots, limit=limit)
    return {
        "root": str(roots[0]) if roots else None,
        "roots": [str(r) for r in roots],
        "builds": [b.summary() for b in found],
    }


@router.get("/audit")
def audit_build(
    path: str,
    expect_package: Optional[str] = None,
    channel_version_code: Optional[int] = None,
):
    """Read the APK and check it against every Store rule we can check here."""
    apk_path = Path(path).expanduser()
    try:
        info = apk_reader.inspect(apk_path)
    except apk_reader.ApkError as error:
        raise HTTPException(400, str(error)) from None

    report = store_audit.run(
        info,
        expect_package=expect_package or None,
        channel_version_code=channel_version_code,
    )
    return {"apk": info.summary(), "report": report.summary()}


class UploadIn(BaseModel):
    path: str
    channel: str
    age_group: Optional[str] = None
    notes: Optional[str] = None
    validate_only: bool = False
    confirm_release: bool = False
    audit: bool = True
    expect_package: Optional[str] = None
    channel_version_code: Optional[int] = None


@router.post("/upload")
def start_upload(body: UploadIn):
    """Begin an upload in the background and hand back a job to watch."""
    try:
        credentials = creds_module.load()
    except creds_module.MissingCredentialsError as error:
        raise HTTPException(400, str(error)) from None

    if not ovrtool.find():
        raise HTTPException(
            400,
            "ovr-platform-util is not installed. Fetch it first — Visor will "
            "never download it behind your back.",
        )

    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if j.status == "running"]
    if running:
        raise HTTPException(409, "An upload is already running.")

    _update_state(
        channel=body.channel,
        age_group=body.age_group,
        last_apk=body.path,
    )

    job = Job(
        id=uuid.uuid4().hex[:12],
        channel=body.channel,
        apk=body.path,
        validate_only=body.validate_only,
    )
    with _JOBS_LOCK:
        _JOBS[job.id] = job

    thread = threading.Thread(
        target=_run_job,
        args=(job,),
        kwargs=dict(
            apk=Path(body.path).expanduser(),
            channel=body.channel,
            credentials=credentials,
            age_group=body.age_group,
            notes=body.notes,
            confirm_release=body.confirm_release,
            validate_only=body.validate_only,
            audit=body.audit,
            expect_package=body.expect_package or None,
            channel_version_code=body.channel_version_code,
        ),
        daemon=True,
        name=f"visor-upload-{job.id}",
    )
    thread.start()
    return job.summary()


@router.get("/upload/{job_id}")
def upload_status(job_id: str, since: int = 0):
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"No such upload job: {job_id}")
    return job.summary(since=since)


@router.post("/upload/{job_id}/cancel")
def cancel_upload(job_id: str):
    """Stop an upload that is still going.

    Honest about what this means: the bytes already sent are sent. Meta
    discards an incomplete upload, so nothing lands, but a cancel is not an
    undo — it is a decision not to finish.
    """
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, f"No such upload job: {job_id}")
    if job.status != "running":
        return job.summary()
    job.cancelled = True
    process = job._process
    if process is not None:
        try:
            process.terminate()
        except OSError:
            pass
    return job.summary()


@router.get("/uploads")
def recent_uploads(limit: int = 10):
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.started, reverse=True)
    return [
        {k: v for k, v in job.summary().items() if k != "lines"} for job in jobs[:limit]
    ]


def _checklist_view(state: dict) -> list[dict]:
    done = set(state.get("checklist_done") or [])
    return [{**item, "done": item["id"] in done} for item in SUBMISSION_CHECKLIST]


class ChecklistIn(BaseModel):
    id: str
    done: bool


@router.post("/checklist")
def set_checklist(body: ChecklistIn):
    known = {item["id"] for item in SUBMISSION_CHECKLIST}
    if body.id not in known:
        raise HTTPException(404, f"No such checklist item: {body.id}")
    with _STATE_LOCK:
        state = _load_state()
        done = set(state.get("checklist_done") or [])
        done.add(body.id) if body.done else done.discard(body.id)
        state["checklist_done"] = sorted(done)
        _save_state(state)
    return {"checklist": _checklist_view(state)}
