"""Hand-computed checks for every metric in ww.eval.metrics."""

import math

import numpy as np
import pytest

from ww.eval import metrics as M


# --- AUC ---------------------------------------------------------------------

def test_auc_perfect_and_inverted():
    assert M.auc([1, 0, 1, 0], [0.9, 0.1, 0.8, 0.3]) == 1.0
    assert M.auc([1, 0, 1, 0], [0.1, 0.9, 0.3, 0.8]) == 0.0


def test_auc_hand_computed():
    # positives .4, .9 vs negatives .5, .1: .4>.5 no, .4>.1 yes, .9>.5 yes, .9>.1 yes -> 3/4
    assert M.auc([1, 1, 0, 0], [0.4, 0.9, 0.5, 0.1]) == pytest.approx(0.75)


def test_auc_ties_count_half():
    assert M.auc([1, 0], [0.5, 0.5]) == 0.5
    # pos .5 vs negs .5 (tie, 0.5) and .2 (win, 1) -> 1.5/2
    assert M.auc([1, 0, 0], [0.5, 0.5, 0.2]) == pytest.approx(0.75)


def test_auc_one_class_is_nan():
    assert math.isnan(M.auc([1, 1], [0.2, 0.3]))
    assert math.isnan(M.auc([], []))


def test_auc_matches_sklearn():
    from sklearn.metrics import roc_auc_score
    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, 200)
    s = np.round(rng.random(200), 1)  # many ties
    assert M.auc(y, s) == pytest.approx(roc_auc_score(y, s))


# --- round-1 top-1 --------------------------------------------------------------

def test_top1_hits_and_chance():
    game = ["a"] * 3 + ["b"] * 4
    rnd = [1] * 7
    y = [1, 0, 0, 1, 0, 1, 0]
    p = [0.9, 0.5, 0.2, 0.3, 0.8, 0.1, 0.2]  # a: top is deceptive; b: top (.8) is honest
    hit, chance = M.top1_per_game(game, rnd, y, p)
    assert hit.tolist() == [1.0, 0.0]
    assert chance.tolist() == pytest.approx([1 / 3, 2 / 4])
    acc, ch = M.top1_accuracy(game, rnd, y, p)
    assert acc == 0.5 and ch == pytest.approx((1 / 3 + 1 / 2) / 2)


def test_top1_ties_split_credit():
    hit, _ = M.top1_per_game(["c"] * 3, [1] * 3, [1, 0, 0], [0.7, 0.7, 0.1])
    assert hit.tolist() == [0.5]


def test_top1_uses_earliest_judged_round():
    # Round 2 is the first judged round; round 3 would give the opposite answer.
    game = ["g"] * 4
    rnd = [2, 2, 3, 3]
    y = [1, 0, 1, 0]
    p = [0.9, 0.1, 0.1, 0.9]
    hit, chance = M.top1_per_game(game, rnd, y, p)
    assert hit.tolist() == [1.0] and chance.tolist() == [0.5]


# --- calibration ------------------------------------------------------------------

def test_ece_hand_computed():
    # each prediction in its own bin: (.1 + .3 + .3 + .1) / 4
    assert M.ece([0, 0, 1, 1], [0.1, 0.3, 0.7, 0.9]) == pytest.approx(0.2)
    # one bin: frac_pos .25, mean_p .15
    assert M.ece([1, 0, 0, 0], [0.15] * 4) == pytest.approx(0.1)
    assert M.ece([0, 1], [0.0, 1.0]) == 0.0


def test_reliability_bins():
    bins = M.reliability([1, 0, 0, 1, 1], [0.05, 0.05, 0.15, 1.0, 0.95])
    assert [(b["lo"], b["count"]) for b in bins] == [(0.0, 2), (0.1, 1), (0.9, 2)]  # 1.0 falls in the last bin
    assert bins[0]["frac_pos"] == 0.5 and bins[0]["mean_p"] == pytest.approx(0.05)
    assert bins[2]["mean_p"] == pytest.approx(0.975) and bins[2]["frac_pos"] == 1.0


def test_brier():
    assert M.brier([1, 0], [0.8, 0.4]) == pytest.approx((0.04 + 0.16) / 2)


# --- coverage / abstention --------------------------------------------------------------

Y = [1, 0, 1, 0]
P = [0.9, 0.4, 0.3, 0.6]  # correct, correct, wrong, wrong at threshold 0.5
C = [0.4, 0.1, 0.2, 0.1]


def test_accuracy_at_coverage_hand_computed():
    assert M.accuracy_at_coverage(Y, P, C, 0.25) == 1.0  # keeps conf .4 (correct)
    assert M.accuracy_at_coverage(Y, P, C, 0.5) == 0.5  # + conf .2 (wrong)
    # k=3: two above the cut plus one of the two tied at .1 (one correct, one wrong) -> 1.5/3
    assert M.accuracy_at_coverage(Y, P, C, 0.75) == pytest.approx(0.5)
    assert M.accuracy_at_coverage(Y, P, C, 1.0) == 0.5


def test_default_confidence_falls_back_to_distance_from_half():
    assert M.default_confidence(P, None).tolist() == pytest.approx(C)
    got = M.default_confidence(P, [0.9, float("nan"), 0.1, float("nan")])
    assert got.tolist() == pytest.approx([0.9, 0.1, 0.1, 0.1])


def test_coverage_curve():
    curve = M.coverage_curve(Y, P, M.default_confidence(P, None), coverages=[0.25, 0.5, 1.0])
    assert curve == [(0.25, 1.0), (0.5, 0.5), (1.0, 0.5)]


