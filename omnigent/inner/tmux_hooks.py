"""tmux hooks that let a start-on-attach pane follow its viewers.

The pane command waits on :data:`START_ON_ATTACH_CHANNEL` before starting, and
the hook table wakes :data:`CLIENTS_CHANGED_CHANNEL` after every attach and the
last detach so the pane application can stop painting an unviewed screen. Shared
by :mod:`omnigent.inner.terminal` (launch) and :mod:`omnigent.repl._tmux_pane`.
"""

from __future__ import annotations

START_ON_ATTACH_CHANNEL = "omnigent-start-on-attach"
CLIENTS_CHANGED_CHANNEL = "omnigent-clients-changed"


def client_hook_commands() -> list[list[str]]:
    """Return the ``set-hook`` commands for a start-on-attach tmux server.

    ``client-detached`` signals only once ``#{session_attached}`` is zero, so a
    viewer leaving while others remain is silent. ``wait-for -S`` remembers a
    signal nobody awaited yet, so a watcher started late misses nothing.

    :returns: Argument lists for one tmux command sequence.
    """
    return [
        [
            "set-hook",
            "-g",
            "client-attached",
            f"wait-for -S {START_ON_ATTACH_CHANNEL} ; wait-for -S {CLIENTS_CHANGED_CHANNEL}",
        ],
        [
            "set-hook",
            "-g",
            "client-detached",
            f"if-shell -F '#{{==:#{{session_attached}},0}}' "
            f"'wait-for -S {CLIENTS_CHANGED_CHANNEL}'",
        ],
    ]
