"""human_mafia loader on a hand-made game in the omonida CSV format.

The omonida dataset has no license, so the fixture is synthetic (invented names
and text), not copied, but it reproduces the format's quirks: duplicated phase
change rows, a stale "Victim - X" suffix, vote changes, and night chat.
"""

from pathlib import Path

from ww.transcripts.loaders import convert
from ww.transcripts.loaders import human_mafia as hm
from ww.transcripts.schema import current_speakers, load, public_view, validate

RAW = Path(__file__).parent / "fixtures" / "raw" / "human_mafia"


def _game():
    (t,) = hm.load_all(RAW)
    return t


def test_players_and_teams():
    t = _game()
    validate(t)
    assert t.game_id == "0000fake"
    assert t.source == "human_mafia"
    assert [p.id for p in t.players] == ["Ann Alpha", "Bob Beta", "Cat Gamma",
                                         "Dan Delta", "Eve Epsilon"]
    assert t.deceptive_ids() == {"Ann Alpha", "Eve Epsilon"}
    assert t.winner == "bystanders" and t.meta["winner_team"] == "honest"


def test_rounds_follow_night_then_day():
    t = _game()
    assert [r.round for r in t.rounds] == [1, 2, 3]
    r1, r2, r3 = t.rounds
    assert r1.night.kill == "Bob Beta"
    assert [m["player_id"] for m in r1.night.extra["mafia_chat"]] == ["Ann Alpha", "Eve Epsilon"]
    assert r1.night.extra["mafia_votes"] == {"Ann Alpha": "Bob Beta", "Eve Epsilon": "Bob Beta"}
    assert any("Victim - Bob Beta" in i for i in r1.night.extra["info"])
    assert r1.public_events == ["Bob Beta was killed during the night."]
    assert [s.player_id for s in r1.statements] == ["Cat Gamma", "Ann Alpha", "Dan Delta"]
    assert r1.statements[1].text == "no idea, i was asleep"  # "Name: " prefix stripped
    assert r1.votes == {"Dan Delta": "Cat Gamma", "Eve Epsilon": "Cat Gamma",
                        "Ann Alpha": "Cat Gamma"}  # last vote per voter
    assert r1.eliminated == "Cat Gamma"

    # Night 2: stale "Victim - Cat Gamma" on the info row, but nobody died.
    assert r2.night.kill is None
    assert r2.public_events == ["Cat Gamma was eliminated by vote. They were a bystander."]
    assert r2.statements[1].text == "I'm here: just thinking"
    assert r2.eliminated == "Eve Epsilon"

    # Final elimination is announced in a last round with no statements.
    assert r3.public_events == ["Eve Epsilon was eliminated by vote. They were a mafioso."]
    assert r3.statements == []


def test_public_view_hides_night_chat_and_roles():
    t = _game()
    view = public_view(t)
    text = repr(view)
    assert "who first" not in text and "mafioso" not in text.replace(
        "They were a mafioso", "")
    assert current_speakers(public_view(t, 1)) == ["Cat Gamma", "Ann Alpha", "Dan Delta"]


def test_convert_writes_valid_files(data_dir):
    convert("human_mafia", raw_dir=RAW)
    files = sorted((data_dir / "transcripts" / "human_mafia").glob("*.json"))
    assert [f.stem for f in files] == ["0000fake"]
    assert load(files[0]).rounds[0].night.kill == "Bob Beta"
