"""Finding the APK the user actually meant.

A Quest project accumulates builds: Shipping and Development side by side,
several days of them, plus whatever a packaging script archived. Asking someone
to type a path from memory at the moment they publish is how the wrong build
reaches the store, so the list is offered newest-first and says which is which.
"""

import os
import time

from tests.apkbuild import make_release_apk
from visor.publish import builds


def write(path, age_seconds=0):
    make_release_apk(path)
    if age_seconds:
        when = time.time() - age_seconds
        os.utime(path, (when, when))
    return path


class TestDiscovery:
    def test_finds_apks_anywhere_below_the_root(self, tmp_path):
        write(tmp_path / "Binaries" / "Android" / "Voyager.apk")
        found = builds.discover(tmp_path)
        assert [b.path.name for b in found] == ["Voyager.apk"]

    def test_newest_first(self, tmp_path):
        write(tmp_path / "old.apk", age_seconds=7200)
        write(tmp_path / "new.apk")
        assert [b.path.name for b in builds.discover(tmp_path)] == [
            "new.apk",
            "old.apk",
        ]

    def test_a_missing_root_is_empty_not_an_error(self, tmp_path):
        assert builds.discover(tmp_path / "nowhere") == []

    def test_a_file_root_is_that_file(self, tmp_path):
        apk = write(tmp_path / "one.apk")
        assert [b.path for b in builds.discover(apk)] == [apk]

    def test_ignores_non_apks(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hello")
        write(tmp_path / "real.apk")
        assert len(builds.discover(tmp_path)) == 1

    def test_limit_keeps_the_newest(self, tmp_path):
        write(tmp_path / "a.apk", age_seconds=100)
        write(tmp_path / "b.apk")
        assert [b.path.name for b in builds.discover(tmp_path, limit=1)] == ["b.apk"]

    def test_skips_intermediate_build_directories(self, tmp_path):
        # Unreal leaves partially-linked APKs under Intermediate; they are never
        # the thing anyone wants to publish.
        write(tmp_path / "Intermediate" / "half.apk")
        write(tmp_path / "ship.apk")
        assert [b.path.name for b in builds.discover(tmp_path)] == ["ship.apk"]


class TestWhatEachBuildSays:
    def test_carries_enough_to_choose_between_two_builds(self, tmp_path):
        found = builds.discover(write(tmp_path / "Voyager.apk"))
        build = found[0]
        assert build.package == "com.speakeasy3d.voyager"
        assert build.version_code >= 1
        assert build.size_bytes > 0
        assert build.modified > 0

    def test_an_unreadable_apk_still_appears(self, tmp_path):
        # Better to list it and say we could not read it than to hide a file the
        # user can plainly see in the folder.
        odd = tmp_path / "odd.apk"
        odd.write_bytes(b"PK\x03\x04" + b"\0" * 64)
        (found,) = builds.discover(tmp_path)
        assert found.package is None
        assert found.error

    def test_summary_is_json_safe(self, tmp_path):
        import json

        found = builds.discover(write(tmp_path / "Voyager.apk"))
        json.dumps([b.summary() for b in found])


class TestDefaultRoots:
    def test_an_unreal_project_offers_its_binaries_directory(self, tmp_path):
        (tmp_path / "Binaries" / "Android").mkdir(parents=True)
        (tmp_path / "Voyager.uproject").write_text("{}")
        roots = builds.default_roots(tmp_path)
        assert tmp_path / "Binaries" / "Android" in roots

    def test_a_plain_directory_offers_itself(self, tmp_path):
        assert builds.default_roots(tmp_path) == [tmp_path]
