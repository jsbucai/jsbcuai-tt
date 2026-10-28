"""One nonblocking writer across import requests, app instances and helper processes."""
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

_registry_guard = threading.Lock()
_locks = {}
_local = threading.local()


class ImportBusy(Exception):
    pass


class ImportFailed(Exception):
    def __init__(self, message, results=None):
        super().__init__(message)
        self.results = results or []


@contextmanager
def import_lock(runtime):
    """Reentrant in one thread; never queue a duplicate import behind another."""
    runtime = Path(runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    key = str(runtime).casefold()
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(blocking=False):
        raise ImportBusy("数据正在更新，请等待本次完成后再试")
    held = getattr(_local, "held", {})
    _local.held = held
    if key in held:
        try:
            yield
        finally:
            lock.release()
        return
    handle = None
    try:
        lock_path = runtime / ".data-import.lock"
        if not lock_path.resolve().is_relative_to(runtime):
            raise ValueError("导入锁不能指向开发目录之外")
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ImportBusy("数据正在更新，请等待本次完成后再试") from exc
        held[key] = True
        try:
            yield
        finally:
            del held[key]
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        lock.release()


def snapshot_directory(root):
    """Resolve an atomically selected immutable generation; support initial flat copies."""
    root = Path(root).resolve()
    pointer = root / "current.json"
    if not pointer.exists():
        return root
    if not pointer.resolve().is_relative_to(root):
        raise ValueError("快照指针不能指向开发目录外")
    content = json.loads(pointer.read_text(encoding="utf-8-sig"))
    selected = (root / content["generation"]).resolve()
    if not selected.is_relative_to(root / "generations") or not selected.is_dir():
        raise ValueError("快照版本无效；保留现有开发数据")
    return selected
