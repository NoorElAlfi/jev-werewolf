"""Live games (ww.live) on the fake clients."""

import json

import pytest

from tests.fakes import FakeOllama, FakeTypeSafe
from ww.clients import use_clients
from ww.config import transcripts_dir
from ww.judges.base import JudgmentCache
from ww.live import run as live
from ww.live.policy import ordinal
from ww.transcripts.generate import game_seed
from ww.transcripts.schema import iter_source, load, public_view


def quiet(*a, **k):
    pass


def play(name, index=0):
    t, policy, judge = live.play_one(name, index, retry_delays=(), judge_retry_delays=())
    live.write_game(t, policy, judge)
    return t, policy, judge


def day_rounds(t):
    return [r for r in t.rounds if r.statements]


def test_jev_game_validates_and_records_decisions(fake_clients):
    t, policy, judge = play("jev")
    t = load(transcripts_dir("live_jev") / "g0000.json")  # validates
    assert t.winner in {"villagers", "werewolves"}
    days = day_rounds(t)
    assert policy.n_score_calls == 2 * len(days)  # before and after the defense
    assert len(FakeTypeSafe.calls) == 2 * len(days)
    for r in days:
        d = t.meta["decisions"][str(r.round)]
        assert r.defense is not None and d["accused"] == r.defense.player_id
        assert r.eliminated == d["top"] and not d["abstained"]
    assert t.meta["game_seed"] == game_seed(0, 0) and t.meta["condition"] == "jev"


def test_live_judgments_match_the_offline_runner_view(fake_clients):
    t, policy, judge = play("jev")
    cache = JudgmentCache(judge)
    assert cache.is_complete("live_jev", "g0000")
    assert t.meta["judge_key"] == cache.key == "jev_batched-0650211bf044"
    stored = cache.load("live_jev", "g0000")
    assert sorted(int(k) for k in stored["rounds"]) == [r.round for r in day_rounds(t)]
    # the post-defense call (every second call) saw exactly public_view(t, round)
    post = FakeTypeSafe.calls[1::2]
    for call, r in zip(post, day_rounds(t)):
        assert call["state"] == public_view(t, r.round)


def test_same_roles_as_generator_game(fake_clients):
    from ww.transcripts.generate import generate

    generate(2, "generic", "llm_ref", seed=0, retry_delays=(), log=quiet)
    t, _, _ = play("jev", 1)
    ref = load(transcripts_dir("llm_ref") / "g0001.json")
    assert [(p.id, p.role) for p in t.players] == [(p.id, p.role) for p in ref.players]


def test_abstention_when_confidence_is_low(fake_clients):
    uniform = FakeTypeSafe  # default fake gives varied probabilities; force flat ones
    with use_clients(typesafe=lambda: uniform(prob_fn=lambda n, q, s: [1 / 3, 1 / 3, 1 / 3])):
        t, policy, _ = play("jev_t028")
    days = day_rounds(t)
    assert all(r.eliminated is None for r in days)  # confidence 0 < 0.28 every day
    assert all(t.meta["decisions"][str(r.round)]["abstained"] for r in days)
    assert t.winner == "werewolves" and len(days) == live.MAX_DAYS
    blob = json.dumps(public_view(t))
    assert "Nobody was voted out on day 1." in blob


def test_no_defense_condition_scores_once_without_defenses(fake_clients):
    t, policy, judge = play("jev_nodef")
    days = day_rounds(t)
    assert policy.n_score_calls == len(days)
    assert all("defense" not in r for c in FakeTypeSafe.calls for r in c["state"]["rounds"])
    assert all(r.defense is not None for r in days)  # the game still has a defense each day


def test_adaptive_wolves_see_only_their_own_feedback(fake_clients):
    t, policy, _ = play("jev_adaptive")
    wolves = {p.id for p in t.players if p.team == "deceptive"}
    ollama = fake_clients["ollama"]
    seen_by = {"wolf": 0, "other": 0}
    for c in ollama.calls:
        system, user = c["messages"][0]["content"], c["messages"][1]["content"]
        pid = system.split(",")[0].replace("You are ", "")
        if "lie detector" in user:
            seen_by["wolf" if pid in wolves else "other"] += 1
            assert f"It rated you" in user
            assert "Day 1 (today)" not in user  # never on day 1
    assert seen_by["other"] == 0
    if len(day_rounds(t)) > 1:
        assert seen_by["wolf"] > 0
        assert t.meta["wolf_feedback"]
    assert "lie detector" not in json.dumps(public_view(t))


def test_llama_condition(fake_clients):
    def responder(model, messages):
        if messages[0]["content"].startswith("You are an expert observer"):
            players = [w.strip(",.") for w in messages[1]["content"].split() if w.startswith("Player")]
            return json.dumps({p: i % 10 for i, p in enumerate(dict.fromkeys(players))})
        return FakeOllama._default(model, messages)

    with use_clients(ollama=FakeOllama(responder)):
        t, policy, judge = play("llama")
    assert t.meta["judge_key"] == "llama_judge-c990a3c24747"
    assert all(t.meta["decisions"][str(r.round)]["top_confidence"] is None for r in day_rounds(t))
    assert t.meta["judge_cost_usd"] == 0.0


def test_run_resumes_and_cost_gate(fake_clients, capsys):
    assert live.run(["jev", "jev_nodef"], 2, log=quiet, retry_delays=(), judge_retry_delays=()) == {"played": 4}
    assert live.run(["jev", "jev_nodef"], 2, log=quiet) == {"played": 0}
    assert [p.stem for p in iter_source(transcripts_dir("live_jev"))] == ["g0000", "g0001"]
    assert live.main(["--conditions", "jev", "--n", "10"]) == 2  # 8 new games x up to 10 calls > 50
    assert "STOP" in capsys.readouterr().err
    assert live.main(["--conditions", "jev", "--n", "10", "--plan-only"]) == 0


def test_ordinal():
    assert [ordinal(i) for i in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]


def test_unknown_condition_rejected():
    with pytest.raises(SystemExit):
        live.main(["--conditions", "nope", "--n", "1"])


def test_live_report_end_to_end(fake_clients, tmp_path):
    from ww.live import report
    from ww.transcripts.generate import generate

    generate(3, "generic", "llm_llama31_8b", seed=0, retry_delays=(), log=quiet)
    live.run(["jev", "jev_t042"], 3, log=quiet, retry_delays=(), judge_retry_delays=())
    # offline judgments of the random source, for the threshold table
    from ww.judges.base import run_judge
    run_judge(live.make_condition_judge(live.CONDITIONS["jev"]), "llm_llama31_8b")
    out = tmp_path / "live.md"
    report.build(["jev", "jev_t042"], out)
    text = out.read_text(encoding="utf-8")
    assert "| jev_t042 | 3 |" in text and "| random | 3 |" in text
    assert (tmp_path / "live_outcomes.png").exists()
    # the hand-written interpretation survives a re-run
    out.write_text(text.replace("(to be written)", "MY NOTES"), encoding="utf-8")
    report.build(["jev", "jev_t042"], out)
    assert "MY NOTES" in out.read_text(encoding="utf-8")
