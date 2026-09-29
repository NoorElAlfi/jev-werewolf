"""Wave 4 H: jev_multidim, jev_noul, jev_pairwise (offline, with FakeTypeSafe)."""

import json

import pytest

from tests.fakes import FakeTypeSafe
from tests.test_public_view import sentinel_transcript
from ww.clients import use_clients
from ww.config import WEREWOLF_PERSONAS, transcripts_dir
from ww.judges.base import JudgeResult, JudgmentCache, run_judge
from ww.judges.jev_multidim import DIMENSIONS, JevMultidimJudge, chunk_questions, main as multidim_main, wrap
from ww.judges.jev_noul import JevNoulJudge
from ww.judges.jev_pairwise import JevPairwiseJudge, pairwise_features, round_features
from ww.transcripts.schema import current_speakers, iter_source, load, public_view

JUDGES = [JevMultidimJudge, JevNoulJudge, JevPairwiseJudge]
GAMES = ("fx_ww6_seer", "fx_ww5_defense", "fx_mafia7")


@pytest.mark.parametrize("cls", JUDGES, ids=lambda c: c.name)
def test_valid_results_for_current_speakers(cls, fixture_source):
    judge = cls()
    counts = run_judge(judge, fixture_source)
    assert counts["scored"] == 6
    cache = JudgmentCache(judge)
    for f in iter_source(transcripts_dir(fixture_source)):
        t = load(f)
        d = cache.load(fixture_source, t.game_id)
        assert d["complete"]
        for rn, r in d["rounds"].items():
            res = JudgeResult.from_dict(r)
            res.check(current_speakers(public_view(t, int(rn))))
            assert set(res.p_deceptive) == set(current_speakers(public_view(t, int(rn))))
            assert res.meta["cost_usd"] >= 0 and res.meta["n_calls"] == 1
    # one request per view; the cache prevents repeats
    n = len(FakeTypeSafe.calls)
    assert n == 6
    run_judge(judge, fixture_source)
    assert len(FakeTypeSafe.calls) == n


def test_question_counts_per_view():
    t = load(sorted((__import__("pathlib").Path(__file__).parent / "fixtures" / "transcripts").glob("fx_ww6*"))[0])
    view = public_view(t, 1)
    n = len(current_speakers(view))
    assert len(JevMultidimJudge().questions(view)) == n * len(DIMENSIONS)
    assert len(JevNoulJudge().questions(view)) == n
    qs = JevPairwiseJudge().questions(view)
    assert len(qs) == n * (n - 1)
    assert all(x != y for _, _, (x, y) in qs)


@pytest.mark.parametrize("cls", JUDGES, ids=lambda c: c.name)
def test_only_public_view_reaches_jev(cls):
    t = sentinel_transcript()
    for rn in (1, 2):
        cls().score(public_view(t, rn))
    blob = json.dumps([c["state"] for c in FakeTypeSafe.calls])
    for tok in ("SENTINEL", "deceptive", "honest"):
        assert tok not in blob


def test_dimensions_are_tactic_neutral():
    """No dimension restates a persona instruction: none of the personas' tactic
    phrases appear in any instruction or criterion."""
    banned = ["deflect", "blend in", "sound cooperative", "avoid detection", "suspicion onto", "werewolf",
              "never admit"]
    for text in WEREWOLF_PERSONAS.values():
        assert any(b in text.lower() for b in banned)  # the list really targets persona wording
    for name, d in DIMENSIONS.items():
        words = (d["instructions"] + " " + " ".join(d["criteria"])).lower()
        for b in banned:
            assert b not in words, (name, b)


def test_multidim_levels_and_prior_composite():
    # every Score answer is certain of its top level -> level 1.0 for every dimension
    def top(name, q, state):
        return [0.0] * (len(q.criteria) - 1) + [1.0]

    t = sentinel_transcript()
    with use_clients(typesafe=lambda: FakeTypeSafe(prob_fn=top)):
        r = JevMultidimJudge().score(public_view(t, 1))
    ans = r.meta["raw"]["answers"]["A"]
    assert set(ans) == set(DIMENSIONS)
    assert all(a["level"] == pytest.approx(1.0) for a in ans.values())
    # prior composite: evasiveness 1, 1 - specificity 0, self_inconsistency 1, event_contradiction 1
    assert r.p_deceptive["A"] == pytest.approx(0.75)


