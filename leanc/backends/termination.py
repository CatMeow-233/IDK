"""How a tool process ended: the signal behind a sandbox's 128+N exit status, and readable signal names."""

from __future__ import annotations

import signal

SIGNAL_EXIT_STATUS_BASE = 128
HIGHEST_SIGNAL_NUMBER = 64

_SIGNAL_NAMES = {member.value: member.name for member in signal.Signals}


def signal_from_exit_status(exit_code: int | None) -> int | None:
    """The signal N behind exit status 128+N, which is how bubblewrap reports a child killed by a signal."""
    if exit_code is None:
        return None
    if SIGNAL_EXIT_STATUS_BASE < exit_code <= SIGNAL_EXIT_STATUS_BASE + HIGHEST_SIGNAL_NUMBER:
        return exit_code - SIGNAL_EXIT_STATUS_BASE
    return None


def signal_name(number: int) -> str:
    return _SIGNAL_NAMES.get(number, f"signal {number}")
