"""Judge interface, result type, on-disk cache and the resumable runner.

A judge sees only public_view() output and returns a suspicion probability per
player. The runner scores every round of every game (the view up to that
round), so metrics can look at round 1 alone or at every round.

Cache layout: $WW_DATA_DIR/judgments/<judge_key>/<source>/<game_id>.json
(the source folder avoids game_id collisions between datasets), plus
<judge_key>/_config.json recording the judge's name and config.

Cache file:
  {"judge", "judge_key", "config", "source", "game_id", "complete": bool,
   "rounds": {"<round>": {"p_deceptive", "confidence"?, "meta"}}}
Rounds are written as they finish, so an interrupted game resumes mid-way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ww.config import judgments_dir, transcripts_dir
from ww.transcripts.schema import Transcript, iter_source, load, public_view

log = logging.getLogger(__name__)


@dataclass
class JudgeResult:
    p_deceptive: dict[str, float]
    confidence: dict[str, float] | None = None
    # latency_s (required), cost_usd, tokens, raw (anything JSON-serializable)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JudgeResult:
        return cls(p_deceptive=d["p_deceptive"], confidence=d.get("confidence"), meta=d.get("meta") or {})

    def check(self, expected_players: list[str] | None = None) -> None:
        """Raise ValueError if probabilities/confidences are out of [0, 1] or
        if expected players are missing."""
        for name, values in (("p_deceptive", self.p_deceptive), ("confidence", self.confidence or {})):
            for pid, v in values.items():
                if not (isinstance(v, (int, float)) and 0.0 <= v <= 1.0):
                    raise ValueError(f"{name}[{pid}] = {v!r} is not in [0, 1]")
        if expected_players is not None:
            missing = set(expected_players) - set(self.p_deceptive)
            if missing:
                raise ValueError(f"no p_deceptive for {sorted(missing)}")
        if "latency_s" not in self.meta:
            raise ValueError("meta.latency_s is required")


@runtime_checkable
class Judge(Protocol):
    name: str
    config: dict[str, Any]  # everything that changes the output; JSON-serializable

    def score(self, view: dict[str, Any]) -> JudgeResult: ...


def judge_key(name: str, config: dict[str, Any]) -> str:
    """Stable key: `<name>-<12 hex of sha256(canonical JSON of name+config)>`."""
    blob = json.dumps({"name": name, "config": config}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{name}-{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


def key_of(judge: Judge) -> str:
    return judge_key(judge.name, judge.config)


class JudgmentCache:
    def __init__(self, judge: Judge, root: Path | None = None):
        self.judge = judge
        self.key = key_of(judge)
        self.dir = (root or judgments_dir()) / self.key

    def path(self, source: str, game_id: str) -> Path:
        return self.dir / source / f"{game_id}.json"

    def load(self, source: str, game_id: str) -> dict[str, Any] | None:
        p = self.path(source, game_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def get_round(self, source: str, game_id: str, round_num: int) -> JudgeResult | None:
        d = self.load(source, game_id)
        if d is None or str(round_num) not in d["rounds"]:
            return None
        return JudgeResult.from_dict(d["rounds"][str(round_num)])

    def put_round(self, source: str, game_id: str, round_num: int, result: JudgeResult, complete: bool = False) -> None:
        d = self.load(source, game_id) or {
            "judge": self.judge.name,
            "judge_key": self.key,
            "config": self.judge.config,
            "source": source,
            "game_id": game_id,
            "complete": False,
            "rounds": {},
        }
        d["rounds"][str(round_num)] = result.to_dict()
        d["complete"] = complete
        self._write(self.path(source, game_id), d)

    def mark_complete(self, source: str, game_id: str) -> None:
        d = self.load(source, game_id)
        if d is not None and not d["complete"]:
            d["complete"] = True
            self._write(self.path(source, game_id), d)

    def is_complete(self, source: str, game_id: str) -> bool:
        d = self.load(source, game_id)
        return bool(d and d["complete"])

    def write_config(self) -> None:
        p = self.dir / "_config.json"
        if not p.exists():
            self._write(p, {"judge": self.judge.name, "judge_key": self.key, "config": self.judge.config})

    @staticmethod
    def _write(path: Path, d: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def rounds_to_judge(t: Transcript, max_round: int | None = None) -> list[int]:
    """Rounds that have statements (a view with nothing new to score is skipped)."""
    return [r.round for r in t.rounds if r.statements and (max_round is None or r.round <= max_round)]


def plan_calls(source: str, limit: int | None = None, max_round: int | None = None) -> dict[str, int]:
    """How many games / views (judge.score calls) a full run would make,
    ignoring the cache. Judges convert views to API calls themselves."""
    files = iter_source(transcripts_dir(source))[: limit if limit else None]
    views = sum(len(rounds_to_judge(load(f), max_round)) for f in files)
    return {"games": len(files), "views": views}


def run_judge(
    judge: Judge,
    source: str,
    limit: int | None = None,
    max_round: int | None = None,
    cache_root: Path | None = None,
) -> dict[str, int]:
    """Score every round of every game in a source, skipping cached rounds.
    Safe to interrupt and re-run. `limit` caps the number of games (taken in
    sorted order, so a --limit trial is a prefix of the full run).
    Returns counts: games, scored (new judge.score calls), cached."""
    cache = JudgmentCache(judge, cache_root)
    cache.write_config()
    files = iter_source(transcripts_dir(source))[: limit if limit else None]
    counts = {"games": 0, "scored": 0, "cached": 0}
    for f in files:
        t = load(f)
        counts["games"] += 1
        if cache.is_complete(source, t.game_id):
            counts["cached"] += len(rounds_to_judge(t, max_round))
            continue
        rounds = rounds_to_judge(t, max_round)
        for i, rn in enumerate(rounds):
            if cache.get_round(source, t.game_id, rn) is not None:
                counts["cached"] += 1
                continue
            view = public_view(t, upto_round=rn)
            start = time.perf_counter()
            result = judge.score(view)
            result.meta.setdefault("latency_s", time.perf_counter() - start)
            result.check()
            cache.put_round(source, t.game_id, rn, result, complete=(i == len(rounds) - 1))
            counts["scored"] += 1
        cache.mark_complete(source, t.game_id)
        log.info("%s: %s done", cache.key, t.game_id)
    return counts


def load_judgments(judge_key_: str, source: str, root: Path | None = None) -> dict[str, dict[str, Any]]:
    """All cached judgment files for one judge key and source, keyed by game_id."""
    d = (root or judgments_dir()) / judge_key_ / source
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))}
