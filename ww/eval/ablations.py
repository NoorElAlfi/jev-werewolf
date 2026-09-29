"""Jev ablations on the generated games (Wave 4 G).

Cells: jev_batched (with the same 90k-char view window as Waves 3 E/F, a no-op
on these games, so the generic cell reuses F's cached judgments) under three
configs, on two transcript sources:

    G  generic criteria, defense included   (F's jev_batched-0650211bf044)
    L  leaky criteria,   defense included
    N  generic criteria, defense excluded

    llm_llama31_8b        200 games, generic wolf persona (no tactic scripted)
    llm_llama31_8b_leaky   60 games, leaky persona ("deflect suspicion onto
                           someone else"); same seeds as g0000-g0059 of the
                           generic set, so roles match and games pair by id

Criteria-leak 2x2: {G, L} x {generic, leaky source}. Defense: {G, N} on both.

    python -m ww.eval.ablations plan
    python -m ww.eval.ablations run --limit 2          # trial (under the gate)
    python -m ww.eval.ablations run --yes              # full, after approval
    python -m ww.eval.ablations report --out reports/ablations.md

Every comparison is a paired bootstrap that resamples whole games (the same
draws for every cell in a comparison).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ww.config import CRITERIA_VARIANTS, WEREWOLF_PERSONAS
from ww.eval import metrics as M
from ww.judges.base import Judge, key_of, load_judgments, run_judge
from ww.judges.jev import JevBatchedJudge
from ww.judges.run import COST_GATE_CALLS, plan
from ww.judges.window import WindowedJudge
from ww.eval.data import load_source

WINDOW = 90_000
SRC_GEN = "llm_llama31_8b"
SRC_LEAKY = "llm_llama31_8b_leaky"
SOURCES = (SRC_GEN, SRC_LEAKY)

CELLS: dict[str, dict[str, Any]] = {
    "G": {"criteria_variant": "generic", "include_defense": True, "desc": "generic criteria, defense in"},
    "L": {"criteria_variant": "leaky", "include_defense": True, "desc": "leaky criteria, defense in"},
    "N": {"criteria_variant": "generic", "include_defense": False, "desc": "generic criteria, defense out"},
}

# Words the leaky persona and the leaky criteria share (the thing being tested).
LEAK_PATTERNS = {
    "deflect": r"\bdeflect\w*",
    "blame": r"\bblam\w*",
    "onto someone/me/him/her/them": r"\bonto (?:someone|me|him|her|them)\b",
}


def cell_judge(cell: str) -> Judge:
    c = CELLS[cell]
    return WindowedJudge(JevBatchedJudge(criteria_variant=c["criteria_variant"], include_defense=c["include_defense"]),
                         WINDOW)


# ---------------------------------------------------------------------------
# Plan / run
# ---------------------------------------------------------------------------


def plan_all(limit: int | None = None) -> list[dict[str, Any]]:
    out = []
    for cell in CELLS:
        j = cell_judge(cell)
        for s in SOURCES:
            p = plan(j, s, limit)
            out.append({"cell": cell, "source": s, "judge_key": key_of(j), **{f"total_{k}": v for k, v in p["total"].items()},
                        **{f"new_{k}": v for k, v in p["new"].items()}})
    return out


def _print_plan(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        print(f"{r['cell']} {r['source']:22s} {r['judge_key']}: {r['total_views']} views; "
              f"not cached {r['new_calls']} calls, est. ${r['new_est_cost_usd']:.4f}")
    print(f"not cached, all cells: {sum(r['new_calls'] for r in rows)} calls, "
          f"est. ${sum(r['new_est_cost_usd'] for r in rows):.4f}", flush=True)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    cell: str
    source: str
    key: str
    t: M.GameTable  # rows: (game, round, player): round, first, y, p, conf, defended
    n_views: int
    cost: float


def build_cell(cell: str, source: str, transcripts: dict[str, Any] | None = None) -> Cell:
    """Player-round table for one cell. Game ids are the bare game_id, so
    the two sources pair by seed. `defended` = the player gave a defense in
    this round or earlier; `first` = this is the game's first judged round."""
    key = key_of(cell_judge(cell))
    ts = transcripts if transcripts is not None else load_source(source)
    js = load_judgments(key, source)
    g, rnd, first, y, p, conf, dfd = [], [], [], [], [], [], []
    n_views, cost = 0, 0.0
    for gid, j in js.items():
        t = ts.get(gid)
        if t is None:
            continue
        team = t.team_of()
        defended_by: dict[int, set[str]] = {}
        seen: set[str] = set()
        for r in sorted(t.rounds, key=lambda r: r.round):
            if r.defense:
                seen.add(r.defense.player_id)
            defended_by[r.round] = set(seen)
        rounds = sorted(j["rounds"].items(), key=lambda kv: int(kv[0]))
        for i, (rk, res) in enumerate(rounds):
            rn = int(rk)
            n_views += 1
            cost += float((res.get("meta") or {}).get("cost_usd") or 0.0)
            for pid, pv in res["p_deceptive"].items():
                if pid not in team:
                    continue
                g.append(gid); rnd.append(rn); first.append(i == 0)
                y.append(int(team[pid] == "deceptive")); p.append(float(pv))
                conf.append(float((res.get("confidence") or {}).get(pid, abs(pv - 0.5))))
                dfd.append(pid in defended_by.get(rn, set()))
    t = M.GameTable(g, round=rnd, first=first, y=y, p=p, conf=conf, defended=dfd)
    return Cell(cell, source, key, t, n_views, cost)


