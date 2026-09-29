"""Judge comparison report.

    python -m ww.eval.report --sources <s1,s2> --judges <j1,j2> --out reports/<name>.md

Reads transcripts ($WW_DATA_DIR/transcripts/<source>/) and cached judgments
($WW_DATA_DIR/judgments/<judge_key>/<source>/). `--judges` takes judge names
or full judge keys (default: every judge folder). Writes a markdown report and
PNG plots next to it (<name>_<scope>_reliability.png, _coverage.png,
_auc_by_round.png). Missing judgments are skipped and listed in the report.

All CIs are 95% percentile intervals from a bootstrap that resamples whole
games; "X - Y" comparisons use a paired bootstrap over the games both judges
scored.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ww.config import data_dir
from ww.eval import metrics as M
from ww.eval.data import JudgeData, JudgeRef, load_judge_data, load_source, resolve_judges

log = logging.getLogger(__name__)

# Categorical palette (fixed order; a judge keeps its color in every plot).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

COVERAGE_REPORTED = 0.5
MIN_GAMES_PER_ROUND = 5  # AUC-by-round rows with fewer games are shown but not plotted

PLAYER_STATS: dict[str, M.Stat] = {
    "auc": M.auc_stat,
    "top1": M.top1_stat,
    "chance": M.chance_stat,
    "top1_minus_chance": M.top1_minus_chance_stat,
    "ece": M.ece_stat,
    "brier": M.brier_stat,
    "acc_cov50": M.acc_at_coverage_stat(COVERAGE_REPORTED),
    "acc_cov100": M.acc_at_coverage_stat(1.0),
}
CALL_STATS: dict[str, M.Stat] = {"latency": M.latency_mean_stat, "cost_per_game": M.cost_per_game_stat}
# Paired comparisons: (key, label, stat, higher_is_better)
PAIRED = [
    ("auc", "AUC (player-round)", M.auc_stat, True),
    ("top1", "Round-1 top-1", M.top1_stat, True),
    ("ece", "ECE", M.ece_stat, False),
]


@dataclass
class JudgeScope:
    judge: JudgeRef
    data: JudgeData
    cis: dict[str, M.CI] = field(default_factory=dict)
    by_round: dict[int, M.CI] = field(default_factory=dict)
    reliability: list[dict[str, float]] = field(default_factory=list)
    coverage: list[tuple[float, float]] = field(default_factory=list)
    cost: dict[str, float] = field(default_factory=dict)
    conf_source: str = ""


@dataclass
class ScopeResult:
    name: str  # a source name, or "pooled"
    judges: list[JudgeScope]
    paired: list[dict[str, Any]] = field(default_factory=list)
    plots: dict[str, str] = field(default_factory=dict)
    ranking: dict[str, list[str]] = field(default_factory=dict)
    ranking_text: dict[str, str] = field(default_factory=dict)
    majority_acc: float = math.nan


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _merge(datas: list[JudgeData]) -> JudgeData:
    return JudgeData(
        players=M.concat([d.players for d in datas]),
        statements=M.concat([d.statements for d in datas]),
        calls=M.concat([d.calls for d in datas]),
        n_games_total=sum(d.n_games_total for d in datas),
        n_games_judged=sum(d.n_games_judged for d in datas),
        n_games_complete=sum(d.n_games_complete for d in datas),
        notes=[n for d in datas for n in d.notes],
    )


def _rank_text(ordered: list[JudgeScope], key: str, sym: str) -> str:
    """'a > b = c': ties (equal to 3 decimals, as displayed) are joined with '='."""
    out = ""
    for i, j in enumerate(ordered):
        if i:
            same = round(j.cis[key].est, 3) == round(ordered[i - 1].cis[key].est, 3)
            out += " = " if same else f" {sym} "
        out += j.judge.label
    return out or "n/a"


def compute_scope(name: str, items: list[tuple[JudgeRef, JudgeData]], n_boot: int, seed: int) -> ScopeResult:
    judges = []
    for ref, d in items:
        js = JudgeScope(ref, d)
        pl = d.players
        js.cis = M.bootstrap_cis(pl, PLAYER_STATS, n_boot, seed)
        js.cis["auc_statement"] = M.bootstrap_ci(d.statements, M.auc_stat, n_boot, seed)
        js.cis.update(M.bootstrap_cis(d.calls, CALL_STATS, n_boot, seed))
        for r in sorted(set(pl.round.tolist())):
            js.by_round[int(r)] = M.bootstrap_ci(pl.filter(pl.round == r), M.auc_stat, n_boot, seed)
        js.reliability = M.reliability(pl.y, pl.p)
        js.coverage = M.coverage_curve(pl.y, pl.p, pl.conf)
        js.cost = M.cost_latency_summary(d.calls.game, d.calls.latency, d.calls.cost)
        hc = pl.has_conf.astype(bool) if len(pl) else np.zeros(0, bool)
        js.conf_source = "judge" if len(hc) and hc.all() else ("abs(p-0.5)" if not hc.any() else f"judge ({hc.mean():.0%}), else abs(p-0.5)")
        judges.append(js)

    res = ScopeResult(name, judges)
    if judges and len(judges[0].data.players):
        # Accuracy of always answering "honest", on the first judge's rows.
        res.majority_acc = float(1 - judges[0].data.players.y.mean())

    for a, b in itertools.combinations(judges, 2):
        for key, label, stat, higher in PAIRED:
            pd = M.paired_bootstrap(a.data.players, b.data.players, stat, n_boot, seed)
            if pd.conclusive:
                better = a if (pd.diff > 0) == higher else b
                verdict = f"{better.judge.label} better"
            elif math.isnan(pd.lo):
                verdict = "not enough games"
            else:
                verdict = "inconclusive (CI includes 0)"
            res.paired.append({"x": a.judge.label, "y": b.judge.label, "metric": label, "key": key, "diff": pd, "verdict": verdict})

    def rank(key: str, higher: bool) -> list[JudgeScope]:
        ok = [j for j in judges if not math.isnan(j.cis[key].est)]
        return sorted(ok, key=lambda j: j.cis[key].est, reverse=higher)

    for key, higher in (("auc", True), ("top1", True), ("ece", False)):
        ordered = rank(key, higher)
        res.ranking[key] = [j.judge.label for j in ordered]
        res.ranking_text[key] = _rank_text(ordered, key, ">" if higher else "<")
    return res


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------


def _style(ax, xlabel: str, ylabel: str) -> None:
    ax.set_xlabel(xlabel, color=INK2)
    ax.set_ylabel(ylabel, color=INK2)
    ax.tick_params(colors=INK2, labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def make_plots(res: ScopeResult, colors: dict[str, int], out_md: Path) -> dict[str, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    judges = [j for j in res.judges if len(j.data.players)]
    if not judges:
        return {}
    stem = f"{out_md.stem}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', res.name)}"
    paths = {}

    def style_of(j: JudgeScope) -> dict[str, Any]:
        i = colors[j.judge.key] % len(PALETTE)
        return {"color": PALETTE[i], "marker": MARKERS[i], "linewidth": 2, "markersize": 6, "label": j.judge.label}

    # Reliability diagrams: one panel per judge (small multiples), shared axes.
    n = len(judges)
    fig, axes = plt.subplots(1, n, figsize=(3.2 * n, 3.4), squeeze=False, sharey=True)
    for ax, j in zip(axes[0], judges):
        ax.plot([0, 1], [0, 1], color=INK2, linewidth=1, linestyle="--", label="perfect calibration")
        xs = [b["mean_p"] for b in j.reliability]
        ys = [b["frac_pos"] for b in j.reliability]
        ax.plot(xs, ys, clip_on=False, zorder=3, **style_of(j))
        for b in j.reliability:
            ax.annotate(str(b["count"]), (b["mean_p"], b["frac_pos"]), textcoords="offset points", xytext=(4, -10), fontsize=6, color=INK2)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        _style(ax, "mean predicted p_deceptive", "share actually deceptive" if ax is axes[0][0] else "")
        ax.set_title(f"{j.judge.label}  (ECE {j.cis['ece'].est:.3f})", fontsize=9, color=INK)
    fig.suptitle(f"Reliability ({res.name}); numbers = predictions per bin", fontsize=10, color=INK)
    fig.tight_layout()
    p = out_md.parent / f"{stem}_reliability.png"
    fig.savefig(p, dpi=130, facecolor="white"); plt.close(fig)
    paths["reliability"] = p.name

    # Coverage curves.
    fig, ax = plt.subplots(figsize=(5.5, 3.8))
    for j in judges:
        ax.plot([c for c, _ in j.coverage], [a for _, a in j.coverage], clip_on=False, zorder=3, **style_of(j))
    if not math.isnan(res.majority_acc):
        ax.axhline(res.majority_acc, color=INK2, linestyle=":", linewidth=1, label="always 'honest'")
    ax.set_xlim(0, 1.02); ax.set_ylim(0, 1.02)
    _style(ax, "coverage (share of predictions kept, most confident first)", "accuracy (p >= 0.5 = deceptive)")
    ax.set_title(f"Accuracy vs coverage ({res.name})", fontsize=10, color=INK)
    ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    p = out_md.parent / f"{stem}_coverage.png"
    fig.savefig(p, dpi=130, facecolor="white"); plt.close(fig)
    paths["coverage"] = p.name

    # AUC by round with CIs (rounds with enough games only).
    fig, ax = plt.subplots(figsize=(5.5, 3.8))
    k = len(judges)
    for idx, j in enumerate(judges):
        pts = [(r, ci) for r, ci in sorted(j.by_round.items()) if ci.n_games >= MIN_GAMES_PER_ROUND and not math.isnan(ci.est)]
        if not pts:
            continue
        off = (idx - (k - 1) / 2) * 0.08
        xs = [r + off for r, _ in pts]
        ys = [ci.est for _, ci in pts]
        lo = [ci.est - ci.lo if not math.isnan(ci.lo) else 0 for _, ci in pts]
        hi = [ci.hi - ci.est if not math.isnan(ci.hi) else 0 for _, ci in pts]
        st = style_of(j)
        ax.errorbar(xs, ys, yerr=[lo, hi], capsize=3, elinewidth=1, **st)
    ax.axhline(0.5, color=INK2, linestyle=":", linewidth=1, label="chance (0.5)")
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylim(0, 1.02)
    _style(ax, "round", "AUC (player-round)")
    ax.set_title(f"AUC by round, 95% CI ({res.name})", fontsize=10, color=INK)
    ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    p = out_md.parent / f"{stem}_auc_by_round.png"
    fig.savefig(p, dpi=130, facecolor="white"); plt.close(fig)
    paths["auc_by_round"] = p.name
    return paths


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _short(v: Any, n: int = 60) -> str:
    s = str(v).replace("|", "/")
    return s if len(s) <= n else s[: n - 3] + "..."


def _money(x: float) -> str:
    if math.isnan(x):
        return "n/a"
    return f"${x:.4f}" if x < 1 else f"${x:.2f}"


def _ci_money(ci: M.CI) -> str:
    if math.isnan(ci.est):
        return "n/a"
    if math.isnan(ci.lo):
        return _money(ci.est)
    return f"{_money(ci.est)} [{_money(ci.lo)}, {_money(ci.hi)}]"


def scope_markdown(res: ScopeResult, n_boot: int) -> list[str]:
    L: list[str] = []
    judges = [j for j in res.judges if len(j.data.players)]
    title = "All sources pooled" if res.name == "pooled" else f"Source: `{res.name}`"
    L += [f"## {title}", ""]
    if not judges:
        return L + ["No judgments for this scope.", ""]

    L += ["### Accuracy and calibration", ""]
    L += [
        "| Judge | Games | AUC player-round | AUC per statement | Round-1 top-1 | R1 chance | Top-1 minus chance | ECE | Brier | "
        f"Acc @ {COVERAGE_REPORTED:.0%} coverage | Acc @ 100% | Confidence used |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for j in judges:
        c = j.cis
        L.append(
            f"| {j.judge.label} | {c['auc'].n_games} | {c['auc'].fmt()} | {c['auc_statement'].fmt()} | {c['top1'].fmt()} | "
            f"{c['chance'].fmt()} | {c['top1_minus_chance'].fmt()} | {c['ece'].fmt()} | {c['brier'].fmt()} | "
            f"{c['acc_cov50'].fmt()} | {c['acc_cov100'].fmt()} | {j.conf_source} |"
        )
    L += [""]
    L += [
        f"- Ranked by AUC (player-round): {res.ranking_text['auc']}",
        f"- Ranked by round-1 top-1: {res.ranking_text['top1']}",
        f"- Ranked by ECE (lower is better): {res.ranking_text['ece']}",
        "- Rankings order point estimates only; see the paired comparisons below for which gaps are real.",
        f"- Chance: AUC 0.5; round-1 top-1 = mean over games of n_deceptive / n_scored_players (column R1 chance). "
        f"Always answering 'honest' scores accuracy {res.majority_acc:.3f} on these rows.",
        "",
    ]

    L += ["### Cost and latency (per judge.score call = one round's view)", ""]
    L += ["| Judge | Calls | Mean latency s | Median s | p95 s | Total cost | Cost per game |", "|---|---|---|---|---|---|---|"]
    for j in judges:
        s = j.cost
        L.append(
            f"| {j.judge.label} | {s['calls']} | {j.cis['latency'].fmt(2)} | {s['latency_median_s']:.2f} | {s['latency_p95_s']:.2f} | "
            f"{_money(s['cost_total_usd'])} | {_ci_money(j.cis['cost_per_game'])} |"
        )
    L += ["", "n/a cost = the judge did not report `meta.cost_usd`.", ""]

    rounds = sorted({r for j in judges for r in j.by_round})
    if rounds:
        L += ["### AUC by round", "", "| Round | " + " | ".join(j.judge.label for j in judges) + " |", "|---" * (len(judges) + 1) + "|"]
        for r in rounds:
            cells = []
            for j in judges:
                ci = j.by_round.get(r)
                cells.append(f"{ci.fmt()} (n={ci.n_games})" if ci else "")
            L.append(f"| {r} | " + " | ".join(cells) + " |")
        L += ["", f"n = games with that round judged. The plot shows rounds with at least {MIN_GAMES_PER_ROUND} games.", ""]

    if res.paired:
        L += ["### Paired comparisons (X minus Y, same games)", ""]
        L += ["| X | Y | Metric | X - Y | 95% CI | p | Games | Verdict |", "|---|---|---|---|---|---|---|---|"]
        for row in res.paired:
            d: M.PairedDiff = row["diff"]
            ci = "n/a" if math.isnan(d.lo) else f"[{d.lo:+.3f}, {d.hi:+.3f}]"
            pv = "n/a" if math.isnan(d.p_value) else (f"<{2 / n_boot:.3g}" if d.p_value == 0 else f"{d.p_value:.3f}")
            diff = "n/a" if math.isnan(d.diff) else f"{d.diff:+.3f}"
            L.append(f"| {row['x']} | {row['y']} | {row['metric']} | {diff} | {ci} | {pv} | {d.n_games} | {row['verdict']} |")
        L += ["", "p = two-sided paired-bootstrap p-value (its resolution is limited by the number of replicates). "
              "For ECE, lower is better, so a negative X - Y favors X.", ""]

    if res.plots:
        L += ["### Plots", ""]
        for key, label in (("reliability", "Reliability diagrams"), ("coverage", "Accuracy vs coverage"), ("auc_by_round", "AUC by round")):
            if key in res.plots:
                L += [f"![{label}]({res.plots[key]})", ""]
    return L


def build_report(
    sources: list[str],
    judges: list[str] | None,
    out: Path,
    n_boot: int = 1000,
    seed: int = 0,
    plots: bool = True,
    command: str | None = None,
) -> dict[str, Any]:
    """Compute everything, write the markdown and plots, and return the results
    (keys: judges, missing_judges, coverage, scopes)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    refs, missing_judges = resolve_judges(judges)
    colors = {r.key: i for i, r in enumerate(refs)}

    coverage_rows = []  # (judge, source, judged, complete, total)
    missing_lines = [f"Judge `{m}`: no judgment folder under `judgments/`." for m in missing_judges]
    per_source: dict[str, list[tuple[JudgeRef, JudgeData]]] = {}
    for src in sources:
        transcripts = load_source(src)
        if not transcripts:
            missing_lines.append(f"Source `{src}`: no transcripts under `transcripts/{src}/`.")
        per_source[src] = []
        for r in refs:
            d = load_judge_data(r, src, transcripts)
            coverage_rows.append((r.label, src, d.n_games_judged, d.n_games_complete, d.n_games_total))
            if d.n_games_judged == 0:
                if transcripts:
                    missing_lines.append(f"`{r.label}` on `{src}`: no judgments (0 of {d.n_games_total} games); skipped.")
                continue
            if d.n_games_judged < d.n_games_total or d.n_games_complete < d.n_games_judged:
                missing_lines.append(
                    f"`{r.label}` on `{src}`: {d.n_games_judged} of {d.n_games_total} games judged "
                    f"({d.n_games_complete} complete); metrics use the judged rounds only."
                )
            for note in d.notes:
                missing_lines.append(f"`{r.label}` on `{src}`: {note}.")
            per_source[src].append((r, d))

    scopes = [compute_scope(src, items, n_boot, seed) for src, items in per_source.items()]
    if len(sources) > 1:
        pooled: dict[str, list[JudgeData]] = {}
        for src, items in per_source.items():
            for r, d in items:
                pooled.setdefault(r.key, []).append(d)
        scopes.append(compute_scope("pooled", [(r, _merge(pooled[r.key])) for r in refs if r.key in pooled], n_boot, seed))
    if plots:
        for s in scopes:
            s.plots = make_plots(s, colors, out)

    L = [f"# Judge report: {out.stem}", ""]
    L += [
        f"Generated {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} by `{command or 'ww.eval.report'}`.",
        f"Data: `{data_dir()}`. Every number below is computed from the transcript and judgment files listed here.",
        f"CIs: 95% percentile bootstrap resampling whole games ({n_boot} replicates, seed {seed}). "
        "Where two judges' CIs overlap, the table alone does not show a difference; use the paired comparisons.",
        "",
        "Unit of analysis: *player-round* = one judge score for one player who spoke in that round's view (views include all earlier rounds). "
        "*Per statement* weights each score by how many statements the player made that round.",
        "",
        "## Judges and data",
        "",
        "| Judge | Judge key | Config |",
        "|---|---|---|",
    ]
    for r in refs:
        cfg = ", ".join(f"{k}={_short(v)}" for k, v in sorted(r.config.items())) or "(none)"
        L.append(f"| {r.label} | `{r.key}` | {cfg} |")
    L += ["", "| Judge | Source | Games judged | Complete | Transcripts |", "|---|---|---|---|---|"]
    for row in coverage_rows:
        L.append("| " + " | ".join(str(x) for x in row) + " |")
    L += [""]
    if missing_lines:
        L += ["### Missing or partial", ""] + [f"- {m}" for m in missing_lines] + [""]
    for s in scopes:
        L += scope_markdown(s, n_boot)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"judges": refs, "missing_judges": missing_judges, "coverage": coverage_rows, "missing": missing_lines, "scopes": scopes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", required=True, help="comma-separated source folders under transcripts/")
    ap.add_argument("--judges", default="", help="comma-separated judge names or judge keys (default: all)")
    ap.add_argument("--out", required=True, help="markdown path, e.g. reports/core.md")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-plots", action="store_true")
    a = ap.parse_args(argv)
    sources = [s for s in a.sources.split(",") if s]
    judges = [j for j in a.judges.split(",") if j] or None
    cmd = "python -m ww.eval.report " + " ".join(argv if argv is not None else sys.argv[1:])
    res = build_report(sources, judges, Path(a.out), a.n_boot, a.seed, plots=not a.no_plots, command=cmd)
    print(f"wrote {a.out}")
    for m in res["missing"]:
        print("  missing/partial:", m)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
