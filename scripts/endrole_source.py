"""Werewolf Among Us with END-role labels (Wave 3 E robustness check).

Writes $WW_DATA_DIR/transcripts/werewolf_among_us_endrole/: the same games, but each
player's role/team comes from their end role (meta.team_changed). Judges see only
public_view(), which has no roles or teams, so the views are identical and the
judgments are copied over instead of re-running any judge.

    python scripts/endrole_source.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ww.config import judgments_dir, transcripts_dir  # noqa: E402
from ww.transcripts.schema import iter_source, load, public_view, save  # noqa: E402

SRC, DST = "werewolf_among_us", "werewolf_among_us_endrole"
DECEPTIVE_ROLES = {"Werewolf", "Minion"}  # same rule as the start-role loader


def main() -> int:
    out = transcripts_dir(DST)
    out.mkdir(parents=True, exist_ok=True)
    changed = 0
    for f in iter_source(transcripts_dir(SRC)):
        t = load(f)
        orig = load(f)
        ends = {c["player_id"]: c["end_role"] for c in t.meta.get("team_changed", [])}
        for p in t.players:
            if p.id in ends:
                p.role = ends[p.id]
                p.team = "deceptive" if p.role in DECEPTIVE_ROLES else "honest"
                changed += 1
        t.source = DST
        t.meta["label_from"] = "end role"
        for r in t.rounds:
            assert public_view(t, r.round) == public_view(orig, r.round)
        save(t, out / f.name)
    n_j = 0
    for kdir in judgments_dir().iterdir():
        src = kdir / SRC
        if src.is_dir():
            dst = kdir / DST
            dst.mkdir(exist_ok=True)
            for jf in src.glob("*.json"):
                d = json.loads(jf.read_text(encoding="utf-8"))
                d["source"] = DST
                (dst / jf.name).write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
                n_j += 1
    print(f"{DST}: {changed} player labels changed; {n_j} judgment files copied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
