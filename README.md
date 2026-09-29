# Jev Werewolf Experiment

Can **Jev** ([TypeSafe](https://docs.typesafe.ai)'s System One model) tell who is lying
in a social-deduction game? This repo runs the same Werewolf / Mafia transcripts past Jev
and past a set of baselines, and reports accuracy, calibration, cost and latency on both
LLM-generated and human game data.

**Short answer.** Jev is cheap, fast and reasonably well calibrated, and it finds
deception in _LLM_ games (AUC 0.61–0.71) where its baselines do not. It does **not** beat
chance on human Mafia, Avalon or One Night Werewolf games — nor does any other judge
here. A per-dimension Jev variant reaches AUC 0.71 on games written by llama3.1:8b, but
that turns out to be that model's wolf _style_ (vague and passive), not a general tell of
deception: the learned weights do not transfer across domains.

Everything below is copied from `reports/`; each report states the command that produced
it and the 95% CIs, which resample whole games.

## Judges compared

| Judge          | What it does                                                                      |
| -------------- | --------------------------------------------------------------------------------- |
| `random`       | uniform p; the chance baseline                                                    |
| `keyword`      | counts suspicious words in the player's own statements                            |
| `length`       | talk volume so far; checks whether a signal is just "said more"                   |
| `llama_judge`  | llama3.1:8b (local, Ollama) reads the whole public view                           |
| `jev_isolated` | Jev scores one player at a time, per round                                        |
| `jev_batched`  | one Jev call per round, all of that round's speakers at once                      |
| `jev_multidim` | five narrow Jev dimensions per player, combined by a fixed pre-label composite    |
| `jev_noul`     | Jev answers "is X concealing their true role?" per player                         |
| `jev_pairwise` | Jev answers "does X protect Y?" for every pair; strongest mutual pair gives p     |
| `features`     | logistic regression on the Jev answers above, `GroupKFold(5)` by game             |
| `frontier`     | claude-opus-5 with structured output — built and tested, **not run** (no API key) |

Every judge sees only `public_view(transcript, upto_round)`: identical text for all of
them, no night actions, no roles. Judges score each player who spoke in that round's view.
Jev is pinned to `jev-1.13.0`; it costs $0.042 per 1M input tokens (output free).

## Results

### Generated games (`llm_llama31_8b`, 200 games, 4,479 player-rounds, 699 views)

| Judge                                           | AUC (player-round)                                 |
| ----------------------------------------------- | -------------------------------------------------- |
| `features` (logistic regression on Jev answers) | 0.825 [0.803, 0.845] held-out                      |
| `jev_multidim`                                  | **0.706 [0.680, 0.730]**                           |
| `jev_pairwise`                                  | 0.545 [0.525, 0.566]                               |
| `jev_batched`                                   | 0.542 [0.504, 0.574]                               |
| `jev_isolated`                                  | 0.513 [0.483, 0.541]                               |
| `length` (talk volume)                          | 0.503 [0.492, 0.514]                               |
| `keyword`                                       | 0.488 [0.462, 0.513]                               |
| `jev_noul`                                      | 0.449 [0.416, 0.482]                               |
| `llama_judge`                                   | 0.403 [0.370, 0.437] (conclusively _below_ chance) |

- `jev_multidim` is the only judge whose round-1 top-1 is above chance (+0.078 [+0.018, +0.143]).
- The learned regression reaches 0.777 [0.755, 0.798] from the five dimensions alone and
  0.825 with all Jev features. It is not a new judge: it needs labels to fit, and it is
  refit per domain.
- `llama_judge` ranks wolves as _less_ suspicious; it is the same model that wrote the
  wolves' lines, and it follows the villagers' accusations, which the wolves steer.
- Details: [`reports/core.md`](reports/core.md), [`reports/richer_jev.md`](reports/richer_jev.md).

### External and human data

| Source                       | Games | Best judge    | AUC                                 |
| ---------------------------- | ----- | ------------- | ----------------------------------- |
| `llm_mafia` (GPT-4o agents)  | 35    | `jev_batched` | **0.640 [0.607, 0.677]**            |
| `human_mafia`                | 44    | —             | every judge at chance (0.451–0.496) |
| `avalon`                     | 20    | `jev_noul`    | 0.608 [0.548, 0.666]                |
| `werewolf_among_us` (spoken) | 191   | `jev_noul`    | 0.547 [0.511, 0.584]                |

- On `llm_mafia`, Jev beats random, keyword, length (0.547) and `llama_judge`; the signal
  starts in round 1 (0.560) and grows in later rounds.
- On the human sources, no judge — Jev included — is clearly useful. Jev tends to read
  honest humans as deceptive (its ECE on `werewolf_among_us` is 0.407 to 0.562, worse
  than random).
- Pooling the three human sources, the learned regression reaches 0.572 [0.540, 0.603],
  mostly on the back of `avalon` (20 games — treat with care).
- Details: [`reports/human.md`](reports/human.md),
  [`reports/human_wau_endrole.md`](reports/human_wau_endrole.md),
  [`reports/richer_jev_human.md`](reports/richer_jev_human.md).

### Live games: a judge decides the vote

Same 50 seat/role setups per condition; the judge's top suspect is voted out.

| Condition                                  | Villagers win                                      |
| ------------------------------------------ | -------------------------------------------------- |
| `random` votes (same 50 games)             | 24% [12, 36]                                       |
| `jev_batched` picks the vote               | **50% [36, 64]** (+26 pp [+12, +40])               |
| `llama_judge` picks the vote               | 12% [4, 22]                                        |
| Jev, abstains below 0.28 / 0.42 confidence | 28% / 12%                                          |
| Jev, without the defense step              | 44% [30, 58] (inconclusive vs 50%, half the calls) |

Details: [`reports/live.md`](reports/live.md).

### Ablations and stress tests

- **Criteria leak** (the top criterion naming the wolves' prompted tactic) makes Jev
  _worse_: −0.061 [−0.070, −0.050] AUC. Villagers say "deflect" 2–4× as often as wolves,
  so word-matching points at the wrong team. [`reports/ablations.md`](reports/ablations.md)
- **Dropping the defense** helps slightly (+0.028 [+0.012, +0.042]) and halves the calls.
  This was not planned in advance; treat it as a result to confirm.
- **Stronger liars**: swapping the wolves to `gpt-oss:20b` makes them _easier_ for Jev
  (jev_isolated 0.667 [0.634, 0.698] vs 0.521 on all-llama games), but the talk-volume
  baseline rises just as much (0.509 → 0.609): a lot of this is a style tell, not lie
  detection. [`reports/stress.md`](reports/stress.md)
- **Adaptive wolves** that see their own Jev score look like they evade it
  (−0.052 [−0.112, +0.008], p = 0.09), but with 50 games per condition the result is
  inconclusive.

### Cost and latency

| Judge                         | Cost                                 | Speed                                                                   |
| ----------------------------- | ------------------------------------ | ----------------------------------------------------------------------- |
| `jev_batched`                 | $0.069 per 699 views (~$0.0003/game) | 0.20 s per view                                                         |
| `jev_isolated`                | $0.092 per 699 views                 | 0.39 s per view                                                         |
| `llama_judge`                 | free (local GPU)                     | 4.7–6.3 s per view on short games; 39.6 s on windowed `llm_mafia` views |
| `random`, `keyword`, `length` | $0                                   | ~0 s                                                                    |

The reports' cost tables add up to just under $2 of Jev spend in total (core $0.16,
human $0.33, ablations $0.19, richer Jev $0.94, live games $0.17, plus the stress-run
judgments). The frontier judge is implemented but has never been called: it is estimated
at $38–114 for 1,208 calls, and needs `ANTHROPIC_API_KEY`.

## Interactive results page

`reports/site/jev-werewolf.html` is a self-contained results page (AUC, calibration,
cost, live and ablation numbers). Open it in a browser; rebuild it with
`python reports/site/build.py`, which re-parses the numbers out of `reports/*.md`.

## Layout

```
werewolf.py                 thin entry point: play one verbose game
ww/config.py                paths, models, game parameters, criteria variants
ww/clients.py               injectable Ollama / TypeSafe / Anthropic clients
ww/gpu.py                   cross-process GPU lock for Ollama jobs
ww/game/                    the game engine
ww/transcripts/             transcript schema, generator, human-data loaders
ww/judges/                  judge protocol, cache, run_judge, all judges
ww/eval/                    metrics, bootstrap CIs, reports, feature regression, ablations
ww/live/                    live games where a judge drives the vote
ww/stress/                  stronger-liar and adaptive-wolf generators
scripts/                    dataset downloader and small analysis scripts
tests/                      offline test suite (223 tests)
reports/                    the results: markdown reports, plots, results page
```

Generated transcripts and cached judgments live under `$WW_DATA_DIR` (default `./data`,
git-ignored) — one JSON file per game, one judgments folder per judge, keyed by a hash of
the judge's name and config, so runs resume and never silently change meaning.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
ollama pull llama3.1:8b                          # generated games, llama_judge, live games
```

`.env` in the repo root:

```
TYPESAFE_API_KEY=...    # the Jev judges
ANTHROPIC_API_KEY=...   # optional, frontier judge only
# WW_DATA_DIR=...       # optional, defaults to ./data
```

Tests are fully offline (fake Ollama/TypeSafe clients are injected by `tests/conftest.py`):

```bash
python -m pytest -q      # 223 passed
```

## Running the experiment

Each module is a CLI with examples in its `--help`; every report ends with the exact
command that generated it.

```bash
# generate 200 games with llama3.1:8b players (GPU lock held; resumable)
python -m ww.transcripts.generate --n 200 --persona generic --out-source llm_llama31_8b --seed 0

# judge a source (resumable, caches per round; >50-call runs print an estimate and gate)
python -m ww.judges.run --judge jev_batched --source llm_llama31_8b --limit 2
python -m ww.judges.run --judge jev_batched --source llm_llama31_8b

# richer Jev judges and the feature regression
python -m ww.judges.jev_multidim --help
python -m ww.eval.features --out reports/richer_jev_features.md

# reports
python -m ww.eval.report --sources llm_llama31_8b --judges <keys> --out reports/core.md
python -m ww.live.report --out reports/live.md
python -m ww.stress.report --out reports/stress.md
python reports/site/build.py

# live games (a judge votes) and stress runs
python -m ww.live.run --conditions jev --n 2          # trial
python -m ww.live.run --conditions llama,jev,jev_t028,jev_t042,jev_nodef --n 50 --yes
```

## Data sources

Downloaded into `$WW_DATA_DIR/external/` by `python scripts/download_datasets.py`
(idempotent; writes a manifest with URL, revision and license). None of it is in git.

| Source              | Games | Origin                                      | License                                              |
| ------------------- | ----- | ------------------------------------------- | ---------------------------------------------------- |
| `human_mafia`       | 44    | omonida/mafia-dataset                       | none stated — research use only, do not redistribute |
| `llm_mafia`         | 35    | cocochief4/llm-mafia                        | CC0 1.0                                              |
| `avalon`            | 20    | sstepput/Avalon-NLU                         | MIT                                                  |
| `werewolf_among_us` | 191   | HF `bolinlai/Werewolf-Among-Us` (text only) | Apache 2.0                                           |

## Method notes

- **Unit of analysis** is the player-round: one judge score for one player who spoke in
  that round's view (views include all earlier rounds). Per-round and round-1-only views
  are reported separately, because late role reveals and confessions can flatter a judge.
- **CIs** are 95% percentile bootstraps that resample whole games (1000 or 2000 draws);
  comparisons between judges are paired bootstraps over the same games. Where CIs overlap,
  the table alone proves nothing.
- **Long games**: `llm_mafia` views reach ~330k characters, over Jev's 32k-token limit, so
  those judges see a window (`ww/judges/window.py`: last 90k characters, keeping public
  events and each current speaker's latest message). It is a no-op on every other source.
- **One GPU job at a time**: anything calling Ollama takes `ww.gpu.gpu_lock()`.

## Limitations

- The generated games are llama3.1:8b with generic personas and random votes. Their wolf
  style (vague, passive, accusatory villagers) is what `jev_multidim` and the regression
  pick up; trained weights do not transfer between generated and human data (cosine
  similarity of coefficients −0.22; trained on human data, the model scores 0.384 on
  generated games, below chance).
- Small samples where the interesting cases are: 20 avalon games, 35 llm_mafia, 44
  human_mafia — CIs there are wide.
- Not run, and how to run it: the frontier judge needs `ANTHROPIC_API_KEY` and a
  cost-gate approval (`python -m ww.judges.frontier --sources ... --limit 2 --yes`).
  The no-defense gain and the adaptive-wolf evasion both need a confirmatory run with
  more games.
- This is a research prototype, not a product; there is no license file in the repo.
