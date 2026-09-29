"""Offline fakes for Ollama and TypeSafe. Inject with ww.clients.use_clients:

    with use_clients(ollama=FakeOllama(), typesafe=FakeTypeSafe):
        ...

or use the `fake_clients` fixture from tests/conftest.py.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from typesafe_sdk import SystemOneResponse

class FakeOllamaError(RuntimeError):
    pass


class FakeOllama:
    """Stands in for the `ollama` module / `ollama.Client`.

    responder(model, messages) -> str decides the reply; the default echoes the
    speaker id from the system prompt, so output is deterministic.
    fail_after=N raises FakeOllamaError on call N+1 (to test resuming).
    """

    def __init__(self, responder: Callable[[str, list[dict]], str] | None = None, fail_after: int | None = None):
        self.responder = responder or self._default
        self.fail_after = fail_after
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _default(model: str, messages: list[dict]) -> str:
        system = messages[0]["content"] if messages else ""
        who = system.split(",")[0].replace("You are ", "") if system.startswith("You are ") else "someone"
        return f"I am {who}. I have been paying attention and I want to find the wolves."

    def chat(self, model: str, messages: list[dict], **kwargs: Any) -> Any:
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise FakeOllamaError(f"fake failure after {self.fail_after} calls")
        self.calls.append({"model": model, "messages": messages, **kwargs})
        content = self.responder(model, messages)
        # ollama.ChatResponse supports both attribute and item access.
        return _Both(message=_Both(role="assistant", content=content), model=model, done=True)


class _Both(SimpleNamespace):
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class FakeTypeSafe:
    """Stands in for typesafe_sdk.TypeSafeClient. Pass the CLASS (or a
    lambda returning an instance) as the `typesafe` factory.

    Returns real SystemOneResponse objects, so code sees the true SDK types.
    Score levels are 0-based (level i = criteria[i]), as in the real API;
    `score` is the probability-weighted mean level and `confidence` is
    1 - normalized entropy of the probabilities.

    prob_fn(name, question, state) -> list[float] (Score) or float (Noul)
    or dict[label, float] (Choice) overrides the default, which is a
    deterministic pseudo-random draw seeded by the question name.
    """

    calls: list[dict[str, Any]] = []  # shared across instances; reset with FakeTypeSafe.reset()

    def __init__(self, *args: Any, prob_fn: Callable[..., Any] | None = None, **kwargs: Any):
        self.prob_fn = prob_fn

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    def __enter__(self) -> FakeTypeSafe:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        pass

    def system_one(self, state: Any, questions: dict[str, Any], **kwargs: Any) -> SystemOneResponse:
        FakeTypeSafe.calls.append({"state": state, "questions": questions, **kwargs})
        answers = {name: self._answer(name, q, state) for name, q in questions.items()}
        # Decode from JSON, like the real SDK does (strict models reject int-from-str keys otherwise).
        payload = {"model": "fake-jev", "usage": {"input_tokens": 100, "output_tokens": 10}, "answers": answers}
        return SystemOneResponse.model_validate_json(json.dumps(payload))

    def _answer(self, name: str, q: Any, state: Any) -> dict[str, Any]:
        qtype = getattr(q, "type", None) or q.get("type")
        rng = random.Random(name)
        if qtype == "score":
            criteria = list(getattr(q, "criteria", None) or q["criteria"])
            probs = self.prob_fn(name, q, state) if self.prob_fn else _normalize([rng.random() for _ in criteria])
            return {
                "type": "score",
                "score": sum(i * p for i, p in enumerate(probs)),
                "confidence": _confidence(probs),
                "legend": {str(i): c for i, c in enumerate(criteria)},
                "probabilities": {str(i): p for i, p in enumerate(probs)},
            }
        if qtype == "noul":
            p = self.prob_fn(name, q, state) if self.prob_fn else rng.random()
            return {"type": "noul", "noul": p}
        if qtype == "choice":
            labels = list((getattr(q, "criteria", None) or q["criteria"]).keys())
            probs = self.prob_fn(name, q, state) if self.prob_fn else dict(zip(labels, _normalize([rng.random() for _ in labels])))
            best = max(probs, key=probs.get)
            return {"type": "choice", "choice": best, "confidence": _confidence(list(probs.values())), "probabilities": probs}
        raise ValueError(f"FakeTypeSafe: unsupported question type {qtype!r}")


def _normalize(xs: list[float]) -> list[float]:
    s = sum(xs)
    return [x / s for x in xs]


def _confidence(probs: list[float]) -> float:
    if len(probs) < 2:
        return 1.0
    h = -sum(p * math.log(p) for p in probs if p > 0)
    return max(0.0, min(1.0, 1.0 - h / math.log(len(probs))))
