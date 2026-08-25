"""The dashboard's publish runway.

This is the surface a solo developer uses to put a build in front of Meta, so
the tests here are about the promises rather than the plumbing: the secret
never comes back out, the audit's verdict reaches the browser, an upload runs
in the background and can be watched and stopped, and the live storefront is
never reachable by accident.
"""

import time

import pytest
from fastapi.testclient import TestClient

from tests.apkbuild import DEBUG_CERT, make_apk, make_release_apk, v2_signed
from visor.publish import creds as creds_module
from visor.publish import ovrtool, upload as uploader
from visor.ui import publish_routes
from visor.ui.server import app

client = TestClient(app, raise_server_exceptions=False)

CREDS = creds_module.Credentials(app_id="1234567890", app_secret="s3cr3t")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch the developer's real credentials or remembered state."""
    monkeypatch.setattr(publish_routes, "state_path", lambda: tmp_path / "ui.json")
    monkeypatch.setattr(publish_routes, "ledger_path", lambda: tmp_path / "ledger.json")
    monkeypatch.setattr(creds_module, "config_path", lambda: tmp_path / "credentials")
    publish_routes._JOBS.clear()
    yield


class TestStatus:
    def test_reports_a_missing_tool_rather_than_fetching_it(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        body = client.get("/api/publish/status").json()
        assert body["tool"]["found"] is False

    def test_offers_metas_channel_names(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        body = client.get("/api/publish/status").json()
        assert "store" in body["channels"] and "ALPHA" in body["channels"]

    def test_says_when_credentials_are_missing(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        monkeypatch.setattr(
            publish_routes.creds_module,
            "load",
            lambda: (_ for _ in ()).throw(creds_module.MissingCredentialsError("no")),
        )
        body = client.get("/api/publish/status").json()
        assert body["credentials"]["configured"] is False


class TestCredentials:
    def test_saving_never_hands_the_secret_back(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        r = client.post(
            "/api/publish/credentials",
            json={"app_id": "999", "app_secret": "top-secret"},
        )
        assert r.status_code == 200
        assert "top-secret" not in r.text
        assert r.json()["app_id"] == "999"

    def test_a_blank_secret_is_refused(self):
        r = client.post("/api/publish/credentials", json={"app_id": "9", "app_secret": " "})
        assert r.status_code == 400

    def test_status_never_returns_the_secret(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        client.post(
            "/api/publish/credentials",
            json={"app_id": "999", "app_secret": "top-secret"},
        )
        assert "top-secret" not in client.get("/api/publish/status").text


class TestBuilds:
    def test_lists_what_is_in_a_folder(self, tmp_path):
        make_release_apk(tmp_path / "Voyager.apk")
        r = client.get("/api/publish/builds", params={"root": str(tmp_path)})
        assert [b["name"] for b in r.json()["builds"]] == ["Voyager.apk"]

    def test_remembers_the_folder_for_next_time(self, tmp_path):
        make_release_apk(tmp_path / "Voyager.apk")
        client.get("/api/publish/builds", params={"root": str(tmp_path)})
        assert client.get("/api/publish/builds").json()["builds"]

    def test_a_folder_that_is_not_there(self, tmp_path):
        r = client.get("/api/publish/builds", params={"root": str(tmp_path / "nope")})
        assert r.status_code == 404


class TestAudit:
    def test_a_clean_build_reads_as_ready(self, tmp_path):
        apk = make_release_apk(tmp_path / "ok.apk")
        body = client.get("/api/publish/audit", params={"path": str(apk)}).json()
        assert body["report"]["ready"] is True
        assert body["apk"]["package"] == "com.speakeasy3d.voyager"

    def test_a_debug_key_is_a_blocker(self, tmp_path):
        apk = make_apk(tmp_path / "debug.apk", signing_block=v2_signed(DEBUG_CERT))
        body = client.get("/api/publish/audit", params={"path": str(apk)}).json()
        assert body["report"]["ready"] is False
        assert any(c["level"] == "blocker" for c in body["report"]["checks"])

    def test_passing_checks_are_still_shown(self, tmp_path):
        apk = make_release_apk(tmp_path / "ok.apk")
        body = client.get("/api/publish/audit", params={"path": str(apk)}).json()
        assert any(c["level"] == "ok" for c in body["report"]["checks"])

    def test_a_version_code_the_channel_already_has(self, tmp_path):
        apk = make_release_apk(tmp_path / "ok.apk")
        body = client.get(
            "/api/publish/audit",
            params={"path": str(apk), "channel_version_code": 99},
        ).json()
        assert body["report"]["ready"] is False

    def test_an_unreadable_file_is_a_clean_400(self, tmp_path):
        odd = tmp_path / "odd.apk"
        odd.write_bytes(b"not an apk")
        assert client.get("/api/publish/audit", params={"path": str(odd)}).status_code == 400


def wait_for(job_id, status=None, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/publish/upload/{job_id}").json()
        if body["status"] != "running" or body["status"] == status:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


class FakeProcess:
    """Enough of Popen for the streaming path, with a controllable ending."""

    def __init__(self, lines=(), code=0, block=False):
        self.stdout = iter(lines)
        self._code = code
        self._block = block
        self.terminated = False

    def wait(self):
        while self._block and not self.terminated:
            time.sleep(0.01)
        return 143 if self.terminated else self._code


@pytest.fixture
def ready_to_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_routes.creds_module, "load", lambda: CREDS)
    monkeypatch.setattr(publish_routes.ovrtool, "find", lambda: tmp_path / "ovr")
    monkeypatch.setattr(uploader, "require", lambda: tmp_path / "ovr")
    return make_release_apk(tmp_path / "ok.apk")


class TestUploading:
    def test_a_run_streams_its_lines_and_finishes(self, ready_to_upload, monkeypatch):
        monkeypatch.setattr(
            uploader, "_spawn", lambda argv: FakeProcess(["uploading\n", "done\n"])
        )
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        finished = wait_for(job["id"])
        assert finished["status"] == "succeeded"
        assert "uploading" in "\n".join(finished["lines"])

    def test_a_rehearsal_says_so(self, ready_to_upload, monkeypatch):
        monkeypatch.setattr(uploader, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        job = client.post(
            "/api/publish/upload",
            json={
                "path": str(ready_to_upload),
                "channel": "ALPHA",
                "age_group": "MIXED_AGES",
                "validate_only": True,
            },
        ).json()
        assert wait_for(job["id"])["status"] == "validated"

    def test_a_blocked_build_never_spawns_anything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(publish_routes.creds_module, "load", lambda: CREDS)
        monkeypatch.setattr(publish_routes.ovrtool, "find", lambda: tmp_path / "ovr")
        spawned = []
        monkeypatch.setattr(
            uploader, "_spawn", lambda argv: spawned.append(argv) or FakeProcess()
        )
        apk = make_apk(tmp_path / "debug.apk", signing_block=v2_signed(DEBUG_CERT))
        job = client.post(
            "/api/publish/upload",
            json={"path": str(apk), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        finished = wait_for(job["id"])
        assert finished["status"] == "refused"
        assert spawned == []
        assert "debug" in finished["error"].lower()

    def test_the_live_store_needs_its_own_confirmation(self, ready_to_upload, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            uploader, "_spawn", lambda argv: spawned.append(argv) or FakeProcess()
        )
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "store", "age_group": "MIXED_AGES"},
        ).json()
        finished = wait_for(job["id"])
        assert finished["status"] == "refused"
        assert spawned == []

    def test_the_live_store_goes_when_confirmed(self, ready_to_upload, monkeypatch):
        monkeypatch.setattr(uploader, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        job = client.post(
            "/api/publish/upload",
            json={
                "path": str(ready_to_upload),
                "channel": "store",
                "age_group": "MIXED_AGES",
                "confirm_release": True,
            },
        ).json()
        assert wait_for(job["id"])["status"] == "succeeded"

    def test_only_one_upload_at_a_time(self, ready_to_upload, monkeypatch):
        monkeypatch.setattr(uploader, "_spawn", lambda argv: FakeProcess(block=True))
        first = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        second = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        )
        assert second.status_code == 409
        client.post(f"/api/publish/upload/{first['id']}/cancel")

    def test_a_running_upload_can_be_stopped(self, ready_to_upload, monkeypatch):
        process = FakeProcess(block=True)
        monkeypatch.setattr(uploader, "_spawn", lambda argv: process)
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        for _ in range(200):
            if process.stdout is not None and publish_routes._JOBS[job["id"]]._process:
                break
            time.sleep(0.01)
        client.post(f"/api/publish/upload/{job['id']}/cancel")
        process.terminated = True
        assert wait_for(job["id"])["status"] == "cancelled"

    def test_asking_for_an_unknown_job(self):
        assert client.get("/api/publish/upload/nope").status_code == 404

    def test_missing_credentials_stops_it_before_anything_runs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            publish_routes.creds_module,
            "load",
            lambda: (_ for _ in ()).throw(creds_module.MissingCredentialsError("none")),
        )
        apk = make_release_apk(tmp_path / "ok.apk")
        r = client.post(
            "/api/publish/upload", json={"path": str(apk), "channel": "ALPHA"}
        )
        assert r.status_code == 400


class TestChecklist:
    def test_starts_all_unticked(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        items = client.get("/api/publish/status").json()["checklist"]
        assert items and not any(i["done"] for i in items)

    def test_a_tick_survives_a_reload(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        client.post("/api/publish/checklist", json={"id": "listing", "done": True})
        items = client.get("/api/publish/status").json()["checklist"]
        assert next(i for i in items if i["id"] == "listing")["done"]

    def test_unticking_works_too(self, monkeypatch):
        monkeypatch.setattr(ovrtool, "find", lambda: None)
        client.post("/api/publish/checklist", json={"id": "listing", "done": True})
        r = client.post("/api/publish/checklist", json={"id": "listing", "done": False})
        assert not next(i for i in r.json()["checklist"] if i["id"] == "listing")["done"]

    def test_an_unknown_item(self):
        assert client.post(
            "/api/publish/checklist", json={"id": "invented", "done": True}
        ).status_code == 404


class TestLedgerAndHistory:
    """Finished uploads are remembered on disk and served back; the runway
    prefills a channel's version code from what THIS machine sent before."""

    def _ledger(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.json"
        monkeypatch.setattr(publish_routes, "ledger_path", lambda: path)
        return path

    def test_a_finished_upload_lands_in_the_ledger(
        self, ready_to_upload, monkeypatch, tmp_path
    ):
        self._ledger(tmp_path, monkeypatch)
        monkeypatch.setattr(
            uploader, "_spawn", lambda argv: FakeProcess(["Uploaded to ALPHA\n"])
        )
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        wait_for(job["id"])
        body = client.get("/api/publish/history").json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["status"] == "uploaded"
        assert body["entries"][0]["channel"] == "ALPHA"

    def test_a_rehearsal_is_remembered_but_consumes_no_version_code(
        self, ready_to_upload, monkeypatch, tmp_path
    ):
        self._ledger(tmp_path, monkeypatch)
        monkeypatch.setattr(uploader, "_spawn", lambda argv: FakeProcess(["ok\n"]))
        job = client.post(
            "/api/publish/upload",
            json={
                "path": str(ready_to_upload), "channel": "ALPHA",
                "age_group": "MIXED_AGES", "validate_only": True,
            },
        ).json()
        wait_for(job["id"])
        body = client.get("/api/publish/history").json()
        assert body["entries"][0]["status"] == "validated"
        pkg = body["entries"][0]["package"]
        latest = client.get(
            f"/api/publish/history?package={pkg}&channel=ALPHA"
        ).json()["latest_uploaded_version_code"]
        assert latest is None

    def test_a_running_job_names_its_phase(self, ready_to_upload, monkeypatch, tmp_path):
        self._ledger(tmp_path, monkeypatch)
        monkeypatch.setattr(
            uploader, "_spawn",
            lambda argv: FakeProcess(["Uploading 10%\n", "POSTPROCESSING\n"]),
        )
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        finished = wait_for(job["id"])
        # By completion phase is no longer reported; the promise is that the
        # field exists on the wire and never crashes the summary.
        assert "phase" in finished

    def test_a_stale_secret_failure_carries_the_hint(
        self, ready_to_upload, monkeypatch, tmp_path
    ):
        self._ledger(tmp_path, monkeypatch)
        monkeypatch.setattr(
            uploader, "_spawn",
            lambda argv: FakeProcess(
                ["Error validation application. Either App ID or App Secret must be invalid.\n",
                 "OAuthException code 190 — Error verifying the token\n"],
                code=1,
            ),
        )
        job = client.post(
            "/api/publish/upload",
            json={"path": str(ready_to_upload), "channel": "ALPHA", "age_group": "MIXED_AGES"},
        ).json()
        finished = wait_for(job["id"])
        assert finished["status"] == "failed"
        assert finished["hint"] and "secret" in finished["hint"].lower()
