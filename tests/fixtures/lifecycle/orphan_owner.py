#!/usr/bin/env python3
"""Stand-in chatsvc owner used to verify child death after owner SIGKILL."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("pid_file", type=Path)
args = parser.parse_args()
process = subprocess.Popen(
    [sys.executable, "-m", "networkclaw_harness.host", "--parent-pid", str(os.getpid())],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    start_new_session=True,
)
args.pid_file.write_text(str(process.pid))
while True:
    time.sleep(1)
