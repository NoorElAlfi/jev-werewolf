"""Loader for the human Mafia dataset (Ibraheem et al., NAACL 2022),
github.com/omonida/mafia-dataset.

LICENSE: none stated. Research use only; do NOT redistribute the data or the
converted transcripts (only tiny excerpts live in tests/fixtures/).

Raw layout (after unzipping mafia_dataset.zip): one folder per game,
`<uuid>-data/` with node.csv (players: fake name, role, elimination time),
info.csv (every transmission: "info" phase changes, "text" chat, "vote") and
network.csv (winner).

Conversion:
  - player id = the fake name (players address each other by it in chat);
    mafioso -> "deceptive", bystander -> "honest".
  - Phases come from the "Phase Change to Daytime/Nighttime" info rows. The
    server often logs the same change several times; consecutive changes to the
    same phase are collapsed.
  - Deaths come from node.csv (property3 = elimination time), matched to the
    phase change at that moment. The "Victim - X" suffix on info rows is not
    used for this because it is sometimes stale (repeats the previous victim).
  - Day "text" rows -> statements. Night "text" rows (the mafia's private chat),
    night votes and the night's info rows -> round.night.extra. Day votes -> the
    round's votes (last vote per voter; in the game votes were visible live).
  - Announcements (next day's public_events): "X was killed during the night."
    and "X was eliminated by vote. They were a <role>." (the README says the
    day elimination is announced with the role).
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ww.config import data_dir
from ww.transcripts.loaders._common import Phase, build_mafia_transcript
from ww.transcripts.schema import PlayerInfo, Transcript

SOURCE = "human_mafia"
LICENSE = "none stated (research use only; do not redistribute)"
TEAM = {"mafioso": "deceptive", "bystander": "honest"}
MATCH_SECONDS = 5.0


def default_raw_dir() -> Path:
    return data_dir() / "external" / "mafia-dataset" / "unzipped" / "mafia_dataset"


def game_dirs(raw_dir: Path | None = None) -> list[Path]:
    raw = Path(raw_dir) if raw_dir else default_raw_dir()
    return sorted(p for p in raw.iterdir() if p.is_dir() and (p / "info.csv").exists())


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.strip())


def _read(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _strip_prefix(contents: str, name: str) -> str:
    p = name + ": "
    return contents[len(p):] if contents.startswith(p) else contents


def load_game(game_dir: Path) -> Transcript:
    game_dir = Path(game_dir)
    game_id = game_dir.name.removesuffix("-data")

    nodes = [n for n in _read(game_dir / "node.csv") if n["type"] != "source"]
    nodes.sort(key=lambda n: int(n["id"]))
    name_of = {n["id"]: n["property1"].strip() for n in nodes}
    role_of = {n["property1"].strip(): n["type"] for n in nodes}
    if len(set(name_of.values())) != len(nodes):
        raise ValueError(f"{game_id}: duplicate player names")
    players = [PlayerInfo(id=name_of[n["id"]], role=n["type"], team=TEAM[n["type"]])
               for n in nodes]
    deaths = [(_ts(n["property3"]), name_of[n["id"]]) for n in nodes
              if n["property2"].strip().lower() == "false" and n["property3"].strip()]

    net = _read(game_dir / "network.csv")[0]
    winner = net.get("property2", "").strip() or None

    rows = sorted(_read(game_dir / "info.csv"), key=lambda r: r["creation_time"])

    phases: list[Phase] = []
    starts: list[datetime] = []  # start time of phases[i]
    dropped = {"before_first_phase": 0, "unknown_vote_target": 0,
               "night_text_by_bystander": 0}
    for r in rows:
        kind, contents = r["type"], r["contents"] or ""
        t = _ts(r["creation_time"])
        if kind == "info":
            new = "night" if "Nighttime" in contents else "day" if "Daytime" in contents else None
            if new is None:
                if phases:
                    phases[-1].info.append(contents)
                continue
            if phases and phases[-1].kind == new:  # duplicate log of the same change
                if contents not in phases[-1].info:
                    phases[-1].info.append(contents)
                continue
            if phases and contents not in phases[-1].info:
                phases[-1].info.append(contents)  # the row that ends the phase
            phases.append(Phase(kind=new, info=[contents]))
            starts.append(t)
            continue
        if not phases:
            dropped["before_first_phase"] += 1
            continue
        speaker = name_of.get(r["origin_id"])
        if speaker is None:
            continue
        body = _strip_prefix(contents, speaker)
        ph = phases[-1]
        if kind == "text":
            if ph.kind == "night" and role_of[speaker] != "mafioso":
                dropped["night_text_by_bystander"] += 1
            ph.texts.append((speaker, body))
        elif kind == "vote":
            target = body.strip()
            if target not in role_of:
                dropped["unknown_vote_target"] += 1
                continue
            ph.votes.append((speaker, target))

    # Deaths happen at a phase change: the phase that ENDS then lost that player.
    unmatched = []
    for t_death, who in deaths:
        best = None
        for i in range(1, len(starts)):
            dt = abs((starts[i] - t_death).total_seconds())
            if dt <= MATCH_SECONDS and (best is None or dt < best[0]):
                best = (dt, i - 1)
        if best is None:
            unmatched.append(who)
            continue
        ph = phases[best[1]]
        ph.eliminated = who
        if ph.kind == "night":
            ph.announcement = f"{who} was killed during the night."
        else:
            ph.announcement = f"{who} was eliminated by vote. They were a {role_of[who]}."

    # A trailing phase with nothing in it (the game ended as it started) adds nothing.
    while phases and not (phases[-1].texts or phases[-1].votes or phases[-1].eliminated):
        phases.pop()

    meta = {
        "dataset": "omonida/mafia-dataset",
        "license": LICENSE,
        "raw_game": game_dir.name,
        "winner_team": {"mafia": "deceptive", "bystanders": "honest"}.get(winner or ""),
        "day_votes_were_public": True,
        "dropped": dropped,
        "unmatched_deaths": unmatched,
    }
    return build_mafia_transcript(game_id, SOURCE, players, phases, winner, meta)


def load_all(raw_dir: Path | None = None) -> list[Transcript]:
    return [load_game(d) for d in game_dirs(raw_dir)]
