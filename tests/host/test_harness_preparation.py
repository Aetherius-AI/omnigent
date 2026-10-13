"""Managed-sandbox harness auto-preparation: the ``IS_SANDBOX`` gate and the
install-only-when-missing decision. Threads run inline so dispatch is
deterministic, and the npm install itself is patched out."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from omnigent.host import harness_preparation as hp
from omnigent.onboarding.harness_install import PI_KEY, HarnessInstallResult


class _SyncThread:
    """A ``threading.Thread`` stand-in that runs its target inline on start."""

    def __init__(
        self,
        *,
        target: Callable[..., object],
        args: Sequence[object] = (),
        name: str | None = None,
        daemon: bool | None = None,
    ) -> None:
        self._target = target
        self._args = tuple(args)

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _sync_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp.threading, "Thread", _SyncThread)


def _record_installs(monkeypatch: pytest.MonkeyPatch, *, installed: bool) -> list[str]:
    """Patch the CLI probe + installer; return the list of install attempts."""
    attempts: list[str] = []

    def fake_installed(key: str, *_a: object, **_k: object) -> bool:
        return installed

    def fake_try_install(key: str) -> HarnessInstallResult:
        attempts.append(key)
        return HarnessInstallResult(True, None)

    monkeypatch.setattr(hp, "harness_cli_installed", fake_installed)
    monkeypatch.setattr(hp, "try_install_harness_cli", fake_try_install)
    return attempts


def test_installs_missing_pi_on_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    attempts = _record_installs(monkeypatch, installed=False)
    assert hp.prepare_host_harnesses() is True
    assert attempts == [PI_KEY]


def test_skips_install_when_pi_already_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    attempts = _record_installs(monkeypatch, installed=True)
    # A prepare thread still runs, but it must not reinstall a present CLI.
    assert hp.prepare_host_harnesses() is True
    assert attempts == []


def test_noop_off_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    attempts = _record_installs(monkeypatch, installed=False)
    assert hp.prepare_host_harnesses() is False
    assert attempts == []


def test_blank_is_sandbox_is_treated_as_laptop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", " ")
    attempts = _record_installs(monkeypatch, installed=False)
    assert hp.prepare_host_harnesses() is False
    assert attempts == []


def test_install_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")

    def boom(key: str, *_a: object, **_k: object) -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hp, "harness_cli_installed", boom)
    # Best-effort: a failing probe/install must leave the daemon starting
    # normally, not crash host boot.
    assert hp.prepare_host_harnesses() is True
