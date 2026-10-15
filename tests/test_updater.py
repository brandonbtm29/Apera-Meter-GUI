"""The update check and the git-based update (no network: GitHub is faked, the remote is local)."""

import subprocess

import pytest

import updater


def test_parse_and_compare_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("1.1") == (1, 1, 0)
    assert updater.parse_version("v2.0.0-beta") == (2, 0, 0)
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert not updater.is_newer("v1.1.0", "1.1.0")
    with pytest.raises(ValueError):
        updater.parse_version("latest")


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


def test_fetch_latest_release_reads_github_answer():
    data = {
        "tag_name": "v1.2.0",
        "name": "Apera Meter GUI 1.2.0",
        "body": "### Added\n- Things",
        "html_url": "https://example.invalid/release",
        "assets": [{"name": "app.zip", "browser_download_url": "https://example.invalid/app.zip"}],
    }
    release = updater.fetch_latest_release(get=lambda *a, **k: FakeResponse(200, data))
    assert release.version == (1, 2, 0)
    assert release.tag == "v1.2.0"
    assert release.assets == {"app.zip": "https://example.invalid/app.zip"}


def test_fetch_latest_release_explains_failures():
    with pytest.raises(updater.UpdateError, match="No releases"):
        updater.fetch_latest_release(get=lambda *a, **k: FakeResponse(404))

    def offline(*a, **k):
        raise updater.requests.ConnectionError("offline")

    with pytest.raises(updater.UpdateError, match="Couldn't reach GitHub"):
        updater.fetch_latest_release(get=offline)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """A 'GitHub' repo with release v1.0.0, and a clone of it like a lab computer has."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "test@example.invalid")
    git(origin, "config", "user.name", "Test")
    (origin / "app.py").write_text("VERSION = 1\n")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "1.0.0")
    git(origin, "tag", "v1.0.0")
    work = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    return origin, work


def release(origin, tag, version):
    (origin / "app.py").write_text(f"VERSION = {version}\n")
    git(origin, "commit", "-q", "-am", tag)
    git(origin, "tag", tag)


def test_git_update_moves_clone_to_release(clone):
    origin, work = clone
    release(origin, "v1.1.0", 2)
    release(origin, "v1.2.0-dev", 3)  # newer commits after the release must not be pulled in
    assert updater.install_kind(str(work)) == "git"

    updater.update_git_clone(str(work), "v1.1.0", log=lambda _: None, install_deps=False)

    assert (work / "app.py").read_text() == "VERSION = 2\n"
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_git_update_refuses_to_overwrite_local_edits(clone):
    origin, work = clone
    release(origin, "v1.1.0", 2)
    (work / "app.py").write_text("VERSION = 'my edit'\n")

    with pytest.raises(updater.UpdateError, match="local changes"):
        updater.update_git_clone(str(work), "v1.1.0", log=lambda _: None, install_deps=False)
    assert (work / "app.py").read_text() == "VERSION = 'my edit'\n"


def test_git_update_refuses_when_clone_has_its_own_commits(clone):
    origin, work = clone
    release(origin, "v1.1.0", 2)
    (work / "notes.txt").write_text("local work\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "local commit")

    with pytest.raises(updater.UpdateError, match="commits that aren't in v1.1.0"):
        updater.update_git_clone(str(work), "v1.1.0", log=lambda _: None, install_deps=False)
    assert (work / "app.py").read_text() == "VERSION = 1\n"
