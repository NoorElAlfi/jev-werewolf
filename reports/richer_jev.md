# Richer Jev judgments (Wave 4 H)

Can Jev do better than one "how deceptive?" Score (jev_batched) if we ask it narrower questions and let code combine the answers?
We tried three new judges, each making one Jev call per round's public view:

- **jev_multidim** asks five tactic-neutral Score dimensions per player: evasiveness, specificity, self-inconsistency, accusation, and contradiction with public events. Its own p is a composite fixed before seeing any labels: the mean of evasiveness, 1 − specificity, self-inconsistency and event contradiction.
- **jev_noul** asks one Noul per player: "Is X concealing their true role?"
- **jev_pairwise** asks one Noul per ordered pair of living players: "Is X protecting or defending Y?" Its own p is the player's strongest mutual-protection pair. The per-player features use only rounds ≤ r, and a test checks this.

`ww/eval/features.py` then fits a logistic regression on those answers, with GroupKFold(5) by game.

Every number below is from one of these files:

- [richer_jev_generated.md](richer_jev_generated.md): the report CLI on `llm_llama31_8b`, 200 games.
- [richer_jev_human.md](richer_jev_human.md): the report CLI on human_mafia, llm_mafia, avalon and werewolf_among_us.
- [richer_jev_features.md](richer_jev_features.md): the regression.

CIs are 95% bootstrap intervals that resample whole games. "Beats" means a paired game bootstrap whose CI excludes 0. There are many comparisons here, so a single p ≈ 0.05 result is weak evidence.

## Interpretation

**1. On our generated games, asking narrower questions helps a lot. The signal is llama-8B's wolf style, not deception in general.**
- **jev_multidim's pre-set composite beats jev_batched.** It reaches AUC 0.706 [0.680, 0.730], against 0.542 [0.504, 0.574] for jev_batched (paired +0.165 [+0.141, +0.191]). It beats random and length too. Its round-1 top-1 is above chance (+0.078 [+0.018, +0.143]), which no Wave 3 judge managed. Its ECE of 0.112 is as good as jev_batched's (the paired test is inconclusive).
- **The learned regression goes further.** Held-out AUC is 0.777 [0.755, 0.798] with the five multidim features alone, and 0.825 [0.803, 0.845] with multidim, noul and pairwise together.
  - Both beat jev_batched's p on the same rows: +0.235 and +0.283.
  - The fixed-weight judge also gets better as the game goes on: its AUC is 0.670, 0.696, 0.738 and 0.798 for rounds 1 to 4.
- **What carries the signal:** the single-feature table in richer_jev_features.md shows wolves as quieter than villagers in three ways:
  - Less specific: mean level 0.471 vs 0.610. Specificity alone has AUC 0.266, so low specificity alone gives 0.734.
  - Much less accusing: 0.138 vs 0.361. Accusation alone has AUC 0.244, so low accusation alone gives 0.756.
  - More protective of others: pw_out_mean 0.137 vs 0.089, AUC 0.674 [0.645, 0.703].

  The regression coefficients agree: accusation −0.90, specificity −0.41, evasiveness +0.50, pw_out_mean +0.87 (all CIs exclude 0).
- **Why jev_batched and llama_judge missed it:**
  - jev_batched's criteria ("evasive, contradictory, or hard to believe") don't describe a passive, agreeable player.
  - llama_judge was *below* chance in Wave 3, which fits this picture: the players who accuse others are mostly villagers.
- **The caveat:** this is how llama3.1:8b plays a wolf told to "avoid detection". It goes vague and passive. It is not a general tell of deception, as sections 2 and 3 show.

**2. On the human games, the gains are small, and only in some datasets.**
- **Single judges.** jev_noul is the only new judge above chance:
  - avalon: 0.608 [0.548, 0.666]. This beats jev_batched, but there are only 20 games.
  - werewolf_among_us: 0.547 [0.511, 0.584]. This also beats jev_batched.
  - human_mafia: 0.464 [0.400, 0.521], at chance.

  jev_multidim is at chance everywhere (0.444 to 0.524). jev_pairwise's own p is *below* chance on avalon (0.385 [0.325, 0.442]): evil players take part in *fewer* mutual-protection pairs. The pairwise p beats jev_batched on human_mafia (+0.076 [+0.009, +0.151], p = 0.022), but its AUC there, 0.527 [0.487, 0.564], is itself at chance.
