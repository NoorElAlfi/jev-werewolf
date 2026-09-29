import json

import pytest

from tests.fakes import FakeOllama, FakeTypeSafe
from tests.test_public_view import sentinel_transcript
from ww.clients import use_clients
from ww.judges.base import JudgmentCache, run_judge
from ww.judges.baselines import KeywordJudge, RandomJudge, cue_counts
from ww.judges.jev import JevBatchedJudge, JevIsolatedJudge, criteria_for, score_to_p
from ww.judges.llama import LlamaJudge, parse_ratings
from ww.judges.run import JUDGES, main, make_judge, plan
from ww.transcripts.schema import current_speakers, public_view


def json_responder(model, messages):
    """Replies with a rating for every player id listed in the prompt."""
    prompt = messages[-1]["content"]
    ids = sorted(set(__import__("re").findall(r"Player\d+", prompt)))
    return json.dumps({pid: (i * 3) % 11 for i, pid in enumerate(ids)})


@pytest.fixture
def ollama_json():
    fake = FakeOllama(responder=json_responder)
    with use_clients(ollama=fake):
        yield fake


ALL = sorted(JUDGES)


# --- every judge returns a valid JudgeResult -----------------------------------

@pytest.mark.parametrize("name", ALL)
def test_every_judge_returns_valid_results(name, fixture_source, ollama_json):
    judge = make_judge(name)
    counts = run_judge(judge, fixture_source)
    assert counts["scored"] == 6
    cache = JudgmentCache(judge)
    for game in ("fx_ww6_seer", "fx_ww5_defense", "fx_mafia7"):
        d = cache.load(fixture_source, game)
        assert d["complete"]
        for rn, r in d["rounds"].items():
            from ww.judges.base import JudgeResult

            res = JudgeResult.from_dict(r)
            res.check()
            assert res.p_deceptive, (name, game, rn)
            if res.confidence is not None:
                assert set(res.confidence) == set(res.p_deceptive)
            assert res.meta["cost_usd"] >= 0


@pytest.mark.parametrize("name", ALL)
def test_scores_exactly_the_current_speakers(name, fixture_source, ollama_json):
    from ww.transcripts.schema import iter_source, load
    from ww.config import transcripts_dir

    judge = make_judge(name)
    for f in iter_source(transcripts_dir(fixture_source)):
        t = load(f)
        view = public_view(t, 1)
        assert set(judge.score(view).p_deceptive) == set(current_speakers(view))


# --- the cache is used -------------------------------------------------------------

@pytest.mark.parametrize("name", ["jev_isolated", "jev_batched", "llama_judge"])
def test_cache_prevents_repeat_calls(name, fixture_source, ollama_json):
    judge = make_judge(name)
    run_judge(judge, fixture_source)
    n_ts, n_ol = len(FakeTypeSafe.calls), len(ollama_json.calls)
    assert n_ts + n_ol > 0
    assert run_judge(make_judge(name), fixture_source) == {"games": 3, "scored": 0, "cached": 6}
    assert (len(FakeTypeSafe.calls), len(ollama_json.calls)) == (n_ts, n_ol)


def test_config_changes_the_key():
    assert JudgmentCache(JevBatchedJudge()).key != JudgmentCache(JevBatchedJudge(criteria_variant="leaky")).key
    assert JudgmentCache(JevBatchedJudge()).key != JudgmentCache(JevBatchedJudge(include_defense=False)).key
    assert JudgmentCache(JevBatchedJudge()).key != JudgmentCache(JevIsolatedJudge()).key
    # workers is not part of the config
    assert JudgmentCache(JevIsolatedJudge(workers=1)).key == JudgmentCache(JevIsolatedJudge(workers=8)).key


# --- judges only see public_view() ---------------------------------------------------

@pytest.mark.parametrize("name", ALL)
def test_nothing_private_reaches_the_models(name, data_dir, ollama_json):
    from ww.transcripts.schema import save

    t = sentinel_transcript()
    save(t, data_dir / "transcripts" / "sentinel" / f"{t.game_id}.json")
    judge = make_judge(name)
    run_judge(judge, "sentinel")
    sent = json.dumps([m["content"] for c in ollama_json.calls for m in c["messages"]])
    sent += json.dumps([{"state": c["state"], "q": {k: q.model_dump() for k, q in c["questions"].items()}}
                        for c in FakeTypeSafe.calls])
    # every private field of the sentinel transcript holds a *SENTINEL* token
    for token in ("SENTINEL", '"role"', '"team"', '"votes"', '"night"', '"eliminated"'):
        assert token not in sent, (name, token)
    # the judge got real content
    if name in ("jev_isolated", "jev_batched"):
        assert "hello" in sent or "sad" in sent
    if name == "llama_judge":
        assert "hello" in sent


