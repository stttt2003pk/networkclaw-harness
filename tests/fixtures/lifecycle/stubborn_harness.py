#!/usr/bin/env python3
"""Protocol-shaped child that ignores shutdown signals for escalation tests."""

import argparse
import json
import signal
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--parent-pid")
parser.parse_args()
signal.signal(signal.SIGTERM, signal.SIG_IGN)

for line in sys.stdin:
    frame = json.loads(line)
    request_id = frame["request_id"]
    kind = frame["type"]
    if kind == "shutdown":
        time.sleep(60)
        continue
    payload = {}
    event_type = "error"
    if kind == "protocol.negotiate":
        event_type, payload = "protocol.negotiated", {"protocol_version": "1.0"}
    elif kind == "capabilities.query":
        event_type, payload = "capabilities.report", {"commands": ["session.open"]}
    elif kind == "health.query":
        event_type, payload = "health.status", {"status": "ready"}
    events = [
        {"protocol_version": "1.0", "type": event_type, "request_id": request_id,
         "sequence": 1, "occurred_at": "2026-09-17T00:00:00Z", "payload": payload, "end": False},
        {"protocol_version": "1.0", "type": "end", "request_id": request_id,
         "sequence": 2, "occurred_at": "2026-09-17T00:00:00Z", "payload": {}, "end": True},
    ]
    for event in events:
        print(json.dumps(event, separators=(",", ":")), flush=True)