def test_noul_p_is_the_noul():
    t = sentinel_transcript()
    with use_clients(typesafe=lambda: FakeTypeSafe(prob_fn=lambda n, q, s: 0.3)):
        r = JevNoulJudge().score(public_view(t, 1))
    assert r.p_deceptive == {"A": pytest.approx(0.3), "B": pytest.approx(0.3), "C": pytest.approx(0.3)}
    assert r.confidence is None


def test_chunking_splits_and_resends_state():
    t = sentinel_transcript()
    view = public_view(t, 1)
    qs = JevMultidimJudge().questions(view)
    chunks = chunk_questions(qs, max_n=4)
    assert [len(c) for c in chunks] == [4, 4, 4, 3]
    assert [q for c in chunks for q in c] == qs


def test_cli_gate_and_plan(fixture_source, capsys):
    assert multidim_main(["--source", fixture_source, "--plan-only"]) == 0
    out = capsys.readouterr().out
    assert "6 views, 6 calls" in out
    assert FakeTypeSafe.calls == []


# --- pairwise: no future information ------------------------------------------

def _texts_after(t, rn):
    out = []
    for r in t.rounds:
        if r.round > rn:
            out += [s.text for s in r.statements] + ([r.defense.text] if r.defense else []) + list(r.public_events)
    return [x for x in out if len(x) > 15]


def test_pairwise_calls_see_no_later_rounds(fixture_source):
    judge = wrap(JevPairwiseJudge(), 90_000)
    for f in iter_source(transcripts_dir(fixture_source)):
        t = load(f)
        for rn in [r.round for r in t.rounds if r.statements]:
            FakeTypeSafe.reset()
            judge.score(public_view(t, rn))
            blob = json.dumps(FakeTypeSafe.calls[0]["state"], ensure_ascii=False)
            assert max(r["round"] for r in FakeTypeSafe.calls[0]["state"]["rounds"]) == rn
            for later in _texts_after(t, rn):
                assert later not in blob, (t.game_id, rn)
            # only living (= current) speakers are asked about
            qs = FakeTypeSafe.calls[0]["questions"]
            living = set(current_speakers(public_view(t, rn)))
            for q in qs.values():
                named = {p for p in public_view(t, rn)["players"] if f"{p} " in q.instructions + " "}
                assert named <= living


def _m(players, v):
    return {x: {y: v for y in players if y != x} for x in players}


def test_pairwise_features_ignore_future_rounds():
    m1 = {"A": {"B": 0.9, "C": 0.1}, "B": {"A": 0.8, "C": 0.2}, "C": {"A": 0.1, "B": 0.1}}
    m2 = _m(["A", "B"], 0.5)
    susp = {1: {"A": 0.2, "B": 0.7, "C": 0.4}, 2: {"A": 0.3, "B": 0.3}}
    base = pairwise_features({1: m1, 2: m2}, 1, susp)
    # changing or removing anything from round 2 onwards leaves round-1 features unchanged
    for later_m, later_s in [({1: m1}, {1: susp[1]}),
                             ({1: m1, 2: _m(["A", "B"], 0.99), 3: _m(["A"], 1.0)},
                              {1: susp[1], 2: {"A": 1.0, "B": 1.0}, 3: {"A": 1.0}})]:
        assert pairwise_features(later_m, 1, later_s) == base
    # round-1 features are the round's own features, and _cum equals them at round 1
    rf = round_features(m1, susp[1])
    for x in "ABC":
        for k, v in rf[x].items():
            assert base[x][k] == pytest.approx(v)
            assert base[x][k + "_cum"] == pytest.approx(v)
    assert base["A"]["pw_mutual_max"] == pytest.approx(0.8)
    assert base["A"]["pw_protects_suspect"] == pytest.approx((0.9 * 0.7 + 0.1 * 0.4) / 1.0)
    # round 2 uses rounds 1 and 2; a change in round 1 does move it (sanity)
    r2 = pairwise_features({1: m1, 2: m2}, 2, susp)
    assert r2["A"]["pw_out_mean_cum"] == pytest.approx((0.5 + 0.5) / 2)
    assert set(r2) == {"A", "B"}
    assert pairwise_features({2: m2}, 1) == {}


def test_pairwise_single_speaker_view_makes_no_call():
    t = sentinel_transcript()
    r = JevPairwiseJudge().score(public_view(t, 2))  # only A speaks in round 2
    assert FakeTypeSafe.calls == []
    assert r.p_deceptive == {"A": 0.0}
    r.check()
