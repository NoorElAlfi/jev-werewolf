"""frontier: a frontier Claude model judges the whole public view (Wave 4 G).

One Messages API call per view. The model reads the same text transcript as
llama_judge (`view_as_text`) and returns, as structured output (JSON schema via
`output_config.format`), a p_deceptive and a confidence in [0, 1] for every
current speaker. Adaptive thinking is on (the default for claude-opus-5) and
`output_config.effort` sets its depth. Server-side refusal fallbacks are
enabled (`fallbacks: "default"`); the model that actually answered is stored
in meta.raw.model, and a final refusal raises FrontierError.

The Anthropic client comes from ww.clients, so tests can inject a fake:

    with use_anthropic(lambda: FakeAnthropic()):
        ...

CLI (cost-gated like ww.judges.run; windowed to 90k chars like jev_batched and
llama_judge, so it scores exactly the same views):

    python -m ww.judges.frontier --sources llm_llama31_8b --limit 2
    python -m ww.judges.frontier --sources human_mafia,llm_mafia,avalon,werewolf_among_us --plan-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from ww.clients import make_anthropic, use_clients
from ww.config import (
    FRONTIER_CHARS_PER_TOKEN,
    FRONTIER_EFFORT,
    FRONTIER_EST_OUTPUT_TOKENS,
    FRONTIER_MAX_TOKENS,
    FRONTIER_MODEL,
    FRONTIER_PRICES_USD_PER_MTOK,
    FRONTIER_TOKENS_PER_CALL,
    transcripts_dir,
)
from ww.judges.base import Judge, JudgmentCache, JudgeResult, key_of, rounds_to_judge
from ww.judges.common import clamp01, view_as_text
from ww.transcripts.schema import current_speakers, iter_source, load, public_view

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
COST_GATE_CALLS = 50
DEFAULT_WINDOW_CHARS = 90_000  # same window as jev_batched / llama_judge in Waves 3 E/F

SYSTEM_PROMPT = (
    "You are an expert observer of social deduction games such as Werewolf, Mafia and Avalon. "
    "In these games some players are secretly on a deceptive team and lie to hide it; everyone "
    "else is honest. You read the public transcript and judge who is deceptive. Base your "
    "judgment only on the transcript."
)

USER_TEMPLATE = """Here is the public transcript of the game so far.

{transcript}

For each of these players: {players}
give
- p_deceptive: the probability (0 to 1) that the player is on the deceptive team;
- confidence: how confident you are in that estimate (0 = a pure guess, 1 = certain).

Return one entry per listed player, using the exact player ids."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "players": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "string"},
                    "p_deceptive": {"type": "number"},
                    "confidence": {"type": "number"},
                },
                "required": ["player_id", "p_deceptive", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["players"],
    "additionalProperties": False,
}


class FrontierError(RuntimeError):
    """The API returned no usable answer (refusal after fallbacks, max_tokens, no text)."""


# ---------------------------------------------------------------------------
# Injectable client (lives in ww.clients; use_anthropic is kept as a shorthand)
# ---------------------------------------------------------------------------


def use_anthropic(factory: Callable[[], Any]):
    """Temporarily replace the Anthropic client factory (tests inject a fake)."""
    return use_clients(anthropic=factory)


def price_of(model: str) -> tuple[float, float]:
    for k, v in FRONTIER_PRICES_USD_PER_MTOK.items():
        if model == k or model.startswith(k):
            return v
    return FRONTIER_PRICES_USD_PER_MTOK[FRONTIER_MODEL]


def cost_usd(model: str, input_tokens: float, output_tokens: float) -> float:
    pin, pout = price_of(model)
    return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout


