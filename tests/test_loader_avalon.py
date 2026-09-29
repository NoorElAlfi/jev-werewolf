"""avalon loader on an excerpt of game 1aKReQ (MIT): quests 1-2, the start of
quest 3 up to its first labelled lie, and the assassination messages."""

from pathlib import Path

from ww.transcripts.loaders import avalon as av
from ww.transcripts.loaders import convert
from ww.transcripts.schema import current_speakers, public_view, validate

RAW = Path(__file__).parent / "fixtures" / "raw" / "avalon"


def _game():
    (t,) = av.load_all(RAW)
    return t


def test_players_and_teams():
    t = validate(_game())
    assert t.game_id == "1aKReQ" and t.source == "avalon"
    roles = {p.id: p.role for p in t.players}
    assert roles["player-1"] == "assassin" and roles["player-5"] == "morgana"
    assert t.deceptive_ids() == {"player-1", "player-5"}
    assert t.winner == "evil" and t.meta["winner_team"] == "deceptive"


def test_rounds_are_quests_and_system_messages_never_come_early():
    t = _game()
    assert [r.round for r in t.rounds] == [1, 2, 3, 4]
    r1, r2, r3, r4 = t.rounds
    assert r1.public_events == ["game started!", "player-4 proposed a party: player-4, player-5"]
    assert len(r1.statements) == 6
    # Quest 1's vote and result (after its chat) open quest 2.
    assert r2.public_events[-1] == "quest succeeded!" and len(r2.public_events) == 3
    # Quest 2's proposal came after player-5's first line, so it opens quest 3.
    assert r2.statements[0].player_id == "player-5"
    assert r3.public_events[0] == "player-5 proposed a party: player-5, player-4, player-1"
    assert len(r2.statements) == 10 and len(r3.statements) == 6
    # Leftover system messages (incl. the assassination) form a final round.
    assert r4.statements == []
    assert r4.public_events[-1] == "the assassin identified merlin, thus evil wins!"
    for r in t.rounds:
        assert r.night.kill is None and r.votes is None and r.eliminated is None


def test_lie_labels_in_meta():
    t = _game()
    lies = t.meta["lies"]
    assert {(x["round"], x["index"], x["player_id"], x["strategy"]) for x in lies} == {
        (2, 0, "player-5", "omission"), (2, 4, "player-1", "omission"),
        (3, 5, "player-1", "influence")}
    for x in lies:
        assert t.rounds[x["round"] - 1].statements[x["index"]].player_id == x["player_id"]
    assert "omission" not in repr(public_view(t))


def test_view_and_convert(data_dir):
    t = _game()
    assert current_speakers(public_view(t, 3)) == ["player-6", "player-1"]
    convert("avalon", raw_dir=RAW)
    assert (data_dir / "transcripts" / "avalon" / "1aKReQ.json").exists()
