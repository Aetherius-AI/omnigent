"""Auto-prepare managed-sandbox harness CLIs so arclets need no manual setup.

``omnigent host`` runs both on a developer's laptop and inside a managed sandbox
(an "arclet"). On an arclet the owner should be able to start a Pi session
without first installing the Pi CLI by hand on every fresh host. This module
installs the missing Pi CLI in the background at host startup, reusing the same
``npm install`` ``omni setup`` runs. The daemon's harness-readiness loop then
publishes the flip from ``binary-missing`` to ready on its own, so the picker
and the session-create gate stop blocking without any manual setup.

Sandbox-only, mirroring :func:`omnigent.host.databricks_credential.configure_host_databricks`:
silently ``npm install -g``-ing a coding CLI onto a laptop is an unwanted
local-machine footprint, so the install is gated on ``IS_SANDBOX=1`` — baked
into the managed host image and never set on a laptop. Best-effort and
non-blocking: a missing ``npm`` or a failed install leaves readiness reporting
the CLI missing, exactly as before.
"""

from __future__ import annotations

import logging
import os
import threading

from omnigent.onboarding.harness_install import (
    PI_KEY,
    harness_cli_installed,
    harness_install_spec,
    try_install_harness_cli,
)

_logger = logging.getLogger(__name__)

# Harness CLIs a managed sandbox prepares automatically. Only Pi today: it is
# the harness arclet owners repeatedly install by hand, and auto-installing a
# single CLI keeps the boot footprint bounded. Other npm/curl harnesses stay
# behind explicit setup.
_MANAGED_HARNESS_KEYS: tuple[str, ...] = (PI_KEY,)


def running_in_managed_sandbox() -> bool:
    """Whether this host is a managed sandbox (image sets ``IS_SANDBOX=1``)."""
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _install_missing_harness(key: str) -> None:
    """Install one managed harness CLI when it is missing and installable."""
    try:
        if harness_cli_installed(key):
            return
        result = try_install_harness_cli(key)
    except Exception:
        _logger.exception("Harness preparation: install of %r failed", key)
        return
    if result.installed:
        _logger.info("Harness preparation: installed %r CLI", key)
    else:
        _logger.info("Harness preparation: skipped %r CLI (%s)", key, result.reason)


def prepare_host_harnesses() -> bool:
    """Start background installs of missing managed-sandbox harness CLIs.

    A no-op off a managed sandbox. For each managed harness that has an install
    spec, spawns a daemon thread that installs it when missing. Non-blocking so
    host registration isn't delayed by a multi-second install; the daemon's
    readiness loop publishes the result once the binary resolves.

    :returns: ``True`` when at least one background install thread was started;
        ``False`` otherwise (not a managed sandbox).
    """
    if not running_in_managed_sandbox():
        return False
    started = False
    for key in _MANAGED_HARNESS_KEYS:
        if harness_install_spec(key) is None:
            continue
        threading.Thread(
            target=_install_missing_harness,
            args=(key,),
            name=f"host-harness-prepare-{key}",
            daemon=True,
        ).start()
        started = True
    return started
