import json

import pytest

from tests.conftest import VALID_FIXTURES
from ww.transcripts.schema import (
    Night,
    PlayerInfo,
    Round,
    SeerCheck,
    Statement,
    Transcript,
    current_speakers,
    load,
    public_view,
)

FORBIDDEN_KEYS = {"role", "team", "night", "kill", "seer_check", "extra", "meta", "winner",
                  "votes", "eliminated", "source", "game_id"}


def _keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from _keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from _keys(v)


def _public_texts(t):
    for r in t.rounds:
        yield from r.public_events
        yield from (s.text for s in r.statements)
        if r.defense:
            yield r.defense.text


def sentinel_transcript():
    """Every private field holds a unique token that can't occur by accident."""
    return Transcript(
        game_id="GAMEID_SENTINEL",
        source="SOURCE_SENTINEL",
        players=[PlayerInfo("A", "ROLE_SENTINEL_WOLF", "deceptive"), PlayerInfo("B", "ROLE_SENTINEL_SEER", "honest"),
                 PlayerInfo("C", "ROLE_SENTINEL_VILL", "honest")],
        rounds=[
            Round(1, public_events=["day one"],
                  statements=[Statement("A", "hello"), Statement("B", "hi"), Statement("C", "hey")],
                  eliminated="C", votes={"A": "C", "B": "A", "C": "A"}),
            Round(2, night=Night(kill="B", seer_check=SeerCheck("B", "A", "deceptive"), extra={"x": "NIGHT_SENTINEL"}),
                  public_events=["day two"], statements=[Statement("A", "sad")], defense=Statement("A", "not me")),
        ],
        winner="WINNER_SENTINEL",
        meta={"generator_model": "META_SENTINEL"},
    )


def test_sentinels_never_reach_the_view():
    t = sentinel_transcript()
    for upto in (None, 1, 2):
        view = public_view(t, upto)
        blob = json.dumps(view)
        for token in ["SENTINEL", "deceptive", "honest"]:
            assert token not in blob, (upto, token)
        assert not FORBIDDEN_KEYS & set(_keys(view))


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_fixture_views_leak_nothing(path):
    t = load(path)
    private = {p.role for p in t.players} | {"deceptive", "honest"}
    for r in t.rounds:
        for v in r.night.extra.values():
            private |= set(v) if isinstance(v, list) else {str(v)}
    public = list(_public_texts(t))
    for r in t.rounds:
        view = public_view(t, r.round)
        assert not FORBIDDEN_KEYS & set(_keys(view))
        # Role words may legitimately appear inside public text (revealed roles,
        # accusations). Remove every public string, then nothing private may remain.
        blob = json.dumps(view, ensure_ascii=False)
        for s in sorted(public, key=len, reverse=True):
            blob = blob.replace(json.dumps(s, ensure_ascii=False), '""')
        for token in private:
            assert token.lower() not in blob.lower(), (r.round, token)


def test_view_contents_and_truncation():
    t = load([p for p in VALID_FIXTURES if p.stem == "fx_ww5_defense"][0])
    v1 = public_view(t, 1)
    assert v1["players"] == [p.id for p in t.players]
    assert [r["round"] for r in v1["rounds"]] == [1]
    assert v1["rounds"][0]["defense"]["player_id"] == "Player3"
    assert [r["round"] for r in public_view(t)["rounds"]] == [1, 2]
    assert current_speakers(public_view(t, 2)) == ["Player1", "Player3", "Player4"]


def test_speakers_dedupe_multi_message_players():
    t = load([p for p in VALID_FIXTURES if p.stem == "fx_mafia7"][0])
    assert current_speakers(public_view(t, 1)) == ["Player1", "Player2", "Player3", "Player4", "Player5", "Player6"]
