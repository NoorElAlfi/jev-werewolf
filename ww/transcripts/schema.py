"""Shared transcript format. One JSON file per game at
$WW_DATA_DIR/transcripts/<source>/<game_id>.json.

Round conventions (every generator and loader follows these):
  - `night` is the night BEFORE this round's day discussion. Round 1 usually has
    an empty night. Night info is private: judges never see it.
  - `public_events` are announced at the start of the day: deaths from the
    preceding night, the previous day's elimination, revealed roles.
  - `statements` are the day's public statements, in speaking order.
  - `defense` (optional) is one accused player's rebuttal after the statements.
  - `eliminated` / `votes` record the day's vote outcome. They happen after the
    statements, so public_view() withholds them; announce an elimination to
    judges through the NEXT round's public_events.

`team` is "deceptive" (werewolf, mafioso, Morgana/Assassin, ...) or "honest",
so metrics work across games with different role names.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ww.config import TEAMS


class TranscriptValidationError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid transcript:\n  " + "\n  ".join(problems))


@dataclass
class PlayerInfo:
    id: str
    role: str
    team: str  # "deceptive" | "honest"


@dataclass
class Statement:
    player_id: str
    text: str


@dataclass
class SeerCheck:
    seer: str
    target: str
    result: str  # the target's team


@dataclass
class Night:
    kill: str | None = None
    seer_check: SeerCheck | None = None
    # Other private night info (e.g. doctor saves, dataset "info" rows).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Round:
    round: int
    night: Night = field(default_factory=Night)
    public_events: list[str] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    defense: Statement | None = None
    eliminated: str | None = None
    votes: dict[str, str] | None = None  # voter id -> target id


@dataclass
class Transcript:
    game_id: str
    source: str
    players: list[PlayerInfo]
    rounds: list[Round]
    winner: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # --- serialization ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = _drop_none(asdict(self))
        for r in d["rounds"]:
            if not r["night"].get("extra"):
                r["night"].pop("extra", None)
        return d

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Transcript:
        return cls(
            game_id=d["game_id"],
            source=d["source"],
            players=[PlayerInfo(**p) for p in d["players"]],
            rounds=[_round_from_dict(r) for r in d["rounds"]],
            winner=d.get("winner"),
            meta=dict(d.get("meta") or {}),
        )

    @classmethod
    def from_json(cls, s: str) -> Transcript:
        return cls.from_dict(json.loads(s))

    # --- helpers -----------------------------------------------------------
    def team_of(self) -> dict[str, str]:
        return {p.id: p.team for p in self.players}

    def deceptive_ids(self) -> set[str]:
        return {p.id for p in self.players if p.team == "deceptive"}


def _round_from_dict(r: dict[str, Any]) -> Round:
    night = r.get("night") or {}
    sc = night.get("seer_check")
    defense = r.get("defense")
    return Round(
        round=r["round"],
        night=Night(
            kill=night.get("kill"),
            seer_check=SeerCheck(**sc) if sc else None,
            extra=dict(night.get("extra") or {}),
        ),
        public_events=list(r.get("public_events") or []),
        statements=[Statement(**s) for s in r.get("statements") or []],
        defense=Statement(**defense) if defense else None,
        eliminated=r.get("eliminated"),
        votes=r.get("votes"),
    )


def _drop_none(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _drop_none(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_drop_none(v) for v in x]
    return x


# --- validation --------------------------------------------------------------

def validate(t: Transcript | dict[str, Any]) -> Transcript:
    """Return the Transcript if valid, else raise TranscriptValidationError
    listing every problem found. Accepts a parsed dict or a Transcript."""
    if isinstance(t, dict):
        try:
            t = Transcript.from_dict(t)
        except (KeyError, TypeError, AttributeError) as e:
            raise TranscriptValidationError([f"malformed structure: {e!r}"]) from e

    problems: list[str] = []

    def need_str(value: Any, where: str) -> None:
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where} must be a non-empty string")

    need_str(t.game_id, "game_id")
    need_str(t.source, "source")
    if not isinstance(t.meta, dict):
        problems.append("meta must be an object")
    if t.winner is not None and not isinstance(t.winner, str):
        problems.append("winner must be a string")

    if not t.players:
        problems.append("players must not be empty")
    ids: set[str] = set()
    for i, p in enumerate(t.players):
        need_str(p.id, f"players[{i}].id")
        need_str(p.role, f"players[{i}].role")
        if p.team not in TEAMS:
            problems.append(f"players[{i}].team must be one of {TEAMS}, got {p.team!r}")
        if p.id in ids:
            problems.append(f"duplicate player id {p.id!r}")
        ids.add(p.id)

    def known(pid: Any, where: str) -> None:
        if pid not in ids:
            problems.append(f"{where} refers to unknown player {pid!r}")

    last = None
    for i, r in enumerate(t.rounds):
        w = f"rounds[{i}]"
        if not isinstance(r.round, int) or isinstance(r.round, bool):
            problems.append(f"{w}.round must be an integer")
        elif last is not None and r.round <= last:
            problems.append(f"{w}.round must increase (got {r.round} after {last})")
        else:
            last = r.round
        if r.night.kill is not None:
            known(r.night.kill, f"{w}.night.kill")
        if r.night.seer_check is not None:
            known(r.night.seer_check.seer, f"{w}.night.seer_check.seer")
            known(r.night.seer_check.target, f"{w}.night.seer_check.target")
            if r.night.seer_check.result not in TEAMS:
                problems.append(f"{w}.night.seer_check.result must be one of {TEAMS}")
        for j, e in enumerate(r.public_events):
            if not isinstance(e, str):
                problems.append(f"{w}.public_events[{j}] must be a string")
        for j, s in enumerate(r.statements):
            known(s.player_id, f"{w}.statements[{j}].player_id")
            if not isinstance(s.text, str):
                problems.append(f"{w}.statements[{j}].text must be a string")
        if r.defense is not None:
            known(r.defense.player_id, f"{w}.defense.player_id")
            if not isinstance(r.defense.text, str):
                problems.append(f"{w}.defense.text must be a string")
        if r.eliminated is not None:
            known(r.eliminated, f"{w}.eliminated")
        if r.votes is not None:
            if not isinstance(r.votes, dict):
                problems.append(f"{w}.votes must be an object")
            else:
                for voter, target in r.votes.items():
                    known(voter, f"{w}.votes voter")
                    if target is not None:
                        known(target, f"{w}.votes target")

    if problems:
        raise TranscriptValidationError(problems)
    return t


# --- the judge-visible view ------------------------------------------------------

def public_view(t: Transcript, upto_round: int | None = None) -> dict[str, Any]:
    """The ONLY thing a judge may see: player IDs, and per round the public
    events, statements and defense, for rounds <= upto_round (all if None).

    Never includes roles, teams, night info, votes, eliminations, winner, meta,
    the source or the game_id. tests/test_public_view.py enforces this.
    """
    rounds = []
    for r in t.rounds:
        if upto_round is not None and r.round > upto_round:
            break
        vr: dict[str, Any] = {
            "round": r.round,
            "public_events": list(r.public_events),
            "statements": [{"player_id": s.player_id, "text": s.text} for s in r.statements],
        }
        if r.defense is not None:
            vr["defense"] = {"player_id": r.defense.player_id, "text": r.defense.text}
        rounds.append(vr)
    return {"players": [p.id for p in t.players], "rounds": rounds}


def current_speakers(view: dict[str, Any]) -> list[str]:
    """Players who spoke in the view's last round, in order: the players a
    judge should score for that view."""
    if not view["rounds"]:
        return []
    last = view["rounds"][-1]
    seen: list[str] = []
    for s in last["statements"]:
        if s["player_id"] not in seen:
            seen.append(s["player_id"])
    return seen


# --- file helpers ------------------------------------------------------------------

def load(path: str | Path) -> Transcript:
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def save(t: Transcript, path: str | Path) -> None:
    """Validate, then write atomically (temp file + rename) so a crash never
    leaves a half-written game that a resumable job would skip."""
    validate(t)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(t.to_json(), encoding="utf-8")
    tmp.replace(path)


def iter_source(source_dir: str | Path) -> list[Path]:
    """Sorted game files in a source folder."""
    return sorted(Path(source_dir).glob("*.json"))
