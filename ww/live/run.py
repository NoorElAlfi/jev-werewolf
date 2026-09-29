"""Play live games where a judge decides each day's vote (Wave 4 I).

    python -m ww.live.run --conditions jev,jev_t042 --n 50 --plan-only
    python -m ww.live.run --conditions jev --n 2                  # trial
    python -m ww.live.run --conditions llama,jev,jev_t028,jev_t042,jev_t061,jev_nodef,jev_adaptive --n 50 --yes

- Same game setup as the Wave 2 generator (9 players, 2 wolves, one Seer,
  llama3.1:8b for every player, generic persona, temperature 0.8, random night
  kills). Game i of every condition uses the generator's seed for game i
  (`game_seed(0, i)`), so it has the same seats and roles as
  `llm_llama31_8b/g{i:04d}`: that source *is* the random-vote condition.
- One transcript per game at $WW_DATA_DIR/transcripts/live_<condition>/g0000.json.
  The judge's final result for each day goes to the ordinary judgment cache
  ($WW_DATA_DIR/judgments/<judge_key>/live_<condition>/), so the Wave 2 report
  works on live games. The transcript is written last; a game with a valid
  transcript is done, so re-running the same command resumes.
- Conditions are interleaved (game 0 of every condition, then game 1, ...),
  so a partial run is balanced across conditions.
- Each game runs under gpu_lock(). Jev runs of more than 50 calls need --yes
  (cost gate).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ww.config import (
    GEN_MAX_ROUNDS,
    GEN_NUM_PLAYERS,
    GEN_NUM_WEREWOLVES,
    GEN_TEMPERATURE,
    OLLAMA_MODEL,
    transcripts_dir,
)
from ww.game.werewolf import Game, GameConfig
from ww.gpu import gpu_lock
from ww.judges.base import Judge, JudgmentCache, key_of
from ww.judges.jev import JevBatchedJudge
from ww.judges.llama import LlamaJudge
from ww.judges.window import WindowedJudge
from ww.live.policy import AdaptiveGame, JudgeDayPolicy
from ww.transcripts.generate import DEFAULT_RETRY_DELAYS, _is_done, game_id, game_seed
from ww.transcripts.schema import Transcript, save

LIVE_VERSION = 1
# The Wave 3 judges used a 90k-char view window; wrapping the same way gives the
# same judge_keys as reports/core.md (the window never cuts these short games).
VIEW_WINDOW_CHARS = 90000
COST_GATE_CALLS = 50
MAX_DAYS = 5  # 9 players / 2 wolves: with no day eliminations the wolves win after night 5
BASE_SEED = 0


@dataclass(frozen=True)
class Condition:
    judge: str                    # "jev_batched" | "llama_judge"
    include_defense: bool = True  # the judge sees defenses, and re-scores after today's defense
    threshold: float | None = None  # abstain when the top suspect's confidence is below this
    adaptive: bool = False        # wolves see their previous-day suspicion score
    note: str = ""

    @property
    def calls_per_day(self) -> int:
        return 2 if self.include_defense else 1

    @property
    def paid(self) -> bool:
        return self.judge.startswith("jev")


# Thresholds 0.28 / 0.42 / 0.61 are the 25th / 50th / 75th percentiles of the top
# suspect's Jev confidence over the 699 jev_batched views of llm_llama31_8b
# (see `python -m ww.live.thresholds`), so they abstain on roughly a quarter,
# half and three quarters of days.
CONDITIONS: dict[str, Condition] = {
    "llama": Condition("llama_judge", note="llama_judge, defense, no abstention"),
    "jev": Condition("jev_batched", note="jev_batched, defense, no abstention"),
    "jev_t028": Condition("jev_batched", threshold=0.28, note="jev_batched, defense, abstain if conf < 0.28"),
    "jev_t042": Condition("jev_batched", threshold=0.42, note="jev_batched, defense, abstain if conf < 0.42"),
    "jev_t061": Condition("jev_batched", threshold=0.61, note="jev_batched, defense, abstain if conf < 0.61"),
    "jev_nodef": Condition("jev_batched", include_defense=False,
                           note="jev_batched without defenses (one score per day), no abstention"),
    "jev_adaptive": Condition("jev_batched", adaptive=True,
                              note="jev_batched, defense, no abstention; wolves see their previous-day score"),
}


def source_of(condition: str) -> str:
    return f"live_{condition}"


def make_condition_judge(c: Condition) -> Judge:
    if c.judge == "jev_batched":
        inner: Any = JevBatchedJudge(include_defense=c.include_defense)
    elif c.judge == "llama_judge":
        inner = LlamaJudge(include_defense=c.include_defense)
    else:
        raise ValueError(f"unknown judge {c.judge!r}")
    return WindowedJudge(inner, VIEW_WINDOW_CHARS)


def game_config() -> GameConfig:
    return GameConfig(num_players=GEN_NUM_PLAYERS, num_werewolves=GEN_NUM_WEREWOLVES, persona="generic",
                      model=OLLAMA_MODEL, temperature=GEN_TEMPERATURE, max_rounds=GEN_MAX_ROUNDS)


def play_one(name: str, index: int, base_seed: int = BASE_SEED,
             retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
             judge_retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS) -> tuple[Transcript, JudgeDayPolicy, Judge]:
    c = CONDITIONS[name]
    judge = make_condition_judge(c)
    seed = game_seed(base_seed, index)
    cls = AdaptiveGame if c.adaptive else Game
    game = cls(game_config(), rng=random.Random(seed), chat_retry_delays=retry_delays)
    on_final = game.record_feedback if c.adaptive else None  # type: ignore[attr-defined]
    policy = JudgeDayPolicy(judge, rescore_after_defense=c.include_defense, threshold=c.threshold,
                            on_final=on_final, retry_delays=judge_retry_delays)
    start = time.monotonic()
    game.play(policy)
    elapsed = time.monotonic() - start
    meta = {
        "generator": "ww.live.run",
        "live_version": LIVE_VERSION,
        "generator_model": game.config.model,
        "persona_variant": game.config.persona,
        "condition": name,
        "condition_spec": {"judge": c.judge, "include_defense": c.include_defense, "threshold": c.threshold,
                           "adaptive": c.adaptive, "note": c.note},
        "judge_key": key_of(judge),
        "seed": base_seed,
        "game_index": index,
        "game_seed": seed,
        "config": game.config.to_dict(),
        "day_policy": policy.name,
        "decisions": {str(k): v for k, v in policy.decisions.items()},
        "n_judge_calls": policy.n_score_calls,
        "judge_cost_usd": policy.cost_usd(),
        "n_model_calls": game.n_model_calls,
        "gen_seconds": round(elapsed, 2),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if c.adaptive:
        meta["wolf_feedback"] = {str(k): v for k, v in game.feedback_log.items()}  # type: ignore[attr-defined]
    return game.transcript(game_id=game_id(index), source=source_of(name), meta=meta), policy, judge


def write_game(t: Transcript, policy: JudgeDayPolicy, judge: Judge) -> None:
    """Judgments first, transcript last (the transcript marks the game done)."""
    cache = JudgmentCache(judge)
    cache.write_config()
    rounds = sorted(policy.final)
    for i, rn in enumerate(rounds):
        cache.put_round(t.source, t.game_id, rn, policy.final[rn], complete=(i == len(rounds) - 1))
    save(t, transcripts_dir(t.source) / f"{t.game_id}.json")


def plan(names: list[str], n: int, start: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {"conditions": {}, "new_games": 0, "paid_calls_max": 0, "paid_calls_expected": 0}
    for name in names:
        c = CONDITIONS[name]
        todo = [i for i in range(start, n) if not _is_done(transcripts_dir(source_of(name)) / f"{game_id(i)}.json")]
        max_calls = len(todo) * MAX_DAYS * c.calls_per_day
        # ~3.5 days per game under random votes (Wave 3); abstention makes games longer
        exp_days = 3.5 if c.threshold is None else min(MAX_DAYS, 3.5 + 1.5 * c.threshold / 0.61)
        exp_calls = round(len(todo) * exp_days * c.calls_per_day)
        out["conditions"][name] = {"new_games": len(todo), "judge_calls_max": max_calls,
                                   "judge_calls_expected": exp_calls, "paid": c.paid}
        out["new_games"] += len(todo)
        if c.paid:
            out["paid_calls_max"] += max_calls
            out["paid_calls_expected"] += exp_calls
    return out


def run(names: list[str], n: int, start: int = 0, base_seed: int = BASE_SEED, log=print,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        judge_retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS) -> dict[str, int]:
    todo = [(i, name) for i in range(start, n) for name in names
            if not _is_done(transcripts_dir(source_of(name)) / f"{game_id(i)}.json")]
    log(f"[live] {len(todo)} games to play ({', '.join(names)}; indices {start}..{n - 1})")
    times: list[float] = []
    for k, (i, name) in enumerate(todo, 1):
        g0 = time.monotonic()
        with gpu_lock():
            t, policy, judge = play_one(name, i, base_seed, retry_delays, judge_retry_delays)
        write_game(t, policy, judge)
        times.append(time.monotonic() - g0)
        mean = sum(times) / len(times)
        eta = mean * (len(todo) - k)
        days = sum(1 for r in t.rounds if r.statements)
        outs = sum(1 for d in policy.decisions.values() if not d["abstained"])
        log(f"[live] {name} {t.game_id} ({k}/{len(todo)}) {times[-1]:.0f}s, {days} days, {outs} voted out, "
            f"winner={t.winner}, judge calls {policy.n_score_calls}, ${policy.cost_usd():.5f}; "
            f"mean {mean:.0f}s/game, ETA ~{time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() + eta))}")
    return {"played": len(todo)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conditions", required=True, help=f"comma-separated, from: {', '.join(CONDITIONS)}")
    ap.add_argument("--n", type=int, required=True, help="games per condition (indices 0..n-1)")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--yes", action="store_true", help=f"allow more than {COST_GATE_CALLS} paid Jev calls")
    a = ap.parse_args(argv)
    names = [s.strip() for s in a.conditions.split(",") if s.strip()]
    bad = [s for s in names if s not in CONDITIONS]
    if bad:
        ap.error(f"unknown condition(s) {bad}")

    def log(msg: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    p = plan(names, a.n, a.start)
    log(f"[live] plan: {json.dumps(p)}")
    if a.plan_only:
        return 0
    if p["paid_calls_max"] > COST_GATE_CALLS and not a.yes:
        print(f"STOP: up to {p['paid_calls_max']} paid Jev calls > {COST_GATE_CALLS}. "
              "Get approval, then re-run with --yes.", file=sys.stderr)
        return 2
    result = run(names, a.n, a.start, log=log)
    log(f"[live] result {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
