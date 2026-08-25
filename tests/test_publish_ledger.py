"""The ledger's promises: what shipped is remembered, remembering is advisory,
and a broken ledger never blocks a new upload."""


from visor.publish import ledger, upload as uploader


def entry(**kw):
    base = dict(
        package="com.example.app", version_code=4, version_name="1.0",
        channel="ALPHA", apk_name="a.apk", apk_path="/b/a.apk",
        size_bytes=100, status="uploaded",
    )
    base.update(kw)
    return ledger.Entry(**base)


class TestRoundTrip:
    def test_what_is_recorded_comes_back(self, tmp_path):
        path = tmp_path / "ledger.json"
        ledger.record(path, entry())
        ledger.record(path, entry(version_code=5, channel="store"))
        got = ledger.load(path)
        assert [e.version_code for e in got] == [4, 5]

    def test_a_missing_ledger_is_empty_not_an_error(self, tmp_path):
        assert ledger.load(tmp_path / "nope.json") == []

    def test_a_corrupt_ledger_is_empty_not_an_error(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("{not json")
        assert ledger.load(path) == []

    def test_an_unknown_schema_row_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text('[{"from_the_future": true}]')
        assert ledger.load(path) == []


class TestLatestVersionCode:
    def test_the_highest_uploaded_code_wins(self):
        entries = [entry(version_code=3), entry(version_code=5), entry(version_code=4)]
        assert ledger.latest_version_code(entries, "com.example.app", "ALPHA") == 5

    def test_rehearsals_consume_nothing(self):
        entries = [entry(version_code=3), entry(version_code=9, status="validated")]
        assert ledger.latest_version_code(entries, "com.example.app", "ALPHA") == 3

    def test_channels_do_not_share_a_counter(self):
        entries = [entry(version_code=7, channel="store")]
        assert ledger.latest_version_code(entries, "com.example.app", "ALPHA") is None

    def test_metas_lowercase_store_still_matches(self):
        entries = [entry(version_code=7, channel="store")]
        assert ledger.latest_version_code(entries, "com.example.app", "STORE") == 7

    def test_other_apps_do_not_leak_in(self):
        entries = [entry(package="com.other.app", version_code=9)]
        assert ledger.latest_version_code(entries, "com.example.app", "ALPHA") is None


class TestBuildUrl:
    def test_a_whole_url_is_found(self):
        url = "https://developer.oculus.com/manage/applications/123/builds/456"
        assert ledger.parse_build_url(f"ok\n{url}\ndone") == url

    def test_a_terminal_wrapped_url_is_rejoined(self):
        # ovr-platform-util wraps at terminal width mid-number (seen live 2026-08-26).
        wrapped = ("https://developer.oculus.com/manage/applications/355070/builds/3829\n"
                   "178371043\n")
        assert ledger.parse_build_url(wrapped) == (
            "https://developer.oculus.com/manage/applications/355070/builds/3829178371043"
        )

    def test_no_url_is_none(self):
        assert ledger.parse_build_url("nothing here") is None


class TestPhase:
    def test_the_postprocessing_silence_is_named(self):
        lines = ["Uploading build", "POSTPROCESSING poll 1"]
        assert uploader.phase_of(lines) == "postprocessing"

    def test_upload_progress_reads_as_uploading(self):
        assert uploader.phase_of(["Uploading build 42%"]) == "uploading"

    def test_no_output_yet_is_starting(self):
        assert uploader.phase_of([]) == "starting"


class TestFailureDiagnosis:
    def test_the_stale_secret_misdirection_is_named(self):
        out = ("Error validation application. Either App ID or App Secret must be invalid.\n"
               "OAuthException code 190 — Error verifying the token")
        hint = uploader.diagnose_failure(out)
        assert hint and "secret" in hint.lower()

    def test_an_ordinary_failure_gets_no_invented_hint(self):
        assert uploader.diagnose_failure("A network error occurred") is None