def _prompt_sha() -> str:
    blob = SYSTEM_PROMPT + USER_TEMPLATE + json.dumps(OUTPUT_SCHEMA, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def parse_answer(data: Any, players: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    """(p, confidence) for the listed players found in the structured output;
    values clamped to [0, 1]. A value in (1.5, 100] is read as a percentage."""
    p, conf = {}, {}
    wanted = {pid.lower(): pid for pid in players}
    for item in (data or {}).get("players", []) if isinstance(data, dict) else []:
        pid = wanted.get(str(item.get("player_id", "")).strip().lower())
        if pid is None or pid in p:
            continue
        try:
            pv, cv = float(item["p_deceptive"]), float(item["confidence"])
        except (KeyError, TypeError, ValueError):
            continue
        p[pid] = clamp01(pv / 100 if 1.5 < pv <= 100 else pv)
        conf[pid] = clamp01(cv / 100 if 1.5 < cv <= 100 else cv)
    return p, conf


class FrontierJudge:
    name = "frontier"

    def __init__(self, model: str = FRONTIER_MODEL, effort: str = FRONTIER_EFFORT, include_defense: bool = True,
                 max_tokens: int = FRONTIER_MAX_TOKENS, fallbacks: bool = True):
        self.config = {"version": 1, "model": model, "effort": effort, "thinking": "adaptive",
                       "include_defense": include_defense, "max_tokens": max_tokens,
                       "fallbacks": "default" if fallbacks else None, "prompt_sha": _prompt_sha()}

    def messages_for(self, view: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
        players = current_speakers(view)
        user = USER_TEMPLATE.format(transcript=view_as_text(view, self.config["include_defense"]),
                                    players=", ".join(players))
        return SYSTEM_PROMPT, [{"role": "user", "content": user}], players

    def estimate(self, view: dict[str, Any]) -> dict[str, float]:
        """Offline estimate for one view (no API call): calls and tokens."""
        system, messages, players = self.messages_for(view)
        if not players:
            return {"calls": 0, "input_tokens": 0.0, "output_tokens": 0.0}
        chars = len(system) + len(messages[0]["content"]) + len(json.dumps(OUTPUT_SCHEMA))
        return {"calls": 1, "input_tokens": FRONTIER_TOKENS_PER_CALL + chars / FRONTIER_CHARS_PER_TOKEN,
                "output_tokens": float(FRONTIER_EST_OUTPUT_TOKENS)}

    def request_kwargs(self, view: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        system, messages, players = self.messages_for(view)
        c = self.config
        kw: dict[str, Any] = {
            "model": c["model"],
            "max_tokens": c["max_tokens"],
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": c["effort"], "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        }
        if c["fallbacks"]:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = c["fallbacks"]
        return kw, players

    def score(self, view: dict[str, Any]) -> JudgeResult:
        start = time.perf_counter()
        kw, players = self.request_kwargs(view)
        if not players:
            return JudgeResult(p_deceptive={}, confidence={}, meta={"latency_s": 0.0, "cost_usd": 0.0})
        client = make_anthropic()
        resp = client.beta.messages.create(**kw)
        latency = time.perf_counter() - start

        stop = _get(resp, "stop_reason")
        model = _get(resp, "model") or self.config["model"]
        usage = _get(resp, "usage")
        in_tok = int(_get(usage, "input_tokens", 0) or 0)
        cache_w = int(_get(usage, "cache_creation_input_tokens", 0) or 0)
        cache_r = int(_get(usage, "cache_read_input_tokens", 0) or 0)
        out_tok = int(_get(usage, "output_tokens", 0) or 0)
        pin, _ = price_of(model)
        cost = cost_usd(model, in_tok, out_tok) + cache_w / 1e6 * pin * 1.25 + cache_r / 1e6 * pin * 0.1
        request_id = _get(resp, "_request_id")
        if stop == "refusal":
            details = _get(resp, "stop_details")
            raise FrontierError(f"refusal (category={_get(details, 'category')}, request_id={request_id})")
        if stop == "max_tokens":
            raise FrontierError(f"hit max_tokens={self.config['max_tokens']} (request_id={request_id})")
        text = next((_get(b, "text") for b in (_get(resp, "content") or []) if _get(b, "type") == "text"), None)
        if text is None:
            raise FrontierError(f"no text block (stop_reason={stop}, request_id={request_id})")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        p, conf = parse_answer(data, players)
        missing = [pid for pid in players if pid not in p]
        for pid in missing:  # recorded, like llama_judge's parse failures
            p[pid] = 0.5
            conf[pid] = 0.0
        fell_back = any(_get(b, "type") == "fallback" for b in (_get(resp, "content") or []))
        raw = {"model": model, "stop_reason": stop, "request_id": request_id, "text": text,
               "parse_failures": missing, "fell_back": fell_back}
        return JudgeResult(
            p_deceptive=p,
            confidence=conf,
            meta={"latency_s": latency, "cost_usd": cost, "parse_ok": not missing,
                  "tokens": {"input": in_tok, "output": out_tok, "cache_write": cache_w, "cache_read": cache_r},
                  "raw": raw},
        )


def make_frontier(max_chars: int | None = DEFAULT_WINDOW_CHARS, **kwargs: Any) -> Judge:
    judge: Judge = FrontierJudge(**kwargs)
    if max_chars:
        from ww.judges.window import WindowedJudge

        judge = WindowedJudge(judge, max_chars)
    return judge


def plan_frontier(judge: Judge, source: str, limit: int | None = None) -> dict[str, Any]:
    """Offline plan for one source: views, calls, tokens, est. cost; total and not yet cached."""
    cache = JudgmentCache(judge)
    files = iter_source(transcripts_dir(source))[: limit if limit else None]
    model = judge.config["model"]
    tot = {"games": len(files), "views": 0, "calls": 0, "input_tokens": 0.0, "output_tokens": 0.0}
    new = {"views": 0, "calls": 0, "input_tokens": 0.0, "output_tokens": 0.0}
    for f in files:
        t = load(f)
        complete = cache.is_complete(source, t.game_id)
        for rn in rounds_to_judge(t):
            e = judge.estimate(public_view(t, rn))  # type: ignore[attr-defined]
            cached = complete or cache.get_round(source, t.game_id, rn) is not None
            for d in (tot,) if cached else (tot, new):
                d["views"] += 1
                d["calls"] += e["calls"]
                d["input_tokens"] += e["input_tokens"]
                d["output_tokens"] += e.get("output_tokens", 0.0)
    for d in (tot, new):
        d["est_cost_usd"] = cost_usd(model, d["input_tokens"], d["output_tokens"])
    return {"judge_key": key_of(judge), "source": source, "total": tot, "new": new}


def run_parallel(judge: Judge, source: str, limit: int | None = None, workers: int = 8) -> dict[str, int]:
    """run_judge(), but games are scored concurrently (rounds within a game stay
    in order, and each game has its own cache file, so writes never collide).
    Same cache format and resume behavior as run_judge."""
    from concurrent.futures import ThreadPoolExecutor

    cache = JudgmentCache(judge)
    cache.write_config()
    files = iter_source(transcripts_dir(source))[: limit if limit else None]

    def one(f: Any) -> tuple[int, int]:
        t = load(f)
        rounds = rounds_to_judge(t)
        if cache.is_complete(source, t.game_id):
            return 0, len(rounds)
        scored = cached = 0
        for i, rn in enumerate(rounds):
            if cache.get_round(source, t.game_id, rn) is not None:
                cached += 1
                continue
            view = public_view(t, upto_round=rn)
            t0 = time.perf_counter()
            result = judge.score(view)
            result.meta.setdefault("latency_s", time.perf_counter() - t0)
            result.check()
            cache.put_round(source, t.game_id, rn, result, complete=(i == len(rounds) - 1))
            scored += 1
        cache.mark_complete(source, t.game_id)
        log.info("%s: %s/%s done", cache.key, source, t.game_id)
        return scored, cached

    counts = {"games": len(files), "scored": 0, "cached": 0}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for s_, c_ in ex.map(one, files):
            counts["scored"] += s_
            counts["cached"] += c_
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", required=True, help="comma-separated sources")
    ap.add_argument("--limit", type=int, default=None, help="only the first N games of each source")
    ap.add_argument("--model", default=FRONTIER_MODEL)
    ap.add_argument("--effort", default=FRONTIER_EFFORT)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_WINDOW_CHARS, help="view window (0 = none)")
    ap.add_argument("--no-defense", action="store_true")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--yes", action="store_true", help=f"allow more than {COST_GATE_CALLS} new calls")
    ap.add_argument("--workers", type=int, default=8, help="games scored concurrently")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    judge = make_frontier(a.max_chars or None, model=a.model, effort=a.effort, include_defense=not a.no_defense)
    sources = [s for s in a.sources.split(",") if s]
    plans = [plan_frontier(judge, s, a.limit) for s in sources]
    new_calls = sum(p["new"]["calls"] for p in plans)
    print(f"judge_key: {key_of(judge)}  (model {a.model}, effort {a.effort}; est. assumes "
          f"{FRONTIER_EST_OUTPUT_TOKENS} output tokens/call)")
    for p in plans:
        t, n = p["total"], p["new"]
        print(f"{p['source']}: {t['games']} games, {t['views']} views, {t['calls']} calls, "
              f"~{t['input_tokens']:.0f} in / ~{t['output_tokens']:.0f} out tokens, est. ${t['est_cost_usd']:.2f}; "
              f"not cached: {n['calls']} calls, est. ${n['est_cost_usd']:.2f}")
    print(f"not cached, all sources: {new_calls} calls, est. ${sum(p['new']['est_cost_usd'] for p in plans):.2f}",
          flush=True)
    if a.plan_only:
        return 0
    if new_calls > COST_GATE_CALLS and not a.yes:
        print(f"STOP: {new_calls} new paid calls > {COST_GATE_CALLS}. Get approval, then re-run with --yes.",
              file=sys.stderr)
        return 2
    for s in sources:
        counts = run_parallel(judge, s, limit=a.limit, workers=a.workers)
        print(f"{s} done: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
