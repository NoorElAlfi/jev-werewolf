"""Download the external datasets into $WW_DATA_DIR/external/.

    python scripts/download_datasets.py            # all four
    python scripts/download_datasets.py avalon     # just one

Idempotent: a dataset whose folder already exists is skipped (pass --force to
re-download). Writes $WW_DATA_DIR/external/MANIFEST.json with the source URL,
commit/revision, license and download date of each dataset.

Datasets (PLAN.md, "External datasets"; Mafiascum is deliberately left out):
  mafia-dataset      github omonida/mafia-dataset      NO LICENSE STATED: research use only,
                                                       do not redistribute
  llm-mafia          github cocochief4/llm-mafia       CC0 1.0
  Avalon-NLU         github sstepput/Avalon-NLU        MIT
  werewolf-among-us  HF bolinlai/Werewolf-Among-Us     Apache 2.0 (text files only; no videos
                                                       or video features)

The downloaded data is git-ignored and must never be committed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ww.config import data_dir  # noqa: E402

GITHUB = {
    "mafia-dataset": {
        "url": "https://github.com/omonida/mafia-dataset.git",
        "license": "none stated (research use only; do not redistribute)",
        "unzip": ["mafia_dataset.zip"],
    },
    "llm-mafia": {
        "url": "https://github.com/cocochief4/llm-mafia.git",
        "license": "CC0-1.0 (LICENSE file in repo)",
        "unzip": ["LLM_Mafia_Dataset.zip"],
    },
    "Avalon-NLU": {
        "url": "https://github.com/sstepput/Avalon-NLU.git",
        "license": "MIT (LICENSE file in repo)",
        "unzip": [],
    },
}

HF_REPO = "bolinlai/Werewolf-Among-Us"
HF_LICENSE = "Apache-2.0 (HF dataset card)"
# Text only: skip videos and the video-feature zips.
HF_SKIP = ("/videos/", "mvit_24_k400_features")

ALIASES = {
    "human_mafia": "mafia-dataset",
    "llm_mafia": "llm-mafia",
    "avalon": "Avalon-NLU",
    "werewolf_among_us": "werewolf-among-us",
}
ALL = ["mafia-dataset", "llm-mafia", "Avalon-NLU", "werewolf-among-us"]


def external_dir() -> Path:
    return data_dir() / "external"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def download_github(name: str, force: bool) -> dict:
    spec = GITHUB[name]
    dest = external_dir() / name
    if dest.exists() and force:
        shutil.rmtree(dest)
    if not dest.exists():
        print(f"[{name}] cloning {spec['url']}")
        _git("clone", "--depth", "1", spec["url"], str(dest))
    else:
        print(f"[{name}] already present, skipping clone")
    for z in spec["unzip"]:
        out = dest / "unzipped"
        if not out.exists():
            print(f"[{name}] unzipping {z}")
            with zipfile.ZipFile(dest / z) as zf:
                zf.extractall(out)
    return {"source": spec["url"], "revision": _git("rev-parse", "HEAD", cwd=dest),
            "license": spec["license"]}


def _hf_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "jev-ww-downloader"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def download_hf(force: bool) -> dict:
    dest = external_dir() / "werewolf-among-us"
    if dest.exists() and force:
        shutil.rmtree(dest)
    info = json.loads(_hf_get(f"https://huggingface.co/api/datasets/{HF_REPO}"))
    sha = info["sha"]
    files = [s["rfilename"] for s in info["siblings"]
             if not any(k in s["rfilename"] for k in HF_SKIP)]
    got = 0
    for f in files:
        out = dest / f
        if out.exists():
            continue
        url = (f"https://huggingface.co/datasets/{HF_REPO}/resolve/{sha}/"
               + urllib.parse.quote(f))
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".part")
        tmp.write_bytes(_hf_get(url))
        tmp.replace(out)
        got += 1
    print(f"[werewolf-among-us] {got} downloaded, {len(files) - got} already present "
          f"(videos and video features skipped)")
    return {"source": f"https://huggingface.co/datasets/{HF_REPO}", "revision": sha,
            "license": HF_LICENSE, "files": len(files)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("datasets", nargs="*", help=f"any of {ALL} or {list(ALIASES)}")
    ap.add_argument("--force", action="store_true", help="re-download")
    a = ap.parse_args(argv)
    names = [ALIASES.get(n, n) for n in a.datasets] or ALL
    for n in names:
        if n not in ALL:
            ap.error(f"unknown dataset {n!r}")

    external_dir().mkdir(parents=True, exist_ok=True)
    manifest_path = external_dir() / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for n in names:
        entry = download_hf(a.force) if n == "werewolf-among-us" else download_github(n, a.force)
        entry["downloaded"] = _dt.date.today().isoformat()
        manifest[n] = entry
        manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
