import pytest

from ww.judges.base import (
    JudgeResult,
    JudgmentCache,
    judge_key,
    key_of,
    load_judgments,
    plan_calls,
    run_judge,
)
from ww.transcripts.schema import current_speakers


class CountingJudge:
    name = "counting"

    def __init__(self, fail_on_call=None, **config):
        self.config = {"version": 1, **config}
        self.calls = 0
        self.views = []
        self.fail_on_call = fail_on_call

    def score(self, view):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("boom")
        self.views.append(view)
        ids = current_speakers(view)
        return JudgeResult(p_deceptive={i: 0.5 for i in ids}, confidence={i: 0.1 for i in ids}, meta={"latency_s": 0.0})


def test_judge_key_is_stable_and_config_sensitive():
    assert judge_key("x", {"a": 1, "b": 2}) == judge_key("x", {"b": 2, "a": 1})
    assert judge_key("x", {"a": 1}) != judge_key("x", {"a": 2})
    assert judge_key("x", {"a": 1}) != judge_key("y", {"a": 1})
    assert key_of(CountingJudge()).startswith("counting-")


def test_plan_calls(fixture_source):
    # seer: 2 rounds with statements (round 3 has none); defense: 2; mafia: 2
    assert plan_calls(fixture_source) == {"games": 3, "views": 6}
    assert plan_calls(fixture_source, limit=1)["games"] == 1
    assert plan_calls(fixture_source, max_round=1)["views"] == 3


def test_cache_miss_then_hit(fixture_source):
    j = CountingJudge()
    assert run_judge(j, fixture_source) == {"games": 3, "scored": 6, "cached": 0}
    assert j.calls == 6
    assert run_judge(j, fixture_source) == {"games": 3, "scored": 0, "cached": 6}
    assert j.calls == 6  # cache hit: judge not called again
    # A different config is a different key: cache miss.
    assert run_judge(CountingJudge(version=2), fixture_source)["scored"] == 6


def test_cache_contents(fixture_source, data_dir):
    j = CountingJudge()
    run_judge(j, fixture_source)
    cache = JudgmentCache(j)
    assert (data_dir / "judgments" / cache.key / "_config.json").exists()
    d = cache.load(fixture_source, "fx_ww6_seer")
    assert d["complete"] and set(d["rounds"]) == {"1", "2"}
    assert set(d["rounds"]["2"]["p_deceptive"]) == {"Player2", "Player4", "Player5", "Player6"}
    assert cache.get_round(fixture_source, "fx_ww6_seer", 2).confidence["Player4"] == 0.1
    assert set(load_judgments(cache.key, fixture_source)) == {"fx_ww6_seer", "fx_ww5_defense", "fx_mafia7"}


def test_runner_only_passes_public_views(fixture_source):
    j = CountingJudge()
    run_judge(j, fixture_source)
    for v in j.views:
        assert set(v) == {"players", "rounds"}


def test_interrupted_run_resumes_mid_game(fixture_source):
    j = CountingJudge(fail_on_call=4)
    with pytest.raises(RuntimeError):
        run_judge(j, fixture_source)
    cache = JudgmentCache(j)
    games = sorted(p.stem for p in (cache.dir / fixture_source).glob("*.json"))
    assert games == ["fx_mafia7", "fx_ww5_defense"]
    assert cache.is_complete(fixture_source, "fx_mafia7")
    assert not cache.is_complete(fixture_source, "fx_ww5_defense")  # only round 1 done
    j.fail_on_call = None
    assert run_judge(j, fixture_source) == {"games": 3, "scored": 3, "cached": 3}


def test_limit_is_a_prefix(fixture_source):
    j = CountingJudge()
    assert run_judge(j, fixture_source, limit=1)["games"] == 1
    assert run_judge(j, fixture_source)["cached"] == 2


def test_result_check():
    JudgeResult({"a": 0.2}, meta={"latency_s": 1}).check(["a"])
    with pytest.raises(ValueError):
        JudgeResult({"a": 1.2}, meta={"latency_s": 1}).check()
    with pytest.raises(ValueError):
        JudgeResult({"a": 0.2}, meta={"latency_s": 1}).check(["a", "b"])
    with pytest.raises(ValueError):
        JudgeResult({"a": 0.2}).check()
