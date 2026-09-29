"""Windowed views for judges with an input limit (Wave 3 E).

llm_mafia views reach ~330k characters, over Jev's 32k-token limit (and far
over llama's context). `WindowedJudge` wraps any judge and hands it a view cut
to at most `max_state_chars` characters of JSON:

- players and every public event are always kept;
- statements are kept newest first, as a contiguous tail of the game, until the
  budget is used up;
- each current speaker's latest statement in the last round is always kept,
  so the judge still scores exactly `current_speakers(view)`;
- a round that lost statements gets a public event saying how many earlier
  messages were omitted, so the judge knows the record is partial.

A view that already fits is passed through unchanged. The wrapper keeps the
inner judge's name; the window size goes into the config, so its judge_key
differs from the unwindowed judge's.

    python -m ww.judges.window --judge jev_batched --source llm_mafia --max-chars 60000 --limit 2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from ww.judges.base import Judge, JudgeResult, run_judge
from ww.transcripts.schema import current_speakers

WINDOW_VERSION = 1


def _size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def window_view(view: dict[str, Any], max_chars: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (windowed view, info). info: kept/total statements, chars before/after."""
    state = {"players": view["players"], "rounds": view["rounds"]}
    full = _size(state)
    n_total = sum(len(r["statements"]) for r in view["rounds"])
    if full <= max_chars:
        return view, {"truncated": False, "chars": full, "chars_full": full, "kept": n_total, "total": n_total}

    rounds = view["rounds"]
    # (round index, statement index) for every statement, oldest first
    order = [(ri, si) for ri, r in enumerate(rounds) for si in range(len(r["statements"]))]
    keep: set[tuple[int, int]] = set()
    last = len(rounds) - 1
    latest: dict[str, int] = {}
    for si, s in enumerate(rounds[last]["statements"]):
        latest[s["player_id"]] = si
    keep.update((last, si) for si in latest.values())

    skeleton = {"players": view["players"],
                "rounds": [{k: v for k, v in r.items() if k != "statements"} | {"statements": []} for r in rounds]}
    # room for the "omitted" notes
    used = _size(skeleton) + 100 * len(rounds)
    used += sum(_size(rounds[ri]["statements"][si]) + 2 for ri, si in keep)
    for ri, si in reversed(order):
        if (ri, si) in keep:
            continue
        cost = _size(rounds[ri]["statements"][si]) + 2
        if used + cost > max_chars:
            break
        keep.add((ri, si))
        used += cost

    out_rounds = []
    for ri, r in enumerate(rounds):
        kept = [s for si, s in enumerate(r["statements"]) if (ri, si) in keep]
        nr = {k: v for k, v in r.items() if k != "statements"}
        dropped = len(r["statements"]) - len(kept)
        if dropped:
            nr["public_events"] = list(r["public_events"]) + [
                f"[{dropped} of this round's {len(r['statements'])} messages are omitted here for length; "
                "the most recent messages are kept]"]
        nr["statements"] = kept
        out_rounds.append(nr)
    wv = {"players": view["players"], "rounds": out_rounds}
    n_kept = len(keep)
    return wv, {"truncated": True, "chars": _size(wv), "chars_full": full, "kept": n_kept, "total": n_total}


class WindowedJudge:
    def __init__(self, inner: Judge, max_state_chars: int):
        self.inner = inner
        self.name = inner.name
        self.max_state_chars = max_state_chars
        self.config = {**inner.config,
                       "view_window": {"version": WINDOW_VERSION, "max_state_chars": max_state_chars}}
        if hasattr(inner, "estimate"):  # only paid judges estimate; plan() checks hasattr
            self.estimate = self._estimate

    def _estimate(self, view: dict[str, Any]) -> dict[str, float]:
        return self.inner.estimate(window_view(view, self.max_state_chars)[0])  # type: ignore[attr-defined]

    def score(self, view: dict[str, Any]) -> JudgeResult:
        wv, info = window_view(view, self.max_state_chars)
        assert set(current_speakers(wv)) == set(current_speakers(view))
        result = self.inner.score(wv)
        result.meta["window"] = info
        return result


def main(argv: list[str] | None = None) -> int:
    from contextlib import nullcontext

    from ww.gpu import gpu_lock
    from ww.judges.run import COST_GATE_CALLS, GPU_JUDGES, PAID_JUDGES, format_plan, make_judge, plan

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judge", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--max-chars", type=int, required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--lock-timeout", type=float, default=-1)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    judge = WindowedJudge(make_judge(a.judge), a.max_chars)
    p = plan(judge, a.source, a.limit)
    print(format_plan(p, judge), flush=True)
    if a.plan_only:
        return 0
    if a.judge in PAID_JUDGES and p["new"]["calls"] > COST_GATE_CALLS and not a.yes:
        print(f"STOP: {p['new']['calls']} new paid calls > {COST_GATE_CALLS}. Get approval, then re-run with --yes.",
              file=sys.stderr)
        return 2
    lock = gpu_lock(timeout=a.lock_timeout) if a.judge in GPU_JUDGES else nullcontext()
    with lock:
        counts = run_judge(judge, a.source, limit=a.limit)
    print(f"done: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
