"""The transcript generator, on the fake Ollama."""

import json

import pytest

from tests.fakes import FakeOllama, FakeOllamaError
from ww.clients import use_clients
from ww.config import transcripts_dir
from ww.transcripts.generate import game_seed, generate, main
from ww.transcripts.schema import iter_source, load, public_view


def quiet(*a, **k):
    pass


def files(source):
    return {p.name: p.read_text(encoding="utf-8") for p in iter_source(transcripts_dir(source))}


def strip_volatile(text):
    d = json.loads(text)
    for k in ("created_at", "gen_seconds"):
        d["meta"].pop(k)
    return d


def test_output_validates_and_has_full_meta(fake_clients):
    r = generate(3, "generic", "gen_test", seed=0, retry_delays=(), log=quiet)
    assert r == {"done": 3, "skipped": 0, "generated": 3}
    paths = iter_source(transcripts_dir("gen_test"))
    assert [p.stem for p in paths] == ["g0000", "g0001", "g0002"]
    for p in paths:
        t = load(p)  # validates
        assert t.source == "gen_test" and t.game_id == p.stem
        assert t.winner in {"villagers", "werewolves"}
        m = t.meta
        assert m["generator_model"] == "llama3.1:8b"
        assert m["persona_variant"] == "generic" and m["config"]["persona"] == "generic"
        assert m["day_policy"] == "random" and m["seed"] == 0
        assert m["game_seed"] == game_seed(0, m["game_index"])
        assert "deflect" not in m["prompts"]["werewolf_persona"]
        roles = sorted(pl.role for pl in t.players)
        assert roles.count("werewolf") == 2 and roles.count("seer") == 1 and len(roles) == 9
        day_rounds = [r for r in t.rounds if r.statements]
        assert day_rounds and all(r.defense is not None for r in day_rounds)
        assert all(r.eliminated for r in day_rounds)
        assert set(m["defense_selection"]) == {str(r.round) for r in day_rounds}
        # the view still hides everything private
        blob = json.dumps(public_view(t))
        assert "werewolf_persona" not in blob and "seer_check" not in blob


def test_per_game_seeds_are_independent_of_run_order(fake_clients):
    generate(3, "generic", "a", seed=0, retry_delays=(), log=quiet)
    generate(3, "generic", "b", seed=0, start=2, retry_delays=(), log=quiet)
    a = strip_volatile(files("a")["g0002.json"])
    b = strip_volatile(files("b")["g0002.json"])
    a["source"] = b["source"] = "x"
    assert a == b


def test_leaky_persona(fake_clients):
    generate(1, "leaky", "leaky_test", seed=0, retry_delays=(), log=quiet)
    t = load(transcripts_dir("leaky_test") / "g0000.json")
    assert "deflect" in t.meta["prompts"]["werewolf_persona"]
    wolf_calls = [c for c in fake_clients["ollama"].calls if "WEREWOLF" in c["messages"][0]["content"]]
    assert wolf_calls and all("deflect suspicion" in c["messages"][0]["content"] for c in wolf_calls)


def test_crash_midway_resumes_correctly():
    # Reference: an uninterrupted run.
    with use_clients(ollama=FakeOllama()):
        generate(4, "generic", "ref", seed=0, retry_delays=(), log=quiet)
    ref = {k: strip_volatile(v) for k, v in files("ref").items()}
    calls_per_game = [load(transcripts_dir("ref") / k).meta["n_model_calls"] for k in sorted(ref)]

    # Crash partway through the third game.
    crash_at = calls_per_game[0] + calls_per_game[1] + 3
    with use_clients(ollama=FakeOllama(fail_after=crash_at)), pytest.raises(FakeOllamaError):
        generate(4, "generic", "run", seed=0, retry_delays=(), log=quiet)
    assert sorted(files("run")) == ["g0000.json", "g0001.json"]  # no partial third game
    assert not list(transcripts_dir("run").glob("*.tmp"))
    before = files("run")

    # Resume: skips the two finished games, produces the rest.
    resumed = FakeOllama()
    with use_clients(ollama=resumed):
        r = generate(4, "generic", "run", seed=0, retry_delays=(), log=quiet)
    assert r == {"done": 4, "skipped": 2, "generated": 2}
    assert len(resumed.calls) == calls_per_game[2] + calls_per_game[3]
    after = files("run")
    assert {k: after[k] for k in before} == before  # finished games untouched
    got = {k: strip_volatile(v) for k, v in after.items()}
    for d in list(got.values()) + list(ref.values()):
        d["source"] = "x"
    assert got == ref


def test_invalid_file_is_regenerated(fake_clients):
    generate(1, "generic", "bad", seed=0, retry_delays=(), log=quiet)
    p = transcripts_dir("bad") / "g0000.json"
    p.write_text("{not json", encoding="utf-8")
    r = generate(1, "generic", "bad", seed=0, retry_delays=(), log=quiet)
    assert r["generated"] == 1
    load(p)


def test_transient_failures_are_retried():
    class Flaky(FakeOllama):
        def chat(self, **kw):
            if not getattr(self, "failed", False):
                self.failed = True
                raise ConnectionError("blip")
            return super().chat(**kw)

    with use_clients(ollama=Flaky()):
        r = generate(1, "generic", "flaky", seed=0, retry_delays=(0.0,), log=quiet)
    assert r["generated"] == 1


def test_cli(fake_clients, capsys):
    assert main(["--n", "2", "--persona", "generic", "--out-source", "cli_test", "--seed", "0"]) == 0
    assert len(iter_source(transcripts_dir("cli_test"))) == 2
    assert "ETA" in capsys.readouterr().out


def test_runs_under_gpu_lock(fake_clients, monkeypatch):
    import ww.transcripts.generate as gen

    entered = []

    class Lock:
        def __enter__(self):
            entered.append(1)

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(gen, "gpu_lock", lambda: Lock())
    generate(2, "generic", "lock_test", seed=0, retry_delays=(), log=quiet)
    assert len(entered) == 2
