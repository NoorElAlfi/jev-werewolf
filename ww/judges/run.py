"""Run a judge over a transcript source, using the resumable base runner.

    python -m ww.judges.run --judge jev_batched --source fixtures --limit 2
    python -m ww.judges.run --judge jev_batched --source llm_llama31_8b --criteria leaky --no-defense
    python -m ww.judges.run --judge llama_judge --source fixtures --lock-timeout 60
    python -m ww.judges.run --judge jev_isolated --source fixtures --plan-only

Before starting it prints the planned views, API calls and estimated cost, for
the whole selection and for the part not yet cached. Jev runs of more than 50
new calls need --yes (CLAUDE.md cost gate: get the user's approval first).
Ollama judges run inside gpu_lock().
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext
from typing import Any

from ww.config import JEV_CHARS_PER_TOKEN, JEV_TOKENS_PER_CALL, transcripts_dir
from ww.gpu import gpu_lock
from ww.judges.base import Judge, JudgmentCache, key_of, rounds_to_judge, run_judge
from ww.judges.baselines import KeywordJudge, RandomJudge
from ww.judges.common import jev_cost_usd
from ww.judges.jev import JevBatchedJudge, JevIsolatedJudge
from ww.judges.llama import LlamaJudge
from ww.transcripts.schema import iter_source, load, public_view

JUDGES = {
    "random": RandomJudge,
    "keyword": KeywordJudge,
    "llama_judge": LlamaJudge,
    "jev_isolated": JevIsolatedJudge,
    "jev_batched": JevBatchedJudge,
}
GPU_JUDGES = {"llama_judge"}
PAID_JUDGES = {"jev_isolated", "jev_batched"}
COST_GATE_CALLS = 50


def make_judge(name: str, **kwargs: Any) -> Judge:
    """Build a judge by name. kwargs go to its constructor (e.g.
    criteria_variant="leaky", include_defense=False for the Jev judges)."""
    if name not in JUDGES:
        raise ValueError(f"unknown judge {name!r}; known: {sorted(JUDGES)}")
    return JUDGES[name](**kwargs)


def plan(judge: Judge, source: str, limit: int | None = None, max_round: int | None = None) -> dict[str, Any]:
    """Offline plan: views, API calls, estimated input tokens and cost, in total
    and for the views not yet cached. Calls: Jev judges count real API calls;
    llama_judge counts one Ollama request per view (retries not included);
    random/keyword make none."""
    cache = JudgmentCache(judge)
    files = iter_source(transcripts_dir(source))[: limit if limit else None]
    tot = {"games": len(files), "views": 0, "calls": 0, "input_tokens": 0.0}
    new = {"views": 0, "calls": 0, "input_tokens": 0.0}
    for f in files:
        t = load(f)
        complete = cache.is_complete(source, t.game_id)
        for rn in rounds_to_judge(t, max_round):
            view = public_view(t, rn)
            if hasattr(judge, "estimate"):
                e = judge.estimate(view)
            elif judge.name in GPU_JUDGES:
                e = {"calls": 1, "input_tokens": 0.0}
            else:
                e = {"calls": 0, "input_tokens": 0.0}
            cached = complete or cache.get_round(source, t.game_id, rn) is not None
            for d in (tot,) if cached else (tot, new):
                d["views"] += 1
                d["calls"] += e["calls"]
                d["input_tokens"] += e["input_tokens"]
    paid = judge.name in PAID_JUDGES
    for d in (tot, new):
        d["est_cost_usd"] = jev_cost_usd(int(d["input_tokens"])) if paid else 0.0
    return {"judge_key": key_of(judge), "total": tot, "new": new}


def format_plan(p: dict[str, Any], judge: Judge) -> str:
    t, n = p["total"], p["new"]
    lines = [
        f"judge_key: {p['judge_key']}",
        f"planned: {t['games']} games, {t['views']} views, {t['calls']} calls, "
        f"~{t['input_tokens']:.0f} input tokens, est. ${t['est_cost_usd']:.6f}",
        f"not yet cached: {n['views']} views, {n['calls']} calls, "
        f"~{n['input_tokens']:.0f} input tokens, est. ${n['est_cost_usd']:.6f}",
    ]
    if judge.name in PAID_JUDGES:
        lines.append(f"(token estimate = {JEV_TOKENS_PER_CALL}/call + JSON chars / {JEV_CHARS_PER_TOKEN}; "
                     "Jev bills input tokens only)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judge", required=True, choices=sorted(JUDGES))
    ap.add_argument("--source", required=True)
    ap.add_argument("--limit", type=int, default=None, help="only the first N games (sorted)")
    ap.add_argument("--max-round", type=int, default=None)
    ap.add_argument("--criteria", default=None, help="Jev criteria variant (default: generic)")
    ap.add_argument("--no-defense", action="store_true", help="Jev/llama: leave defenses out")
    ap.add_argument("--seed", type=int, default=None, help="random judge seed")
    ap.add_argument("--plan-only", action="store_true", help="print the plan and exit")
    ap.add_argument("--yes", action="store_true", help=f"allow a paid run of more than {COST_GATE_CALLS} new calls")
    ap.add_argument("--lock-timeout", type=float, default=-1, help="seconds to wait for the GPU lock (-1 = forever)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    kwargs: dict[str, Any] = {}
    if a.judge in PAID_JUDGES:
        if a.criteria:
            kwargs["criteria_variant"] = a.criteria
        if a.no_defense:
            kwargs["include_defense"] = False
    elif a.judge == "llama_judge" and a.no_defense:
        kwargs["include_defense"] = False
    if a.judge == "random" and a.seed is not None:
        kwargs["seed"] = a.seed
    judge = make_judge(a.judge, **kwargs)

    p = plan(judge, a.source, a.limit, a.max_round)
    print(format_plan(p, judge), flush=True)
    if a.plan_only:
        return 0
    if a.judge in PAID_JUDGES and p["new"]["calls"] > COST_GATE_CALLS and not a.yes:
        print(f"STOP: {p['new']['calls']} new paid calls > {COST_GATE_CALLS}. Get approval, then re-run with --yes.",
              file=sys.stderr)
        return 2

    lock = gpu_lock(timeout=a.lock_timeout) if a.judge in GPU_JUDGES else nullcontext()
    with lock:
        counts = run_judge(judge, a.source, limit=a.limit, max_round=a.max_round)
    print(f"done: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
