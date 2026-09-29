"""Dataset loaders: convert each external dataset into the shared transcript
schema and write it to $WW_DATA_DIR/transcripts/<source>/.

    python -m ww.transcripts.loaders human_mafia      # convert + print stats
    python -m ww.transcripts.loaders --stats all      # stats of converted data

Each loader module exposes SOURCE, LICENSE, default_raw_dir(), load_game(...)
and load_all(raw_dir=None) -> list[Transcript] (every game already validated).
"""

from importlib import import_module

LOADERS = {
    "human_mafia": "ww.transcripts.loaders.human_mafia",
    "llm_mafia": "ww.transcripts.loaders.llm_mafia",
    "avalon": "ww.transcripts.loaders.avalon",
    "werewolf_among_us": "ww.transcripts.loaders.werewolf_among_us",
}


def get_loader(source: str):
    return import_module(LOADERS[source])


def convert(source: str, raw_dir=None, out_dir=None):
    """Load every game of `source` and write it; returns the transcripts."""
    from ww.transcripts.loaders._common import write_source

    ts = get_loader(source).load_all(raw_dir)
    write_source(ts, source, out_dir)
    return ts
