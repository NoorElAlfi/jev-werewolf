"""Report on live games: villager win rate and game length per condition (Wave 4 I).

    python -m ww.live.report --out reports/live.md

Reads the live transcripts ($WW_DATA_DIR/transcripts/live_<condition>/) and, as
the random-vote condition, the same game indices of `llm_llama31_8b` (the Wave 2
generator plays exactly the same setup with uniformly random votes, and game i
has the same seats and roles in every condition). Every number is computed
from those files. CIs: 95% percentile bootstrap resampling whole games; paired
differences resample game indices present in both conditions.

The generated file starts with an interpretation section written by hand
between the INTERPRETATION markers; re-running keeps it.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ww.config import data_dir
from ww.eval.data import load_source
from ww.live.run import CONDITIONS, source_of
from ww.live.thresholds import summarize, top_suspect_rows
from ww.transcripts.schema import Transcript

RANDOM_SOURCE = "llm_llama31_8b"
BEGIN, END = "<!-- INTERPRETATION START -->", "<!-- INTERPRETATION END -->"
N_BOOT = 2000


@dataclass
class GameRow:
    index: int
    villagers_won: float
    days: float
    wolves_out: float
    villagers_out: float
    abstained: float        # days nobody was voted out
    top_correct: float      # days the judge's top suspect was a wolf (nan for random)
    day1_out_is_wolf: float  # nan if nobody was voted out on day 1
    judge_calls: float
    cost: float
    seconds: float


@dataclass
class CondData:
    name: str
    label: str
    rows: list[GameRow] = field(default_factory=list)

    def by_index(self) -> dict[int, GameRow]:
        return {r.index: r for r in self.rows}


def game_row(t: Transcript) -> GameRow:
    team = t.team_of()
    days = [r for r in t.rounds if r.statements]
    outs = [r.eliminated for r in days if r.eliminated]
    dec = t.meta.get("decisions") or {}
    tops = [d["top"] for d in dec.values()]
    d1 = days[0].eliminated if days else None
    return GameRow(
        index=int(t.meta.get("game_index", int(t.game_id[1:]))),
        villagers_won=float(t.winner == "villagers"),
        days=float(len(days)),
        wolves_out=float(sum(team[p] == "deceptive" for p in outs)),
        villagers_out=float(sum(team[p] == "honest" for p in outs)),
        abstained=float(sum(r.eliminated is None for r in days)),
        top_correct=float(sum(team[p] == "deceptive" for p in tops)) if tops else math.nan,
        day1_out_is_wolf=float(team[d1] == "deceptive") if d1 else math.nan,
        judge_calls=float(t.meta.get("n_judge_calls", 0)),
        cost=float(t.meta.get("judge_cost_usd", 0.0)),
        seconds=float(t.meta.get("gen_seconds", math.nan)),
    )


def load_conditions(names: list[str], random_n: int | None) -> list[CondData]:
    out = []
    for name in names:
        ts = load_source(source_of(name))
        out.append(CondData(name, name, [game_row(t) for t in ts.values()]))
    n = random_n or max((len(c.rows) for c in out), default=0)
    ref = load_source(RANDOM_SOURCE)
    rows = [game_row(t) for t in ref.values()]
    rnd = CondData("random", "random", [r for r in rows if r.index < n])
    rnd_all = CondData("random_all", f"random (all {len(rows)})", rows)
    return [rnd] + out + ([rnd_all] if len(rows) > n else [])


# --- statistics -------------------------------------------------------------------

Stat = Any  # callable(list[GameRow]) -> float


def mean_of(attr: str) -> Stat:
    def f(rows):
        v = np.array([getattr(r, attr) for r in rows], dtype=float)
        v = v[~np.isnan(v)]
        return float(v.mean()) if len(v) else math.nan
    return f


def ratio_of(num: str, den: str) -> Stat:
    def f(rows):
        d = sum(getattr(r, den) for r in rows)
        return sum(getattr(r, num) for r in rows) / d if d else math.nan
    return f


def ci(rows: list[GameRow], stat: Stat, seed: int = 0) -> tuple[float, float, float]:
    est = stat(rows)
    if len(rows) < 2 or math.isnan(est):
        return est, math.nan, math.nan
    rng = np.random.default_rng(seed)
    vals = np.array([stat([rows[i] for i in draw]) for draw in rng.integers(0, len(rows), (N_BOOT, len(rows)))])
    vals = vals[~np.isnan(vals)]
    return est, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def paired(x: CondData, y: CondData, stat: Stat, seed: int = 0) -> tuple[float, float, float, float, int]:
    xi, yi = x.by_index(), y.by_index()
    common = sorted(set(xi) & set(yi))
    if len(common) < 2:
        return math.nan, math.nan, math.nan, math.nan, len(common)
    xr, yr = [xi[i] for i in common], [yi[i] for i in common]
    diff = stat(xr) - stat(yr)
    rng = np.random.default_rng(seed)
    vals = []
    for draw in rng.integers(0, len(common), (N_BOOT, len(common))):
        vals.append(stat([xr[i] for i in draw]) - stat([yr[i] for i in draw]))
    v = np.array(vals)
    v = v[~np.isnan(v)]
    p = float(min(1.0, 2 * min((v <= 0).mean(), (v >= 0).mean())))
    return diff, float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)), p, len(common)


def fmt(c: tuple[float, float, float], pct: bool = False, digits: int = 2) -> str:
    est, lo, hi = c
    if math.isnan(est):
        return "n/a"
    if pct:
        return f"{100 * est:.0f}% [{100 * lo:.0f}, {100 * hi:.0f}]" if not math.isnan(lo) else f"{100 * est:.0f}%"
    return f"{est:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]" if not math.isnan(lo) else f"{est:.{digits}f}"


def verdict(lo: float, hi: float) -> str:
    if math.isnan(lo):
        return "n/a"
    return "inconclusive (CI includes 0)" if lo <= 0 <= hi else ("X higher" if lo > 0 else "X lower")


METRICS: list[tuple[str, str, Stat, bool]] = [
    ("Villager win rate", "villagers_won", mean_of("villagers_won"), True),
    ("Days per game", "days", mean_of("days"), False),
    ("Wolves voted out / game", "wolves_out", mean_of("wolves_out"), False),
    ("Villagers voted out / game", "villagers_out", mean_of("villagers_out"), False),
    ("Days with no elimination", "abstained", ratio_of("abstained", "days"), True),
    ("Top suspect is a wolf (per day)", "top_correct", ratio_of("top_correct", "days"), True),
    ("Day-1 elimination is a wolf", "day1_out_is_wolf", mean_of("day1_out_is_wolf"), True),
]


# --- plot ----------------------------------------------------------------------

def plot(conds: list[CondData], out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid, blue = "#0b0b0b", "#52514e", "#e5e4e0", "#2a78d6"
    labels = [c.label for c in conds]
    fig, axes = plt.subplots(1, 2, figsize=(10, 0.45 * len(conds) + 1.4), sharey=True)
    for ax, (title, stat, pct) in zip(axes, [("Villager win rate", mean_of("villagers_won"), True),
                                             ("Days per game", mean_of("days"), False)]):
        for k, c in enumerate(conds):
            est, lo, hi = ci(c.rows, stat)
            s = 100 if pct else 1
            if not math.isnan(lo):
                ax.plot([lo * s, hi * s], [k, k], color=blue, lw=2, solid_capstyle="round")
            ax.plot([est * s], [k], "o", color=blue, ms=8, mec="#fcfcfb", mew=2)
        ax.set_title(title + (" (%)" if pct else ""), color=ink, fontsize=11, loc="left")
        ax.grid(axis="x", color=grid, lw=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.spines["bottom"].set_color(muted)
        ax.tick_params(colors=muted, labelsize=9, length=0)
    axes[0].set_yticks(range(len(conds)))
    axes[0].set_yticklabels(labels, color=ink)
    axes[0].invert_yaxis()
    fig.text(0.01, 0.005, "Dots: point estimate; bars: 95% bootstrap CI (resampling games).", color=muted, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out_png, dpi=130, facecolor="#fcfcfb")
    plt.close(fig)


# --- markdown -------------------------------------------------------------------

def build(names: list[str], out: Path, random_n: int | None = None) -> str:
    conds = load_conditions(names, random_n)
    by = {c.name: c for c in conds}
    lines: list[str] = []
    old = out.read_text(encoding="utf-8") if out.exists() else ""
    if BEGIN in old and END in old:
        lines.append(old[old.index(BEGIN): old.index(END) + len(END)])
    else:
        lines += [BEGIN, "## Interpretation", "", "(to be written)", END]
    lines += ["", "---", "",
              f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m ww.live.report --out {out.as_posix()}`. "
              f"Data: `{data_dir()}`. CIs: 95% percentile bootstrap resampling whole games ({N_BOOT} replicates, "
              "seed 0). Paired differences use the game indices present in both conditions; game i has the same "
              "seats and roles in every condition.", ""]

    lines += ["## Conditions", "", "| Condition | Source | Games | Setup |", "|---|---|---|---|"]
    for c in conds:
        if c.name.startswith("random"):
            lines.append(f"| {c.label} | `{RANDOM_SOURCE}` (indices 0..{max(r.index for r in c.rows)}) | {len(c.rows)} | "
                         "Wave 2 generator: uniformly random day votes (no judge) |")
        else:
            lines.append(f"| {c.label} | `{source_of(c.name)}` | {len(c.rows)} | {CONDITIONS[c.name].note} |")
    lines += ["", "All conditions: 9 players (2 wolves, 1 Seer), llama3.1:8b for every player, generic werewolf "
              "persona, temperature 0.8, random night kills, roles revealed on death. Judge conditions: the judge's "
              "top suspect gives the day's defense; with the defense on, the judge re-scores after it; the final top "
              "suspect is voted out unless abstention applies (then nobody is, and the wolves still kill at night).",
              ""]

    png = out.with_name(out.stem + "_outcomes.png")
    plot([c for c in conds], png)
    lines += ["## Outcomes", "", f"![Villager win rate and days per game]({png.name})", ""]
    head = "| Condition | Games | " + " | ".join(m[0] for m in METRICS) + " | Jev calls / game | Jev cost / game |"
    lines += [head, "|" + "---|" * (len(METRICS) + 4)]
    for c in conds:
        cells = [fmt(ci(c.rows, m[2]), pct=m[3]) for m in METRICS]
        calls = np.mean([r.judge_calls for r in c.rows]) if c.rows else math.nan
        cost = np.mean([r.cost for r in c.rows]) if c.rows else math.nan
        judge = None if c.name.startswith("random") else CONDITIONS[c.name].judge
        paid = judge is not None and judge.startswith("jev")
        lines.append(f"| {c.label} | {len(c.rows)} | " + " | ".join(cells)
                     + (f" | {calls:.1f} | ${cost:.5f} |" if paid else " | - | - |"))
    lines += ["", "Rates marked with % pool days (or games) across the condition; 'Top suspect is a wolf' "
              "counts the judge's final top suspect each day, whether or not it abstained. "
              "Chance for a uniformly random top suspect on day 1 is 2/9 = 22%.", ""]

    lines += ["## Paired comparisons (X minus Y, same game indices)", "",
              "| X | Y | Metric | X - Y | 95% CI | p | Games | Verdict |", "|---|---|---|---|---|---|---|---|"]
    pairs: list[tuple[str, str]] = []
    jev_like = [c.name for c in conds if not c.name.startswith("random")]
    for n in jev_like:
        pairs.append((n, "random"))
    for n in jev_like:
        if n != "jev" and "jev" in by:
            pairs.append((n, "jev"))
    for x, y in pairs:
        if x not in by or y not in by:
            continue
        for label, attr, stat, pct in METRICS[:3]:
            d, lo, hi, p, n = paired(by[x], by[y], stat)
            s = 100 if pct else 1
            unit = " pp" if pct else ""
            ptxt = "n/a" if math.isnan(p) else ("<0.001" if p < 0.001 else f"{p:.3f}")
            lines.append(f"| {x} | {y} | {label} | {s * d:+.2f}{unit} | [{s * lo:+.2f}, {s * hi:+.2f}] | {ptxt} | {n} | "
                         f"{verdict(lo, hi)} |")
    lines.append("")

    # thresholds
    s = summarize(top_suspect_rows("jev_batched-0650211bf044", RANDOM_SOURCE))
    lines += ["## How the abstention thresholds were chosen", "",
              f"From the {s['n_views']} offline jev_batched views of `{RANDOM_SOURCE}` (Wave 3; judge "
              "`jev_batched-0650211bf044`, the same judge used live): the confidence Jev reports for its top "
              f"suspect has 25th/50th/75th percentiles {s['percentiles'][25]:.2f} / {s['percentiles'][50]:.2f} / "
              f"{s['percentiles'][75]:.2f}. These are the three thresholds (abstain when the top suspect's "
              "confidence is below them), i.e. abstain on roughly 25%, 50% and 75% of days if live views look "
              "like offline ones. Offline, the top suspect is a wolf this often per confidence quartile "
              "(`python -m ww.live.thresholds`):", "",
              "| Confidence quartile | Range | Views | Top suspect is a wolf | Chance (wolves / speakers) |",
              "|---|---|---|---|---|"]
    for b in s["quartiles"]:
        lines.append(f"| {b['bin']} | [{b['lo']:.2f}, {b['hi']:.2f}] | {b['n']} | {100 * b['top_is_wolf']:.1f}% | "
                     f"{100 * b['chance']:.1f}% |")
    lines += ["", f"Overall: {100 * s['top_is_wolf']:.1f}% vs chance {100 * s['chance']:.1f}%. The coverage curves in "
              "core.md rank player-rounds for a yes/no call at p >= 0.5; the vote only needs the top suspect, "
              "so this table is the relevant version of that curve.", ""]
    text = "\n".join(lines) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Live games: a judge decides the vote\n\n" + text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conditions", default="llama,jev,jev_t028,jev_t042,jev_nodef")
    ap.add_argument("--random-n", type=int, default=None, help="random games to pair (default: max live games)")
    ap.add_argument("--out", default="reports/live.md")
    a = ap.parse_args(argv)
    build([s for s in a.conditions.split(",") if s], Path(a.out), a.random_n)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
