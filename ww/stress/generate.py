"""Stronger liars: generate games where the wolves use a stronger model (Wave 4 I).

    python -m ww.stress.generate --n 100 --out-source llm_gptoss20b_wolves --wolf-model gpt-oss:20b

- Same setup as the Wave 2 generator (9 players, 2 wolves, one Seer, generic
  persona, random day votes and night kills, one defense a day), and game i uses
  the generator's seed for game i, so game i has the same seats and roles as
  `llm_llama31_8b/g{i:04d}` (a matched design). Only the wolves' model changes;
  villagers and the Seer stay on llama3.1:8b.
- gpt-oss is a reasoning model. Ollama returns its reasoning in
  `message.thinking` and the spoken answer in `message.content`; only `content`
  is used. `--wolf-think` sets the reasoning effort ("low" by default, for speed).
  An empty `content` is retried with a new seed.
- On an 8 GB GPU, gpt-oss:20b (13 GB) and llama3.1:8b can't both sit in VRAM,
  and swapping them costs ~25 s per call. `--wolf-num-gpu 0` (default) keeps
  gpt-oss entirely on the CPU, so both stay loaded and nothing is swapped.
- Typographic punctuation (curly quotes, dashes, ellipses, non-breaking spaces)
  is converted to ASCII in every player's text, because gpt-oss uses it and
  llama mostly doesn't: left in, it would label the wolves.
- One file per game, resumable, each game under gpu_lock().
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any

from ww.clients import get_ollama
from ww.config import (
    GEN_MAX_ROUNDS,
    GEN_NUM_PLAYERS,
    GEN_NUM_WEREWOLVES,
    GEN_TEMPERATURE,
    OLLAMA_MODEL,
    WEREWOLF_PERSONAS,
    transcripts_dir,
)
from ww.game.werewolf import SEER_PERSONA, SPEAK_RULES, VILLAGER_PERSONA, Game, GameConfig, Player, RandomDayPolicy
from ww.gpu import gpu_lock
from ww.transcripts.generate import DEFAULT_RETRY_DELAYS, _is_done, game_id, game_seed
from ww.transcripts.schema import Transcript, save

STRESS_VERSION = 1
MAX_EMPTY_RETRIES = 3

_TYPO = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": " - ", "‒": "-", "‑": "-", "‐": "-", "−": "-",
    "…": "...", " ": " ", " ": " ", " ": " ", "​": "",
}


def ascii_typography(text: str) -> str:
    for k, v in _TYPO.items():
        text = text.replace(k, v)
    return " ".join(text.split())


class MixedModelGame(Game):
    """Wolves speak with `wolf_model`; everyone else with config.model."""

    def __init__(self, *args: Any, wolf_model: str, wolf_think: str | None = "low",
                 wolf_num_gpu: int | None = 0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.wolf_model = wolf_model
        self.wolf_think = wolf_think
        self.wolf_num_gpu = wolf_num_gpu
        self._speaker: Player | None = None
        self.stats = {"wolf_calls": 0, "wolf_seconds": 0.0, "wolf_eval_tokens": 0, "wolf_empty_retries": 0,
                      "wolf_thinking_chars": 0, "other_calls": 0, "other_seconds": 0.0}

    # Remember who is about to speak, so chat() can pick the model.
    def statement_prompt(self, player: Player, today: list) -> list[dict]:
        self._speaker = player
        return super().statement_prompt(player, today)

    def defense_prompt(self, player: Player, reason: str) -> list[dict]:
        self._speaker = player
        return super().defense_prompt(player, reason)

    def night_kill_prompt(self, wolf: Player, targets: list[str]) -> list[dict]:
        self._speaker = wolf
        return super().night_kill_prompt(wolf, targets)

    def _once(self, model: str, messages: list[dict], wolf: bool) -> Any:
        options: dict[str, Any] = {"num_ctx": self.config.num_ctx}
        if self.config.temperature is not None:
            options.update(temperature=self.config.temperature, seed=self.rng.getrandbits(31))
        kwargs: dict[str, Any] = {}
        if wolf:
            if self.wolf_num_gpu is not None:
                options["num_gpu"] = self.wolf_num_gpu
            if self.wolf_think is not None:
                kwargs["think"] = self.wolf_think
        delays = list(self.chat_retry_delays)
        while True:
            try:
                return get_ollama().chat(model=model, messages=messages, options=options, **kwargs)
            except Exception:
                if not delays:
                    raise
                time.sleep(delays.pop(0))

    def chat(self, messages: list[dict]) -> str:
        wolf = self._speaker is not None and self._speaker.is_wolf
        model = self.wolf_model if wolf else self.config.model
        t0 = time.monotonic()
        for attempt in range(MAX_EMPTY_RETRIES + 1):
            resp = self._once(model, messages, wolf)
            self.n_model_calls += 1
            content = ascii_typography(resp.message.content or "")
            if content or not wolf:
                break
            self.stats["wolf_empty_retries"] += 1
        else:
            raise RuntimeError(f"{model} returned empty content {MAX_EMPTY_RETRIES + 1} times")
        dt = time.monotonic() - t0
        if wolf:
            self.stats["wolf_calls"] += 1
            self.stats["wolf_seconds"] += dt
            self.stats["wolf_eval_tokens"] += int(getattr(resp, "eval_count", 0) or 0)
            self.stats["wolf_thinking_chars"] += len(getattr(resp.message, "thinking", None) or "")
        else:
            self.stats["other_calls"] += 1
            self.stats["other_seconds"] += dt
        return content


def generate_one(index: int, base_seed: int, source: str, wolf_model: str, wolf_think: str | None,
                 wolf_num_gpu: int | None, retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS) -> Transcript:
    config = GameConfig(num_players=GEN_NUM_PLAYERS, num_werewolves=GEN_NUM_WEREWOLVES, persona="generic",
                        model=OLLAMA_MODEL, temperature=GEN_TEMPERATURE, max_rounds=GEN_MAX_ROUNDS)
    seed = game_seed(base_seed, index)
    game = MixedModelGame(config, rng=random.Random(seed), chat_retry_delays=retry_delays,
                          wolf_model=wolf_model, wolf_think=wolf_think, wolf_num_gpu=wolf_num_gpu)
    start = time.monotonic()
    game.play(RandomDayPolicy())
    elapsed = time.monotonic() - start
    stats = dict(game.stats)
    stats["wolf_seconds"] = round(stats["wolf_seconds"], 2)
    stats["other_seconds"] = round(stats["other_seconds"], 2)
    meta = {
        "generator": "ww.stress.generate",
        "stress_version": STRESS_VERSION,
        "generator_model": config.model,
        "wolf_model": wolf_model,
        "wolf_think": wolf_think,
        "wolf_num_gpu": wolf_num_gpu,
        "persona_variant": config.persona,
        "seed": base_seed,
        "game_index": index,
        "game_seed": seed,
        "config": config.to_dict(),
        "day_policy": "random",
        "defense_selection": {str(k): v for k, v in game.defense_selection.items()},
        "prompts": {"werewolf_persona": WEREWOLF_PERSONAS[config.persona], "villager_persona": VILLAGER_PERSONA,
                    "seer_persona": SEER_PERSONA, "speak_rules": SPEAK_RULES},
        "text_postprocess": "clean_text + ascii_typography (all players)",
        "n_model_calls": game.n_model_calls,
        "model_stats": stats,
        "gen_seconds": round(elapsed, 2),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return game.transcript(game_id=game_id(index), source=source, meta=meta)


def generate(n: int, out_source: str, wolf_model: str, wolf_think: str | None = "low", wolf_num_gpu: int | None = 0,
             seed: int = 0, start: int = 0, retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
             log=print) -> dict[str, int]:
    out = transcripts_dir(out_source)
    out.mkdir(parents=True, exist_ok=True)
    todo = [i for i in range(start, n) if not _is_done(out / f"{game_id(i)}.json")]
    skipped = (n - start) - len(todo)
    log(f"[stress] {out_source}: {n - start} games requested, {skipped} done, {len(todo)} to generate "
        f"(wolves={wolf_model} think={wolf_think} num_gpu={wolf_num_gpu})")
    times: list[float] = []
    for k, i in enumerate(todo, 1):
        g0 = time.monotonic()
        with gpu_lock():
            t = generate_one(i, seed, out_source, wolf_model, wolf_think, wolf_num_gpu, retry_delays)
        save(t, out / f"{game_id(i)}.json")
        times.append(time.monotonic() - g0)
        mean = sum(times) / len(times)
        eta = mean * (len(todo) - k)
        s = t.meta["model_stats"]
        log(f"[stress] {out_source} {t.game_id} ({k}/{len(todo)}) {times[-1]:.0f}s "
            f"(wolves {s['wolf_calls']} calls {s['wolf_seconds']:.0f}s, others {s['other_calls']} calls "
            f"{s['other_seconds']:.0f}s, empty retries {s['wolf_empty_retries']}), "
            f"{sum(1 for r in t.rounds if r.statements)} days, winner={t.winner}; mean {mean:.0f}s/game, "
            f"ETA ~{time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() + eta))}")
    return {"done": skipped + len(todo), "skipped": skipped, "generated": len(todo)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--out-source", required=True)
    ap.add_argument("--wolf-model", required=True)
    ap.add_argument("--wolf-think", default="low", help="reasoning effort for the wolf model; 'none' to omit")
    ap.add_argument("--wolf-num-gpu", type=int, default=0, help="GPU layers for the wolf model; -1 to omit")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    a = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    think = None if a.wolf_think == "none" else a.wolf_think
    num_gpu = None if a.wolf_num_gpu < 0 else a.wolf_num_gpu
    result = generate(a.n, a.out_source, a.wolf_model, think, num_gpu, a.seed, a.start, log=log)
    log(f"[stress] result {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
