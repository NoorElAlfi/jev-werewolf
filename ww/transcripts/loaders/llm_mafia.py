"""Loader for the LLM Mafia dataset ("Hidden in Plain Text", IEEE ICA 2025),
github.com/cocochief4/llm-mafia. License: CC0 1.0.

Raw layout (after unzipping LLM_Mafia_Dataset.zip): one folder per game
(`0631/`, ...) with player_names.txt, mafia_names.txt, who_wins.txt,
public_manager_chat.txt (phase changes and "X was voted out. Their role was
..." results), public_daytime_chat.txt (day chat plus "Game-Manager: X voted
for Y" lines) and public_nighttime_chat.txt (mafia-only chat and night votes).
Chat lines look like "[HH:MM:SS] Name: text"; a line without a "[" prefix
continues the previous message.

Conversion (same approach as human_mafia):
  - mafia -> "deceptive", bystander -> "honest".
  - Phases come from the manager's "Now it's Daytime/Nighttime" lines; every
    chat message is assigned to the phase whose window contains its timestamp
    (times are unwrapped across midnight). The game starts with a day, so
    round 1 has an empty night.
  - Day chat -> statements; night chat, night votes and the manager lines of
    that night -> round.night.extra; day votes -> round.votes.
  - The manager's result lines are used verbatim as the next day's
    public_events (that is exactly what the players were shown, including the
    role reveal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ww.config import data_dir
from ww.transcripts.loaders._common import Phase, build_mafia_transcript
from ww.transcripts.schema import PlayerInfo, Transcript

SOURCE = "llm_mafia"
LICENSE = "CC0-1.0"
MANAGER = "Game-Manager"

LINE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s?([^:]+?):\s?(.*)$")
VOTE = re.compile(r"^(.+?) voted for (.+?)\.?$")
OUT = re.compile(r"^(.+?) was voted out\.")
PHASE = re.compile(r"^Now it's (Daytime|Nighttime)")


@dataclass
class _Msg:
    t: int  # seconds, unwrapped
    speaker: str
    text: str


def default_raw_dir() -> Path:
    return data_dir() / "external" / "llm-mafia" / "unzipped"


def game_dirs(raw_dir: Path | None = None) -> list[Path]:
    raw = Path(raw_dir) if raw_dir else default_raw_dir()
    return sorted(p for p in raw.iterdir() if p.is_dir() and (p / "player_names.txt").exists())


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _parse_chat(path: Path, start: int) -> list[_Msg]:
    out: list[_Msg] = []
    for ln in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        m = LINE.match(ln)
        if m:
            h, mi, s, who, text = m.groups()
            t = int(h) * 3600 + int(mi) * 60 + int(s)
            if t < start - 3600:  # the game crossed midnight
                t += 86400
            out.append(_Msg(t, who.strip(), text.strip()))
        elif ln.strip() and out:
            out[-1].text += "\n" + ln.strip()
    return out


def load_game(game_dir: Path) -> Transcript:
    game_dir = Path(game_dir)
    game_id = game_dir.name
    names = _lines(game_dir / "player_names.txt")
    mafia = set(_lines(game_dir / "mafia_names.txt"))
    if len(set(names)) != len(names) or not mafia <= set(names):
        raise ValueError(f"{game_id}: bad player/mafia names")
    players = [PlayerInfo(id=n, role="mafia" if n in mafia else "bystander",
                          team="deceptive" if n in mafia else "honest") for n in names]

    st = _lines(game_dir / "game_start_time.txt")
    if st:
        h, mi, s = (int(x) for x in st[0].split(":"))
        start = h * 3600 + mi * 60 + s
    else:
        start = _parse_chat(game_dir / "public_manager_chat.txt", 0)[0].t

    manager = _parse_chat(game_dir / "public_manager_chat.txt", start)
    phases: list[Phase] = []
    starts: list[int] = []
    for m in manager:
        pm = PHASE.match(m.text)
        if pm:
            phases.append(Phase(kind="day" if pm.group(1) == "Daytime" else "night"))
            starts.append(m.t)
            continue
        om = OUT.match(m.text)
        if om and phases:
            ph = phases[-1]
            ph.eliminated = om.group(1).strip()
            ph.announcement = m.text
            ph.info.append(m.text)
        elif phases:
            phases[-1].info.append(m.text)

    def phase_at(t: int) -> int | None:
        idx = None
        for i, s in enumerate(starts):
            if s <= t:
                idx = i
        return idx

    dropped = {"outside_phase": 0, "wrong_phase_kind": 0, "unknown_vote": 0}
    for fname, kind in (("public_daytime_chat.txt", "day"),
                        ("public_nighttime_chat.txt", "night")):
        for m in _parse_chat(game_dir / fname, start):
            i = phase_at(m.t)
            # A vote cast at the same second the next phase starts belongs to
            # the phase that just ended.
            if i is not None and phases[i].kind != kind and i > 0 and phases[i - 1].kind == kind \
                    and m.t == starts[i]:
                i -= 1
            if i is None:
                dropped["outside_phase"] += 1
                continue
            ph = phases[i]
            if ph.kind != kind:
                dropped["wrong_phase_kind"] += 1
                continue
            if m.speaker == MANAGER:
                vm = VOTE.match(m.text)
                if vm:
                    voter, target = vm.group(1).strip(), vm.group(2).strip()
                    if voter in names and target in names:
                        ph.votes.append((voter, target))
                    else:
                        dropped["unknown_vote"] += 1
                continue
            if m.speaker not in names:
                raise ValueError(f"{game_id}: unknown speaker {m.speaker!r}")
            ph.texts.append((m.speaker, m.text))

    win_lines = _lines(game_dir / "who_wins.txt")
    win = win_lines[0].lower() if win_lines else ""
    winner = "mafia" if win.startswith("mafia") else "bystanders" if win.startswith("bystander") else None

    meta = {
        "dataset": "cocochief4/llm-mafia",
        "license": LICENSE,
        "raw_game": game_id,
        "generator": "GPT-4o agents",
        "winner_team": {"mafia": "deceptive", "bystanders": "honest"}.get(winner or ""),
        "day_votes_were_public": True,
        "dropped": dropped,
    }
    return build_mafia_transcript(game_id, SOURCE, players, phases, winner, meta)


def load_all(raw_dir: Path | None = None) -> list[Transcript]:
    return [load_game(d) for d in game_dirs(raw_dir)]
