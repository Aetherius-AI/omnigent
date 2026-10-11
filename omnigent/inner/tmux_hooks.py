"""tmux hooks that let a start-on-attach pane follow its viewers.

A terminal launched with ``tmux_start_on_attach`` runs ``tmux wait-for
<START_ON_ATTACH_CHANNEL>; exec <command>`` so nothing starts until a client
looks at it. The same hook table wakes :data:`CLIENTS_CHANGED_CHANNEL` after
every attach and after the last detach, so the pane application can stop
painting a screen nobody is viewing. Shared by the runner-side launch
(:mod:`omnigent.inner.terminal`) and the REPL that consumes the signals
(:mod:`omnigent.repl._tmux_pane`).
"""

from __future__ import annotations

START_ON_ATTACH_CHANNEL = "omnigent-start-on-attach"
CLIENTS_CHANGED_CHANNEL = "omnigent-clients-changed"


def client_hook_commands() -> list[list[str]]:
    """Return the ``set-hook`` commands for a start-on-attach tmux server.

    ``client-attached`` releases the start gate and reports the new viewer.
    ``client-detached`` reports only once ``#{session_attached}`` has dropped
    to zero, so a viewer leaving while others remain is silent. ``wait-for -S``
    remembers a signal nobody awaited yet, which lets the pane application
    start its watcher after the first attach without missing a detach that
    raced it.

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
