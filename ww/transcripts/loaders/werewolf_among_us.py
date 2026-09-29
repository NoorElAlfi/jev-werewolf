"""Loader for Werewolf Among Us (Lai et al., ACL Findings 2023),
huggingface.co/datasets/bolinlai/Werewolf-Among-Us. License: Apache 2.0.

Text only: the transcripts (`<subset>/transcripts/*.txt`, lines "Name (mm:ss):
text") and the per-game annotations (`Youtube/vote_outcome_youtube_released/
<video>.json`, `Ego4D/vote_outcome_ego4d/<uid>.json`: playerNames, startRoles,
endRoles, votingOutcome, warning). Youtube transcript file names use "#" for
spaces; an Ego4D game is split into parts `<uid>_<Game>_<part>.txt` (survey
breaks removed), which are concatenated in part order.

One Night Ultimate Werewolf has one night and one day, so each game is ONE
round: the day discussion is its statements, and all votes happen at once at
the end.

Labels come from the START role (as PLAN.md asks): Werewolf and Minion ->
"deceptive", everyone else -> "honest". Roles can change during the night
(Robber, Troublemaker, Drunk, Doppelganger), so a player may end on the other
team; meta["team_changed"] lists those players, and the progress file reports
how many labels that affects. Other judgement calls:
  - Tanner (wins by getting voted out, on nobody's team) counts as honest; the
    Doppelganger is labelled by its start card (honest).
  - Players with role "Moderator" are not players; their lines and the app
    narration ("Audio", "Cell Phone Voice", ...) go to night.extra["narration"]
    as a count, not statements. Other unknown speakers are dropped and counted.
  - Games are skipped if any player's start role is unknown ("NA"), or if the
    game is not ONUW (warning "Different game", e.g. Avalon roles).
  - votes: voter -> target from votingOutcome (0-based indices into
    playerNames); "NA" and votes for the middle are left out, and so are all
    votes of the 3 games with a moderator (their indexing is ambiguous).
    `eliminated` is set only when one player has strictly the most votes (at
    least 2); meta["eliminated_all"] has every player who died under the ONUW
    rule (all tied at the max, if >= 2).
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from pathlib import Path

from ww.config import data_dir
from ww.transcripts.schema import Night, PlayerInfo, Round, Statement, Transcript, validate

SOURCE = "werewolf_among_us"
LICENSE = "Apache-2.0"
DECEPTIVE_START_ROLES = {"werewolf", "minion"}
NARRATORS = {"audio", "game audio", "cell phone voice", "automated", "siri", "siri voice",
             "timer", "voiceover", "twitch alert"}
LINE = re.compile(r"^(.+?) \((\d{1,2}:\d{2}(?::\d{2})?)\): ?(.*)$")


def default_raw_dir() -> Path:
    return data_dir() / "external" / "werewolf-among-us"


def _team(role: str) -> str:
    return "deceptive" if role.strip().lower() in DECEPTIVE_START_ROLES else "honest"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def game_specs(raw_dir: Path | None = None) -> list[dict]:
    """One spec per game: id, subset, annotation, and transcript part files."""
    raw = Path(raw_dir) if raw_dir else default_raw_dir()
    specs = []
    yt_ann = {}
    for f in sorted((raw / "Youtube" / "vote_outcome_youtube_released").glob("*.json")):
        for g, x in json.loads(f.read_text(encoding="utf-8")).items():
            yt_ann[(f.stem, g)] = x
    for f in sorted((raw / "Youtube" / "transcripts").glob("*.txt")):
        m = re.match(r"(.*)_(Game\d+)$", f.stem)
        video, game = m.group(1).replace("#", " "), m.group(2)
        specs.append({"game_id": f"yt_{_safe(video)}_{game}", "subset": "Youtube",
                      "video": video, "game": game, "ann": yt_ann.get((video, game)),
                      "parts": [f]})
    eg_ann = {}
    for f in sorted((raw / "Ego4D" / "vote_outcome_ego4d").glob("*.json")):
        for g, x in json.loads(f.read_text(encoding="utf-8")).items():
            eg_ann[(f.stem, g)] = x
    parts: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for f in (raw / "Ego4D" / "transcripts").glob("*.txt"):
        m = re.match(r"(.*)_(Game\d+)_(\d+)$", f.stem)
        parts.setdefault((m.group(1), m.group(2)), []).append((int(m.group(3)), f))
    for (uid, game), ps in sorted(parts.items()):
        specs.append({"game_id": f"ego_{uid}_{game}", "subset": "Ego4D", "video": uid,
                      "game": game, "ann": eg_ann.get((uid, game)),
                      "parts": [p for _, p in sorted(ps)]})
    return specs


def skip_reason(spec: dict) -> str | None:
    ann = spec["ann"]
    if not ann or "startRoles" not in ann:
        return "no annotation"
    if "different game" in str(ann.get("warning", "")).lower():
        return "not ONUW"
    for role in ann["startRoles"]:
        if str(role).strip().upper() in ("NA", "N/A", ""):
            return "unknown start role"
    return None


def _match_speaker(sp: str, names: list[str]) -> str | None:
    sp = sp.strip()
    if sp in names:
        return sp
    low = {n.lower(): n for n in names}
    if sp.lower() in low:
        return low[sp.lower()]
    close = difflib.get_close_matches(sp, names, n=1, cutoff=0.75)
    return close[0] if close else None


def load_game(spec: dict) -> Transcript:
    ann = spec["ann"]
    names_all = [n.strip() for n in ann["playerNames"]]
    starts = [str(r).strip() for r in ann["startRoles"]]
    ends = [str(r).strip() for r in ann.get("endRoles") or [""] * len(names_all)]
    moderators = {n for n, r in zip(names_all, starts) if r.lower() == "moderator"}
    names = [n for n in names_all if n not in moderators]

    players, team_changed, end_unknown = [], [], []
    for n, s, e in zip(names_all, starts, ends):
        if n in moderators:
            continue
        players.append(PlayerInfo(id=n, role=s, team=_team(s)))
        if e.upper() in ("NA", "N/A", ""):
            end_unknown.append(n)
        elif _team(e) != _team(s):
            team_changed.append({"player_id": n, "start_role": s, "end_role": e})

    statements: list[Statement] = []
    narration = 0
    dropped = Counter()
    fuzzy = Counter()
    for part in spec["parts"]:
        for ln in part.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            m = LINE.match(ln)
            if not m:
                if statements:  # continuation of the previous utterance
                    statements[-1].text += "\n" + ln.strip()
                continue
            sp, text = m.group(1).strip(), m.group(3).strip()
            if sp.lower() in NARRATORS or sp in moderators:
                narration += 1
                continue
            who = _match_speaker(sp, names)
            if who is None:
                dropped[sp] += 1
                continue
            if who != sp:
                fuzzy[f"{sp}->{who}"] += 1
            statements.append(Statement(who, text))

    votes: dict[str, str] = {}
    middle_or_na = 0
    # With a moderator in playerNames, some votes point at the moderator, so the
    # indexing is ambiguous (3 games): leave those games' votes out.
    raw_votes = [] if moderators else (ann.get("votingOutcome") or [])
    for voter, target in zip(names_all, raw_votes):
        if isinstance(target, int) and 0 <= target < len(names_all) \
                and names_all[target] not in moderators:
            votes[voter] = names_all[target]
        else:
            middle_or_na += 1
    tally = Counter(votes.values())
    top = max(tally.values(), default=0)
    dead = sorted(p for p, c in tally.items() if c == top) if top >= 2 else []

    night_extra = {"end_roles": {n: e for n, e in zip(names_all, ends) if n not in moderators}}
    if narration:
        night_extra["narration_lines"] = narration
    r = Round(round=1, night=Night(extra=night_extra), statements=statements,
              votes=votes or None, eliminated=dead[0] if len(dead) == 1 else None)

    meta = {
        "dataset": "bolinlai/Werewolf-Among-Us",
        "license": LICENSE,
        "subset": spec["subset"],
        "video": spec["video"],
        "game": spec["game"],
        "label_from": "start role",
        "team_changed": team_changed,
        "end_role_unknown": end_unknown,
        "eliminated_all": dead,
        "votes_missing_or_middle": middle_or_na,
        "votes_left_out_moderator_game": bool(moderators),
        "moderators": sorted(moderators),
        "dropped_speakers": dict(dropped),
        "fuzzy_speaker_matches": dict(fuzzy),
        "warning": ann.get("warning"),
    }
    return validate(Transcript(game_id=spec["game_id"], source=SOURCE, players=players,
                               rounds=[r], winner=None, meta=meta))


def load_all(raw_dir: Path | None = None) -> list[Transcript]:
    out = []
    for spec in game_specs(raw_dir):
        if skip_reason(spec) is None:
            out.append(load_game(spec))
    return out


def skipped(raw_dir: Path | None = None) -> dict[str, str]:
    return {s["game_id"]: why for s in game_specs(raw_dir) if (why := skip_reason(s))}
