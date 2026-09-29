"""Night phase, Seer, wolf partners, public-only history, win conditions."""

import json
import random
import re

import pytest

from tests.fakes import FakeOllama
from ww.clients import use_clients
from ww.config import CRITERIA_VARIANTS, SUSPICION_CRITERIA, WEREWOLF_PERSONAS
from ww.game.werewolf import (
    Game,
    GameConfig,
    Player,
    RandomDayPolicy,
    check_win_condition,
    clean_text,
    make_players,
)
from ww.transcripts.schema import public_view, validate


def speaker_of(messages):
    m = re.match(r"You are (Player\d+),", messages[0]["content"])
    return m.group(1)


def play(seed=0, **cfg):
    config = GameConfig(num_players=cfg.pop("num_players", 7), num_werewolves=cfg.pop("num_werewolves", 2), **cfg)
    game = Game(config, rng=random.Random(seed))
    t = game.play(RandomDayPolicy())
    return game, t


# --- criteria and persona variants -------------------------------------------------

def test_variants_in_config():
    assert CRITERIA_VARIANTS["leaky"] == SUSPICION_CRITERIA
    assert set(WEREWOLF_PERSONAS) >= {"generic", "leaky"}
    tactic_words = ("deflect", "blame", "blend", "cooperative", "someone else")
    for w in tactic_words:
        assert w not in " ".join(CRITERIA_VARIANTS["generic"]).lower()
        assert w not in WEREWOLF_PERSONAS["generic"].lower()
    assert "deflect" in WEREWOLF_PERSONAS["leaky"]


def test_persona_variant_reaches_wolf_prompt_only(fake_clients):
    game, t = play(seed=1, persona="leaky")
    for call in fake_clients["ollama"].calls:
        pid = speaker_of(call["messages"])
        leaky = "deflect suspicion" in call["messages"][0]["content"]
        assert leaky == game.player(pid).is_wolf


# --- roles and setup -----------------------------------------------------------------

def test_make_players_has_one_seer():
    ps = make_players(7, 2, seer=True, rng=random.Random(3))
    assert sum(p.role == "seer" for p in ps) == 1
    assert sum(p.role == "werewolf" for p in ps) == 2
    assert {p.team for p in ps if p.role == "seer"} == {"honest"}


# --- night ordering ----------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(8))
def test_night_ordering(seed):
    game, t = play(seed=seed)
    validate(t)
    assert t.rounds[0].night.kill is None and t.rounds[0].night.seer_check is None
    wolves = t.deceptive_ids()
    dead: set[str] = set()
    for i, r in enumerate(t.rounds):
        if i > 0:
            prev = t.rounds[i - 1]
            # the elimination is announced first, then the night's death
            if prev.eliminated:
                assert r.public_events[0].startswith(f"{prev.eliminated} was voted out by the village on day {prev.round}.")
            if r.night.kill:
                assert r.night.kill not in wolves
                assert r.night.kill not in dead and r.night.kill != prev.eliminated
                assert any(e.startswith(f"{r.night.kill} was killed by the werewolves during night {prev.round}.") for e in r.public_events)
            if r.night.seer_check:
                sc = r.night.seer_check
                assert sc.target != sc.seer
                assert sc.seer not in dead and sc.seer != prev.eliminated
                assert sc.result == t.team_of()[sc.target]
            # a night follows every day vote unless that vote ended the game
            if prev.eliminated and r is not t.rounds[-1]:
                assert r.night.kill
        speakers = {s.player_id for s in r.statements}
        assert not (speakers & dead), "dead players must not speak"
        if r.night.kill:
            dead.add(r.night.kill)
            assert r.night.kill not in speakers
        if r.eliminated:
            assert r.eliminated in speakers
            dead.add(r.eliminated)


def test_role_reveal_flag():
    _, t = play(seed=2, reveal_role_on_death=True)
    assert any(" was a " in e for r in t.rounds for e in r.public_events)
    _, t2 = play(seed=2, reveal_role_on_death=False)
    assert not any(" was a " in e for r in t2.rounds for e in r.public_events)


def test_llm_night_policy(fake_clients):
    fake_clients["ollama"].responder = lambda m, msgs: (
        "Player1" if "choose one player to kill" in msgs[-1]["content"] else "I have thoughts about this."
    )
    game, t = play(seed=4, night_policy="llm")
    kills = [r.night for r in t.rounds if r.night.kill]
    assert kills
    for n in kills:
        assert n.extra["kill_policy"] == "llm"
        assert (n.kill == "Player1") != n.extra["kill_fallback"]


# --- private knowledge ----------------------------------------------------------------

def _game_with_seer_checks():
    for seed in range(50):
        game, t = play(seed=seed)
        checks = [r.night.seer_check for r in t.rounds if r.night.seer_check]
        # need a check followed by at least one more day in which others speak
        if checks and any(r.statements for r in t.rounds[2:]):
            return game, t, checks
    raise AssertionError("no seed produced a Seer check")


def test_seer_result_only_in_seer_prompt(fake_clients):
    game, t, checks = _game_with_seer_checks()
    seer = checks[0].seer
    secret_lines = [f"{c.target} IS a werewolf" if c.result == "deceptive" else f"{c.target} is NOT a werewolf"
                    for c in checks]
    seer_saw = False
    for call in fake_clients["ollama"].calls:
        text = "\n".join(m["content"] for m in call["messages"])
        if speaker_of(call["messages"]) == seer:
            seer_saw = seer_saw or all(s in text for s in secret_lines[:1])
        else:
            assert "you are the Seer" not in text
            assert "Your checks so far" not in text
            for s in secret_lines:
                assert s not in text
    assert seer_saw, "the Seer should see their own result"


