"""Check GitHub for a newer release of the app and install it.

How an update is installed depends on how the app was installed:

* **git clone** (the usual lab setup): fast-forward the clone to the release tag, reinstall the
  Python dependencies, then restart. Local edits or commits stop the update instead of being
  overwritten. Only signed releases are installed: the tag's commit must carry a signature from one
  of the keys in ``.github/allowed_signers``, as this computer already has it (the copy from the
  version it's running, so a new commit can't add its own key). Someone who got into the GitHub
  account could publish a release, but not sign it, so the app won't install it.
* **standalone build** (the zip from the releases page): open the release page so the new zip can
  be downloaded; a running .exe can't replace itself reliably.
* **anything else** (e.g. a downloaded source zip): open the release page.

Nothing here touches the user's data or settings. git and pip run without the caller's ``GIT_*``
variables (``GIT_CONFIG_*`` can make git run commands). The functions are kept free of Qt so they can
be tested on their own; meter_app.py adds the dialogs.
"""

import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

import requests

GITHUB_REPO = "brandonbtm29/Apera-Meter-GUI"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
SIGNERS = os.path.join(".github", "allowed_signers")  # trusted signing keys, in the app folder
SIGNATURE_STATUS = {  # git's %G? letters, for a commit that isn't signed by a trusted key
    "N": "isn't signed",
    "U": "is signed with a key that isn't on the trusted list",
    "B": "has a bad signature",
    "E": "couldn't be checked",
    "X": "has an expired signature",
    "Y": "is signed with an expired key",
    "R": "is signed with a revoked key",
}


class UpdateError(Exception):
    """An update couldn't be checked or installed; the message is shown to the user."""


@dataclass
class Release:
    tag: str
    version: tuple
    name: str
    notes: str
    url: str
    assets: dict = field(default_factory=dict)  # file name -> download URL


