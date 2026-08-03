"""Resolving and protecting the Meta app credentials.

The app secret is the one genuinely dangerous value Visor handles. It has to
reach ovr-platform-util's argv (Meta offers no other channel), which already
means it is visible to `ps` on a shared machine — so the least Visor can do is
never be the thing that writes it to a log, a traceback, or a tool result.
"""

import stat

import pytest

from visor.publish import creds


ENV = {"OCULUS_APP_ID": "1234567890", "OCULUS_APP_SECRET": "s3cr3t-from-env"}


def write_credentials(path, body, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


class TestConfigPath:
    def test_honours_xdg_config_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert creds.config_path() == tmp_path / "visor" / "credentials"

    def test_falls_back_to_dot_config(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert creds.config_path().parts[-3:] == (".config", "visor", "credentials")


class TestLoadFromEnv:
    def test_reads_both_values(self, tmp_path):
        got = creds.load(env=ENV, path=tmp_path / "nonexistent")
        assert got.app_id == "1234567890"
        assert got.app_secret == "s3cr3t-from-env"

    def test_env_wins_over_file(self, tmp_path):
        path = write_credentials(
            tmp_path / "credentials",
            "OCULUS_APP_ID=from-file\nOCULUS_APP_SECRET=from-file\n",
        )
        got = creds.load(env=ENV, path=path)
        assert got.app_id == "1234567890"
        assert got.app_secret == "s3cr3t-from-env"

    def test_blank_env_value_is_not_a_credential(self, tmp_path):
        """An exported-but-empty var is a common CI mistake; it must fall
        through to the file rather than uploading with an empty secret."""
        path = write_credentials(
            tmp_path / "credentials",
            "OCULUS_APP_ID=from-file\nOCULUS_APP_SECRET=file-secret\n",
        )
        got = creds.load(env={"OCULUS_APP_ID": "", "OCULUS_APP_SECRET": ""}, path=path)
        assert got.app_id == "from-file"
        assert got.app_secret == "file-secret"


class TestLoadFromFile:
    def test_reads_key_values(self, tmp_path):
        path = write_credentials(
            tmp_path / "credentials",
            "OCULUS_APP_ID=999\nOCULUS_APP_SECRET=abc\n",
        )
        got = creds.load(env={}, path=path)
        assert (got.app_id, got.app_secret) == ("999", "abc")

    def test_tolerates_comments_blanks_quotes_and_spacing(self, tmp_path):
        path = write_credentials(
            tmp_path / "credentials",
            "# Voyager\n\n  OCULUS_APP_ID = 999 \nOCULUS_APP_SECRET=\"abc\"\n",
        )
        got = creds.load(env={}, path=path)
        assert (got.app_id, got.app_secret) == ("999", "abc")

    def test_warns_when_world_readable(self, tmp_path):
        path = write_credentials(
            tmp_path / "credentials",
            "OCULUS_APP_ID=999\nOCULUS_APP_SECRET=abc\n",
            mode=0o644,
        )
        with pytest.warns(UserWarning, match="0600"):
            creds.load(env={}, path=path)

    def test_no_warning_when_locked_down(self, tmp_path, recwarn):
        path = write_credentials(
            tmp_path / "credentials", "OCULUS_APP_ID=9\nOCULUS_APP_SECRET=a\n"
        )
        creds.load(env={}, path=path)
        assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


class TestMissing:
    def test_raises_when_nothing_is_configured(self, tmp_path):
        with pytest.raises(creds.MissingCredentialsError):
            creds.load(env={}, path=tmp_path / "nonexistent")

    def test_error_names_both_routes(self, tmp_path):
        with pytest.raises(creds.MissingCredentialsError) as excinfo:
            creds.load(env={}, path=tmp_path / "nonexistent")
        message = str(excinfo.value)
        assert "OCULUS_APP_SECRET" in message
        assert str(tmp_path / "nonexistent") in message

    def test_partial_credentials_still_raise(self, tmp_path):
        with pytest.raises(creds.MissingCredentialsError):
            creds.load(env={"OCULUS_APP_ID": "999"}, path=tmp_path / "nonexistent")

    def test_error_never_contains_the_secret(self, tmp_path):
        with pytest.raises(creds.MissingCredentialsError) as excinfo:
            creds.load(env={"OCULUS_APP_SECRET": "leaky"}, path=tmp_path / "none")
        assert "leaky" not in str(excinfo.value)


class TestSecretNeverPrints:
    """repr() is what a traceback and most loggers reach for."""

    def test_repr_hides_the_secret(self):
        got = creds.load(env=ENV, path=None)
        assert "s3cr3t-from-env" not in repr(got)

    def test_str_hides_the_secret(self):
        got = creds.load(env=ENV, path=None)
        assert "s3cr3t-from-env" not in str(got)

    def test_repr_still_identifies_the_app(self):
        got = creds.load(env=ENV, path=None)
        assert "1234567890" in repr(got)


class TestRedact:
    def test_removes_the_secret(self):
        got = creds.load(env=ENV, path=None)
        assert "s3cr3t-from-env" not in got.redact("token=s3cr3t-from-env failed")

    def test_leaves_a_visible_marker(self):
        got = creds.load(env=ENV, path=None)
        assert "REDACTED" in got.redact("token=s3cr3t-from-env failed")

    def test_scrubs_every_occurrence(self):
        got = creds.load(env=ENV, path=None)
        out = got.redact("s3cr3t-from-env and again s3cr3t-from-env")
        assert "s3cr3t-from-env" not in out

    def test_redacts_inside_an_argv_list(self):
        got = creds.load(env=ENV, path=None)
        argv = ["ovr-platform-util", "--app_secret", "s3cr3t-from-env"]
        assert "s3cr3t-from-env" not in got.redact(" ".join(argv))

    def test_empty_secret_does_not_shred_the_text(self):
        """str.replace('', X) inserts X between every character — a redactor
        that mangles unrelated output is a redactor people switch off."""
        empty = creds.Credentials(app_id="1", app_secret="")
        assert empty.redact("nothing to hide here") == "nothing to hide here"

    def test_non_string_input_is_returned_untouched(self):
        got = creds.load(env=ENV, path=None)
        assert got.redact(None) is None


class TestFilePermissions:
    def test_save_writes_owner_only(self, tmp_path):
        path = tmp_path / "visor" / "credentials"
        creds.save(creds.Credentials("111", "shhh"), path=path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_saved_file_round_trips(self, tmp_path):
        path = tmp_path / "visor" / "credentials"
        creds.save(creds.Credentials("111", "shhh"), path=path)
        got = creds.load(env={}, path=path)
        assert (got.app_id, got.app_secret) == ("111", "shhh")
