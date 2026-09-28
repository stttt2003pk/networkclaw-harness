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

    actual_parent_pid = os.getppid()
    # PID 1 is a valid owner inside a single-process container: chatrtmgr is
    # commonly the container init and therefore the Gateway's real parent.
    if expected_parent_pid <= 0 or actual_parent_pid != expected_parent_pid:
        raise RuntimeError(
            "Harness parent does not match the declared owner "
            f"(expected={expected_parent_pid}, actual={actual_parent_pid})"
        )
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        if os.getppid() != expected_parent_pid:
            os.kill(os.getpid(), signal.SIGTERM)

    def monitor() -> None:
        while os.getppid() == expected_parent_pid:
            time.sleep(interval_seconds)
        LOGGER.error("Harness parent disappeared; fencing Harness")
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        os._exit(75)

    threading.Thread(target=monitor, name="parent-death-guard", daemon=True).start()
