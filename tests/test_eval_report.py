"""End-to-end report test on synthetic transcripts and synthetic judges.

Three judges are run through the real runner (run_judge) so their cache files
look exactly like real ones:
  - perfect: p = 1 for deceptive players, 0 for honest ones
  - random: p uniform in [0, 1]
  - miscal: ranks players well but is badly overconfident (every p in [0.85, 1])
The report must rank them: AUC perfect > miscal > random; ECE perfect < random < miscal.
"""

import random
import re

import pytest

from ww.eval import metrics as M
from ww.eval.report import build_report, main
from ww.judges.base import run_judge
from ww.transcripts.schema import PlayerInfo, Round, Statement, Transcript, current_speakers, save
from ww.config import transcripts_dir

N_GAMES = 40
SOURCE = "synthetic"


def make_source(source=SOURCE, n_games=N_GAMES):
    labels = {}
    rng = random.Random(0)
    for g in range(n_games):
        ids = [f"G{g}P{i}" for i in range(6)]
        wolves = set(rng.sample(ids, 2))
        players = [PlayerInfo(pid, "werewolf" if pid in wolves else "villager", "deceptive" if pid in wolves else "honest") for pid in ids]
        labels.update({p.id: p.team == "deceptive" for p in players})
        alive = list(ids)
        rounds = []
        n_rounds = 2 + g % 3  # 2..4 rounds, so later rounds have fewer games
        for r in range(1, n_rounds + 1):
            stmts = [Statement(pid, f"I am {pid}, round {r}.") for pid in alive]
            stmts.append(Statement(alive[0], "One more thing."))  # a player with two statements
            events = [] if r == 1 else [f"{rounds[-1].eliminated} was eliminated."]
            rounds.append(Round(round=r, public_events=events, statements=stmts, eliminated=alive[-1]))
            alive = alive[:-1]
        save(Transcript(f"game{g:03d}", source, players, rounds), transcripts_dir(source) / f"game{g:03d}.json")
    return labels


class SynthJudge:
    def __init__(self, name, labels, p_fn, cost=None):
        self.name = name
        self.config = {"kind": name}
        self.labels = labels
        self.p_fn = p_fn
        self.cost = cost

    def score(self, view):
        from ww.judges.base import JudgeResult
        rn = view["rounds"][-1]["round"]
        p = {}
        for pid in current_speakers(view):
            rng = random.Random(f"{self.name}-{pid}-{rn}")
            p[pid] = self.p_fn(self.labels[pid], rng)
        meta = {"latency_s": 0.01}
        if self.cost is not None:
            meta["cost_usd"] = self.cost
        return JudgeResult(p_deceptive=p, meta=meta)


def perfect(dec, rng):
    return 1.0 if dec else 0.0


def rand(dec, rng):
    return rng.random()


def miscal(dec, rng):
    s = (1.0 if dec else 0.0) + rng.gauss(0, 0.5)
    return 0.85 + 0.15 * min(1.0, max(0.0, (s + 1) / 3))


@pytest.fixture
def judged(data_dir):
    labels = make_source()
    for name, fn, cost in (("perfect", perfect, 0.0), ("random", rand, 0.0), ("miscal", miscal, 0.002)):
        run_judge(SynthJudge(name, labels, fn, cost), SOURCE)
    # A judge that only got through part of the source.
    run_judge(SynthJudge("partial", labels, perfect), SOURCE, limit=10)
    return labels


