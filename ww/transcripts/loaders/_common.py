"""Shared helpers for the dataset loaders: the Mafia-style phase builder,
writing a converted source, and per-source statistics."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ww.config import transcripts_dir
from ww.transcripts.schema import (
    Night,
    PlayerInfo,
    Round,
    Statement,
    Transcript,
    iter_source,
    load,
    save,
    validate,
)


@dataclass
class Phase:
    """One day or night phase of a Mafia-style game, already parsed.

    texts: (speaker, text) in order. For a day these are the public statements;
    for a night they are the mafia's private chat.
    votes: (voter, target) in order; the last vote per voter counts.
    eliminated: who left the game when this phase ended (day vote or night kill).
    announcement: the public text announcing that elimination (shown at the
    start of the next day).
    """

    kind: str  # "day" | "night"
    texts: list[tuple[str, str]] = field(default_factory=list)
    votes: list[tuple[str, str]] = field(default_factory=list)
    eliminated: str | None = None
    announcement: str | None = None
    info: list[str] = field(default_factory=list)  # raw "info" rows of this phase


def last_votes(votes: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for voter, target in votes:
        out.pop(voter, None)  # keep insertion order = order of final votes
        out[voter] = target
    return out


def build_mafia_transcript(
    game_id: str,
    source: str,
    players: list[PlayerInfo],
    phases: list[Phase],
    winner: str | None,
    meta: dict[str, Any],
) -> Transcript:
    """Turn a list of day/night phases into schema rounds.

    A night opens a new round (its info goes into that round's private `night`);
    the following day fills the same round. A day with no night before it opens
    a round with an empty night (e.g. LLM Mafia starts with a day). The
    announcements of every elimination since the previous day become the next
    day's `public_events`. If eliminations are still unannounced at the end, a
    final round with no statements carries them (and any trailing night).
    """
    rounds: list[Round] = []
    pending: list[str] = []
    cur: Round | None = None  # a round whose night is filled but day is not yet

    def new_round() -> Round:
        r = Round(round=len(rounds) + 1)
        rounds.append(r)
        return r

    for ph in phases:
        if ph.kind == "night":
            cur = new_round()
            extra: dict[str, Any] = {}
            if ph.info:
                extra["info"] = list(ph.info)
            if ph.texts:
                extra["mafia_chat"] = [{"player_id": p, "text": t} for p, t in ph.texts]
            if ph.votes:
                extra["mafia_votes"] = last_votes(ph.votes)
            cur.night = Night(kill=ph.eliminated, extra=extra)
            if ph.announcement:
                pending.append(ph.announcement)
        elif ph.kind == "day":
            r = cur if cur is not None else new_round()
            cur = None
            r.public_events = pending
            pending = []
            r.statements = [Statement(p, t) for p, t in ph.texts]
            v = last_votes(ph.votes)
            r.votes = v or None
            r.eliminated = ph.eliminated
            if ph.announcement:
                pending.append(ph.announcement)
        else:
            raise ValueError(f"unknown phase kind {ph.kind!r}")

    if pending:
        r = cur if cur is not None else new_round()
        r.public_events = pending

    t = Transcript(game_id=game_id, source=source, players=players, rounds=rounds,
                   winner=winner, meta=meta)
    return validate(t)


def write_source(transcripts: Iterable[Transcript], source: str,
                 out_dir: Path | None = None) -> Path:
    """Validate and save each game to $WW_DATA_DIR/transcripts/<source>/."""
    out = Path(out_dir) if out_dir else transcripts_dir(source)
    n = 0
    for t in transcripts:
        save(t, out / f"{t.game_id}.json")
        n += 1
    print(f"[{source}] wrote {n} games to {out}")
    return out


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def stats(transcripts: list[Transcript]) -> dict[str, Any]:
    """Per-source numbers for the progress file."""
    players = [len(t.players) for t in transcripts]
    rounds = [len(t.rounds) for t in transcripts]
    spoken = [r for t in transcripts for r in t.rounds if r.statements]
    n_stmt = sum(len(r.statements) for r in spoken)
    dec_players = sum(len(t.deceptive_ids()) for t in transcripts)
    dec_stmt = 0
    for t in transcripts:
        d = t.deceptive_ids()
        dec_stmt += sum(1 for r in t.rounds for s in r.statements if s.player_id in d)
    chars = [sum(len(s.text) for s in r.statements) for r in spoken]
    stmt_per_round = [len(r.statements) for r in spoken]
    return {
        "games": len(transcripts),
        "players_total": sum(players),
        "players_per_game": (min(players), _median(players), max(players)) if players else None,
        "rounds_total": sum(rounds),
        "rounds_with_statements": len(spoken),
        "rounds_per_game": (min(rounds), _median(rounds), max(rounds)) if rounds else None,
        "statements": n_stmt,
        "statements_per_round_median": _median(stmt_per_round),
        "deceptive_share_players": dec_players / sum(players) if players else None,
        "deceptive_share_statements": dec_stmt / n_stmt if n_stmt else None,
        "chars_per_round_median": _median(chars),
        "chars_per_round_p90": (sorted(chars)[int(0.9 * (len(chars) - 1))] if chars else None),
        "chars_per_round_max": max(chars) if chars else None,
    }


def load_source(source: str, out_dir: Path | None = None) -> list[Transcript]:
    d = Path(out_dir) if out_dir else transcripts_dir(source)
    return [load(p) for p in iter_source(d)]


def format_stats(source: str, s: dict[str, Any]) -> str:
    def f(x: Any) -> str:
        if isinstance(x, float):
            return f"{x:.3f}"
        if isinstance(x, tuple):
            return "/".join(f(v) for v in x)
        return str(x)
    lines = [f"{source}:"] + [f"  {k}: {f(v)}" for k, v in s.items()]
    return "\n".join(lines)
