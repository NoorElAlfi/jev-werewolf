"""werewolf_among_us loader on excerpts (Apache 2.0): a Youtube game (app
narration, "#" in file names), an Ego4D game in two parts with a moderator and
role changes, and an Ego4D game with unknown roles that must be skipped."""

from pathlib import Path

from ww.transcripts.loaders import convert
from ww.transcripts.loaders import werewolf_among_us as wau
from ww.transcripts.schema import current_speakers, public_view, validate

RAW = Path(__file__).parent / "fixtures" / "raw" / "werewolf_among_us"
EGO = "ego_0a6ef9dc-a2dc-452b-a907-d6fa2ed4cae0_Game1"
YT = "yt_ONE_NIGHT_ULTIMATE_WEREWOLF_Retro_1_Game2"


def _games():
    return {t.game_id: t for t in wau.load_all(RAW)}


def test_skips_games_without_start_roles():
    assert set(_games()) == {EGO, YT}
    assert wau.skipped(RAW) == {"ego_0d495a56-676b-4bb0-bb7d-9abf88fb7beb_Game1":
                                "unknown start role"}


def test_youtube_game():
    t = validate(_games()[YT])
    assert [p.id for p in t.players] == ["Justin", "Alvin", "James", "Mitchell"]
    assert t.deceptive_ids() == {"Justin"}
    assert len(t.rounds) == 1
    r = t.rounds[0]
    assert "Audio" not in {s.player_id for s in r.statements}
    assert r.night.extra["narration_lines"] == 5
    assert r.statements[0].text == "I love this guy."
    assert r.votes == {"Justin": "James", "Alvin": "James", "James": "Justin",
                       "Mitchell": "James"}
    assert r.eliminated == "James"
    # Alvin (Robber -> Villager) and James (Villager -> Robber) stay honest.
    assert t.meta["team_changed"] == []


def test_ego4d_game_parts_moderator_and_role_changes():
    t = validate(_games()[EGO])
    ids = [p.id for p in t.players]
    assert "Jack" not in ids  # the moderator is not a player
    assert ids == ["Erin", "Sebastian", "Brent", "Hailey"]
    assert t.deceptive_ids() == {"Erin"}  # START role Werewolf
    changed = {x["player_id"]: (x["start_role"], x["end_role"]) for x in t.meta["team_changed"]}
    assert changed == {"Erin": ("Werewolf", "Robber"), "Brent": ("Seer", "Werewolf")}
    r = t.rounds[0]
    texts = [s.text for s in r.statements]
    assert texts[0] == "Okay. Do you need the script?"   # part 1 first player line
    assert "Hailey's sus." in texts                      # part 2 is appended
    assert r.night.extra["narration_lines"] == 12        # the moderator Jack's lines
    assert r.votes is None and t.meta["votes_left_out_moderator_game"]


def test_view_and_convert(data_dir):
    t = _games()[EGO]
    view = repr(public_view(t))
    assert "Werewolf" not in view and "Robber" not in view
    assert current_speakers(public_view(t))[0] == "Hailey"
    convert("werewolf_among_us", raw_dir=RAW)
    out = sorted(p.stem for p in (data_dir / "transcripts" / "werewolf_among_us").glob("*.json"))
    assert out == sorted([EGO, YT])
