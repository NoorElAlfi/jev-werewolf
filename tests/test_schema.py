import json

import pytest

from tests.conftest import FIXTURES, VALID_FIXTURES
from ww.transcripts.schema import Transcript, TranscriptValidationError, load, save, validate


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_round_trip(path, tmp_path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    t = validate(raw)
    assert json.loads(t.to_json()) == raw
    assert Transcript.from_json(t.to_json()) == t
    save(t, tmp_path / "x.json")
    assert load(tmp_path / "x.json") == t


def test_valid_fixtures_cover_the_required_cases():
    ts = {p.stem: validate(json.loads(p.read_text(encoding="utf-8"))) for p in VALID_FIXTURES}
    seer = ts["fx_ww6_seer"]
    assert len(seer.players) == 6
    assert any(r.night.kill for r in seer.rounds) and any(r.night.seer_check for r in seer.rounds)
    assert any(r.defense for r in ts["fx_ww5_defense"].rounds)
    assert {p.role for p in ts["fx_mafia7"].players} == {"mafioso", "bystander"}
    for t in ts.values():
        assert t.deceptive_ids() and t.deceptive_ids() < set(t.team_of())


def test_validate_rejects_invalid_fixture():
    raw = json.loads((FIXTURES / "fx_invalid.json").read_text(encoding="utf-8"))
    with pytest.raises(TranscriptValidationError) as e:
        validate(raw)
    text = "\n".join(e.value.problems)
    for needle in ["team must be one of", "duplicate player id", "unknown player 'Player9'",
                   "unknown player 'Player7'", "must increase"]:
        assert needle in text


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("game_id"),
    lambda d: d.update(players=[]),
    lambda d: d["rounds"][0]["statements"].append({"player_id": "Player1"}),
    lambda d: d["rounds"][1]["night"].update(seer_check={"seer": "Player4", "target": "Player5", "result": "wolf"}),
    lambda d: d["rounds"][0].update(votes={"Player1": "Nobody"}),
])
def test_validate_rejects_mutations(mutate):
    raw = json.loads((FIXTURES / "fx_ww6_seer.json").read_text(encoding="utf-8"))
    mutate(raw)
    with pytest.raises(TranscriptValidationError):
        validate(raw)


def test_save_refuses_invalid(tmp_path):
    t = validate(json.loads(VALID_FIXTURES[0].read_text(encoding="utf-8")))
    t.players[0].team = "evil"
    with pytest.raises(TranscriptValidationError):
        save(t, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()