# --- cost / latency ---------------------------------------------------------------------

def test_cost_latency_summary():
    s = M.cost_latency_summary(["a", "a", "b"], [1.0, 2.0, 3.0], [0.01, 0.01, float("nan")])
    assert s["calls"] == 3 and s["games"] == 2
    assert s["latency_mean_s"] == 2.0 and s["latency_median_s"] == 2.0 and s["latency_total_s"] == 6.0
    assert s["cost_total_usd"] == pytest.approx(0.02)
    assert s["cost_per_call_usd"] == pytest.approx(0.01)
    assert s["cost_per_game_usd"] == pytest.approx(0.01)
    assert s["calls_with_cost"] == 2


def test_cost_not_reported_is_nan():
    s = M.cost_latency_summary(["a"], [1.0], [float("nan")])
    assert math.isnan(s["cost_total_usd"]) and math.isnan(s["cost_per_game_usd"])


# --- bootstrap ----------------------------------------------------------------------------

def _table(games, **cols):
    return M.GameTable(games, **cols)


def test_bootstrap_resamples_whole_games():
    # game a has 1 row, game b has 3; a replicate of 2 games has 2, 4 or 6 rows, never odd.
    t = _table(["a", "b", "b", "b"], x=[1, 1, 1, 1])
    seen = set()
    games = t.games
    for draw in M._draws(len(games), 200, 0):
        seen.add(len(t.take_games([games[i] for i in draw])))
    assert seen == {2, 4, 6}


def test_take_games_counts_repeats_as_separate_games():
    t = _table(["a", "a", "b", "b"], round=[1] * 4, y=[1, 0, 0, 1], p=[0.9, 0.1, 0.9, 0.1])
    rep = t.take_games(["a", "a"])
    assert len(rep.games) == 2 and len(rep) == 4
    hit, _ = M.top1_per_game(rep.game, rep.round, rep.y, rep.p)
    assert hit.tolist() == [1.0, 1.0]


def test_bootstrap_ci_brackets_estimate_and_is_deterministic():
    rng = np.random.default_rng(0)
    games = [f"g{i}" for i in range(30) for _ in range(6)]
    y = np.tile([1, 1, 0, 0, 0, 0], 30)
    p = np.clip(y * 0.3 + rng.random(len(y)) * 0.7, 0, 1)
    t = _table(games, y=y, p=p)
    a = M.bootstrap_ci(t, M.auc_stat, n_boot=300, seed=3)
    b = M.bootstrap_ci(t, M.auc_stat, n_boot=300, seed=3)
    assert a == b
    assert a.lo <= a.est <= a.hi and a.n_games == 30
    assert 0.5 < a.est < 1.0


def test_bootstrap_single_game_has_no_ci():
    ci = M.bootstrap_ci(_table(["a", "a"], y=[1, 0], p=[0.8, 0.2]), M.auc_stat)
    assert ci.est == 1.0 and math.isnan(ci.lo) and ci.fmt() == "1.000"


def test_bootstrap_cis_drops_undefined_replicates():
    # Games with only one class each: many replicates have one class -> NaN AUC, dropped.
    t = _table(["a", "b", "c", "d"], y=[1, 0, 1, 0], p=[0.9, 0.1, 0.8, 0.2])
    ci = M.bootstrap_cis(t, {"auc": M.auc_stat}, n_boot=200)["auc"]
    assert ci.est == 1.0 and ci.lo == 1.0 and ci.hi == 1.0


def _judged(n_games, p_fn, seed=0):
    rng = np.random.default_rng(seed)
    games, rounds, ys, ps = [], [], [], []
    for g in range(n_games):
        y = [1, 1, 0, 0, 0, 0]
        for yi in y:
            games.append(f"g{g}"); rounds.append(1); ys.append(yi); ps.append(p_fn(yi, rng))
    return _table(games, round=rounds, y=ys, p=ps, conf=np.abs(np.array(ps) - 0.5))


def test_paired_bootstrap_identical_judges():
    t = _judged(20, lambda y, r: r.random())
    d = M.paired_bootstrap(t, t, M.auc_stat, n_boot=200)
    assert d.diff == 0 and d.lo == 0 and d.hi == 0 and not d.conclusive and d.p_value == 1.0


def test_paired_bootstrap_detects_real_gap():
    good = _judged(30, lambda y, r: 0.6 * y + 0.4 * r.random())
    rand = _judged(30, lambda y, r: r.random(), seed=1)
    d = M.paired_bootstrap(good, rand, M.auc_stat, n_boot=300)
    assert d.diff > 0.3 and d.conclusive and d.lo > 0 and d.p_value < 0.05 and d.n_games == 30
    rev = M.paired_bootstrap(rand, good, M.auc_stat, n_boot=300)
    assert rev.diff == pytest.approx(-d.diff) and rev.hi < 0


def test_paired_bootstrap_uses_common_games_only():
    a = _judged(10, lambda y, r: r.random())
    b = a.only_games([f"g{i}" for i in range(6)])
    d = M.paired_bootstrap(a, b, M.auc_stat, n_boot=50)
    assert d.n_games == 6 and d.diff == 0


def test_concat_and_filter():
    a = _table(["a"], x=[1])
    b = _table(["b", "b"], x=[2, 3])
    c = M.concat([a, b])
    assert c.games == ["a", "b"] and c.x.tolist() == [1, 2, 3]
    assert c.filter(c.x > 1).games == ["b"]
