"""Child-side protection against an absent or replaced process owner."""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import sys
import threading
import time

LOGGER = logging.getLogger(__name__)


def install_parent_death_guard(expected_parent_pid: int, *, interval_seconds: float = 0.1) -> None:
    """Exit the Harness when its owning chatsvc process disappears."""

    if expected_parent_pid <= 1 or os.getppid() != expected_parent_pid:
        raise RuntimeError("Harness parent does not match the declared chatsvc owner")
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        if os.getppid() != expected_parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

    def monitor() -> None:
        while os.getppid() == expected_parent_pid:
            time.sleep(interval_seconds)
        LOGGER.error("chatsvc parent disappeared; fencing Harness")
        os._exit(75)

    threading.Thread(target=monitor, name="parent-death-guard", daemon=True).start()
