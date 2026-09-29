"""Loader for Avalon-NLU (Stepputtis et al., EMNLP Findings 2023),
github.com/sstepput/Avalon-NLU. License: MIT.

Raw layout: dataset/<game>.json with "users" (name, role), "messages" (player,
msg, mid, quest, turn; player "system" for game events), "persuasion"
(per-message persuasion and deception-strategy labels, joined by mid) and
"beliefs".

Conversion:
  - Morgana and Assassin -> "deceptive"; Merlin, Percival, servants -> "honest".
  - One round per quest (the message's `quest` field). There is no night:
    `night` stays empty. Quest 6 is the assassination phase (system messages
    only), so it becomes a final round with public_events and no statements.
  - Player messages -> statements, in order.
  - System messages (party proposals, party-vote outcomes, quest results) are
    public, but the schema only has start-of-round public_events. Each system
    message goes to the public_events of the first round whose statements all
    come after it: announcements before the first chat line of a quest open that
    quest; ones in the middle of a quest's chat are shown at the start of the
    next quest. So judges never see an event before the statements that preceded
    it, at the cost of seeing mid-quest proposals/votes one round late.
  - Party votes are yes/no on a party, not votes for a player, so `votes` and
    `eliminated` stay empty; they remain in the public_events text.
  - The deception-strategy labels on lies are kept in meta["lies"] as
    {"round", "index" (into that round's statements), "player_id", "strategy"},
    for statement-level analysis. meta is never shown to judges.
"""

from __future__ import annotations

import json
from pathlib import Path

from ww.config import data_dir
from ww.transcripts.schema import PlayerInfo, Round, Statement, Transcript, validate

SOURCE = "avalon"
LICENSE = "MIT"
DECEPTIVE_ROLES = {"morgana", "assassin"}


def default_raw_dir() -> Path:
    return data_dir() / "external" / "Avalon-NLU" / "dataset"


def game_files(raw_dir: Path | None = None) -> list[Path]:
    raw = Path(raw_dir) if raw_dir else default_raw_dir()
    return sorted(raw.glob("*.json"))


def _winner(system_msgs: list[str]) -> str | None:
    for m in reversed(system_msgs):
        m = m.lower()
        if "evil wins" in m or ("evil" in m and "wins" in m):
            return "evil"
        if "forces of good" in m or "good wins" in m:
            return "good"
    return None


def load_game(path: Path) -> Transcript:
    path = Path(path)
    d = json.loads(path.read_text(encoding="utf-8"))
    game_id = path.stem

    users = sorted(d["users"].values(), key=lambda u: int(u["index"]))
    players = []
    for u in users:
        role = u["role"]
        base = role.split("-")[0]  # servant-1 -> servant
        players.append(PlayerInfo(id=u["name"], role=role,
                                  team="deceptive" if base in DECEPTIVE_ROLES else "honest"))
    ids = {p.id for p in players}

    labels = {p["mid"]: p for p in d.get("persuasion", {}).values()}
    msgs = [d["messages"][k] for k in sorted(d["messages"], key=int)]

    rounds: dict[int, Round] = {}
    pending: list[str] = []  # system messages waiting for the next round to open
    system_all: list[str] = []
    lies: list[dict] = []
    started: set[int] = set()  # quests whose chat has begun
    unknown_speakers = 0

    def rnd(q: int) -> Round:
        if q not in rounds:
            rounds[q] = Round(round=q)
        return rounds[q]

    for m in msgs:
        q = int(m["quest"])
        text = m["msg"]
        if m["player"] == "system":
            system_all.append(text)
            pending.append(text)
            continue
        if m["player"] not in ids:
            unknown_speakers += 1
            continue
        r = rnd(q)
        if q not in started:
            started.add(q)
            r.public_events = pending
            pending = []
        lab = labels.get(m["mid"])
        if lab and lab.get("deception"):
            lies.append({"round": q, "index": len(r.statements), "player_id": m["player"],
                         "strategy": lab["deception"]})
        r.statements.append(Statement(m["player"], text))

    if pending:
        rnd(max(rounds, default=0) + 1).public_events = pending

    winner = _winner(system_all)
    meta = {
        "dataset": "sstepput/Avalon-NLU",
        "license": LICENSE,
        "raw_game": path.name,
        "winner_team": {"evil": "deceptive", "good": "honest"}.get(winner or ""),
        "round_unit": "quest",
        "lies": lies,
        "n_beliefs": len(d.get("beliefs", {})),
        "unknown_speakers": unknown_speakers,
    }
    t = Transcript(game_id=game_id, source=SOURCE, players=players,
                   rounds=[rounds[q] for q in sorted(rounds)], winner=winner, meta=meta)
    return validate(t)


def load_all(raw_dir: Path | None = None) -> list[Transcript]:
    return [load_game(p) for p in game_files(raw_dir)]
