"""llama_judge: the local Ollama model rates each current speaker's suspicion
0-10 given the whole public view.

GPU: this judge sends one Ollama request per view (plus retries). The caller
must hold gpu_lock() around a run; `python -m ww.judges.run` does that.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ww.clients import get_ollama
from ww.config import OLLAMA_MODEL
from ww.judges.base import JudgeResult
from ww.judges.common import clamp01, view_as_text
from ww.transcripts.schema import current_speakers

SYSTEM_PROMPT = (
    "You are an expert observer of social deduction games such as Werewolf and Mafia. "
    "In these games some players are secretly on a deceptive team and lie to hide it; "
    "everyone else is honest. You read the public transcript and judge who is deceptive."
)

USER_TEMPLATE = """Here is the public transcript of the game so far.

{transcript}

For each of these players: {players}
rate how likely it is that the player is on the deceptive team, from 0 (certainly honest)
to 10 (certainly deceptive). Base it only on the transcript.

Reply with ONLY a JSON object mapping each player id to an integer from 0 to 10, e.g.
{example}"""

RETRY_MESSAGE = (
    "I could not read ratings for: {missing}. Reply with ONLY a JSON object mapping every one "
    "of these player ids to an integer from 0 to 10: {players}"
)

_VALUE_KEYS = ("score", "rating", "suspicion", "value", "deceptive", "probability")


def _to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v)
        return float(m.group()) if m else None
    if isinstance(v, dict):
        for k in _VALUE_KEYS:
            for kk, vv in v.items():
                if kk.lower() == k:
                    return _to_number(vv)
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]", "", str(s)).lower()


def _json_objects(text: str) -> list[Any]:
    """Every JSON value we can pull out of the text: the whole text, fenced
    blocks, then each balanced {...} span."""
    found: list[Any] = []
    candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : i + 1])
    for c in candidates:
        try:
            found.append(json.loads(c))
        except (ValueError, TypeError):
            continue
    return found


def parse_ratings(text: str, players: list[str]) -> dict[str, float]:
    """Pull a 0-10 rating per player out of a model reply. Returns only the
    players it could read (values clamped to [0, 10]). Tries JSON first (flat
    dicts, nested {"ratings": {...}}, lists of {"player": .., "score": ..}),
    then 'PlayerX: 7'-style text."""
    by_norm = {_norm(p): p for p in players}
    out: dict[str, float] = {}

    def take(pid_like: Any, value: Any) -> None:
        pid = by_norm.get(_norm(pid_like))
        num = _to_number(value)
        if pid is not None and num is not None and pid not in out:
            out[pid] = max(0.0, min(10.0, num))

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _norm(k) in by_norm:
                    take(k, v)
                elif isinstance(v, (dict, list)):
                    walk(v)
            # {"player": "Player1", "score": 7}
            pk = next((k for k in obj if k.lower() in ("player", "player_id", "id", "name")), None)
            if pk is not None and isinstance(obj[pk], str):
                take(obj[pk], {k: v for k, v in obj.items() if k != pk})
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for obj in _json_objects(text):
        walk(obj)
    for p in players:
        if p in out:
            continue
        m = re.search(re.escape(p) + r"(?![\w])[\"'*\s]*(?:[:=\-–]|is|rated|gets)?[\"'*\s]*(-?\d+(?:\.\d+)?)",
                      text, re.IGNORECASE)
        if m:
            out[p] = max(0.0, min(10.0, float(m.group(1))))
    return out


class LlamaJudge:
    name = "llama_judge"

    def __init__(self, model: str = OLLAMA_MODEL, include_defense: bool = True, temperature: float = 0.0,
                 seed: int = 0, max_retries: int = 1, json_mode: bool = True):
        self.config = {"version": 1, "model": model, "include_defense": include_defense,
                       "temperature": temperature, "seed": seed, "max_retries": max_retries,
                       "json_mode": json_mode, "prompt_sha": _prompt_sha()}

    def score(self, view: dict[str, Any]) -> JudgeResult:
        start = time.perf_counter()
        players = current_speakers(view)
        c = self.config
        example = json.dumps({p: 5 for p in players[:2]} | ({"...": "..."} if len(players) > 2 else {}))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(
                transcript=view_as_text(view, c["include_defense"]), players=", ".join(players), example=example)},
        ]
        kwargs: dict[str, Any] = {"options": {"temperature": c["temperature"], "seed": c["seed"]}}
        if c["json_mode"]:
            kwargs["format"] = "json"

        ratings: dict[str, float] = {}
        replies: list[str] = []
        tokens = {"prompt": 0, "completion": 0}
        for attempt in range(1 + c["max_retries"]):
            resp = get_ollama().chat(model=c["model"], messages=messages, **kwargs)
            text = resp["message"]["content"]
            replies.append(text)
            tokens["prompt"] += int(getattr(resp, "prompt_eval_count", 0) or 0)
            tokens["completion"] += int(getattr(resp, "eval_count", 0) or 0)
            for pid, v in parse_ratings(text, players).items():
                ratings.setdefault(pid, v)
            missing = [p for p in players if p not in ratings]
            if not missing:
                break
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": RETRY_MESSAGE.format(missing=", ".join(missing), players=", ".join(players))},
            ]

        missing = [p for p in players if p not in ratings]
        p = {pid: clamp01(ratings[pid] / 10.0) if pid in ratings else 0.5 for pid in players}
        raw = {"ratings": ratings, "parse_failures": missing, "attempts": len(replies), "replies": replies}
        return JudgeResult(
            p_deceptive=p,
            meta={"latency_s": time.perf_counter() - start, "cost_usd": 0.0, "tokens": tokens,
                  "parse_ok": not missing, "raw": raw},
        )


def _prompt_sha() -> str:
    import hashlib

    return hashlib.sha256((SYSTEM_PROMPT + USER_TEMPLATE + RETRY_MESSAGE).encode()).hexdigest()[:12]
