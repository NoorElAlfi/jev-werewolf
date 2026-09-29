"""WindowedJudge / window_view (Wave 3 E): offline, with the fakes."""

import json

from tests.fakes import FakeTypeSafe
from ww.judges.base import key_of, run_judge
from ww.judges.jev import JevBatchedJudge
from ww.judges.window import WindowedJudge, window_view
from ww.transcripts.schema import current_speakers


def _view(n_rounds=3, per_round=20, text_len=200):
    players = [f"P{i}" for i in range(5)]
    rounds = []
    for r in range(1, n_rounds + 1):
        st = [{"player_id": players[i % 5], "text": f"r{r}s{i} " + "x" * text_len} for i in range(per_round)]
        rounds.append({"round": r, "public_events": [f"event {r}"], "statements": st})
    return {"players": players, "rounds": rounds}


def test_small_view_passes_through():
    v = _view(1, 3, 10)
    wv, info = window_view(v, 100_000)
    assert wv is v and not info["truncated"]


def test_window_keeps_budget_tail_speakers_and_events():
    v = _view()
    wv, info = window_view(v, 5_000)
    assert info["truncated"] and len(json.dumps(wv, ensure_ascii=False)) <= 5_000
    assert set(current_speakers(wv)) == set(current_speakers(v))
    # every original event is still there, in order
    for r, wr in zip(v["rounds"], wv["rounds"]):
        assert wr["public_events"][: len(r["public_events"])] == r["public_events"]
    # kept statements are the newest ones: the last statement of the game is kept
    assert wv["rounds"][-1]["statements"][-1] == v["rounds"][-1]["statements"][-1]
    assert any("omitted" in e for e in wv["rounds"][0]["public_events"])
    assert info["kept"] == sum(len(r["statements"]) for r in wv["rounds"])


def test_forced_speakers_even_when_budget_tiny():
    v = _view(2, 20, 300)
    wv, _ = window_view(v, 10)
    assert set(current_speakers(wv)) == set(current_speakers(v))


def test_windowed_judge_key_and_run(fixture_source):
    inner = JevBatchedJudge()
    w = WindowedJudge(JevBatchedJudge(), 2_000)
    assert w.name == "jev_batched" and key_of(w) != key_of(inner)
    counts = run_judge(w, fixture_source)
    assert counts["scored"] > 0 and FakeTypeSafe.calls
    for call in FakeTypeSafe.calls:
        state = call["state"] if isinstance(call, dict) and "state" in call else None
        if state is not None:
            assert len(json.dumps(state, ensure_ascii=False)) <= 2_000