def auc_first(t: M.GameTable) -> float:
    m = t.first.astype(bool)
    return M.auc(t.y[m], t.p[m]) if m.any() else math.nan


def auc_defended(t: M.GameTable) -> float:
    m = t.defended.astype(bool)
    return M.auc(t.y[m], t.p[m]) if m.any() else math.nan


def mean_p_gap(t: M.GameTable) -> float:
    """mean p(wolves) - mean p(villagers)."""
    y = t.y.astype(bool)
    return float(t.p[y].mean() - t.p[~y].mean()) if y.any() and (~y).any() else math.nan


STATS: dict[str, M.Stat] = {"auc": M.auc_stat, "auc_r1": auc_first, "auc_defended": auc_defended,
                            "ece": M.ece_stat, "gap": mean_p_gap}


def joint_bootstrap(tables: dict[str, M.GameTable], f: Callable[[dict[str, M.GameTable]], float],
                    n_boot: int = 2000, seed: int = 0, alpha: float = 0.05) -> M.PairedDiff:
    """f(tables) with a CI from resampling the games common to every table
    (the same draw for all tables). Returns a PairedDiff (diff = estimate)."""
    common = sorted(set.intersection(*(set(t.games) for t in tables.values())))
    sub = {k: t.only_games(common) for k, t in tables.items()}
    est = f(sub) if common else math.nan
    if len(common) < 2 or math.isnan(est):
        return M.PairedDiff(est, math.nan, math.nan, math.nan, len(common))
    rng = np.random.default_rng(seed)
    vals = np.empty(n_boot)
    for b in range(n_boot):
        draw = [common[i] for i in rng.integers(0, len(common), len(common))]
        vals[b] = f({k: t.take_games(draw) for k, t in sub.items()})
    v = vals[~np.isnan(vals)]
    lo, hi = (float(np.percentile(v, 100 * alpha / 2)), float(np.percentile(v, 100 * (1 - alpha / 2)))) if len(v) > 1 \
        else (math.nan, math.nan)
    pval = float(min(1.0, 2 * min((v <= 0).mean(), (v >= 0).mean()))) if len(v) else math.nan
    return M.PairedDiff(float(est), lo, hi, pval, len(common))


# ---------------------------------------------------------------------------
# Lexical check (free): how often the leak words appear, by team
# ---------------------------------------------------------------------------