def _table_rows(md, section):
    """Rows of the first markdown table after a heading, as {judge: [cells]}."""
    part = md.split(section, 1)[1]
    rows = {}
    for line in part.splitlines():
        if line.startswith("| ") and not line.startswith("| Judge") and not line.startswith("|---"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[cells[0]] = cells[1:]
        elif rows and not line.startswith("|"):
            break
    return rows


def _est(cell):
    return float(cell.split()[0])


def test_report_ranks_synthetic_judges(judged, tmp_path):
    out = tmp_path / "reports" / "synth.md"
    res = build_report([SOURCE], ["perfect", "random", "miscal"], out, n_boot=200)
    md = out.read_text(encoding="utf-8")

    scope = res["scopes"][0]
    assert scope.ranking["auc"] == ["perfect", "miscal", "random"]
    assert scope.ranking["ece"] == ["perfect", "random", "miscal"]
    assert scope.ranking["top1"][0] == "perfect"
    assert "Ranked by AUC (player-round): perfect > miscal > random" in md
    assert "Ranked by ECE (lower is better): perfect < random < miscal" in md

    rows = _table_rows(md, "### Accuracy and calibration")
    assert set(rows) == {"perfect", "random", "miscal"}
    # columns: Games, AUC, AUC stmt, top1, chance, top1-chance, ECE, Brier, acc50, acc100, conf
    assert rows["perfect"][0] == str(N_GAMES)
    assert _est(rows["perfect"][1]) == 1.0 and _est(rows["perfect"][2]) == 1.0
    assert _est(rows["perfect"][3]) == 1.0 and _est(rows["perfect"][6]) == 0.0
    assert 0.35 < _est(rows["random"][1]) < 0.65
    assert _est(rows["miscal"][1]) > 0.75
    assert _est(rows["miscal"][6]) > 0.5  # overconfident: ECE near 0.9 - 1/3
    assert rows["perfect"][10] == "abs(p-0.5)"
    # chance = 2 deceptive among 6 speakers in round 1
    assert _est(rows["random"][4]) == pytest.approx(1 / 3, abs=1e-3)

    # CIs are present and bracket the random judge's chance-level AUC.
    lo, hi = map(float, re.search(r"\[([\d.]+), ([\d.]+)\]", rows["random"][1]).groups())
    assert lo < 0.5 < hi

    # Paired comparisons: perfect beats random on AUC, conclusively.
    pr = [p for p in scope.paired if p["x"] == "perfect" and p["y"] == "random" and p["key"] == "auc"][0]
    assert pr["diff"].lo > 0 and pr["verdict"] == "perfect better"
    assert "| perfect | random | AUC (player-round) |" in md

    # Cost and latency.
    cost = _table_rows(md, "### Cost and latency")
    assert cost["miscal"][4].startswith("$0.")  # total cost reported
    miscal_scope = [j for j in scope.judges if j.judge.label == "miscal"][0]
    assert miscal_scope.cost["cost_total_usd"] == pytest.approx(0.002 * miscal_scope.cost["calls"])

    # AUC by round has a row per round, with game counts shrinking.
    assert "| 4 |" in md and "(n=13)" in md  # games with g % 3 == 2

    # Plots exist next to the report and are linked.
    for kind in ("reliability", "coverage", "auc_by_round"):
        p = out.parent / f"synth_{SOURCE}_{kind}.png"
        assert p.exists() and p.stat().st_size > 1000
        assert f"({p.name})" in md


def test_report_lists_missing_and_partial(judged, tmp_path):
    out = tmp_path / "r.md"
    res = build_report([SOURCE, "nosuch_source"], ["perfect", "partial", "ghost"], out, n_boot=50, plots=False)
    md = out.read_text(encoding="utf-8")
    assert res["missing_judges"] == ["ghost"]
    assert "Judge `ghost`: no judgment folder" in md
    assert "Source `nosuch_source`: no transcripts" in md
    assert f"`partial` on `{SOURCE}`: 10 of {N_GAMES} games judged" in md
    # the partial judge is still evaluated, on its 10 games
    rows = _table_rows(md, f"## Source: `{SOURCE}`")
    assert rows["partial"][0] == "10"
    # pooled section appears because two sources were requested
    assert "## All sources pooled" in md
    # paired comparison perfect vs partial uses the 10 common games
    scope = res["scopes"][0]
    pr = [p for p in scope.paired if p["key"] == "auc"][0]
    assert pr["diff"].n_games == 10 and pr["diff"].diff == 0


def test_report_with_no_judgments_at_all(data_dir, tmp_path):
    make_source(n_games=3)
    out = tmp_path / "empty.md"
    res = build_report([SOURCE], None, out, n_boot=10)
    md = out.read_text(encoding="utf-8")
    assert res["judges"] == []
    assert "No judgments for this scope." in md


def test_cli(judged, tmp_path, capsys):
    out = tmp_path / "cli.md"
    assert main(["--sources", SOURCE, "--judges", "perfect,random", "--out", str(out), "--n-boot", "20", "--no-plots"]) == 0
    assert out.exists()
    assert "wrote" in capsys.readouterr().out


def test_judge_key_selects_one_config(judged, tmp_path):
    from ww.eval.data import available_judges
    key = [j.key for j in available_judges() if j.name == "random"][0]
    res = build_report([SOURCE], [key], tmp_path / "k.md", n_boot=10, plots=False)
    assert [j.key for j in res["judges"]] == [key]
    assert res["judges"][0].label == "random"


def test_statement_rows_weight_repeat_speakers(judged):
    from ww.eval.data import load_judge_data, load_source, resolve_judges
    refs, _ = resolve_judges(["perfect"])
    d = load_judge_data(refs[0], SOURCE, load_source(SOURCE))
    # each judged round has one more statement than scored players
    assert len(d.statements) == len(d.players) + len(d.calls)
    assert M.auc(d.statements.y, d.statements.p) == 1.0
