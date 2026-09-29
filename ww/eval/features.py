"""Richer-Jev features -> logistic regression (Wave 4 H).

Rows are player-rounds (one per scored player per judged view), the same unit as the
report's player-round AUC. Features come from cached judgments:

- multidim:  md_<dimension> = jev_multidim's normalized level (0..1) per dimension
- noul:      noul = jev_noul's P(concealing their true role)
- pairwise:  jev_pairwise's per-player features (round and cumulative, see
             ww.judges.jev_pairwise.pairwise_features), where pw_protects_suspect
             weighs protection by jev_batched's round-r p of the protected player
- batched:   jev_batched's p (the Wave 3 reference judge)

Every feature for round r comes from views up to round r only.

Model: StandardScaler + L2 logistic regression (C=1). Held-out predictions come
from GroupKFold(5) grouped by game, so no game is in both a training and a test
fold. AUC is computed on the pooled out-of-fold predictions, with a 95% CI from a
bootstrap that resamples whole games; "vs jev_batched" is a paired game bootstrap
on the same rows. Coefficients are fitted on all rows of a domain (standardized
features, so they are comparable within a domain) with game-bootstrap CIs.
Transfer: fit on one domain, predict another, compared with that domain's own
out-of-fold AUC on the same rows (paired bootstrap).

    python -m ww.eval.features --out reports/richer_jev_features.md
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ww.eval import metrics as M
from ww.eval.data import available_judges, load_source
from ww.judges.base import load_judgments
from ww.judges.jev_multidim import DIMENSIONS
from ww.judges.jev_pairwise import ROUND_FEATURES, pairwise_features

DOMAINS: dict[str, list[str]] = {
    "generated": ["llm_llama31_8b"],
    "human": ["human_mafia", "avalon", "werewolf_among_us"],
    "llm_mafia": ["llm_mafia"],
}
DEFAULT_BATCHED_KEY = "jev_batched-0650211bf044"  # Wave 3's jev_batched (90k window)

MD_FEATURES = [f"md_{d}" for d in DIMENSIONS]
PW_FEATURES = [f for base in (*ROUND_FEATURES, "pw_protects_suspect") for f in (base, base + "_cum")]
FEATURE_SETS: dict[str, list[str]] = {
    "jev_batched p (reference)": ["batched"],
    "multidim": MD_FEATURES,
    "noul": ["noul"],
    "pairwise": PW_FEATURES,
    "multidim+noul+pairwise": MD_FEATURES + ["noul"] + PW_FEATURES,
    "all + jev_batched p": MD_FEATURES + ["noul"] + PW_FEATURES + ["batched"],
}


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

@dataclass
class Rows:
    game: list[str] = field(default_factory=list)  # "<source>/<game_id>"
    source: list[str] = field(default_factory=list)
    round: list[int] = field(default_factory=list)
    player: list[str] = field(default_factory=list)
    y: list[int] = field(default_factory=list)
    feats: list[dict[str, float]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.y)

    def matrix(self, names: list[str]) -> tuple[np.ndarray, np.ndarray]:
        """(X, mask of rows with every feature present)."""
        X = np.array([[f.get(n, math.nan) for n in names] for f in self.feats], dtype=float).reshape(len(self), len(names))
        return X, ~np.isnan(X).any(axis=1)


def build_rows(sources: list[str], keys: dict[str, str | None]) -> Rows:
    """keys: {"multidim", "noul", "pairwise", "batched"} -> judge_key (None = skip)."""
    rows = Rows()
    for source in sources:
        transcripts = load_source(source)
        J = {k: (load_judgments(v, source) if v else {}) for k, v in keys.items()}
        for gid, t in transcripts.items():
            team = t.team_of()
            md, nl, pw, bt = (J[k].get(gid, {}).get("rounds", {}) for k in ("multidim", "noul", "pairwise", "batched"))
            matrices = {int(r): d["meta"]["raw"]["pairs"] for r, d in pw.items()}
            susp = {int(r): d["p_deceptive"] for r, d in bt.items()}
            round_nums = sorted({int(r) for j in (md, nl, pw, bt) for r in j})
            for rn in round_nums:
                players: set[str] = set()
                for j in (md, nl, pw, bt):
                    if str(rn) in j:
                        players |= set(j[str(rn)]["p_deceptive"])
                pwf = pairwise_features(matrices, rn, susp if susp else None) if rn in matrices else {}
                for pid in sorted(players):
                    if pid not in team:
                        continue
                    f: dict[str, float] = {}
                    if str(rn) in md and pid in md[str(rn)]["meta"]["raw"]["answers"]:
                        for d, a in md[str(rn)]["meta"]["raw"]["answers"][pid].items():
                            f[f"md_{d}"] = a["level"]
                    if str(rn) in nl and pid in nl[str(rn)]["p_deceptive"]:
                        f["noul"] = nl[str(rn)]["p_deceptive"][pid]
                    if pid in pwf:
                        f.update(pwf[pid])
                    if str(rn) in bt and pid in bt[str(rn)]["p_deceptive"]:
                        f["batched"] = bt[str(rn)]["p_deceptive"][pid]
                    rows.game.append(f"{source}/{gid}")
                    rows.source.append(source)
                    rows.round.append(rn)
                    rows.player.append(pid)
                    rows.y.append(int(team[pid] == "deceptive"))
                    rows.feats.append(f)
    return rows


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def make_model(C: float = 1.0):
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=2000))


def cv_oof(X: np.ndarray, y: np.ndarray, groups: np.ndarray, n_splits: int = 5, C: float = 1.0) -> np.ndarray:
    """Out-of-fold P(deceptive) with GroupKFold by game."""
    n_splits = min(n_splits, len(set(groups.tolist())))
    oof = np.full(len(y), math.nan)
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        assert not set(groups[tr].tolist()) & set(groups[te].tolist())
        if len(set(y[tr].tolist())) < 2:
            oof[te] = float(y[tr].mean())
            continue
        m = make_model(C).fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])[:, 1]
    return oof


def fit_coefs(X: np.ndarray, y: np.ndarray, groups: np.ndarray, names: list[str], n_boot: int = 200,
              seed: int = 0, C: float = 1.0) -> dict[str, M.CI]:
    """Standardized coefficients fitted on all rows, with game-bootstrap CIs."""
    est = make_model(C).fit(X, y)[-1].coef_[0]
    games = list(dict.fromkeys(groups.tolist()))
    idx: dict[str, list[int]] = {}
    for i, g in enumerate(groups.tolist()):
        idx.setdefault(g, []).append(i)
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        rows = np.concatenate([idx[games[i]] for i in rng.integers(0, len(games), len(games))])
        if len(set(y[rows].tolist())) < 2:
            continue
        boots.append(make_model(C).fit(X[rows], y[rows])[-1].coef_[0])
    B = np.array(boots)
    out = {}
    for j, n in enumerate(names):
        lo, hi = (np.percentile(B[:, j], [2.5, 97.5]) if len(B) > 1 else (math.nan, math.nan))
        out[n] = M.CI(float(est[j]), float(lo), float(hi), len(games))
    return out


def table(game, y, p) -> M.GameTable:
    return M.GameTable(np.asarray(game), y=np.asarray(y, dtype=int), p=np.asarray(p, dtype=float))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _paired(a: M.GameTable, b: M.GameTable, n_boot: int) -> str:
    d = M.paired_bootstrap(a, b, M.auc_stat, n_boot=n_boot)
    if math.isnan(d.diff):
        return "n/a"
    star = " **" if d.conclusive else ""
    return f"{d.diff:+.3f} [{d.lo:+.3f}, {d.hi:+.3f}]{star}"


def analyse(rows_by_domain: dict[str, Rows], n_boot: int = 1000, n_boot_coef: int = 200) -> list[str]:
    md: list[str] = []
    fitted: dict[tuple[str, str], Any] = {}
    oofs: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
    coefs: dict[str, dict[str, M.CI]] = {}

    md.append("## Held-out AUC (GroupKFold by game, 5 folds)\n")
    md.append("Rows: player-rounds that have every feature of the set. \"vs jev_batched p\" is the paired game "
              "bootstrap of (this set's out-of-fold AUC) minus (jev_batched's raw p AUC) on the same rows; "
              "** marks a CI that excludes 0.\n")
    md.append("| Domain | Feature set | Features | Rows | Games | Held-out AUC [95% CI] | vs jev_batched p |")
    md.append("|---|---|---|---|---|---|---|")
    for dom, rows in rows_by_domain.items():
        if not len(rows):
            continue
        y_all = np.asarray(rows.y)
        g_all = np.asarray(rows.game)
        for set_name, names in FEATURE_SETS.items():
            X, mask = rows.matrix(names)
            if mask.sum() < 20 or len(set(y_all[mask].tolist())) < 2 or len(set(g_all[mask].tolist())) < 2:
                md.append(f"| {dom} | {set_name} | {len(names)} | {int(mask.sum())} | - | not enough data | |")
                continue
            Xm, ym, gm = X[mask], y_all[mask], g_all[mask]
            if names == ["batched"]:
                oof = Xm[:, 0]  # the raw p, no model
            else:
                oof = cv_oof(Xm, ym, gm)
                fitted[(dom, set_name)] = make_model().fit(Xm, ym)
            oofs[(dom, set_name)] = (mask, oof)
            ci = M.bootstrap_ci(table(gm, ym, oof), M.auc_stat, n_boot=n_boot)
            bX, bmask = rows.matrix(["batched"])
            if names != ["batched"] and bmask[mask].all():
                vs = _paired(table(gm, ym, oof), table(gm, ym, bX[mask][:, 0]), n_boot)
            else:
                vs = ""
            md.append(f"| {dom} | {set_name} | {len(names)} | {int(mask.sum())} | {len(set(gm.tolist()))} | "
                      f"{ci.fmt()} | {vs} |")
    md.append("")

    # single features, no model
    md.append("## Single-feature AUC (the raw feature as the score, no model)\n")
    md.append("AUC below 0.5 means low values point to the deceptive team. Means are over deceptive / honest rows.\n")
    singles = [n for n in dict.fromkeys(sum(FEATURE_SETS.values(), [])) if not n.endswith("_cum")]
    doms = [d for d, r in rows_by_domain.items() if len(r)]
    md.append("| Feature | " + " | ".join(f"{d} AUC | {d} mean dec / hon" for d in doms) + " |")
    md.append("|---|" + "---|---|" * len(doms))
    for n in singles:
        cells = []
        for d in doms:
            rows = rows_by_domain[d]
            X, mask = rows.matrix([n])
            y, g = np.asarray(rows.y)[mask], np.asarray(rows.game)[mask]
            if mask.sum() < 20 or len(set(y.tolist())) < 2:
                cells += ["-", "-"]
                continue
            x = X[mask][:, 0]
            cells.append(M.bootstrap_ci(table(g, y, x), M.auc_stat, n_boot=n_boot).fmt())
            cells.append(f"{x[y == 1].mean():.3f} / {x[y == 0].mean():.3f}")
        md.append(f"| {n} | " + " | ".join(cells) + " |")
    md.append("")

    # per-source AUC inside the pooled human domain
    for dom, rows in rows_by_domain.items():
        srcs = sorted(set(rows.source))
        if len(srcs) < 2:
            continue
        md.append(f"### {dom}: out-of-fold AUC by source (pooled model)\n")
        md.append("| Feature set | " + " | ".join(srcs) + " |")
        md.append("|---|" + "---|" * len(srcs))
        src = np.asarray(rows.source)
        for set_name in FEATURE_SETS:
            if (dom, set_name) not in oofs:
                continue
            mask, oof = oofs[(dom, set_name)]
            cells = []
            for s in srcs:
                sm = src[mask] == s
                if sm.sum() < 10:
                    cells.append("-")
                    continue
                gm = np.asarray(rows.game)[mask][sm]
                ym = np.asarray(rows.y)[mask][sm]
                cells.append(M.bootstrap_ci(table(gm, ym, oof[sm]), M.auc_stat, n_boot=n_boot).fmt())
            md.append(f"| {set_name} | " + " | ".join(cells) + " |")
        md.append("")

    # coefficients
    coef_set = "multidim+noul+pairwise"
    names = FEATURE_SETS[coef_set]
    md.append(f"## Coefficients ({coef_set}, standardized, fitted on all rows of a domain)\n")
    md.append("Log-odds change per standard deviation of the feature, holding the others fixed. 95% CIs from "
              f"{n_boot_coef} game-bootstrap refits; ** marks a CI that excludes 0. Correlated features share "
              "weight, so read signs with care.\n")
    doms = [d for d in rows_by_domain if (d, coef_set) in fitted]
    for dom in doms:
        rows = rows_by_domain[dom]
        X, mask = rows.matrix(names)
        coefs[dom] = fit_coefs(X[mask], np.asarray(rows.y)[mask], np.asarray(rows.game)[mask], names,
                               n_boot=n_boot_coef)
    if doms:
        md.append("| Feature | " + " | ".join(doms) + " |")
        md.append("|---|" + "---|" * len(doms))
        for n in names:
            cells = []
            for dom in doms:
                c = coefs[dom][n]
                star = " **" if not (c.lo <= 0 <= c.hi) else ""
                cells.append(f"{c.est:+.2f} [{c.lo:+.2f}, {c.hi:+.2f}]{star}")
            md.append(f"| {n} | " + " | ".join(cells) + " |")
        md.append("")
        if len(doms) >= 2:
            md.append("Agreement of coefficient vectors between domains (sign agreement among features "
                      "significant in both; cosine similarity of all coefficients):\n")
            md.append("| Domains | Significant in both | Same sign | Cosine |")
            md.append("|---|---|---|---|")
            for i, a in enumerate(doms):
                for b in doms[i + 1:]:
                    va = np.array([coefs[a][n].est for n in names])
                    vb = np.array([coefs[b][n].est for n in names])
                    sig = [n for n in names if not (coefs[a][n].lo <= 0 <= coefs[a][n].hi)
                           and not (coefs[b][n].lo <= 0 <= coefs[b][n].hi)]
                    same = sum(np.sign(coefs[a][n].est) == np.sign(coefs[b][n].est) for n in sig)
                    cos = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-12))
                    md.append(f"| {a} vs {b} | {len(sig)} | {same} | {cos:+.2f} |")
            md.append("")

    # transfer
    md.append("## Transfer: train on one domain, test on another\n")
    md.append("\"In-domain\" is the target domain's own out-of-fold AUC on the same rows; the last column is "
              "transfer minus in-domain (paired game bootstrap).\n")
    md.append("| Feature set | Train | Test | Transfer AUC [95% CI] | In-domain AUC | Transfer - in-domain |")
    md.append("|---|---|---|---|---|---|")
    for set_name in ("multidim", "multidim+noul+pairwise", "all + jev_batched p"):
        names = FEATURE_SETS[set_name]
        for a in rows_by_domain:
            if (a, set_name) not in fitted:
                continue
            for b, rows_b in rows_by_domain.items():
                if b == a or (b, set_name) not in oofs:
                    continue
                X, mask = rows_b.matrix(names)
                gm, ym = np.asarray(rows_b.game)[mask], np.asarray(rows_b.y)[mask]
                p_tr = fitted[(a, set_name)].predict_proba(X[mask])[:, 1]
                ci_tr = M.bootstrap_ci(table(gm, ym, p_tr), M.auc_stat, n_boot=n_boot)
                _, oof_b = oofs[(b, set_name)]
                ci_in = M.bootstrap_ci(table(gm, ym, oof_b), M.auc_stat, n_boot=n_boot)
                diff = _paired(table(gm, ym, p_tr), table(gm, ym, oof_b), n_boot)
                md.append(f"| {set_name} | {a} | {b} | {ci_tr.fmt()} | {ci_in.fmt()} | {diff} |")
    md.append("")
    return md


def resolve_key(name: str, given: str | None) -> str | None:
    if given:
        return None if given == "none" else given
    keys = [j.key for j in available_judges() if j.name == name]
    if not keys:
        return None
    if len(keys) > 1:
        raise SystemExit(f"several judgment folders for {name}: {keys}; pass --{name.split('_', 1)[1]} KEY")
    return keys[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--multidim", default=None, help="judge_key (default: the only jev_multidim folder)")
    ap.add_argument("--noul", default=None)
    ap.add_argument("--pairwise", default=None)
    ap.add_argument("--batched", default=DEFAULT_BATCHED_KEY)
    ap.add_argument("--domains", default=",".join(DOMAINS), help="subset of " + ",".join(DOMAINS))
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--n-boot-coef", type=int, default=200)
    a = ap.parse_args(argv)
    keys = {"multidim": resolve_key("jev_multidim", a.multidim), "noul": resolve_key("jev_noul", a.noul),
            "pairwise": resolve_key("jev_pairwise", a.pairwise), "batched": a.batched}
    rows = {d: build_rows(DOMAINS[d], keys) for d in a.domains.split(",")}
    lines = ["# Richer Jev features: logistic regression", "",
             "Generated by `python -m ww.eval.features " + " ".join(argv if argv is not None else sys.argv[1:]) + "`.",
             "", "Judge keys: " + ", ".join(f"{k}=`{v}`" for k, v in keys.items()), "",
             "Domains: " + "; ".join(f"{d} = {', '.join(DOMAINS[d])} ({len(r)} player-rounds, "
                                     f"{len(set(r.game))} games)" for d, r in rows.items()), ""]
    lines += analyse(rows, n_boot=a.n_boot, n_boot_coef=a.n_boot_coef)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
