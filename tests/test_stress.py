"""Stronger-liar generator (ww.stress.generate) on the fake Ollama."""

import pytest

from tests.fakes import FakeOllama, FakeOllamaError, _Both
from ww.clients import use_clients
from ww.config import transcripts_dir
from ww.stress.generate import ascii_typography, generate
from ww.transcripts.generate import generate as base_generate
from ww.transcripts.schema import iter_source, load


def quiet(*a, **k):
    pass


class ThinkingOllama(FakeOllama):
    """Wolf-model replies carry `thinking` and curly quotes; the first reply of
    each wolf call can be empty."""

    def __init__(self, empty_first=False):
        super().__init__()
        self.empty_first = empty_first
        self._last_empty = False

    def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        who = messages[0]["content"].split(",")[0].replace("You are ", "")
        if model == "gpt-oss:20b":
            if self.empty_first and not self._last_empty:
                self._last_empty = True
                content = ""
            else:
                self._last_empty = False
                content = f"I’m {who} — just “listening”…"
            msg = _Both(role="assistant", content=content, thinking="We are the wolf. Hide it.")
        else:
            msg = _Both(role="assistant", content=f"I am {who}, a plain speaker.")
        return _Both(message=msg, model=model, done=True, eval_count=42)


def test_wolves_use_the_wolf_model_and_everyone_else_llama():
    fake = ThinkingOllama()
    with use_clients(ollama=fake):
        r = generate(2, "stress_test", "gpt-oss:20b", log=quiet, retry_delays=())
    assert r == {"done": 2, "skipped": 0, "generated": 2}
    for p in iter_source(transcripts_dir("stress_test")):
        t = load(p)
        wolves = {pl.id for pl in t.players if pl.team == "deceptive"}
        for rnd in t.rounds:
            for s in rnd.statements + ([rnd.defense] if rnd.defense else []):
                if s.player_id in wolves:
                    assert s.text.startswith(f"I'm {s.player_id} - just \"listening\"...")
                    assert "We are the wolf" not in s.text  # thinking is never used
                else:
                    assert s.text == f"I am {s.player_id}, a plain speaker."
        m = t.meta
        assert m["wolf_model"] == "gpt-oss:20b" and m["generator_model"] == "llama3.1:8b"
        assert m["model_stats"]["wolf_calls"] > 0 and m["model_stats"]["other_calls"] > 0
    for c in fake.calls:
        if c["model"] == "gpt-oss:20b":
            assert c["think"] == "low" and c["options"]["num_gpu"] == 0
        else:
            assert "think" not in c and "num_gpu" not in c["options"]


def test_empty_wolf_content_is_retried():
    with use_clients(ollama=ThinkingOllama(empty_first=True)):
        generate(1, "stress_empty", "gpt-oss:20b", log=quiet, retry_delays=())
    t = load(transcripts_dir("stress_empty") / "g0000.json")
    assert t.meta["model_stats"]["wolf_empty_retries"] == t.meta["model_stats"]["wolf_calls"]
    assert all(s.text for r in t.rounds for s in r.statements)


def test_same_roles_as_generator_and_resume(fake_clients):
    base_generate(3, "generic", "llm_ref", seed=0, retry_delays=(), log=quiet)
    with use_clients(ollama=ThinkingOllama()):
        generate(3, "stress_r", "gpt-oss:20b", log=quiet, retry_delays=())
        assert generate(3, "stress_r", "gpt-oss:20b", log=quiet)["generated"] == 0
    for i in range(3):
        a = load(transcripts_dir("llm_ref") / f"g000{i}.json")
        b = load(transcripts_dir("stress_r") / f"g000{i}.json")
        assert [(p.id, p.role) for p in a.players] == [(p.id, p.role) for p in b.players]


def test_crash_leaves_no_partial_game():
    with use_clients(ollama=FakeOllama(fail_after=30)):
        with pytest.raises(FakeOllamaError):
            generate(3, "stress_crash", "gpt-oss:20b", log=quiet, retry_delays=())
    for p in iter_source(transcripts_dir("stress_crash")):
        load(p)


def test_ascii_typography():
    assert ascii_typography("It’s “fine” — ok… x") == 'It\'s "fine" - ok... x'


def test_stress_report_end_to_end(fake_clients, tmp_path):
    from ww.judges.base import run_judge
    from ww.live import run as live
    from ww.stress import report

    base_generate(2, "generic", "llm_llama31_8b", seed=0, retry_delays=(), log=quiet)
    with use_clients(ollama=ThinkingOllama()):
        generate(2, "llm_gptoss20b_wolves", "gpt-oss:20b", log=quiet, retry_delays=())
    judge = live.make_condition_judge(live.CONDITIONS["jev"])
    for src in ("llm_llama31_8b", "llm_gptoss20b_wolves"):
        run_judge(judge, src)
    live.run(["jev", "jev_adaptive"], 2, log=quiet, retry_delays=(), judge_retry_delays=())
    out = tmp_path / "stress.md"
    report.build(out)
    text = out.read_text(encoding="utf-8")
    assert "| jev_batched | 2 |" in text and "| random | 0 | not judged yet" in text
    assert "## 2. Adaptive wolves" in text and "| AUC |" in text