def test_run_judge_passes_only_views(fixture_source):
    """A wrapper judge records what score() receives."""
    seen = []
    inner = JevBatchedJudge()

    class Spy:
        name, config = inner.name, inner.config

        def score(self, view):
            seen.append(view)
            return inner.score(view)

    run_judge(Spy(), fixture_source)
    assert seen and all(set(v) == {"players", "rounds"} for v in seen)


def test_isolated_sees_only_own_statements(fixture_source):
    from ww.config import transcripts_dir
    from ww.transcripts.schema import load

    t = load(transcripts_dir(fixture_source) / "fx_ww5_defense.json")
    view = public_view(t, 2)
    judge = JevIsolatedJudge()
    for state, questions, qmap in judge.requests(view):
        pid = qmap["q0"]
        own = {s["text"] for r in view["rounds"] for s in r["statements"] if s["player_id"] == pid}
        others = {s["text"] for r in view["rounds"] for s in r["statements"] if s["player_id"] != pid} - own
        blob = json.dumps(state)
        assert all(json.dumps(x)[1:-1] in blob for x in own)
        assert not any(json.dumps(x)[1:-1] in blob for x in others)
        assert set(state) <= {"player", "statements", "defense"}


def test_batched_defense_flag(fixture_source):
    from ww.config import transcripts_dir
    from ww.transcripts.schema import load

    t = load(transcripts_dir(fixture_source) / "fx_ww5_defense.json")
    view = public_view(t, 1)
    [(state_on, q_on, _)] = JevBatchedJudge().requests(view)
    [(state_off, q_off, _)] = JevBatchedJudge(include_defense=False).requests(view)
    assert any("defense" in r for r in state_on["rounds"])
    assert not any("defense" in r for r in state_off["rounds"])
    assert len(q_on) == len(current_speakers(view))


# --- Score -> p mapping, raw, confidence ------------------------------------------------

def test_jev_mapping_and_raw():
    probs = [0.2, 0.3, 0.5]

    def prob_fn(name, q, state):
        return probs

    view = {"players": ["A", "B"], "rounds": [{"round": 1, "public_events": [],
            "statements": [{"player_id": "A", "text": "hi"}, {"player_id": "B", "text": "yo"}]}]}
    for cls in (JevIsolatedJudge, JevBatchedJudge):
        with use_clients(typesafe=lambda: FakeTypeSafe(prob_fn=prob_fn)):
            r = cls().score(view)
        expected = (0 * 0.2 + 1 * 0.3 + 2 * 0.5) / 2
        assert r.p_deceptive["A"] == pytest.approx(expected)
        assert r.meta["raw"]["answers"]["A"]["probabilities"] == {"0": 0.2, "1": 0.3, "2": 0.5}
        assert 0 < r.confidence["A"] < 1
        assert r.meta["cost_usd"] > 0 and r.meta["tokens"]["input"] > 0
    assert score_to_p(2.0, 3) == 1.0 and score_to_p(0.0, 3) == 0.0


def test_call_counts():
    view = {"players": ["A", "B", "C"], "rounds": [{"round": 1, "public_events": [],
            "statements": [{"player_id": p, "text": "x"} for p in "ABC"]}]}
    FakeTypeSafe.reset()
    JevIsolatedJudge().score(view)
    assert len(FakeTypeSafe.calls) == 3
    FakeTypeSafe.reset()
    JevBatchedJudge().score(view)
    assert len(FakeTypeSafe.calls) == 1 and len(FakeTypeSafe.calls[0]["questions"]) == 3
    assert FakeTypeSafe.calls[0]["model"] == JevBatchedJudge().config["model"]


def test_criteria_variants():
    assert "deflecting" not in " ".join(criteria_for("generic"))
    assert "deflecting" in " ".join(criteria_for("leaky"))
    with pytest.raises(ValueError):
        criteria_for("nope")


