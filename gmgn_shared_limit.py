"""Cross-process GMGN request pacing for the embedded Radar and paper worker."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_PATH = Path(os.getenv("GMGN_SHARED_LIMIT_FILE", "/tmp/gmgn_shared_limit.json"))
MIN_GAP = float(os.getenv("GMGN_SHARED_GAP_SECONDS", "4"))

@contextmanager
def request_slot():
    """Hold a process-wide file lock through the HTTP request."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(str(LOCK_PATH) + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(LOCK_PATH.read_text())
            except (OSError, ValueError):
                state = {}
            wait = max(float(state.get("next", 0)), float(state.get("blocked", 0))) - time.time()
            if wait > 0:
                time.sleep(min(wait, 900))
            LOCK_PATH.write_text(json.dumps({"next": time.time() + MIN_GAP, "blocked": float(state.get("blocked", 0))}))
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def note_rate_limit(headers=None):
    delay = 60.0
    if headers:
        raw = headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except (ValueError, TypeError):
                from email.utils import parsedate_to_datetime
                try:
                    delay = parsedate_to_datetime(raw).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
    delay = min(900.0, max(1.0, delay))
    with open(str(LOCK_PATH) + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(LOCK_PATH.read_text())
            except (OSError, ValueError):
                state = {}
            state["blocked"] = max(float(state.get("blocked", 0)), time.time() + delay)
            LOCK_PATH.write_text(json.dumps(state))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return delay
