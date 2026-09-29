"""Choose abstention thresholds for live games from offline judgments.

For every judged view of a source, take the judge's top suspect and its
confidence. Report confidence percentiles and how often the top suspect is
really deceptive in each confidence quartile. The live-game thresholds are the
25th / 50th / 75th percentiles (rounded to 2 decimals).

    python -m ww.live.thresholds --judge jev_batched-0650211bf044 --source llm_llama31_8b
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import numpy as np

from ww.eval.data import load_source
from ww.judges.base import load_judgments


def top_suspect_rows(judge_key: str, source: str) -> list[dict[str, Any]]:
    transcripts = load_source(source)
    rows = []
    for gid, j in sorted(load_judgments(judge_key, source).items()):
        t = transcripts.get(gid)
        if t is None:
            continue
        team = t.team_of()
        for rn, r in sorted(j["rounds"].items(), key=lambda kv: int(kv[0])):
            p, c = r["p_deceptive"], r.get("confidence") or {}
            if not p or not c:
                continue
            top = max(sorted(p), key=lambda k: p[k])
            rows.append({"game": gid, "round": int(rn), "top": top, "p": p[top], "conf": c[top],
                         "correct": team[top] == "deceptive",
                         "chance": float(np.mean([team[k] == "deceptive" for k in p]))})
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    conf = np.array([r["conf"] for r in rows])
    correct = np.array([r["correct"] for r in rows], dtype=float)
    chance = np.array([r["chance"] for r in rows])
    pct = {q: float(np.percentile(conf, q)) for q in (10, 25, 50, 75, 90)}
    edges = [0.0, pct[25], pct[50], pct[75], 1.0]
    bins = []
    for lo, hi, name in zip(edges[:-1], edges[1:], ("q1", "q2", "q3", "q4")):
        m = (conf >= lo) & ((conf < hi) if name != "q4" else (conf <= hi))
        bins.append({"bin": name, "lo": lo, "hi": hi, "n": int(m.sum()),
                     "top_is_wolf": float(correct[m].mean()) if m.any() else float("nan"),
                     "chance": float(chance[m].mean()) if m.any() else float("nan")})
    return {"n_views": len(rows), "top_is_wolf": float(correct.mean()), "chance": float(chance.mean()),
            "percentiles": pct, "thresholds": [round(pct[25], 2), round(pct[50], 2), round(pct[75], 2)],
            "quartiles": bins}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judge", default="jev_batched-0650211bf044")
    ap.add_argument("--source", default="llm_llama31_8b")
    a = ap.parse_args(argv)
    print(json.dumps(summarize(top_suspect_rows(a.judge, a.source)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