- **Learned regression, pooled over the three human sources.** All features plus jev_batched's p reach 0.572 [0.540, 0.603], which beats jev_batched (+0.080 [+0.038, +0.119]).
  - By source: avalon 0.674 [0.608, 0.737], werewolf_among_us 0.557 [0.516, 0.596], human_mafia 0.510 [0.465, 0.555].
  - Human Mafia stays at chance under every approach.
  - The avalon result rests on 20 games.
- **Calibration.** jev_multidim's composite has ECE 0.060 to 0.135 on the four external sources, against jev_batched's 0.053 to 0.407. It is much better on werewolf_among_us (0.070 vs 0.407) and human_mafia (0.073 vs 0.195), and about the same on llm_mafia (0.060 vs 0.053). The reason is that its p stays near the base rate, not that it separates the teams.

**3. On LLM Mafia, the narrow questions do worse than the single holistic Score.**
- **Single judges.** jev_batched stays the best at 0.640 [0.607, 0.677]. jev_multidim (0.532), jev_noul (0.566) and jev_pairwise (0.513) all lose to it in paired tests.
- **Learned regression.** The learned model without jev_batched's p reaches 0.578 [0.525, 0.628], still below jev_batched. Adding that p gives 0.668 [0.614, 0.726], which does not conclusively beat jev_batched alone (+0.028 [−0.015, +0.072]). With 35 games and 19 features, overfitting is a real risk.

**4. The learned weights do not transfer between domains.**
- **Coefficients.** Between generated and human, 3 features are significant in both, and all 3 have *opposite* signs:
  - evasiveness: +0.50 generated, −0.12 human
  - specificity: −0.41 generated, +0.22 human
  - noul: −0.15 generated, +0.24 human

  The cosine similarity of the two coefficient vectors is −0.22.
- **Transfer AUCs.**
  - Trained on generated, the full model scores 0.499 on human (in-domain 0.563) and 0.559 on llm_mafia.
  - Trained on human, it scores 0.384 on generated, clearly *below* chance.
  - The only partial transfer is llm_mafia → generated, at 0.656 (in-domain 0.825). Low specificity points toward the deceptive team in both.
- **Conclusion.** Which signals matter depends on who is lying. A weighting learned on one population of liars doesn't carry over to another. A deployment would need labelled data from its own domain.

**5. Cost and latency.**
- All three judges ran on all 5 sources: 3 × 1,208 views, one call per view.
- Actual cost was $0.942, trials included:

  | Judge | Generated | Human sources | Total |
  |---|---|---|---|
  | jev_multidim | $0.155 | $0.200 | $0.355 |
  | jev_noul | $0.065 | $0.142 | $0.207 |
  | jev_pairwise | $0.177 | $0.202 | $0.379 |

- The pre-run estimate was $1.21. The token fit overestimates question-heavy calls by about 20%.
- Mean latency is 0.19 to 0.23 s per view on generated games.
- **Pairwise does not add calls.** All n(n−1) questions for a view go in one request, so asking every pair adds only question tokens: about 2.5× jev_batched's cost on generated games.

**Bottom line.** Splitting "is X deceptive?" into narrow, tactic-neutral questions and weighting them in code turns Jev from barely-above-chance (0.54) into a strong detector (0.71 with fixed weights, 0.83 learned) on llama-8B games. It also gives modest gains on Avalon and One Night Werewolf. However, the signals behind those gains differ from dataset to dataset, and even reverse sign. So these are *per-domain* detectors, not a general lie detector. On human Mafia, nothing works. On GPT-4o Mafia, the holistic jev_batched Score is still best.

## Caveats
- **Accusation.** It was left out of the multidim composite because its direction wasn't known in advance. Its strong negative role in the generated games was found afterwards, so it's only evidence where it was learned under cross-validation.
- **Living players.** "Living" means players who spoke in that round, because the public view has no alive list. Silent living players are not in the pairwise questions.
- **Pooled human AUC.** The pooled human AUC mixes three sources with different base rates, so the per-source table is the cleaner read.
- **Rows aren't independent.** Rows are player-rounds, and views include every earlier round, so rows from the same game are correlated. The folds and bootstraps are grouped by game for that reason.
