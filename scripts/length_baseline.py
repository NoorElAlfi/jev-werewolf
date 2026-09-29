"""A 'how much did they talk' baseline judge (Wave 3 E), to check whether a judge's
signal is just message volume. Free and offline.

p_deceptive = 1 - exp(-chars / scale), where chars = the player's total statement
characters in the view (all rounds so far). Monotone in chars, so its AUC is the
AUC of talk volume. A judge that only matches this AUC may be reading volume.

    python scripts/length_baseline.py human_mafia llm_mafia avalon werewolf_among_us
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ww.judges.base import JudgeResult, key_of, run_judge  # noqa: E402
from ww.judges.common import statements_of  # noqa: E402
from ww.transcripts.schema import current_speakers  # noqa: E402


class LengthJudge:
    name = "length"

    def __init__(self, scale: float = 5000.0):
        self.config = {"version": 1, "scale": scale}

    def score(self, view):
        t0 = time.perf_counter()
        p = {}
        for pid in current_speakers(view):
            chars = sum(len(s["text"]) for s in statements_of(view, pid))
            p[pid] = 1 - math.exp(-chars / self.config["scale"])
        return JudgeResult(p_deceptive=p, meta={"latency_s": time.perf_counter() - t0, "cost_usd": 0.0})


if __name__ == "__main__":
    j = LengthJudge()
    for s in sys.argv[1:]:
        print(key_of(j), s, run_judge(j, s))