def test_seer_result_not_in_public_view():
    game, t, checks = _game_with_seer_checks()
    for upto in [None] + [r.round for r in t.rounds]:
        blob = json.dumps(public_view(t, upto))
        assert "seer_check" not in blob and "\"night\"" not in blob
        for c in checks:
            assert "IS a werewolf" not in blob and "is NOT a werewolf" not in blob
            assert c.result not in blob  # "deceptive"/"honest" never appear


def test_wolves_know_partners_only_in_their_prompts(fake_clients):
    game, t = play(seed=5)
    wolves = sorted(t.deceptive_ids())
    for call in fake_clients["ollama"].calls:
        pid = speaker_of(call["messages"])
        text = call["messages"][1]["content"]
        if pid in wolves:
            partner = next(w for w in wolves if w != pid)
            assert f"your fellow werewolves are {partner}" in text
        else:
            assert "fellow werewolves" not in text


def test_history_holds_only_public_facts(fake_clients):
    game, t = play(seed=6)
    roles = {p.id: p.role for p in t.players}
    for call in fake_clients["ollama"].calls:
        history = call["messages"][1]["content"].split("What has happened so far (public):\n", 1)[1]
        history = history.split("\n\n", 1)[0]
        assert "Secret" not in history
        # a role may appear in the history only via a death announcement
        for pid, role in roles.items():
            for line in history.splitlines():
                if f"{pid} was a {role}" in line:
                    assert "was voted out" in line or "was killed by the werewolves" in line


# --- win conditions ---------------------------------------------------------------------

def P(pid, role, alive=True):
    return Player(pid, role, alive)


def test_check_win_condition():
    assert check_win_condition([P("a", "werewolf", False), P("b", "villager")]) == "villagers"
    assert check_win_condition([P("a", "werewolf"), P("b", "villager")]) == "werewolves"
    assert check_win_condition([P("a", "werewolf"), P("b", "seer"), P("c", "villager")]) is None
    assert check_win_condition([P("a", "werewolf"), P("b", "werewolf"), P("c", "villager"), P("d", "seer")]) == "werewolves"


class FixedDay(RandomDayPolicy):
    def __init__(self, order):
        self.order = list(order)

    def choose_eliminated(self, game, rnd):
        return self.order.pop(0), None


def test_night_kill_ends_game():
    # 4 players, 1 wolf: day 1 removes a villager (1 wolf vs 2), the night kill makes it 1 vs 1.
    players = [P("Player1", "werewolf"), P("Player2", "villager"), P("Player3", "villager"), P("Player4", "seer")]
    game = Game(GameConfig(num_players=4, num_werewolves=1), rng=random.Random(0), players=players)
    t = game.play(FixedDay(["Player2"]))
    validate(t)
    assert t.winner == "werewolves"
    assert len(t.rounds) == 2
    last = t.rounds[-1]
    assert last.night.kill in {"Player3", "Player4"} and not last.statements
    assert last.public_events[0].startswith("Player2 was voted out")


def test_eliminating_last_wolf_ends_game_without_night():
    players = [P("Player1", "werewolf"), P("Player2", "villager"), P("Player3", "villager"), P("Player4", "seer")]
    game = Game(GameConfig(num_players=4, num_werewolves=1), rng=random.Random(0), players=players)
    t = game.play(FixedDay(["Player1"]))
    assert t.winner == "villagers"
    assert t.rounds[-1].night.kill is None and not t.rounds[-1].statements


def test_day_vote_can_end_game_for_wolves():
    # 5 players, 2 wolves: day 1 removes a villager -> 2 wolves vs 2 -> wolves win, no night.
    players = [P("Player1", "werewolf"), P("Player2", "werewolf"), P("Player3", "villager"),
               P("Player4", "villager"), P("Player5", "seer")]
    game = Game(GameConfig(num_players=5, num_werewolves=2), rng=random.Random(0), players=players)
    t = game.play(FixedDay(["Player3", "Player4"]))
    assert t.winner == "werewolves"
    assert len(t.rounds) == 2  # day 1, then the final announcement round
    assert t.rounds[-1].night.kill is None


@pytest.mark.parametrize("seed", range(10))
def test_games_always_end_with_a_winner(seed):
    game, t = play(seed=seed)
    assert t.winner in {"villagers", "werewolves"}
    assert t.winner == check_win_condition(game.players)


# --- text cleaning -----------------------------------------------------------------------

@pytest.mark.parametrize("raw,clean", [
    ('""I have been watching closely.""', "I have been watching closely."),
    ("“I trust Player2.”", "I trust Player2."),
    ("Player3: I think it's Player4.", "I think it's Player4."),
    ('**Player3:** "Let\'s be careful."', "Let's be careful."),
    ("  plain   text \n here ", "plain text here"),
    ("I said 'no' to that.", "I said 'no' to that."),
])
def test_clean_text(raw, clean):
    assert clean_text(raw) == clean


def test_quote_wrapped_wolves_are_cleaned(fake_clients):
    fake_clients["ollama"].responder = lambda m, msgs: (
        '""I am innocent.""' if "WEREWOLF" in msgs[0]["content"] else "I am innocent."
    )
    _, t = play(seed=7)
    texts = {s.text for r in t.rounds for s in r.statements}
    assert texts == {"I am innocent."}


def test_ollama_options_seeded_when_temperature_set():
    o = FakeOllama()
    with use_clients(ollama=o):
        play(seed=0, temperature=0.8)
    assert all(c["options"]["temperature"] == 0.8 and "seed" in c["options"] for c in o.calls)
