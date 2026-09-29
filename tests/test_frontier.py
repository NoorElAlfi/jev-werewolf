"""Frontier judge, offline: a fake Anthropic client is injected for every test."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from ww.judges.base import JudgmentCache, load_judgments, run_judge
from ww.judges.frontier import (
    FALLBACK_BETA,
    OUTPUT_SCHEMA,
    FrontierError,
    FrontierJudge,
    make_frontier,
    main,
    parse_answer,
    plan_frontier,
    run_parallel,
    use_anthropic,
)
from ww.transcripts.schema import current_speakers, iter_source, load, public_view
from ww.config import transcripts_dir


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: .beta.messages.create(**kw)."""

    calls: list[dict[str, Any]] = []

    def __init__(self, responder=None, stop_reason: str = "end_turn"):
        self.responder = responder or self._default
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    @staticmethod
    def _default(kw: dict[str, Any]) -> str:
        text = kw["messages"][0]["content"]
        players = text.split("For each of these players: ")[1].split("\n")[0].split(", ")
        return json.dumps({"players": [{"player_id": p, "p_deceptive": (i % 3) / 2, "confidence": 0.7}
                                       for i, p in enumerate(players)]})

    def _create(self, **kw: Any) -> Any:
        FakeAnthropic.calls.append(kw)
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.responder(kw))]
        return SimpleNamespace(
            model=kw["model"], stop_reason=self.stop_reason, stop_details=None, content=content,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0),
            _request_id="req_fake",
        )


@pytest.fixture(autouse=True)
def fake_anthropic():
    FakeAnthropic.calls = []
    with use_anthropic(lambda: FakeAnthropic()):
        yield FakeAnthropic


def _views(source: str) -> list[dict[str, Any]]:
    out = []
    for f in iter_source(transcripts_dir(source)):
        t = load(f)
        out += [public_view(t, r.round) for r in t.rounds if r.statements]
    return out


def test_scores_every_current_speaker(fixture_source):
    j = FrontierJudge()
    for view in _views(fixture_source):
        res = j.score(view)
        res.check(current_speakers(view))
        assert set(res.p_deceptive) == set(current_speakers(view))
        assert set(res.confidence) == set(current_speakers(view))
        assert res.meta["parse_ok"]
        assert res.meta["cost_usd"] == pytest.approx(1000 * 5e-6 + 200 * 25e-6)


def test_request_shape(fixture_source):
    view = _views(fixture_source)[0]
    FrontierJudge().score(view)
    kw = FakeAnthropic.calls[-1]
    assert kw["model"] == "claude-opus-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "budget_tokens" not in json.dumps(kw["thinking"])


def test_only_public_view_reaches_the_model(fixture_source):
    """Roles, teams, night info and meta never appear in the request."""
    for f in iter_source(transcripts_dir(fixture_source)):
        t = load(f)
        run_judge(FrontierJudge(), fixture_source)
        blob = json.dumps(FakeAnthropic.calls, default=str).lower()
        for p in t.players:
            assert f'"{p.role.lower()}"' not in blob
        assert "deceptive team" in blob  # the prompt's own wording is fine
        assert '"team"' not in blob and "seer_check" not in blob


def test_cache_is_used(fixture_source):
    j = FrontierJudge()
    first = run_judge(j, fixture_source)
    n = len(FakeAnthropic.calls)
    second = run_judge(j, fixture_source)
    assert first["scored"] > 0 and second["scored"] == 0
    assert len(FakeAnthropic.calls) == n


def test_parallel_runner_matches_serial(fixture_source, data_dir):
    j = make_frontier(max_chars=90_000)
    counts = run_parallel(j, fixture_source, workers=3)
    assert counts["scored"] == len(_views(fixture_source))
    got = load_judgments(JudgmentCache(j).key, fixture_source)
    assert all(d["complete"] for d in got.values())
    assert run_parallel(j, fixture_source, workers=3)["scored"] == 0


def test_missing_players_are_recorded(fixture_source):
    def only_first(kw):
        text = kw["messages"][0]["content"]
        first = text.split("For each of these players: ")[1].split(", ")[0].split("\n")[0]
        return json.dumps({"players": [{"player_id": first, "p_deceptive": 0.9, "confidence": 0.8}]})

    view = _views(fixture_source)[0]
    with use_anthropic(lambda: FakeAnthropic(responder=only_first)):
        res = FrontierJudge().score(view)
    sp = current_speakers(view)
    assert res.p_deceptive[sp[0]] == 0.9
    assert not res.meta["parse_ok"]
    assert set(res.meta["raw"]["parse_failures"]) == set(sp[1:])
    assert all(res.p_deceptive[p] == 0.5 for p in sp[1:])


def test_refusal_and_max_tokens_raise(fixture_source):
    view = _views(fixture_source)[0]
    for stop in ("refusal", "max_tokens"):
        with use_anthropic(lambda: FakeAnthropic(stop_reason=stop)):
            with pytest.raises(FrontierError):
                FrontierJudge().score(view)


def test_parse_answer_clamps_and_rescales():
    p, c = parse_answer({"players": [{"player_id": "player1", "p_deceptive": 80, "confidence": 1.4},
                                     {"player_id": "Player2", "p_deceptive": -0.1, "confidence": 0.5},
                                     {"player_id": "Nobody", "p_deceptive": 0.5, "confidence": 0.5}]},
                        ["Player1", "Player2"])
    assert p == {"Player1": 0.8, "Player2": 0.0}
    assert c == {"Player1": 1.0, "Player2": 0.5}
    assert parse_answer(None, ["Player1"]) == ({}, {})


def test_config_changes_key():
    from ww.judges.base import key_of

    assert key_of(FrontierJudge()) != key_of(FrontierJudge(effort="high"))
    assert key_of(make_frontier(90_000)) != key_of(make_frontier(None))


def test_cli_plan_and_gate(fixture_source, capsys):
    assert main(["--sources", fixture_source, "--plan-only"]) == 0
    out = capsys.readouterr().out
    assert "judge_key: frontier-" in out and "est. $" in out
    assert FakeAnthropic.calls == []
    p = plan_frontier(make_frontier(), fixture_source)
    assert p["new"]["calls"] == len(_views(fixture_source)) and p["new"]["est_cost_usd"] > 0


def test_cli_gate_blocks_large_runs(fixture_source, monkeypatch):
    import ww.judges.frontier as fr

    monkeypatch.setattr(fr, "COST_GATE_CALLS", 1)
    assert main(["--sources", fixture_source]) == 2
    assert FakeAnthropic.calls == []
    assert main(["--sources", fixture_source, "--yes", "--workers", "2"]) == 0
    assert len(FakeAnthropic.calls) == len(_views(fixture_source))
