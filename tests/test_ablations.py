"""Ablation runner and report, offline (FakeTypeSafe from conftest)."""

from __future__ import annotations

import math
import shutil

import numpy as np
import pytest

from tests.conftest import VALID_FIXTURES
from tests.fakes import FakeTypeSafe
from ww.eval import ablations as A
from ww.eval import metrics as M
from ww.judges.base import key_of


@pytest.fixture
def two_sources(data_dir):
    for s in A.SOURCES:
        d = data_dir / "transcripts" / s
        d.mkdir(parents=True)
        for p in VALID_FIXTURES:
            shutil.copy(p, d / p.name)
    return A.SOURCES


def test_generic_cell_reuses_wave3_key():
    """Cell G must hit the judgments F already paid for."""
    assert key_of(A.cell_judge("G")) == "jev_batched-0650211bf044"
    keys = {key_of(A.cell_judge(c)) for c in A.CELLS}
    assert len(keys) == len(A.CELLS)


def test_cells_differ_only_in_what_they_should():
    g, l, n = (A.cell_judge(c).config for c in "GLN")
    assert g["criteria"] != l["criteria"] and g["include_defense"] == l["include_defense"]
    assert g["criteria"] == n["criteria"] and g["include_defense"] and not n["include_defense"]


def test_run_gate_then_full_run_and_report(two_sources, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(A, "COST_GATE_CALLS", 1)
    assert A.main(["run"]) == 2
    assert FakeTypeSafe.calls == []
    assert A.main(["run", "--yes"]) == 0
    n_calls = len(FakeTypeSafe.calls)
    assert n_calls > 0
    assert A.main(["run", "--yes"]) == 0  # everything cached now
    assert len(FakeTypeSafe.calls) == n_calls
    # the no-defense cell never sends a defense
    no_def = [c for c in FakeTypeSafe.calls if all("defense" not in r for r in c["state"]["rounds"])]
    assert no_def

    out = tmp_path / "abl.md"
    A.write_report(out, n_boot=50)
    md = out.read_text(encoding="utf-8")
    assert "## Criteria-leak 2×2" in md and "## Defense ablation" in md
    assert "not run" not in md
    assert "(interpretation not written yet)" in md
    # a hand-written interpretation survives regeneration
    out.write_text(md.replace("(interpretation not written yet)", "MY NOTES"), encoding="utf-8")
    A.write_report(out, n_boot=50)
    assert "MY NOTES" in out.read_text(encoding="utf-8")


def test_missing_cells_are_reported(two_sources, tmp_path):
    out = tmp_path / "abl.md"
    A.write_report(out, n_boot=20)
    md = out.read_text(encoding="utf-8")
    assert "Missing cells" in md and "not run" in md


def test_joint_bootstrap_pairs_games():
    g = ["a", "a", "b", "b", "c", "c", "d", "d"]
    y = [1, 0, 1, 0, 1, 0, 1, 0]
    good = M.GameTable(g, y=y, p=[0.9, 0.1] * 4)
    bad = M.GameTable(g, y=y, p=[0.1, 0.9] * 4)
    d = A.joint_bootstrap({"x": good, "y": bad}, lambda t: M.auc_stat(t["x"]) - M.auc_stat(t["y"]), n_boot=100)
    assert d.diff == pytest.approx(1.0) and d.lo == pytest.approx(1.0) and d.conclusive
    same = A.joint_bootstrap({"x": good, "y": good}, lambda t: M.auc_stat(t["x"]) - M.auc_stat(t["y"]), n_boot=50)
    assert same.diff == 0 and not same.conclusive
    # only common games are used
    part = good.only_games(["a", "b"])
    d2 = A.joint_bootstrap({"x": good, "y": part}, lambda t: len(t["x"].games) - len(t["y"].games), n_boot=10)
    assert d2.n_games == 2 and d2.diff == 0


def test_stats_on_small_table():
    t = M.GameTable(["a"] * 4, round=[1, 1, 2, 2], first=[True, True, False, False], y=[1, 0, 1, 0],
                    p=[0.8, 0.2, 0.3, 0.6], conf=[0.5] * 4, defended=[False, False, True, True])
    assert A.auc_first(t) == 1.0
    assert A.auc_defended(t) == 0.0
    assert A.mean_p_gap(t) == pytest.approx((0.8 + 0.3) / 2 - (0.2 + 0.6) / 2)
    empty = t.filter(np.zeros(4, dtype=bool))
    assert math.isnan(A.auc_first(empty))


def test_leak_word_rates(two_sources):
    r = A.leak_word_rates(A.SRC_GEN)
    assert set(r) == {"deceptive", "honest"}
    for team in r.values():
        assert team["n_statements"] > 0 and 0 <= team["share_any"] <= 1
