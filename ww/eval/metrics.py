"""Evaluation metrics for suspicion judges, with game-level bootstrap CIs.

Everything works on a `GameTable`: parallel numpy columns plus a `game`
column. The bootstrap resamples whole games (all rows of a drawn game come
along together), because rows within a game are not independent.

Conventions
  - y = 1 for a deceptive player, 0 for an honest one.
  - p = the judge's p_deceptive for that player in that round's view.
  - conf = the judge's confidence if it gave one, else |p - 0.5|.
  - "Round 1" = the earliest judged round of each game (the first round with
    statements), whatever its number.

Metric functions take plain arrays so they can be tested by hand; the
`*_stat` helpers adapt them to a GameTable for the bootstrap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np
from scipy.stats import rankdata

# ---------------------------------------------------------------------------
# Game-grouped table
# ---------------------------------------------------------------------------


class GameTable:
    """Column store with a `game` column (any hashable ids)."""

    def __init__(self, game: Sequence[str], **cols: Sequence[Any]):
        self.game = np.asarray(game)
        n = len(self.game)
        self.cols: dict[str, np.ndarray] = {}
        for k, v in cols.items():
            a = np.asarray(v)
            if len(a) != n:
                raise ValueError(f"column {k} has {len(a)} rows, expected {n}")
            self.cols[k] = a
        self._index: dict[str, np.ndarray] | None = None

    def __getattr__(self, name: str) -> np.ndarray:
        cols = self.__dict__.get("cols", {})
        if name in cols:
            return cols[name]
        raise AttributeError(name)

    def __len__(self) -> int:
        return len(self.game)

    @property
    def games(self) -> list[str]:
        """Distinct game ids, in first-seen order."""
        return list(dict.fromkeys(self.game.tolist()))

    def index(self) -> dict[str, np.ndarray]:
        if self._index is None:
            idx: dict[str, list[int]] = {}
            for i, g in enumerate(self.game.tolist()):
                idx.setdefault(g, []).append(i)
            self._index = {g: np.asarray(v, dtype=int) for g, v in idx.items()}
        return self._index

    def filter(self, mask: np.ndarray) -> GameTable:
        return GameTable(self.game[mask], **{k: v[mask] for k, v in self.cols.items()})

    def only_games(self, games: Sequence[str]) -> GameTable:
        keep = set(games)
        return self.filter(np.array([g in keep for g in self.game.tolist()], dtype=bool))

    def take_games(self, games: Sequence[Any]) -> GameTable:
        """Bootstrap replicate: rows of each drawn game, in draw order. The
        replicate's `game` column is the draw position (0..len-1), so a game
        drawn twice counts as two games."""
        idx = self.index()
        parts = [idx[g] for g in games if g in idx]
        if not parts:
            return GameTable([], **{k: v[:0] for k, v in self.cols.items()})
        rows = np.concatenate(parts)
        new_game = np.repeat(np.arange(len(parts)), [len(q) for q in parts])
        return GameTable(new_game, **{k: v[rows] for k, v in self.cols.items()})


def concat(tables: Sequence[GameTable]) -> GameTable:
    """Stack tables with the same columns (game ids must already be distinct)."""
    tables = [t for t in tables if len(t)]
    if not tables:
        return GameTable([])
    keys = list(tables[0].cols)
    return GameTable(np.concatenate([t.game for t in tables]), **{k: np.concatenate([t.cols[k] for t in tables]) for k in keys})


# ---------------------------------------------------------------------------
# Discrimination
# ---------------------------------------------------------------------------


def auc(y: Sequence[int], score: Sequence[float]) -> float:
    """ROC AUC via the Mann-Whitney statistic; ties count half.
    NaN if only one class is present."""
    y = np.asarray(y, dtype=int)
    s = np.asarray(score, dtype=float)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return math.nan
    ranks = rankdata(s)  # average ranks for ties
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def top1_per_game(game: Sequence[Any], rnd: Sequence[int], y: Sequence[int], p: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """For each game, take its earliest judged round and the players scored in
    it. Returns (hit, chance) arrays with one entry per game (sorted by id):
      hit    = share of deceptive players among those tied for the top p
               (expected accuracy with a random tie-break; 1 or 0 without ties)
      chance = n_deceptive / n_players among the scored players.
    """
    game = np.asarray(game)
    if len(game) == 0:
        return np.zeros(0), np.zeros(0)
    rnd = np.asarray(rnd, dtype=int)
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    _, code = np.unique(game, return_inverse=True)
    code = code.ravel()
    n = int(code.max()) + 1
    first = np.full(n, np.iinfo(np.int64).max)
    np.minimum.at(first, code, rnd)
    m = rnd == first[code]
    code, y, p = code[m], y[m], p[m]
    best = np.full(n, -np.inf)
    np.maximum.at(best, code, p)
    top = (p == best[code]).astype(float)
    hit = np.bincount(code, weights=y * top, minlength=n) / np.bincount(code, weights=top, minlength=n)
    chance = np.bincount(code, weights=y, minlength=n) / np.bincount(code, minlength=n)
    return hit, chance


def top1_accuracy(game, rnd, y, p) -> tuple[float, float]:
    """(mean round-1 top-1 accuracy, mean chance rate) over games."""
    hit, chance = top1_per_game(game, rnd, y, p)
    if len(hit) == 0:
        return math.nan, math.nan
    return float(hit.mean()), float(chance.mean())


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def reliability(y: Sequence[int], p: Sequence[float], n_bins: int = 10) -> list[dict[str, float]]:
    """Equal-width bins on [0, 1] (the last bin includes 1.0). One dict per
    non-empty bin: lo, hi, count, mean_p, frac_pos."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    b = np.minimum((p * n_bins).astype(int), n_bins - 1)
    out = []
    for i in range(n_bins):
        m = b == i
        if m.any():
            out.append(
                {"lo": i / n_bins, "hi": (i + 1) / n_bins, "count": int(m.sum()),
                 "mean_p": float(p[m].mean()), "frac_pos": float(y[m].mean())}
            )
    return out


