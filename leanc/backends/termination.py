from __future__ import annotations

import signal

from leanc.foundation.runner import ProcResult

SIGNAL_EXIT_STATUS_BASE = 128
UNSANDBOXED = "none"
HIGHEST_SIGNAL_NUMBER = 64

_SIGNAL_NAMES = {member.value: member.name for member in signal.Signals}


def signal_from_exit_status(exit_code: int | None) -> int | None:
    if exit_code is None:
        return None
    if SIGNAL_EXIT_STATUS_BASE < exit_code <= SIGNAL_EXIT_STATUS_BASE + HIGHEST_SIGNAL_NUMBER:
        return exit_code - SIGNAL_EXIT_STATUS_BASE
    return None


def signal_name(number: int) -> str:
    return _SIGNAL_NAMES.get(number, f"signal {number}")


def trusted_tool_terminating_signal(proc: ProcResult) -> int | None:
    if proc.signal is not None:
        return proc.signal
    return signal_from_exit_status(proc.exit_code)


def untrusted_program_terminating_signal(proc: ProcResult) -> int | None:
    if proc.signal is not None:
        return proc.signal
    if proc.sandbox == UNSANDBOXED:
        return None
    return signal_from_exit_status(proc.exit_code)
