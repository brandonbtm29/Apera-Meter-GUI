"""The update check and the git-based update (no network: GitHub is faked, the remote is local)."""

import os
import subprocess

import pytest

import updater

REAL_RELEASE_SIGNED = updater.release_signed


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
    # Ignore the user's own signing settings: with tag.gpgSign on, "git tag" would open an editor
    # for a tag message and the test would hang.
    cmd = ["git", "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", *args]
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A 'GitHub' repo with release v1.0.0, and a clone of it like a lab computer has. The throwaway
    releases aren't signed, so the signature check passes them here; the signing tests below use the
    real one."""
    monkeypatch.setattr(updater, "release_signed", lambda app_dir, ref, log=print: "")
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


def test_git_and_pip_run_without_the_callers_git_variables(monkeypatch):
    """GIT_CONFIG_* can make git run commands and GIT_DIR points it at another repo: none come along."""
    for name, value in {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "evil",
        "git_dir": "/elsewhere",
        "GIT_SSH_COMMAND": "evil",
    }.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    updater._run(["git", "status"], ".", lambda _: None)
    assert not [k for k in seen["env"] if k.upper().startswith("GIT_")]
    assert "PATH" in seen["env"] or "Path" in seen["env"]


# ------------------------------------------------------------------ signed releases
def _signers_file(repo, text='me@example.com namespaces="git" ssh-ed25519 AAAA\n'):
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "allowed_signers").write_text(text)


@pytest.mark.parametrize(
    ("letter", "code", "words"),
    [
        ("N", 0, "isn't signed"),
        ("U", 0, "key that isn't on the trusted list"),
        ("B", 0, "bad signature"),
        ("E", 0, "couldn't be checked"),
        ("R", 0, "revoked key"),
        ("", 0, "couldn't be checked"),  # git printed nothing
        ("G", 128, "couldn't be checked"),  # git failed
    ],
)
def test_only_good_signatures_pass(tmp_path, monkeypatch, letter, code, words):
    _signers_file(tmp_path)
    monkeypatch.setattr(updater, "_ssh_keygen", lambda: "/usr/bin/ssh-keygen")
    calls = []

    def run(cmd, cwd, log):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, code, stdout=letter + "\n", stderr="")

    monkeypatch.setattr(updater, "_run", run)
    why = updater.release_signed(str(tmp_path), "abc123")
    assert words in why and "allowed_signers" in why and "wasn't installed" in why
    cmd = calls[0]
    assert cmd[:2] == ["git", "-c"] and cmd[-1] == "abc123" and "--format=%G?" in cmd
    assert f"gpg.ssh.allowedSignersFile={tmp_path / '.github' / 'allowed_signers'}" in cmd  # this copy's own
    assert "gpg.ssh.program=/usr/bin/ssh-keygen" in cmd

    monkeypatch.setattr(updater, "_run", lambda cmd, cwd, log: subprocess.CompletedProcess(cmd, 0, "G\n", ""))
    assert updater.release_signed(str(tmp_path), "abc123") == ""


def test_no_list_or_no_ssh_keygen_means_no_update(tmp_path, monkeypatch):
    def run(cmd, cwd, log):
        raise AssertionError("git shouldn't run")

    monkeypatch.setattr(updater, "_run", run)
    assert "has no .github/allowed_signers" in updater.release_signed(str(tmp_path), "abc123")
    _signers_file(tmp_path)
    monkeypatch.setattr(updater, "_ssh_keygen", lambda: None)
    assert "ssh-keygen isn't installed" in updater.release_signed(str(tmp_path), "abc123")


def test_update_refuses_a_release_that_is_not_signed(clone, monkeypatch):
    origin, work = clone
    release(origin, "v1.1.0", 2)
    before = git(work, "rev-parse", "HEAD")
    monkeypatch.setattr(updater, "release_signed", REAL_RELEASE_SIGNED)  # this clone has no allowed_signers
    with pytest.raises(updater.UpdateError, match="has no .github/allowed_signers"):
        updater.update_git_clone(str(work), "v1.1.0", log=lambda _: None, install_deps=False)
    monkeypatch.setattr(
        updater,
        "release_signed",
        lambda app_dir, ref, log=print: "The release isn't signed, so it wasn't installed.",
    )
    with pytest.raises(updater.UpdateError, match="Release v1.1.0 isn't signed"):
        updater.update_git_clone(str(work), "v1.1.0", log=lambda _: None, install_deps=False)
    assert git(work, "rev-parse", "HEAD") == before and (work / "app.py").read_text() == "VERSION = 1\n"


def _key(tmp_path, name):
    key = tmp_path / name
    subprocess.run(
        [updater._ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _signed_release(origin, tag, version, key, signers_text=None):
    """Like release(), but the commit is signed with `key` (and may change allowed_signers)."""
    (origin / "app.py").write_text(f"VERSION = {version}\n")
    if signers_text is not None:
        _signers_file(origin, signers_text)
    git(origin, "add", "-A")
    sign = [
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key}",
        "-c",
        f"gpg.ssh.program={updater._ssh_keygen()}",
    ]
    r = subprocess.run(
        ["git", "-c", "tag.gpgSign=false", *sign, "commit", "-S", "-qm", tag],
        cwd=origin,
        capture_output=True,
        text=True,
    )
    if r.returncode and os.name == "nt":
        pytest.skip(f"can't sign a test commit here: {r.stderr.strip()[:200]}")
    assert r.returncode == 0, r.stderr
    git(origin, "tag", tag)


@pytest.mark.skipif(updater._ssh_keygen() is None, reason="OpenSSH's ssh-keygen not installed")
def test_real_signatures(tmp_path, monkeypatch):
    """Signed with a listed key: installs. Unsigned, signed by an unlisted key, or a release that lists its
    own new key: refused, because the check uses this copy's list, not the downloaded one."""
    trusted, stranger = _key(tmp_path, "trusted"), _key(tmp_path, "stranger")
    line = 'me@example.com namespaces="git" {}'.format
    trusted_line = line((tmp_path / "trusted.pub").read_text().strip())
    stranger_line = line((tmp_path / "stranger.pub").read_text().strip())
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "test@example.invalid")
    git(origin, "config", "user.name", "Test")
    _signed_release(origin, "v1.0.0", 1, trusted, signers_text=trusted_line + "\n")
    work = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(work))

    def update(tag):
        updater.update_git_clone(str(work), tag, log=lambda _: None, install_deps=False)

    _signed_release(origin, "v1.1.0", 2, trusted)
    update("v1.1.0")
    assert (work / "app.py").read_text() == "VERSION = 2\n"

    release(origin, "v1.2.0", 3)  # not signed
    with pytest.raises(updater.UpdateError, match="Release v1.2.0 isn't signed"):
        update("v1.2.0")

    _signed_release(origin, "v1.3.0", 4, stranger)
    with pytest.raises(updater.UpdateError, match="isn't on the trusted list"):
        update("v1.3.0")

    # A release that adds its own key to the list is still checked against this copy's list
    _signed_release(origin, "v1.4.0", 5, stranger, signers_text=trusted_line + "\n" + stranger_line + "\n")
    with pytest.raises(updater.UpdateError, match="isn't on the trusted list"):
        update("v1.4.0")
    assert (work / "app.py").read_text() == "VERSION = 2\n"