def ece(y: Sequence[int], p: Sequence[float], n_bins: int = 10) -> float:
    """Expected calibration error: sum over bins of (n_bin / N) * |frac_pos - mean_p|."""
    bins = reliability(y, p, n_bins)
    n = sum(b["count"] for b in bins)
    if n == 0:
        return math.nan
    return float(sum(b["count"] / n * abs(b["frac_pos"] - b["mean_p"]) for b in bins))


def brier(y: Sequence[int], p: Sequence[float]) -> float:
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    return float(np.mean((p - y) ** 2)) if len(y) else math.nan


# ---------------------------------------------------------------------------
# Selective prediction (abstention)
# ---------------------------------------------------------------------------


def default_confidence(p: Sequence[float], conf: Sequence[float] | None) -> np.ndarray:
    """The judge's confidence where given (non-NaN), else |p - 0.5|."""
    p = np.asarray(p, dtype=float)
    fallback = np.abs(p - 0.5)
    if conf is None:
        return fallback
    c = np.asarray(conf, dtype=float)
    return np.where(np.isnan(c), fallback, c)


def accuracy_at_coverage(y, p, conf, coverage: float, threshold: float = 0.5) -> float:
    """Accuracy of the call `p >= threshold -> deceptive` on the most confident
    `coverage` share of predictions (k = ceil(coverage * N)); the judge abstains
    on the rest. Ties in confidence at the cut-off are split evenly (expected
    accuracy under a random tie-break)."""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    c = np.asarray(conf, dtype=float)
    n = len(y)
    if n == 0:
        return math.nan
    k = max(1, int(math.ceil(coverage * n - 1e-9)))
    correct = ((p >= threshold).astype(int) == y).astype(float)
    t = np.sort(c)[::-1][k - 1]
    above = c > t
    tied = c == t
    m = k - int(above.sum())
    return float((correct[above].sum() + m * correct[tied].mean()) / k)


def coverage_curve(y, p, conf, coverages: Sequence[float] | None = None, threshold: float = 0.5) -> list[tuple[float, float]]:
    """[(coverage, accuracy)] at each coverage level (default 0.1 .. 1.0)."""
    if coverages is None:
        coverages = np.round(np.linspace(0.1, 1.0, 10), 2)
    return [(float(cv), accuracy_at_coverage(y, p, conf, cv, threshold)) for cv in coverages]


# ---------------------------------------------------------------------------
# Cost and latency
# ---------------------------------------------------------------------------


def cost_latency_summary(game: Sequence[str], latency_s: Sequence[float], cost_usd: Sequence[float]) -> dict[str, float]:
    """One row per judge.score call. NaN costs mean 'not reported'; a judge that
    never reports a cost has cost fields NaN (free judges should report 0)."""
    game = np.asarray(game, dtype=object)
    lat = np.asarray(latency_s, dtype=float)
    cost = np.asarray(cost_usd, dtype=float)
    n_games = len(set(game.tolist()))
    have_cost = ~np.isnan(cost)
    total_cost = float(cost[have_cost].sum()) if have_cost.any() else math.nan
    return {
        "calls": int(len(lat)),
        "games": n_games,
        "latency_mean_s": float(np.nanmean(lat)) if len(lat) else math.nan,
        "latency_median_s": float(np.nanmedian(lat)) if len(lat) else math.nan,
        "latency_p95_s": float(np.nanpercentile(lat, 95)) if len(lat) else math.nan,
        "latency_total_s": float(np.nansum(lat)),
        "cost_total_usd": total_cost,
        "cost_per_call_usd": total_cost / int(have_cost.sum()) if have_cost.any() else math.nan,
        "cost_per_game_usd": total_cost / n_games if have_cost.any() and n_games else math.nan,
        "calls_with_cost": int(have_cost.sum()),
    }


# ---------------------------------------------------------------------------
# GameTable statistics (for the bootstrap)
# ---------------------------------------------------------------------------
# Prediction tables have columns: round, y, p, conf.
# Call tables have columns: latency, cost.

Stat = Callable[[GameTable], float]


def auc_stat(t: GameTable) -> float:
    return auc(t.y, t.p)


