"""Shared host/container lane already used by browser, OCR and Codex workers."""
import fcntl
from functools import wraps


def exclusive_heavy_work(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with (self.store.runtime / '.web-browser-lane.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return function(self, *args, **kwargs)
    return wrapped
