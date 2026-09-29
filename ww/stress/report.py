"""Stress-test report (Wave 4 I): stronger liars and adaptive wolves.

    python -m ww.stress.report --out reports/stress.md

1. Stronger liars: the core judges on `llm_gptoss20b_wolves` (wolves played by
   gpt-oss:20b, everyone else by llama3.1:8b) against the same game indices of
   `llm_llama31_8b` (all llama). Game i has the same seats and roles in both, so
   differences use a matched bootstrap that resamples game indices and takes
   both games of each drawn index.
2. Adaptive wolves: jev_batched's own live judgments in `live_jev_adaptive`
   (wolves see their previous-day score) against `live_jev` (they don't),
   overall and by round, with the same matched bootstrap.

Every number is computed from transcripts and cached judgments. The hand-written
interpretation between the INTERPRETATION markers is kept on re-runs.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ww.config import data_dir
from ww.eval import metrics as M
from ww.eval.data import build_tables, load_source
from ww.judges.base import load_judgments
from ww.transcripts.schema import Transcript

BEGIN, END = "<!-- INTERPRETATION START -->", "<!-- INTERPRETATION END -->"
N_BOOT = 1000

STRONG_SOURCE = "llm_gptoss20b_wolves"
BASE_SOURCE = "llm_llama31_8b"
CORE_JUDGES = [  # the judge keys of reports/core.md
    ("random", "random-f760e1b3a694"),
    ("keyword", "keyword-e6b64872a12e"),
    ("length", "length-daf73ce8d4a6"),
    ("llama_judge", "llama_judge-c990a3c24747"),
    ("jev_isolated", "jev_isolated-8b754593c8ab"),
    ("jev_batched", "jev_batched-0650211bf044"),
]
LIVE_JEV_KEY = "jev_batched-0650211bf044"

STATS: list[tuple[str, Callable[[M.GameTable], float]]] = [
    ("AUC", M.auc_stat),
    ("R1 top-1 minus chance", M.top1_minus_chance_stat),
    ("ECE", M.ece_stat),
]


def _index(gid: str) -> int:
    return int(re.search(r"g(\d+)$", gid).group(1))


def player_table(judge_key: str, source: str, transcripts: dict[str, Transcript]) -> M.GameTable:
    return build_tables(source, transcripts, load_judgments(judge_key, source)).players


def matched(a: M.GameTable, b: M.GameTable, stat: Callable[[M.GameTable], float], seed: int = 0):
    """stat(a) - stat(b) over the game indices present in both, with a
    bootstrap that resamples indices (both games of a drawn index go in)."""
    ia = {_index(g): g for g in a.games}
    ib = {_index(g): g for g in b.games}
    common = sorted(set(ia) & set(ib))
    if len(common) < 2:
        return math.nan, math.nan, math.nan, math.nan, len(common)
    a, b = a.only_games([ia[i] for i in common]), b.only_games([ib[i] for i in common])
    diff = stat(a) - stat(b)
    vals = np.empty(N_BOOT)
    rng = np.random.default_rng(seed)
    for k, draw in enumerate(rng.integers(0, len(common), (N_BOOT, len(common)))):
        idx = [common[j] for j in draw]
        vals[k] = stat(a.take_games([ia[i] for i in idx])) - stat(b.take_games([ib[i] for i in idx]))
    v = vals[~np.isnan(vals)]
    p = float(min(1.0, 2 * min((v <= 0).mean(), (v >= 0).mean()))) if len(v) else math.nan
    return diff, float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)), p, len(common)


def restrict(t: M.GameTable, indices: set[int]) -> M.GameTable:
    return t.only_games([g for g in t.games if _index(g) in indices])


def fci(c: M.CI) -> str:
    return c.fmt(3)


def fdiff(d) -> tuple[str, str, str]:
    diff, lo, hi, p, n = d
    if math.isnan(diff):
        return "n/a", "n/a", "n/a"
    verdict = "inconclusive (CI includes 0)" if lo <= 0 <= hi else ("higher" if lo > 0 else "lower")
    return f"{diff:+.3f} [{lo:+.3f}, {hi:+.3f}]", ("<0.001" if p < 0.001 else f"{p:.3f}"), verdict


def text_stats(ts: dict[str, Transcript]) -> dict[str, float]:
    out: dict[str, list[int]] = {"deceptive": [], "honest": []}
    for t in ts.values():
        team = t.team_of()
        for r in t.rounds:
            for s in r.statements:
                out[team[s.player_id]].append(len(s.text))
    return {k: float(np.mean(v)) if v else math.nan for k, v in out.items()}


def by_round_rows(label: str, a: M.GameTable, b: M.GameTable, max_round: int = 5) -> list[str]:
    rows = []
    for rn in range(1, max_round + 1):
        ra, rb = a.filter(a.round == rn), b.filter(b.round == rn)
        if len(ra.games) < 5 or len(rb.games) < 5:
            continue
        ca, cb = M.bootstrap_ci(ra, M.auc_stat, N_BOOT), M.bootstrap_ci(rb, M.auc_stat, N_BOOT)
        d, p, v = fdiff(matched(ra, rb, M.auc_stat))
        rows.append(f"| {rn} | {fci(ca)} (n={len(ra.games)}) | {fci(cb)} (n={len(rb.games)}) | {d} | {p} | {v} |")
    return rows


def wolf_gap_by_round(t: M.GameTable, max_round: int = 5) -> list[tuple[int, float, float, int]]:
    """Mean p of wolves minus mean p of villagers, per round."""
    out = []
    for rn in range(1, max_round + 1):
        m = t.round == rn
        if m.sum() == 0:
            continue
        y, p = t.y[m], t.p[m]
        if y.sum() == 0 or (1 - y).sum() == 0:
            continue
        out.append((rn, float(p[y == 1].mean()), float(p[y == 0].mean()), len(set(t.game[m].tolist()))))
    return out


def section_strong(lines: list[str]) -> None:
    strong = load_source(STRONG_SOURCE)
    base_all = load_source(BASE_SOURCE)
    idx = {_index(g) for g in strong}
    base = {g: t for g, t in base_all.items() if _index(g) in idx}
    lines += ["## 1. Stronger liars: gpt-oss:20b wolves", "",
              f"`{STRONG_SOURCE}`: {len(strong)} games; wolves speak with gpt-oss:20b (reasoning effort low, "
              "run on CPU), villagers and the Seer with llama3.1:8b; votes are random, as in the Wave 2 generator. "
              f"Compared with the same {len(base)} game indices of `{BASE_SOURCE}` (all llama3.1:8b; same seats and "
              "roles). Typographic punctuation was converted to ASCII in the gpt-oss games so it can't label the "
              "wolves.", ""]
    ss, sb = text_stats(strong), text_stats(base)
    lines += ["| Source | Mean chars / statement, wolves | villagers |", "|---|---|---|",
              f"| {STRONG_SOURCE} | {ss['deceptive']:.0f} | {ss['honest']:.0f} |",
              f"| {BASE_SOURCE} (same indices) | {sb['deceptive']:.0f} | {sb['honest']:.0f} |", ""]
    if strong:
        ms = [t.meta.get("model_stats", {}) for t in strong.values()]
        wolf_calls = sum(m.get("wolf_calls", 0) for m in ms)
        wolf_s = sum(m.get("wolf_seconds", 0.0) for m in ms)
        empties = sum(m.get("wolf_empty_retries", 0) for m in ms)
        gen = [t.meta.get("gen_seconds", math.nan) for t in strong.values()]
        lines += [f"Generation: {wolf_calls} wolf calls, mean {wolf_s / max(wolf_calls, 1):.1f} s each; "
                  f"{empties} empty gpt-oss answers were retried; mean {np.nanmean(gen):.0f} s per game.", ""]

    lines += ["| Judge | Games | AUC gpt-oss wolves | AUC llama wolves | Difference | p | Verdict |",
              "|---|---|---|---|---|---|---|"]
    detail: list[str] = []
    for name, key in CORE_JUDGES:
        a = player_table(key, STRONG_SOURCE, strong)
        b = restrict(player_table(key, BASE_SOURCE, base), idx)
        if not len(a) or not len(b):
            lines.append(f"| {name} | 0 | not judged yet | | | | |")
            continue
        ca, cb = M.bootstrap_ci(a, M.auc_stat, N_BOOT), M.bootstrap_ci(b, M.auc_stat, N_BOOT)
        d, p, v = fdiff(matched(a, b, M.auc_stat))
        lines.append(f"| {name} | {len(a.games)} | {fci(ca)} | {fci(cb)} | {d} | {p} | {v} |")
        for label, stat in STATS[1:]:
            ca, cb = M.bootstrap_ci(a, stat, N_BOOT), M.bootstrap_ci(b, stat, N_BOOT)
            d, p, v = fdiff(matched(a, b, stat))
            detail.append(f"| {name} | {label} | {fci(ca)} | {fci(cb)} | {d} | {p} | {v} |")
    lines += ["", "Difference = gpt-oss-wolf games minus llama-wolf games (matched by game index). AUC is per "
              "player-round, as in core.md.", "",
              "| Judge | Metric | gpt-oss wolves | llama wolves | Difference | p | Verdict |",
              "|---|---|---|---|---|---|---|"] + detail + [""]
    a = player_table(LIVE_JEV_KEY, STRONG_SOURCE, strong)
    b = restrict(player_table(LIVE_JEV_KEY, BASE_SOURCE, base), idx)
    if len(a) and len(b):
        lines += ["jev_batched AUC by round:", "",
                  "| Round | gpt-oss wolves | llama wolves | Difference | p | Verdict |", "|---|---|---|---|---|---|"]
        lines += by_round_rows("strong", a, b) + [""]


def section_adaptive(lines: list[str]) -> None:
    ad = load_source("live_jev_adaptive")
    na = load_source("live_jev")
    lines += ["## 2. Adaptive wolves (live games)", "",
              f"`live_jev_adaptive` ({len(ad)} games): jev_batched drives the vote (defense on, no abstention) and "
              "each wolf's prompt shows the score Jev gave it the previous day, its rank, and that the village "
              "votes out Jev's top suspect. `live_jev` "
              f"({len(na)} games): the same, without the feedback. Both use game indices 0..n-1 (same seats and "
              "roles). AUC uses Jev's own final judgment of each day. Caveat: in live games the judge also decides "
              "who is eliminated, so later rounds contain only the players it did not vote out; this affects both "
              "conditions, but not necessarily equally.", ""]
    a = player_table(LIVE_JEV_KEY, "live_jev_adaptive", ad)
    b = player_table(LIVE_JEV_KEY, "live_jev", na)
    if not len(a) or not len(b):
        lines += ["Not played yet.", ""]
        return
    lines += ["| Metric | Adaptive | Non-adaptive | Difference | p | Verdict |", "|---|---|---|---|---|---|"]
    for label, stat in STATS:
        ca, cb = M.bootstrap_ci(a, stat, N_BOOT), M.bootstrap_ci(b, stat, N_BOOT)
        d, p, v = fdiff(matched(a, b, stat))
        lines.append(f"| {label} | {fci(ca)} | {fci(cb)} | {d} | {p} | {v} |")
    wa = [float(t.winner == "villagers") for t in ad.values()]
    wb = [float(t.winner == "villagers") for t in na.values()]
    lines += [f"| Villager win rate | {np.mean(wa):.3f} | {np.mean(wb):.3f} | (see live report for CIs) | | |", "",
              "AUC by round:", "", "| Round | Adaptive | Non-adaptive | Difference | p | Verdict |",
              "|---|---|---|---|---|---|"]
    lines += by_round_rows("adaptive", a, b) + [""]
    lines += ["Mean Jev p for wolves vs villagers, by round (does the feedback pull wolves' scores down?):", "",
              "| Round | Adaptive: wolves | villagers | games | Non-adaptive: wolves | villagers | games |",
              "|---|---|---|---|---|---|---|"]
    ga, gb = {r[0]: r for r in wolf_gap_by_round(a)}, {r[0]: r for r in wolf_gap_by_round(b)}
    for rn in sorted(set(ga) | set(gb)):
        x, y = ga.get(rn), gb.get(rn)
        cells = [f"{x[1]:.3f} | {x[2]:.3f} | {x[3]}" if x else "- | - | -",
                 f"{y[1]:.3f} | {y[2]:.3f} | {y[3]}" if y else "- | - | -"]
        lines.append(f"| {rn} | {cells[0]} | {cells[1]} |")
    lines.append("")


def build(out: Path) -> None:
    old = out.read_text(encoding="utf-8") if out.exists() else ""
    lines = ["# Stress tests: stronger and adaptive liars", ""]
    if BEGIN in old and END in old:
        lines.append(old[old.index(BEGIN): old.index(END) + len(END)])
    else:
        lines += [BEGIN, "## Interpretation", "", "(to be written)", END]
    lines += ["", "---", "",
              f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m ww.stress.report --out {out.as_posix()}`. "
              f"Data: `{data_dir()}`. CIs: 95% percentile bootstrap ({N_BOOT} replicates, seed 0) resampling whole "
              "games; differences resample game indices and take both matched games.", ""]
    section_strong(lines)
    section_adaptive(lines)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="reports/stress.md")
    a = ap.parse_args(argv)
    build(Path(a.out))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