# --- llama parsing ------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"Player1": 7, "Player2": 2}', {"Player1": 7, "Player2": 2}),
    ('Sure!\n```json\n{"player1": "8/10", "Player 2": 3}\n```', {"Player1": 8, "Player2": 3}),
    ('{"ratings": {"Player1": {"score": 4}, "Player2": 11}}', {"Player1": 4, "Player2": 10}),
    ('[{"player": "Player1", "rating": 6}, {"player": "Player2", "rating": 1}]', {"Player1": 6, "Player2": 1}),
    ("Player1: 5\nPlayer2 - 9", {"Player1": 5, "Player2": 9}),
    ("Player10: 9, Player1: 2", {"Player1": 2}),
    ("no idea", {}),
])
def test_parse_ratings(text, expected):
    assert parse_ratings(text, ["Player1", "Player2"]) == expected


def test_llama_records_parse_failures_and_retries():
    replies = iter(["I cannot decide.", '{"A": 9}'])
    fake = FakeOllama(responder=lambda m, msgs: next(replies))
    view = {"players": ["A", "B"], "rounds": [{"round": 1, "public_events": [],
            "statements": [{"player_id": "A", "text": "hi"}, {"player_id": "B", "text": "yo"}]}]}
    with use_clients(ollama=fake):
        r = LlamaJudge(max_retries=1).score(view)
    assert len(fake.calls) == 2
    assert r.p_deceptive == {"A": 0.9, "B": 0.5}
    assert r.meta["raw"]["parse_failures"] == ["B"] and r.meta["parse_ok"] is False
    assert fake.calls[0]["format"] == "json" and fake.calls[0]["options"]["temperature"] == 0.0


# --- baselines -----------------------------------------------------------------------

def test_random_is_deterministic():
    view = {"players": ["A"], "rounds": [{"round": 1, "public_events": [], "statements": [{"player_id": "A", "text": "x"}]}]}
    assert RandomJudge().score(view).p_deceptive == RandomJudge().score(view).p_deceptive
    assert RandomJudge(seed=1).score(view).p_deceptive != RandomJudge().score(view).p_deceptive


def test_keyword_orders_by_cues():
    assert cue_counts("Honestly, maybe I think Player2 is suspicious. Trust me.") == \
        {"hedge": 2, "accusation": 1, "defensive": 2}
    view = {"players": ["A", "B"], "rounds": [{"round": 1, "public_events": [], "statements": [
        {"player_id": "A", "text": "I was home all night and saw Player3 at the well."},
        {"player_id": "B", "text": "Honestly, trust me, maybe Player1 is lying, I swear."}]}]}
    p = KeywordJudge().score(view).p_deceptive
    assert p["A"] == 0.0 and p["B"] > 0.5


# --- CLI ------------------------------------------------------------------------------

def test_plan_counts(fixture_source):
    p = plan(JevBatchedJudge(), fixture_source)
    assert p["total"]["views"] == 6 and p["total"]["calls"] == 6
    iso = plan(JevIsolatedJudge(), fixture_source)
    assert iso["total"]["calls"] > 6 and iso["total"]["est_cost_usd"] > 0
    assert plan(RandomJudge(), fixture_source)["total"]["calls"] == 0


def test_cli_runs_and_prints_plan(fixture_source, capsys):
    assert main(["--judge", "jev_batched", "--source", fixture_source, "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "planned:" in out and "est. $" in out and "done:" in out
    # second run: nothing new
    main(["--judge", "jev_batched", "--source", fixture_source, "--limit", "1"])
    assert "not yet cached: 0 views" in capsys.readouterr().out


def test_cli_cost_gate(fixture_source, monkeypatch):
    import ww.judges.run as run_mod

    monkeypatch.setattr(run_mod, "COST_GATE_CALLS", 2)
    n = len(FakeTypeSafe.calls)
    assert main(["--judge", "jev_isolated", "--source", fixture_source]) == 2
    assert len(FakeTypeSafe.calls) == n  # nothing was called
    assert main(["--judge", "jev_isolated", "--source", fixture_source, "--yes"]) == 0


def test_cli_llama_takes_gpu_lock(fixture_source, ollama_json, monkeypatch):
    import ww.judges.run as run_mod

    taken = []
    from contextlib import contextmanager

    @contextmanager
    def fake_lock(timeout=-1):
        taken.append(timeout)
        yield

    monkeypatch.setattr(run_mod, "gpu_lock", fake_lock)
    assert main(["--judge", "llama_judge", "--source", fixture_source, "--lock-timeout", "5"]) == 0
    assert taken == [5.0]
    main(["--judge", "keyword", "--source", fixture_source])
    assert taken == [5.0]