def parse_version(text):
    """'v1.2.3' -> (1, 2, 3). Anything after the numbers (e.g. '-beta') is ignored."""
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(text))
    if not match:
        raise ValueError(f"Not a version number: {text!r}")
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def fetch_latest_release(timeout=10, get=requests.get):
    """Ask GitHub for the newest published release."""
    try:
        response = get(
            LATEST_RELEASE_API,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise UpdateError(f"Couldn't reach GitHub to check for updates.\n\n{e}") from e
    if response.status_code == 404:
        raise UpdateError("No releases have been published yet.")
    if response.status_code != 200:
        raise UpdateError(f"GitHub answered with HTTP {response.status_code} while checking for updates.")
    data = response.json()
    tag = data.get("tag_name", "")
    return Release(
        tag=tag,
        version=parse_version(tag),
        name=data.get("name") or tag,
        notes=data.get("body") or "",
        url=data.get("html_url") or RELEASES_PAGE,
        assets={a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
    )


def install_kind(app_dir):
    """'frozen' for the standalone build, 'git' for a git clone, else 'other'."""
    if getattr(sys, "frozen", False):
        return "frozen"
    if os.path.isdir(os.path.join(app_dir, ".git")) and shutil.which("git"):
        return "git"
    return "other"


def _is_installed(distribution):
    try:
        importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _clean_env():
    """This process's environment without GIT_* variables: GIT_CONFIG_* can make git run commands and
    GIT_DIR points it at another repository."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def _run(cmd, cwd, log):
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(  # noqa: S603 - fixed git/pip commands, no user input
        cmd, cwd=cwd, capture_output=True, text=True, check=False, env=_clean_env()
    )
    output = (result.stdout + result.stderr).strip()
    if output:
        log(output)
    return result


def _git(args, app_dir, log, error):
    result = _run(["git", *args], app_dir, log)
    if result.returncode != 0:
        raise UpdateError(error)
    return result.stdout.strip()


def _ssh_keygen(windows=None):
    """OpenSSH's ssh-keygen, which checks SSH signatures. On Windows, Git's own copy comes first: System32's
    can hang on `-Y verify` when run in an SSH session (seen on BMAX, 2026-11-27)."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        roots = []
        git = shutil.which("git")
        if git:
            # ...\Git\cmd\git.exe or ...\Git\mingw64\bin\git.exe: Git's folder is one or two levels up
            folder = os.path.dirname(os.path.realpath(git))
            for _ in range(3):
                roots.append(folder)
                folder = os.path.dirname(folder)
        roots.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git"))
        for root in roots:
            candidate = os.path.join(root, "usr", "bin", "ssh-keygen.exe")
            if os.path.isfile(candidate):
                return candidate
    found = shutil.which("ssh-keygen")
    if found:
        return found
    system32 = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh-keygen.exe"
    )
    return system32 if os.path.isfile(system32) else None


def release_signed(app_dir, ref, log=print):
    """'' if commit `ref` is signed with a key in this copy's own .github/allowed_signers, otherwise why
    it won't be installed, in plain words."""
    signers = os.path.join(app_dir, SIGNERS)
    keep = "so it wasn't installed and the app stays on the version it has."
    if not os.path.isfile(signers):
        return f"This copy has no .github/allowed_signers to check the update's signature against, {keep}"
    program = _ssh_keygen()
    if program is None:
        return f"OpenSSH's ssh-keygen isn't installed, so the update's signature can't be checked, {keep}"
    cmd = [
        "git",
        "-c",
        f"gpg.ssh.allowedSignersFile={signers}",
        "-c",
        f"gpg.ssh.program={program}",
        "log",
        "-1",
        "--format=%G?",
        ref,
    ]
    try:
        result = _run(cmd, app_dir, log)
        out = result.stdout.strip()
        status = out[-1:] if result.returncode == 0 and out else "E"
    except (OSError, subprocess.SubprocessError):
        status = "E"
    if status == "G":
        return ""
    why = SIGNATURE_STATUS.get(status, "couldn't be checked")
    return (
        f"The release {why}, {keep}\n"
        "Updates are installed only when they're signed with a key listed in .github/allowed_signers."
    )


def update_git_clone(app_dir, tag, python=sys.executable, log=print, install_deps=True):
    """Fast-forward the clone in app_dir to `tag` and reinstall dependencies.

    Raises UpdateError with a plain explanation if anything stops the update, including a release
    that isn't signed with a trusted key. The clone is left exactly as it was if the git steps fail.
    """
    branch = _git(
        ["rev-parse", "--abbrev-ref", "HEAD"], app_dir, log, "Couldn't read the current git branch."
    )
    if branch == "HEAD":
        raise UpdateError(
            "This copy isn't on a branch (detached HEAD), so it can't be updated automatically.\n"
            "Check out 'main' and try again."
        )
    changes = _git(
        ["status", "--porcelain", "--untracked-files=no"],
        app_dir,
        log,
        "Couldn't check this copy for local changes.",
    )
    if changes:
        raise UpdateError(
            "This copy has local changes to the app's files, so it wasn't updated:\n\n"
            f"{changes}\n\nCommit or undo them, then try again."
        )
    _git(
        ["fetch", "--tags", "origin"],
        app_dir,
        log,
        "Couldn't download the update from GitHub. Check the internet connection.",
    )
    commit = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        app_dir,
        log,
        f"The release {tag} wasn't found on GitHub.",
    )
    unsigned = release_signed(app_dir, commit, log)
    if unsigned:
        raise UpdateError(unsigned.replace("The release", f"Release {tag}", 1))
    _git(
        ["merge", "--ff-only", commit],
        app_dir,
        log,
        f"This copy has commits that aren't in {tag}, so it can't simply move to the new version.\n"
        "Update it by hand with git.",
    )

    if not install_deps:
        return

    # The theme package was swapped for a maintained fork in 1.1.0. Both install a "qdarktheme"
    # folder, so remove the old one and (re)install the fork's files over it.
    if _is_installed("pyqtdarktheme"):
        _run([python, "-m", "pip", "uninstall", "-y", "pyqtdarktheme"], app_dir, log)
        _run(
            [python, "-m", "pip", "install", "--force-reinstall", "--no-deps", "pyqtdarktheme-fork>=2.3.6"],
            app_dir,
            log,
        )
    extras = ".[ocr]" if importlib.util.find_spec("torch") is not None else "."
    result = _run([python, "-m", "pip", "install", "--disable-pip-version-check", "-e", extras], app_dir, log)
    if result.returncode != 0:
        raise UpdateError(
            f"The app's files were updated to {tag}, but installing its Python packages failed.\n"
            f'Run "{python} -m pip install -e {extras}" in the app folder to finish.'
        )
