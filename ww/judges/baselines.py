"""Cheap baselines: a random judge and a keyword-cue judge. No model calls."""

from __future__ import annotations

import math
import random
import re
import time
from typing import Any

from ww.judges.base import JudgeResult
from ww.judges.common import clamp01, statements_of, view_digest
from ww.transcripts.schema import current_speakers


class RandomJudge:
    """Uniform random p_deceptive per player. Deterministic for a given view and
    seed, so re-runs and different machines agree. No confidence (metrics fall
    back to |p - 0.5|)."""

    name = "random"

    def __init__(self, seed: int = 0):
        self.config = {"version": 1, "seed": seed}

    def score(self, view: dict[str, Any]) -> JudgeResult:
        start = time.perf_counter()
        rng = random.Random(f"{self.config['seed']}:{view_digest(view)}")
        p = {pid: rng.random() for pid in current_speakers(view)}
        return JudgeResult(p_deceptive=p, meta={"latency_s": time.perf_counter() - start, "cost_usd": 0.0})


# Hand-made cue lists. Deliberately small and generic: these are the kinds of
# surface cues a naive "lie detector" would look for. Matched as whole words /
# phrases, case-insensitively.
HEDGE_CUES = [
    "maybe", "perhaps", "possibly", "probably", "might", "i think", "i guess",
    "i suppose", "not sure", "i'm not sure", "kind of", "sort of", "i don't know",
    "hard to say", "i believe", "it seems", "seems like", "i feel like",
]
ACCUSATION_CUES = [
    "suspicious", "suspect", "suspicion", "accuse", "accusing", "lying", "liar",
    "lie", "vote for", "vote out", "deflect", "deflecting", "hiding", "dodged",
    "dodging", "quick to", "sus", "don't trust", "can't trust", "fishy",
]
DEFENSIVE_CUES = [
    "trust me", "honestly", "to be honest", "i swear", "believe me", "not me",
    "i'm innocent", "i am innocent", "why would i", "i'm just", "i have nothing to hide",
    "i promise", "i would never",
]
CUE_WEIGHTS = {"hedge": 1.0, "accusation": 1.0, "defensive": 1.5}


def _compile(cues: list[str]) -> re.Pattern[str]:
    alts = sorted((re.escape(c).replace(r"\ ", r"\s+") for c in cues), key=len, reverse=True)
    return re.compile(r"(?<![\w'])(?:" + "|".join(alts) + r")(?![\w'])", re.IGNORECASE)


_PATTERNS = {
    "hedge": _compile(HEDGE_CUES),
    "accusation": _compile(ACCUSATION_CUES),
    "defensive": _compile(DEFENSIVE_CUES),
}


def cue_counts(text: str) -> dict[str, int]:
    text = text.replace("’", "'")
    return {k: len(p.findall(text)) for k, p in _PATTERNS.items()}


class KeywordJudge:
    """Counts hedging, accusation and defensive cues in each player's statements
    so far (all rounds in the view), per 100 words, weighted, and squashes the
    rate to (0, 1): p = 1 - exp(-rate / scale). More cues -> more suspicious.
    No confidence."""

    name = "keyword"

    def __init__(self, scale: float = 10.0):
        self.config = {"version": 1, "scale": scale, "weights": CUE_WEIGHTS,
                       "cues": {"hedge": HEDGE_CUES, "accusation": ACCUSATION_CUES, "defensive": DEFENSIVE_CUES}}

    def score(self, view: dict[str, Any]) -> JudgeResult:
        start = time.perf_counter()
        p: dict[str, float] = {}
        raw: dict[str, Any] = {}
        for pid in current_speakers(view):
            text = " ".join(s["text"] for s in statements_of(view, pid))
            counts = cue_counts(text)
            words = max(1, len(text.split()))
            weighted = sum(CUE_WEIGHTS[k] * n for k, n in counts.items())
            rate = 100.0 * weighted / words
            p[pid] = clamp01(1.0 - math.exp(-rate / self.config["scale"]))
            raw[pid] = {"counts": counts, "words": words, "rate_per_100w": rate}
        return JudgeResult(p_deceptive=p, meta={"latency_s": time.perf_counter() - start, "cost_usd": 0.0, "raw": raw})
