import argparse
import json

from ww.transcripts.loaders import LOADERS, convert
from ww.transcripts.loaders._common import format_stats, load_source, stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m ww.transcripts.loaders")
    ap.add_argument("sources", nargs="+", help=f"{list(LOADERS)} or 'all'")
    ap.add_argument("--stats", action="store_true",
                    help="only print stats of already-converted transcripts")
    ap.add_argument("--json", action="store_true", help="print stats as JSON")
    a = ap.parse_args()
    sources = list(LOADERS) if a.sources == ["all"] else a.sources
    for s in sources:
        ts = load_source(s) if a.stats else convert(s)
        st = stats(ts)
        print(json.dumps({s: st}) if a.json else format_stats(s, st))


if __name__ == "__main__":
    main()
