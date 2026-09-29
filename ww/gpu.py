"""Cross-process GPU lock. Anything that sends more than a handful of Ollama
requests must run inside `with gpu_lock(): ...`. A second process waits until
the first releases it."""

import logging
import os
import time
from contextlib import contextmanager

from filelock import FileLock, Timeout

from ww.config import data_dir

log = logging.getLogger(__name__)


def gpu_lock_path():
    return data_dir() / ".gpu.lock"


@contextmanager
def gpu_lock(timeout: float = -1, poll_interval: float = 5.0):
    """Hold the shared GPU lock. timeout=-1 waits forever; otherwise raises
    filelock.Timeout after `timeout` seconds."""
    path = gpu_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        if timeout == 0:
            raise
        log.warning("GPU lock %s is held by another job; waiting...", path)
        start = time.monotonic()
        lock.acquire(timeout=timeout, poll_interval=poll_interval)
        log.warning("GPU lock acquired after %.0fs", time.monotonic() - start)
    try:
        # Record the holder so a waiting agent can see who has the GPU.
        (path.parent / ".gpu.lock.owner").write_text(f"pid={os.getpid()} since={time.ctime()}\n")
        yield
    finally:
        lock.release()
