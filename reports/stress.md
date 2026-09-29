# Stress tests: stronger and adaptive liars

<!-- INTERPRETATION START -->
## Interpretation

**Stronger liars (gpt-oss:20b wolves): Jev spots them more easily, not less.** The comparison is the same 100 setups as the all-llama games, and the result is the opposite of what a "stronger liar" should produce.

- jev_isolated's AUC rises from 0.521 [0.482, 0.563] to **0.667 [0.634, 0.698]** (+0.146 [+0.092, +0.199]), and jev_batched's from 0.539 to **0.648 [0.611, 0.682]** (+0.109 [+0.049, +0.165]). The gain holds in rounds 1-3. Both Jev judges are also better calibrated on these games (ECE lower by about 0.04-0.05).
- **A large part of this is a style tell, not better lie detection.** The length baseline rises from 0.509 to 0.609 [0.595, 0.623], because gpt-oss wolves write longer statements than the llama villagers they play alongside (253 vs 226 characters; in the all-llama games wolves write 210). When the wolves are played by a different model, the judge can tell them apart by how they write. jev_isolated's CI sits above length's without overlap. jev_batched's CI overlaps it. This report has no paired Jev-vs-length test, so how much Jev adds beyond style remains open. This matches Wave 3's human.md, where Jev detects GPT-4o mafia but no judge beats chance on human games.
- Keyword gets worse (0.414, -0.071 [-0.127, -0.014]). llama_judge stays below chance (0.408, unchanged).
- Takeaway: mixing models in one game does not produce harder-to-detect liars. A fair stronger-liar test needs every player on the same model.

**Adaptive wolves (each wolf sees its previous-day Jev score): there is a hint that they evade Jev, but it's inconclusive.**