def leak_word_rates(source: str, transcripts: dict[str, Any] | None = None) -> dict[str, dict[str, float]]:
    ts = transcripts if transcripts is not None else load_source(source)
    counts = {"deceptive": [0, 0], "honest": [0, 0]}  # [statements with any leak word, statements]
    per_word = {team: {w: 0 for w in LEAK_PATTERNS} for team in counts}
    for t in ts.values():
        team = t.team_of()
        for r in t.rounds:
            texts = [(s.player_id, s.text) for s in r.statements]
            if r.defense:
                texts.append((r.defense.player_id, r.defense.text))
            for pid, text in texts:
                tm = team[pid]
                counts[tm][1] += 1
                hit = False
                for w, pat in LEAK_PATTERNS.items():
                    if re.search(pat, text, re.IGNORECASE):
                        per_word[tm][w] += 1
                        hit = True
                counts[tm][0] += hit
    return {tm: {"n_statements": n, "share_any": (k / n if n else math.nan),
                 **{f"share_{w}": (per_word[tm][w] / n if n else math.nan) for w in LEAK_PATTERNS}}
            for tm, (k, n) in counts.items()}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _d(x: M.PairedDiff, digits: int = 3) -> str:
    if math.isnan(x.diff):
        return "n/a"
    if math.isnan(x.lo):
        return f"{x.diff:+.{digits}f}"
    verdict = "**conclusive**" if x.conclusive else "CI includes 0"
    return (f"{x.diff:+.{digits}f} [{x.lo:+.{digits}f}, {x.hi:+.{digits}f}], p = {x.p_value:.3f}, "
            f"{x.n_games} games; {verdict}")


