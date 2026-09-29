"""Build reports/site/jev-werewolf.html from template.html and the numbers in reports/*.md.

    python reports/site/build.py

Every number on the page is parsed from core.md, human.md, stress.md, live.md and
ablations.md (the template's hand-written copy also quotes a few figures; check those
against the reports after regenerating them). Published as a claude.ai artifact:
https://claude.ai/artifact/MYzp9jNYbx2wz1Jc8NY5pL
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORTS = HERE.parent


def ci(s):
    m = re.match(r"\s*([-+]?[\d.]+)\s*\[([-+]?[\d.]+),\s*([-+]?[\d.]+)\]", s)
    return [float(m.group(1)), float(m.group(2)), float(m.group(3))] if m else None


def cells(line):
    return [x.strip() for x in line.strip().strip("|").split("|")]


def judge_tables(out):
    for rep in ["core", "human"]:
        src = sec = None
        for line in open(REPORTS / f"{rep}.md", encoding="utf-8"):
            if line.startswith("## Source:"):
                src = re.search(r"`(.+?)`", line).group(1)
            elif line.startswith("## All sources pooled"):
                src = "pooled"
            if line.startswith("### "):
                sec = line.strip()
            if not (src and sec and line.startswith("| ")):
                continue
            c = cells(line)
            if "Accuracy" in sec and len(c) > 7 and ci(c[2]):
                out["auc"].setdefault(src, {})[c[0]] = ci(c[2])
                out["ece"].setdefault(src, {})[c[0]] = ci(c[7])
            if "Cost and latency" in sec and len(c) >= 7 and ci(c[2]):
                out["lat"].setdefault(src, {})[c[0]] = float(c[2].split()[0])
                out["cost"].setdefault(src, {})[c[0]] = float(c[6].split()[0].replace("$", ""))


def main():
    out = {"auc": {}, "ece": {}, "cost": {}, "lat": {}, "stress": {}, "live": {}, "abl": {}}
    judge_tables(out)
    for line in open(REPORTS / "stress.md", encoding="utf-8"):
        c = cells(line)
        if len(c) == 7 and c[1].isdigit() and ci(c[2]) and ci(c[3]):
            out["stress"][c[0]] = {"gptoss": ci(c[2]), "llama": ci(c[3]), "diff": ci(c[4])}
    for line in open(REPORTS / "live.md", encoding="utf-8"):
        c = cells(line)
        if len(c) >= 11 and c[1].isdigit() and "%" in c[2]:
            m = re.match(r"(\d+)% \[(\d+), (\d+)\]", c[2])
            out["live"][c[0]] = {"win": [int(m.group(i)) for i in (1, 2, 3)], "games": int(c[1]),
                                 "wolves_out": ci(c[4]), "no_elim": c[6], "top_wolf": c[7]}
    for line in open(REPORTS / "ablations.md", encoding="utf-8"):
        c = cells(line)
        if len(c) == 11 and c[0] in ("G", "L", "N") and c[1].startswith("`"):
            out["abl"].setdefault(c[0], {})["leak" if "leaky" in c[1] else "gen"] = c[5]
    page = (HERE / "template.html").read_text(encoding="utf-8")
    page = page.replace("__DATA__", json.dumps(out, separators=(",", ":")))
    (HERE / "jev-werewolf.html").write_text(page, encoding="utf-8")
    print("wrote", HERE / "jev-werewolf.html")


if __name__ == "__main__":
    main()
