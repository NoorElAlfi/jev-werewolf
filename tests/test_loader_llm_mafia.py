"""llm_mafia loader on a trimmed excerpt of game 0631 (CC0): full manager and
night files, a few day messages, both vote blocks, and a multi-line message."""

from pathlib import Path

from ww.transcripts.loaders import convert
from ww.transcripts.loaders import llm_mafia as lm
from ww.transcripts.schema import current_speakers, public_view, validate

RAW = Path(__file__).parent / "fixtures" / "raw" / "llm_mafia"


def _game():
    (t,) = lm.load_all(RAW)
    return t


def test_players_and_teams():
    t = validate(_game())
    assert t.game_id == "0631" and t.source == "llm_mafia"
    assert len(t.players) == 10
    assert t.deceptive_ids() == {"River", "Stevie"}
    assert t.winner == "bystanders" and t.meta["winner_team"] == "honest"


def test_rounds():
    t = _game()
    assert [r.round for r in t.rounds] == [1, 2, 3]
    r1, r2, r3 = t.rounds
    # The game starts with a day: round 1 has an empty night.
    assert r1.night.kill is None and not r1.night.extra
    assert r1.public_events == []
    assert [s.player_id for s in r1.statements] == ["River", "Ronny", "Tyler"]
    assert len(r1.votes) == 10
    assert r1.votes["Alex"] == "River"  # cast the same second the night began
    assert r1.eliminated == "River"

    assert r2.night.kill == "Adrian"
    assert r2.night.extra["mafia_votes"] == {"Stevie": "Adrian"}
    assert r2.public_events == [
        "River was voted out. Their role was mafia. There are now 1 mafia players left.",
        "Adrian was voted out. Their role was bystander. There are now 1 mafia players left.",
    ]
    assert [s.player_id for s in r2.statements] == ["Whitney", "Stevie", "Alex", "Noah"]
    assert "stevie and elliot, your votes" in r2.statements[3].text  # continuation lines
    assert r2.eliminated == "Stevie" and len(r2.votes) == 8

    assert r3.statements == []
    assert r3.public_events[0].startswith("Stevie was voted out. Their role was mafia.")


def test_view_and_convert(data_dir):
    t = _game()
    assert current_speakers(public_view(t, 2)) == ["Whitney", "Stevie", "Alex", "Noah"]
    assert "voted for" not in repr(public_view(t))  # votes are not statements
    convert("llm_mafia", raw_dir=RAW)
    assert (data_dir / "transcripts" / "llm_mafia" / "0631.json").exists()