def compute(n_boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    ts = {s: load_source(s) for s in SOURCES}
    cells = {(c, s): build_cell(c, s, ts[s]) for c in CELLS for s in SOURCES}
    out: dict[str, Any] = {"cells": {}, "cmp": {}, "missing": [], "n_boot": n_boot,
                           "games_in_source": {s: len(ts[s]) for s in SOURCES}}
    for (c, s), cell in cells.items():
        if not len(cell.t):
            out["missing"].append((c, s))
            continue
        n_games_judged = len(cell.t.games)
        out["cells"][(c, s)] = {"key": cell.key, "games": n_games_judged, "views": cell.n_views, "cost": cell.cost,
                                "rows": len(cell.t),
                                **{k: v for k, v in M.bootstrap_cis(cell.t, STATS, n_boot, seed).items()}}

    def have(*cs: tuple[str, str]) -> bool:
        return all(len(cells[c].t) for c in cs)

    def diff(name: str, a: tuple[str, str], b: tuple[str, str], stat: M.Stat = M.auc_stat,
             games: list[str] | None = None) -> None:
        if not have(a, b):
            return
        ta, tb = cells[a].t, cells[b].t
        if games is not None:
            ta, tb = ta.only_games(games), tb.only_games(games)
        out["cmp"][name] = joint_bootstrap({"a": ta, "b": tb}, lambda d: stat(d["a"]) - stat(d["b"]), n_boot, seed)

    gs, ls = SRC_GEN, SRC_LEAKY
    paired60 = sorted(ts[ls])  # generic-source games with a leaky twin
    # criteria leak
    diff("L-G on generic source", ("L", gs), ("G", gs))
    diff("L-G on generic source, paired games only", ("L", gs), ("G", gs), games=paired60)
    diff("L-G on leaky source", ("L", ls), ("G", ls))
    diff("persona: G leaky src - G generic src", ("G", ls), ("G", gs))
    diff("persona: L leaky src - L generic src", ("L", ls), ("L", gs))
    diff("both leaky - both generic: L/leaky src - G/generic src", ("L", ls), ("G", gs))
    if have(("L", ls), ("G", ls), ("L", gs), ("G", gs)):
        tabs = {"Ll": cells[("L", ls)].t, "Gl": cells[("G", ls)].t, "Lg": cells[("L", gs)].t, "Gg": cells[("G", gs)].t}
        out["cmp"]["interaction: (L-G on leaky src) - (L-G on generic src)"] = joint_bootstrap(
            tabs, lambda d: (M.auc_stat(d["Ll"]) - M.auc_stat(d["Gl"])) - (M.auc_stat(d["Lg"]) - M.auc_stat(d["Gg"])),
            n_boot, seed)
    # the same comparisons on round 1 only
    diff("R1: L-G on generic source", ("L", gs), ("G", gs), auc_first)
    diff("R1: L-G on leaky source", ("L", ls), ("G", ls), auc_first)
    # defense
    diff("N-G on generic source", ("N", gs), ("G", gs))
    diff("N-G on leaky source", ("N", ls), ("G", ls))
    diff("defenders only: N-G on generic source", ("N", gs), ("G", gs), auc_defended)
    diff("defenders only: N-G on leaky source", ("N", ls), ("G", ls), auc_defended)
    diff("ECE: N-G on generic source", ("N", gs), ("G", gs), M.ece_stat)
    diff("ECE: L-G on generic source", ("L", gs), ("G", gs), M.ece_stat)
    out["lex"] = {s: leak_word_rates(s, ts[s]) for s in SOURCES}
    return out


def render(res: dict[str, Any]) -> str:
    L: list[str] = []
    a = L.append
    a("# Ablations: criteria leak and defense (Wave 4 G)")
    a("")
    a("<!-- INTERPRETATION -->")
    a("")
    a("## Setup")
    a("")
    a(f"Judge: jev_batched (one Jev call per view, whole public view as state), with the 90k-char view window "
      f"used in Waves 3 E/F (a no-op on these games). Cells:")
    a("")
    for c, d in CELLS.items():
        a(f"- **{c}**: {d['desc']}")
    a("")
    a("Criteria (low to high suspicion):")
    a("")
    for v, crit in CRITERIA_VARIANTS.items():
        a(f"- {v}: " + " / ".join(f"\"{x}\"" for x in crit))
    a("")
    a("Wolf personas:")
    a("")
    for v, txt in WEREWOLF_PERSONAS.items():
        a(f"- {v}: \"{txt}\"")
    a("")
    a(f"Sources: `{SRC_GEN}` ({res['games_in_source'][SRC_GEN]} games, generic persona) and `{SRC_LEAKY}` "
      f"({res['games_in_source'][SRC_LEAKY]} games, leaky persona, same seeds as the first 60 generic games, so "
      "roles match and the games pair by id). Metrics are over player-rounds (every scored player in every judged "
      f"round). CIs: 95% bootstrap over whole games, {res['n_boot']} draws; comparisons are paired (same draws for "
      "every cell). \"Conclusive\" means the paired CI excludes 0.")
    a("")
    if res["missing"]:
        a("**Missing cells (not yet run):** " + ", ".join(f"{c} on `{s}`" for c, s in res["missing"]))
        a("")
    a("## Every cell")
    a("")
    a("| Cell | Source | judge_key | Games | Views | AUC | Round-1 AUC | AUC, defenders only | ECE | mean p wolves − villagers | Cost |")
    a("|---|---|---|---|---|---|---|---|---|---|---|")
    for (c, s), r in sorted(res["cells"].items(), key=lambda kv: (kv[0][1], kv[0][0])):
        a(f"| {c} | `{s}` | `{r['key']}` | {r['games']} | {r['views']} | {r['auc'].fmt()} | {r['auc_r1'].fmt()} | "
          f"{r['auc_defended'].fmt()} | {r['ece'].fmt()} | {r['gap'].fmt()} | ${r['cost']:.4f} |")
    a("")
    a("## Criteria-leak 2×2 (AUC)")
    a("")
    grid = res["cells"]
    a("| Criteria \\ transcripts | generic persona | leaky persona |")
    a("|---|---|---|")
    for c, lab in (("G", "generic criteria"), ("L", "leaky criteria")):
        cells = [grid[(c, s)]["auc"].fmt() if (c, s) in grid else "not run" for s in SOURCES]
        a(f"| {lab} | {cells[0]} | {cells[1]} |")
    a("")
    a("Paired comparisons (AUC difference):")
    a("")
    for name, d in res["cmp"].items():
        if name.startswith(("L-G", "persona", "both", "interaction", "R1", "ECE: L")):
            a(f"- {name}: {_d(d)}")
    a("")
    a("### Do the leak words actually appear? (free lexical check)")
    a("")
    a("Share of statements and defenses containing each word, by team (from the transcripts):")
    a("")
    words = list(LEAK_PATTERNS)
    a("| Source | Team | Statements | any | " + " | ".join(words) + " |")
    a("|---|---|---|---|" + "---|" * len(words))
    for s, by in res["lex"].items():
        for tm, r in by.items():
            a(f"| `{s}` | {tm} | {r['n_statements']} | {r['share_any']:.3f} | "
              + " | ".join(f"{r['share_' + w]:.3f}" for w in words) + " |")
    a("")
    a("## Defense ablation (AUC)")
    a("")
    a("| Defense \\ transcripts | generic persona | leaky persona |")
    a("|---|---|---|")
    for c, lab in (("G", "included"), ("N", "excluded")):
        cells = [grid[(c, s)]["auc"].fmt() if (c, s) in grid else "not run" for s in SOURCES]
        a(f"| {lab} | {cells[0]} | {cells[1]} |")
    a("")
    a("Paired comparisons:")
    a("")
    for name, d in res["cmp"].items():
        if name.startswith(("N-G", "defenders", "ECE: N")):
            a(f"- {name}: {_d(d)}")
    a("")
    a("\"Defenders only\" restricts to player-rounds of players who gave a defense in that round or earlier, "
      "the only players whose own text changes between G and N (the others lose context only).")
    a("")
    a("## Reproduce")
    a("")
    a("```")
    a("python -m ww.eval.ablations plan")
    a("python -m ww.eval.ablations run --yes")
    a("python -m ww.eval.ablations report --out reports/ablations.md")
    a("```")
    a("")
    return "\n".join(L)


def write_report(out: Path, interpretation: str | None = None, n_boot: int = 2000) -> str:
    md = render(compute(n_boot=n_boot))
    if interpretation is None and out.exists():  # keep a hand-written interpretation across regenerations
        old = out.read_text(encoding="utf-8")
        m = re.search(r"<!-- INTERPRETATION -->\n(.*?)\n<!-- /INTERPRETATION -->", old, re.DOTALL)
        interpretation = m.group(1) if m else None
    block = f"<!-- INTERPRETATION -->\n{interpretation or '(interpretation not written yet)'}\n<!-- /INTERPRETATION -->"
    md = md.replace("<!-- INTERPRETATION -->", block, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--limit", type=int, default=None)
    p_run = sub.add_parser("run")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--cells", default=",".join(CELLS))
    p_run.add_argument("--yes", action="store_true")
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--out", default="reports/ablations.md")
    p_rep.add_argument("--n-boot", type=int, default=2000)
    a = ap.parse_args(argv)

    if a.cmd == "plan":
        _print_plan(plan_all(a.limit))
        return 0
    if a.cmd == "run":
        rows = [r for r in plan_all(a.limit) if r["cell"] in a.cells.split(",")]
        _print_plan(rows)
        new = sum(r["new_calls"] for r in rows)
        if new > COST_GATE_CALLS and not a.yes:
            print(f"STOP: {new} new paid calls > {COST_GATE_CALLS}. Get approval, then re-run with --yes.",
                  file=sys.stderr)
            return 2
        for r in rows:
            if r["new_calls"]:
                counts = run_judge(cell_judge(r["cell"]), r["source"], limit=a.limit)
                print(f"{r['cell']} {r['source']}: {counts}", flush=True)
        return 0
    write_report(Path(a.out), n_boot=a.n_boot)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