def ece_stat(t: GameTable) -> float:
    return ece(t.y, t.p)


def brier_stat(t: GameTable) -> float:
    return brier(t.y, t.p)


def top1_stat(t: GameTable) -> float:
    return top1_accuracy(t.game, t.round, t.y, t.p)[0]


def chance_stat(t: GameTable) -> float:
    return top1_accuracy(t.game, t.round, t.y, t.p)[1]


def top1_minus_chance_stat(t: GameTable) -> float:
    hit, chance = top1_per_game(t.game, t.round, t.y, t.p)
    return float((hit - chance).mean()) if len(hit) else math.nan


def acc_at_coverage_stat(coverage: float) -> Stat:
    def f(t: GameTable) -> float:
        return accuracy_at_coverage(t.y, t.p, t.conf, coverage)
    f.__name__ = f"acc_at_{coverage:g}"
    return f


def latency_mean_stat(t: GameTable) -> float:
    return float(np.nanmean(t.latency)) if len(t) else math.nan


def cost_per_game_stat(t: GameTable) -> float:
    c = np.asarray(t.cost, dtype=float)
    if len(t) == 0 or np.isnan(c).all():
        return math.nan
    return float(np.nansum(c) / len(t.games))


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@dataclass
class CI:
    est: float
    lo: float
    hi: float
    n_games: int

    def fmt(self, digits: int = 3) -> str:
        if math.isnan(self.est):
            return "n/a"
        if math.isnan(self.lo):
            return f"{self.est:.{digits}f}"
        return f"{self.est:.{digits}f} [{self.lo:.{digits}f}, {self.hi:.{digits}f}]"


@dataclass
class PairedDiff:
    diff: float  # stat(X) - stat(Y) on the common games
    lo: float
    hi: float
    p_value: float  # two-sided bootstrap p for diff == 0
    n_games: int

    @property
    def conclusive(self) -> bool:
        return not (math.isnan(self.lo) or self.lo <= 0 <= self.hi)


def _percentile_ci(vals: np.ndarray, alpha: float) -> tuple[float, float]:
    vals = vals[~np.isnan(vals)]
    if len(vals) < 2:
        return math.nan, math.nan
    return float(np.percentile(vals, 100 * alpha / 2)), float(np.percentile(vals, 100 * (1 - alpha / 2)))


def _draws(n_games: int, n_boot: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, n_games, size=(n_boot, n_games))


def bootstrap_cis(
    t: GameTable, stats: dict[str, Stat], n_boot: int = 1000, seed: int = 0, alpha: float = 0.05
) -> dict[str, CI]:
    """Point estimate plus a percentile CI for each stat, from resampling whole
    games with replacement (the same draws for every stat). Replicates where a
    stat is undefined (NaN, e.g. an AUC on a one-class sample) are dropped."""
    games = t.games
    est = {k: (f(t) if len(t) else math.nan) for k, f in stats.items()}
    if len(games) < 2:
        return {k: CI(est[k], math.nan, math.nan, len(games)) for k in stats}
    vals = {k: np.full(n_boot, math.nan) for k in stats}
    for b, draw in enumerate(_draws(len(games), n_boot, seed)):
        rep = t.take_games([games[i] for i in draw])
        for k, f in stats.items():
            if not math.isnan(est[k]):
                vals[k][b] = f(rep)
    out = {}
    for k in stats:
        lo, hi = _percentile_ci(vals[k], alpha) if not math.isnan(est[k]) else (math.nan, math.nan)
        out[k] = CI(float(est[k]), lo, hi, len(games))
    return out


def bootstrap_ci(t: GameTable, stat: Stat, n_boot: int = 1000, seed: int = 0, alpha: float = 0.05) -> CI:
    """bootstrap_cis for a single stat."""
    return bootstrap_cis(t, {"s": stat}, n_boot, seed, alpha)["s"]


def paired_bootstrap(
    x: GameTable, y: GameTable, stat: Stat, n_boot: int = 1000, seed: int = 0, alpha: float = 0.05
) -> PairedDiff:
    """stat(X) - stat(Y), resampling the same games for both judges. Only games
    present in both tables are used. p_value is the two-sided bootstrap
    p-value: 2 * min(share of replicate diffs <= 0, share >= 0)."""
    in_y = set(y.games)
    common = [g for g in x.games if g in in_y]
    x, y = x.only_games(common), y.only_games(common)
    diff = stat(x) - stat(y) if common else math.nan
    if len(common) < 2 or math.isnan(diff):
        return PairedDiff(diff, math.nan, math.nan, math.nan, len(common))
    vals = np.empty(n_boot)
    for b, draw in enumerate(_draws(len(common), n_boot, seed)):
        games = [common[i] for i in draw]
        vals[b] = stat(x.take_games(games)) - stat(y.take_games(games))
    lo, hi = _percentile_ci(vals, alpha)
    v = vals[~np.isnan(vals)]
    p = float(min(1.0, 2 * min((v <= 0).mean(), (v >= 0).mean()))) if len(v) else math.nan
    return PairedDiff(float(diff), lo, hi, p, len(common))