- AUC is 0.466 with the feedback and 0.517 without, a difference of -0.052 [-0.112, +0.008] (p = 0.09). Round 1, before the wolves have seen any feedback, is identical (0.483 vs 0.486). The two conditions diverge from round 2 on, but no round's difference is significant.
- Without feedback, Jev's mean p for wolves climbs above the villagers' from round 2 (e.g. 0.226 vs 0.166 in round 3). With feedback, the wolf and villager means stay level (0.161 vs 0.172). That is the pattern you'd expect if the wolves adapt, but it's a comparison of means with no test.
- Villagers win 36% with adaptive wolves and 50% without (live.md's CIs apply). With 50 games per condition, the test can't separate the two. About twice as many games would be needed to settle it.
<!-- INTERPRETATION END -->

---

Generated 2026-09-27 12:45 by `python -m ww.stress.report --out reports/stress.md`. Data: `B:\jev-blackjack\data`. CIs: 95% percentile bootstrap (1000 replicates, seed 0) resampling whole games; differences resample game indices and take both matched games.

## 1. Stronger liars: gpt-oss:20b wolves

`llm_gptoss20b_wolves`: 100 games; wolves speak with gpt-oss:20b (reasoning effort low, run on CPU), villagers and the Seer with llama3.1:8b; votes are random, as in the Wave 2 generator. Compared with the same 100 game indices of `llm_llama31_8b` (all llama3.1:8b; same seats and roles). Typographic punctuation was converted to ASCII in the gpt-oss games so it can't label the wolves.

| Source | Mean chars / statement, wolves | villagers |
|---|---|---|
| llm_gptoss20b_wolves | 253 | 226 |
| llm_llama31_8b (same indices) | 210 | 225 |

Generation: 685 wolf calls, mean 37.1 s each; 1 empty gpt-oss answers were retried; mean 278 s per game.

| Judge | Games | AUC gpt-oss wolves | AUC llama wolves | Difference | p | Verdict |
|---|---|---|---|---|---|---|
| random | 100 | 0.516 [0.486, 0.548] | 0.500 [0.472, 0.528] | +0.016 [-0.026, +0.061] | 0.466 | inconclusive (CI includes 0) |
| keyword | 100 | 0.414 [0.369, 0.460] | 0.485 [0.448, 0.520] | -0.071 [-0.127, -0.014] | 0.008 | lower |
| length | 100 | 0.609 [0.595, 0.623] | 0.509 [0.494, 0.526] | +0.100 [+0.080, +0.117] | <0.001 | higher |
| llama_judge | 100 | 0.408 [0.362, 0.451] | 0.399 [0.354, 0.440] | +0.009 [-0.041, +0.058] | 0.730 | inconclusive (CI includes 0) |
| jev_isolated | 100 | 0.667 [0.634, 0.698] | 0.521 [0.482, 0.563] | +0.146 [+0.092, +0.199] | <0.001 | higher |
| jev_batched | 100 | 0.648 [0.611, 0.682] | 0.539 [0.487, 0.589] | +0.109 [+0.049, +0.165] | <0.001 | higher |

Difference = gpt-oss-wolf games minus llama-wolf games (matched by game index). AUC is per player-round, as in core.md.

| Judge | Metric | gpt-oss wolves | llama wolves | Difference | p | Verdict |
|---|---|---|---|---|---|---|
| random | R1 top-1 minus chance | 0.028 [-0.052, 0.118] | 0.058 [-0.032, 0.138] | -0.030 [-0.150, +0.100] | 0.672 | inconclusive (CI includes 0) |
| random | ECE | 0.289 [0.274, 0.308] | 0.311 [0.297, 0.330] | -0.022 [-0.046, +0.000] | 0.058 | inconclusive (CI includes 0) |
| keyword | R1 top-1 minus chance | -0.062 [-0.132, 0.018] | 0.023 [-0.057, 0.108] | -0.085 [-0.185, +0.020] | 0.092 | inconclusive (CI includes 0) |
| keyword | ECE | 0.171 [0.140, 0.203] | 0.141 [0.118, 0.168] | +0.029 [-0.011, +0.065] | 0.152 | inconclusive (CI includes 0) |
| length | R1 top-1 minus chance | 0.138 [0.048, 0.238] | 0.018 [-0.062, 0.108] | +0.120 [-0.010, +0.250] | 0.070 | inconclusive (CI includes 0) |
| length | ECE | 0.178 [0.171, 0.185] | 0.185 [0.177, 0.192] | -0.007 [-0.011, -0.002] | 0.004 | lower |
| llama_judge | R1 top-1 minus chance | -0.032 [-0.102, 0.048] | 0.068 [-0.022, 0.158] | -0.100 [-0.200, +0.010] | 0.066 | inconclusive (CI includes 0) |
| llama_judge | ECE | 0.332 [0.307, 0.359] | 0.345 [0.318, 0.375] | -0.014 [-0.045, +0.017] | 0.382 | inconclusive (CI includes 0) |
| jev_isolated | R1 top-1 minus chance | 0.118 [0.028, 0.198] | 0.033 [-0.042, 0.118] | +0.085 [-0.030, +0.195] | 0.160 | inconclusive (CI includes 0) |
| jev_isolated | ECE | 0.078 [0.068, 0.096] | 0.118 [0.098, 0.142] | -0.039 [-0.062, -0.014] | 0.006 | lower |
| jev_batched | R1 top-1 minus chance | 0.083 [-0.002, 0.178] | 0.068 [-0.017, 0.163] | +0.015 [-0.090, +0.125] | 0.818 | inconclusive (CI includes 0) |
| jev_batched | ECE | 0.065 [0.046, 0.087] | 0.114 [0.087, 0.143] | -0.048 [-0.081, -0.017] | 0.004 | lower |

jev_batched AUC by round:

| Round | gpt-oss wolves | llama wolves | Difference | p | Verdict |
|---|---|---|---|---|---|
| 1 | 0.628 [0.590, 0.668] (n=100) | 0.529 [0.475, 0.577] (n=100) | +0.099 [+0.041, +0.157] | <0.001 | higher |
| 2 | 0.648 [0.608, 0.687] (n=100) | 0.529 [0.469, 0.585] (n=100) | +0.119 [+0.054, +0.183] | <0.001 | higher |
| 3 | 0.652 [0.597, 0.704] (n=97) | 0.543 [0.480, 0.605] (n=97) | +0.118 [+0.035, +0.199] | 0.008 | higher |
| 4 | 0.641 [0.548, 0.725] (n=54) | 0.553 [0.439, 0.668] (n=55) | +0.052 [-0.089, +0.193] | 0.502 | inconclusive (CI includes 0) |

## 2. Adaptive wolves (live games)

`live_jev_adaptive` (50 games): jev_batched drives the vote (defense on, no abstention) and each wolf's prompt shows the score Jev gave it the previous day, its rank, and that the village votes out Jev's top suspect. `live_jev` (50 games): the same, without the feedback. Both use game indices 0..n-1 (same seats and roles). AUC uses Jev's own final judgment of each day. Caveat: in live games the judge also decides who is eliminated, so later rounds contain only the players it did not vote out; this affects both conditions, but not necessarily equally.

| Metric | Adaptive | Non-adaptive | Difference | p | Verdict |
|---|---|---|---|---|---|
| AUC | 0.466 [0.412, 0.529] | 0.517 [0.452, 0.585] | -0.052 [-0.112, +0.008] | 0.094 | inconclusive (CI includes 0) |
| R1 top-1 minus chance | 0.018 [-0.082, 0.138] | -0.022 [-0.122, 0.098] | +0.040 [-0.060, +0.140] | 0.478 | inconclusive (CI includes 0) |
| ECE | 0.136 [0.113, 0.164] | 0.104 [0.087, 0.137] | +0.032 [-0.001, +0.055] | 0.064 | inconclusive (CI includes 0) |
| Villager win rate | 0.360 | 0.500 | (see live report for CIs) | | |

AUC by round:

| Round | Adaptive | Non-adaptive | Difference | p | Verdict |
|---|---|---|---|---|---|
| 1 | 0.483 [0.419, 0.547] (n=50) | 0.486 [0.423, 0.552] (n=50) | -0.003 [-0.051, +0.041] | 0.914 | inconclusive (CI includes 0) |
| 2 | 0.449 [0.365, 0.535] (n=50) | 0.509 [0.425, 0.601] (n=50) | -0.060 [-0.140, +0.025] | 0.162 | inconclusive (CI includes 0) |
| 3 | 0.451 [0.350, 0.550] (n=46) | 0.551 [0.451, 0.647] (n=48) | -0.077 [-0.198, +0.046] | 0.212 | inconclusive (CI includes 0) |
| 4 | 0.500 [0.339, 0.665] (n=23) | 0.560 [0.409, 0.722] (n=26) | +0.095 [-0.180, +0.426] | 0.506 | inconclusive (CI includes 0) |

Mean Jev p for wolves vs villagers, by round (does the feedback pull wolves' scores down?):

| Round | Adaptive: wolves | villagers | games | Non-adaptive: wolves | villagers | games |
|---|---|---|---|---|---|---|
| 1 | 0.166 | 0.166 | 50 | 0.175 | 0.172 | 50 |
| 2 | 0.150 | 0.151 | 50 | 0.188 | 0.155 | 50 |
| 3 | 0.161 | 0.172 | 46 | 0.226 | 0.166 | 48 |
| 4 | 0.167 | 0.166 | 23 | 0.219 | 0.182 | 26 |

