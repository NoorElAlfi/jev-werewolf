"""Generate Werewolf transcripts with the local Ollama model (no judge involved).

    python -m ww.transcripts.generate --n 200 --persona generic --out-source llm_llama31_8b --seed 0

- Day eliminations are uniformly random (RandomDayPolicy), so the transcripts
  do not favor any judge. Each round records a defense from one randomly chosen
  accused player (someone named in another player's statement; else anyone).
- One file per game at $WW_DATA_DIR/transcripts/<out-source>/g0000.json, ...
  Games already on disk (and valid) are skipped, so re-running the same command
  resumes. Files are written atomically, so a crash never leaves half a game.
- Game i uses its own seed derived from (--seed, i): the same command always
  plays the same setups, whatever order or how many runs it takes.
- Each game runs under gpu_lock(), taken per game so other GPU jobs can get in
  between games.
- The full config (and prompts) go into each transcript's meta.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ww.config import (
    GEN_MAX_ROUNDS,
    GEN_NUM_PLAYERS,
    GEN_NUM_WEREWOLVES,
    GEN_TEMPERATURE,
    NIGHT_KILL_POLICY,
    OLLAMA_MODEL,
    REVEAL_ROLE_ON_DEATH,
    SEER_ENABLED,
    WEREWOLF_PERSONAS,
    transcripts_dir,
)
from ww.game.werewolf import SEER_PERSONA, SPEAK_RULES, VILLAGER_PERSONA, Game, GameConfig, RandomDayPolicy
from ww.gpu import gpu_lock
from ww.transcripts.schema import TranscriptValidationError, load, save

GENERATOR_VERSION = 2
DEFAULT_RETRY_DELAYS = (5.0, 30.0, 120.0)


def game_seed(base_seed: int, index: int) -> int:
    digest = hashlib.sha256(f"ww-gen:{base_seed}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def game_id(index: int) -> str:
    return f"g{index:04d}"


def _is_done(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        load(path)
        return True
    except (TranscriptValidationError, ValueError, OSError):
        return False


def generate_one(config: GameConfig, index: int, base_seed: int, source: str,
                 retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS) -> Any:
    seed = game_seed(base_seed, index)
    game = Game(config, rng=random.Random(seed), chat_retry_delays=retry_delays)
    start = time.monotonic()
    game.play(RandomDayPolicy())
    elapsed = time.monotonic() - start
    meta = {
        "generator": "ww.transcripts.generate",
        "generator_version": GENERATOR_VERSION,
        "generator_model": config.model,
        "persona_variant": config.persona,
        "seed": base_seed,
        "game_index": index,
        "game_seed": seed,
        "config": config.to_dict(),
        "day_policy": "random",
        "defense_policy": "random accused: a player named by another player today, else any living player",
        "defense_selection": {str(k): v for k, v in game.defense_selection.items()},
        "prompts": {
            "werewolf_persona": WEREWOLF_PERSONAS[config.persona],
            "villager_persona": VILLAGER_PERSONA,
            "seer_persona": SEER_PERSONA,
            "speak_rules": SPEAK_RULES,
        },
        "n_model_calls": game.n_model_calls,
        "gen_seconds": round(elapsed, 2),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return game.transcript(game_id=game_id(index), source=source, meta=meta)


def generate(n: int, persona: str, out_source: str, seed: int = 0, *, start: int = 0,
             num_players: int = GEN_NUM_PLAYERS, num_werewolves: int = GEN_NUM_WEREWOLVES,
             night_policy: str = NIGHT_KILL_POLICY, reveal_role_on_death: bool = REVEAL_ROLE_ON_DEATH,
             seer: bool = SEER_ENABLED, model: str = OLLAMA_MODEL, temperature: float = GEN_TEMPERATURE,
             max_rounds: int = GEN_MAX_ROUNDS, retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
             log=print) -> dict[str, int]:
    """Generate games start..n-1 into $WW_DATA_DIR/transcripts/<out_source>/,
    skipping ones already done. Returns {"done", "skipped", "generated"}.
    Raises if Ollama keeps failing (after retries); re-run to resume."""
    config = GameConfig(num_players=num_players, num_werewolves=num_werewolves, persona=persona, seer=seer,
                        night_policy=night_policy, reveal_role_on_death=reveal_role_on_death, model=model,
                        temperature=temperature, max_rounds=max_rounds)
    out = transcripts_dir(out_source)
    out.mkdir(parents=True, exist_ok=True)
    todo = [i for i in range(start, n) if not _is_done(out / f"{game_id(i)}.json")]
    skipped = (n - start) - len(todo)
    log(f"[gen] {out_source}: {n - start} games requested, {skipped} already done, {len(todo)} to generate "
        f"(persona={persona}, seed={seed}, model={model})")
    times: list[float] = []
    t0 = time.monotonic()
    for k, i in enumerate(todo, 1):
        g_start = time.monotonic()
        with gpu_lock():
            t = generate_one(config, i, seed, out_source, retry_delays=retry_delays)
        save(t, out / f"{game_id(i)}.json")
        times.append(time.monotonic() - g_start)
        mean = sum(times) / len(times)
        eta = mean * (len(todo) - k)
        n_rounds = sum(1 for r in t.rounds if r.statements)
        log(f"[gen] {out_source} {game_id(i)} ({k}/{len(todo)}) {times[-1]:.0f}s, {n_rounds} day rounds, "
            f"{t.meta['n_model_calls']} calls, winner={t.winner}; mean {mean:.0f}s/game, "
            f"ETA {eta / 60:.0f} min (~{time.strftime('%Y-%m-%d %H:%M', time.localtime(time.time() + eta))})")
    log(f"[gen] {out_source}: finished {len(todo)} games in {(time.monotonic() - t0) / 60:.1f} min")
    return {"done": skipped + len(todo), "skipped": skipped, "generated": len(todo)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, required=True, help="number of games (indices 0..n-1)")
    ap.add_argument("--persona", choices=sorted(WEREWOLF_PERSONAS), required=True)
    ap.add_argument("--out-source", required=True, help="folder name under $WW_DATA_DIR/transcripts/")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--start", type=int, default=0, help="first game index (default 0)")
    ap.add_argument("--players", type=int, default=GEN_NUM_PLAYERS)
    ap.add_argument("--wolves", type=int, default=GEN_NUM_WEREWOLVES)
    ap.add_argument("--night-policy", choices=["random", "llm"], default=NIGHT_KILL_POLICY)
    ap.add_argument("--no-reveal", action="store_true", help="don't announce roles on death")
    ap.add_argument("--no-seer", action="store_true")
    ap.add_argument("--model", default=OLLAMA_MODEL)
    ap.add_argument("--temperature", type=float, default=GEN_TEMPERATURE)
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

    result = generate(args.n, args.persona, args.out_source, args.seed, start=args.start,
                      num_players=args.players, num_werewolves=args.wolves, night_policy=args.night_policy,
                      reveal_role_on_death=not args.no_reveal, seer=not args.no_seer, model=args.model,
                      temperature=args.temperature, log=log)
    log(f"[gen] result {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
