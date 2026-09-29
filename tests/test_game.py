import random

import pytest
from typesafe_sdk import Score, SystemOneResponse

from tests.fakes import FakeOllama, FakeOllamaError, FakeTypeSafe
from ww.clients import get_ollama, make_typesafe
from ww.game import play_game
from ww.game.werewolf import score_suspicion


def test_fakes_are_injected(fake_clients):
    assert get_ollama() is fake_clients["ollama"]
    assert isinstance(make_typesafe(), FakeTypeSafe)


def test_fake_typesafe_mimics_sdk_types():
    with make_typesafe() as c:
        r = c.system_one(state={"x": 1}, questions={"q": Score(instructions="?", criteria=["low", "mid", "high"])})
    assert isinstance(r, SystemOneResponse)
    a = r.scores["q"]
    assert set(a.probabilities) == {0, 1, 2}  # 0-based levels, like the real API
    assert abs(sum(a.probabilities.values()) - 1) < 1e-9
    assert abs(a.score - sum(k * p for k, p in a.probabilities.items())) < 1e-9
    assert 0 <= a.confidence <= 1


def test_game_runs_offline_with_fakes(fake_clients, capsys):
    random.seed(0)
    result = play_game(num_players=6, num_werewolves=2, verbose=True)
    assert result["winner"] in {"villagers", "werewolves"}
    assert result["eliminations"]
    assert fake_clients["ollama"].calls
    assert FakeTypeSafe.calls
    assert "GAME OVER" in capsys.readouterr().out


def test_score_suspicion_batches_all_players():
    scores = score_suspicion({"Player1": "hi", "Player2": "hello"})
    assert set(scores) == {"Player1", "Player2"}
    assert len(FakeTypeSafe.calls) == 1


def test_fake_ollama_can_fail_after_n():
    o = FakeOllama(fail_after=1)
    o.chat(model="m", messages=[{"role": "system", "content": "You are Player1, x"}])
    with pytest.raises(FakeOllamaError):
        o.chat(model="m", messages=[])
