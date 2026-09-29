"""Load transcripts and cached judgments into metric tables.

A judge is found by its judgment folder: $WW_DATA_DIR/judgments/<judge_key>/,
whose _config.json names the judge. `resolve_judges` accepts full keys or
judge names.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ww.config import judgments_dir, transcripts_dir
from ww.eval.metrics import GameTable
from ww.judges.base import load_judgments
from ww.transcripts.schema import Transcript, iter_source, load

log = logging.getLogger(__name__)


@dataclass
class JudgeRef:
    key: str  # judge_key folder name
    name: str  # judge name from _config.json
    label: str  # what the report shows
    config: dict[str, Any] = field(default_factory=dict)


def available_judges(root: Path | None = None) -> list[JudgeRef]:
    root = root or judgments_dir()
    out = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        cfg_path = d / "_config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        out.append(JudgeRef(key=d.name, name=cfg.get("judge", d.name.rsplit("-", 1)[0]), label=d.name, config=cfg.get("config", {})))
    return out


def resolve_judges(requested: list[str] | None, root: Path | None = None) -> tuple[list[JudgeRef], list[str]]:
    """Map each requested item (a judge_key, or a judge name) to judgment
    folders. Returns (judges in request order, requested items not found).
    With no request, every folder is used. Labels are the judge name when that
    name maps to one folder, else the full key."""
    avail = available_judges(root)
    if not requested:
        chosen = avail
        missing: list[str] = []
    else:
        chosen, missing = [], []
        for item in requested:
            hits = [j for j in avail if j.key == item] or [j for j in avail if j.name == item]
            if not hits:
                missing.append(item)
            for j in hits:
                if j.key not in {c.key for c in chosen}:
                    chosen.append(j)
    names = [j.name for j in chosen]
    for j in chosen:
        j.label = j.name if names.count(j.name) == 1 else j.key
    return chosen, missing


def load_source(source: str) -> dict[str, Transcript]:
    """All transcripts of a source, keyed by game_id. Invalid files are skipped with a warning."""
    out = {}
    for f in iter_source(transcripts_dir(source)):
        try:
            t = load(f)
        except Exception as e:  # noqa: BLE001 - report and continue
            log.warning("skipping invalid transcript %s: %s", f, e)
            continue
        out[t.game_id] = t
    return out


@dataclass
class JudgeData:
    """Metric tables for one judge on one source. Game ids are `<source>/<game_id>`."""

    players: GameTable  # one row per (game, round, scored player): round, y, p, conf, has_conf
    statements: GameTable  # one row per statement in a judged round: round, y, p, conf
    calls: GameTable  # one row per judge.score call (round): round, latency, cost
    n_games_total: int  # transcripts in the source
    n_games_judged: int  # transcripts with a judgment file
    n_games_complete: int
    notes: list[str] = field(default_factory=list)


def _f(x: Any) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else math.nan


def build_tables(source: str, transcripts: dict[str, Transcript], judgments: dict[str, dict[str, Any]]) -> JudgeData:
    pg, pr, py, pp, pc, phc = [], [], [], [], [], []
    sg, sr, sy, sp, sc = [], [], [], [], []
    cg, cr, cl, cc = [], [], [], []
    notes: list[str] = []
    unknown_players = 0
    orphan = sorted(set(judgments) - set(transcripts))
    if orphan:
        notes.append(f"{len(orphan)} judgment file(s) have no transcript and were ignored")
    complete = 0
    for gid, j in judgments.items():
        t = transcripts.get(gid)
        if t is None:
            continue
        complete += bool(j.get("complete"))
        team = t.team_of()
        rounds_by_num = {r.round: r for r in t.rounds}
        g = f"{source}/{gid}"
        for rkey, res in sorted(j.get("rounds", {}).items(), key=lambda kv: int(kv[0])):
            rn = int(rkey)
            p_dec = res.get("p_deceptive") or {}
            conf = res.get("confidence") or {}
            meta = res.get("meta") or {}
            cg.append(g); cr.append(rn); cl.append(_f(meta.get("latency_s"))); cc.append(_f(meta.get("cost_usd")))
            scored = {}
            for pid, p in p_dec.items():
                if pid not in team:
                    unknown_players += 1
                    continue
                c = _f(conf.get(pid))
                scored[pid] = (float(p), c)
                pg.append(g); pr.append(rn); py.append(int(team[pid] == "deceptive")); pp.append(float(p))
                pc.append(c if not math.isnan(c) else abs(float(p) - 0.5)); phc.append(not math.isnan(c))
            r = rounds_by_num.get(rn)
            if r is None:
                continue
            for s in r.statements:
                if s.player_id in scored:
                    p, c = scored[s.player_id]
                    sg.append(g); sr.append(rn); sy.append(int(team[s.player_id] == "deceptive")); sp.append(p)
                    sc.append(c if not math.isnan(c) else abs(p - 0.5))
    if unknown_players:
        notes.append(f"{unknown_players} score(s) for players not in the transcript were ignored")
    return JudgeData(
        players=GameTable(pg, round=pr, y=py, p=pp, conf=pc, has_conf=phc),
        statements=GameTable(sg, round=sr, y=sy, p=sp, conf=sc),
        calls=GameTable(cg, round=cr, latency=cl, cost=cc),
        n_games_total=len(transcripts),
        n_games_judged=len(set(judgments) & set(transcripts)),
        n_games_complete=complete,
        notes=notes,
    )


def load_judge_data(judge: JudgeRef, source: str, transcripts: dict[str, Transcript], root: Path | None = None) -> JudgeData:
    return build_tables(source, transcripts, load_judgments(judge.key, source, root))
