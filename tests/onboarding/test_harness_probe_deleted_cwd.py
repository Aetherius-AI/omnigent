"""A removed host-daemon cwd must not make a present harness CLI read as
"not configured".

A daemon can outlive the directory it was started from; its readiness probes
then exec the vendor CLI from that deleted cwd, where ``claude`` exits
non-zero. These drive the real probe subprocess against a fake CLI that aborts
from a deleted cwd like the real one.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import omnigent._platform as _platform
from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

# Fake ``claude``: aborts from a deleted cwd like the real binary, else reports
# a supported version and a logged-in status.
_FAKE_CLAUDE = f"""#!{sys.executable}
import json
import os
import sys

try:
    os.getcwd()
except OSError:
    sys.stderr.write("error: The current working directory was deleted\\n")
    sys.exit(1)

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.268 (Claude Code)")
elif args == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True}}))
else:
    sys.exit(2)
"""


def _install_fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake ``claude`` first on ``PATH`` and clear the on-disk fallback."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "claude"
    cli.write_text(_FAKE_CLAUDE, encoding="utf-8")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # A developer's real claude under a fallback dir must not stand in for the fake.
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())


@contextlib.contextmanager
def _deleted_cwd(tmp_path: Path) -> Iterator[None]:
    """Run the body with the process cwd pointing at a just-removed directory."""
    original = os.getcwd()
    dead = tmp_path / "dead-worktree"
    dead.mkdir()
    os.chdir(dead)
    try:
        dead.rmdir()
        yield
    finally:
        os.chdir(original)


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_missing_harness_cli_survives_deleted_daemon_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch preflight's ``missing_harness_cli`` gate must still see a
    valid claude when the daemon's cwd was removed."""
    _install_fake_claude(tmp_path, monkeypatch)
    with _deleted_cwd(tmp_path):
        assert hi.missing_harness_cli("claude-native") is None
    # And the version is read correctly, not swallowed as a probe failure.
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_version(ANTHROPIC_FAMILY)[0] == "2.1.268"


@pytest.mark.skipif(os.name == "nt", reason="POSIX deleted-cwd semantics")
def test_login_probe_survives_deleted_daemon_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login/auth-status probe must also tolerate a removed cwd."""
    _install_fake_claude(tmp_path, monkeypatch)
    with _deleted_cwd(tmp_path):
        assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True


def test_cli_probe_cwd_only_overrides_when_the_cwd_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid cwd is inherited (``None``); a removed one falls back to a dir."""
    assert hi._cli_probe_cwd() is None
    with _deleted_cwd(tmp_path):
        fallback = hi._cli_probe_cwd()
    assert fallback is not None
    assert Path(fallback).is_dir()
