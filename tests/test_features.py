"""Wave 4 H: ww/eval/features.py (offline, synthetic data and fake judges)."""

import numpy as np

from ww.eval import features as F
from ww.eval.metrics import auc
from ww.judges.base import key_of, run_judge
from ww.judges.jev import JevBatchedJudge
from ww.judges.jev_multidim import JevMultidimJudge
from ww.judges.jev_noul import JevNoulJudge
from ww.judges.jev_pairwise import JevPairwiseJudge


def synthetic(n_games=60, per_game=12, seed=0, flip=False):
    rng = np.random.default_rng(seed)
    game = np.repeat([f"g{i}" for i in range(n_games)], per_game)
    y = (rng.random(len(game)) < 0.25).astype(int)
    signal = y * (-1.5 if flip else 1.5) + rng.normal(size=len(y))
    noise = rng.normal(size=len(y))
    return game, y, np.c_[signal, noise]


def test_cv_oof_finds_planted_signal_and_keeps_games_apart():
    game, y, X = synthetic()
    oof = F.cv_oof(X, y, game)  # asserts internally that no game is in train and test
    assert not np.isnan(oof).any()
    assert auc(y, oof) > 0.75
    # a pure-noise feature is near chance out of fold
    assert abs(auc(y, F.cv_oof(X[:, [1]], y, game)) - 0.5) < 0.1


def test_coefficients_point_the_right_way():
    game, y, X = synthetic()
    c = F.fit_coefs(X, y, game, ["signal", "noise"], n_boot=50)
    assert c["signal"].lo > 0
    assert c["noise"].lo <= 0 <= c["noise"].hi
    game, y, X = synthetic(flip=True)
    assert F.fit_coefs(X, y, game, ["signal", "noise"], n_boot=20)["signal"].hi < 0


def _rows(game, y, X, source):
    r = F.Rows()
    for g, yy, x in zip(game, y, X):
        r.game.append(f"{source}/{g}"); r.source.append(source); r.round.append(1); r.player.append("P")
        r.y.append(int(yy))
        f = {n: float(x[0]) for n in F.MD_FEATURES}
        f.update({n: float(x[1]) for n in F.PW_FEATURES})
        f["noul"] = float(x[1]); f["batched"] = float(x[1])
        r.feats.append(f)
    return r


def test_analyse_runs_and_transfer_flags_flipped_signal():
    a = _rows(*synthetic(seed=1), "srcA")
    b = _rows(*synthetic(seed=2, flip=True), "srcB")
    md = "\n".join(F.analyse({"A": a, "B": b}, n_boot=50, n_boot_coef=20))
    assert "Held-out AUC" in md and "Transfer" in md and "Coefficients" in md
    # the flipped domain: transfer from A lands below 0.5
    line = [ln for ln in md.splitlines() if ln.startswith("| multidim | A | B |")][0]
    assert float(line.split("|")[4].split()[0]) < 0.5


def test_build_rows_from_fake_judgments(fixture_source):
    keys = {}
    for name, j in [("multidim", JevMultidimJudge()), ("noul", JevNoulJudge()), ("pairwise", JevPairwiseJudge()),
                    ("batched", JevBatchedJudge())]:
        run_judge(j, fixture_source)
        keys[name] = key_of(j)
    rows = F.build_rows([fixture_source], keys)
    assert len(rows) > 0
    X, mask = rows.matrix(F.FEATURE_SETS["all + jev_batched p"])
    # every row of a view with >= 2 speakers has every feature
    assert mask.sum() >= len(rows) - 2
    for f in rows.feats:
        for n in F.MD_FEATURES:
            assert 0.0 <= f[n] <= 1.0
    assert set(rows.y) <= {0, 1}


def test_build_rows_skips_missing_judges(fixture_source):
    j = JevNoulJudge()
    run_judge(j, fixture_source)
    rows = F.build_rows([fixture_source], {"multidim": None, "noul": key_of(j), "pairwise": None, "batched": None})
    assert len(rows) and all(set(f) == {"noul"} for f in rows.feats)
    # a key with no judgment folder contributes nothing
    assert len(F.build_rows([fixture_source], {"multidim": "missing-key", "noul": None, "pairwise": None, "batched": None})) == 0
