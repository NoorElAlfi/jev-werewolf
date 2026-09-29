import subprocess
import sys
import time
from pathlib import Path

import pytest
from filelock import Timeout

from ww.gpu import gpu_lock, gpu_lock_path

REPO = Path(__file__).resolve().parent.parent

HOLDER = """
import sys, time
from ww.gpu import gpu_lock
with gpu_lock():
    print("held", flush=True)
    time.sleep(float(sys.argv[1]))
"""


def test_gpu_lock_excludes_second_process(data_dir):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, "3"], stdout=subprocess.PIPE, text=True, cwd=REPO)
    try:
        assert proc.stdout.readline().strip() == "held"
        assert gpu_lock_path().parent == data_dir
        with pytest.raises(Timeout):
            with gpu_lock(timeout=0.5, poll_interval=0.05):
                pass
        # Waiting (instead of failing) succeeds once the holder exits.
        start = time.monotonic()
        with gpu_lock(timeout=30, poll_interval=0.05):
            waited = time.monotonic() - start
        assert 1.0 < waited < 30  # holder sleeps ~3s in total
    finally:
        proc.kill()
        proc.wait()


def test_gpu_lock_is_reusable_in_one_process():
    with gpu_lock(timeout=1):
        pass
    with gpu_lock(timeout=1):
        pass
