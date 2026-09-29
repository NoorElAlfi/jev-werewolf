import shutil
from pathlib import Path

import pytest

from tests.fakes import FakeOllama, FakeTypeSafe
from ww.clients import use_clients

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
VALID_FIXTURES = sorted(p for p in FIXTURES.glob("*.json") if p.stem != "fx_invalid")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Every test gets its own empty WW_DATA_DIR, so tests never touch real data."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("WW_DATA_DIR", str(d))
    return d


def _no_anthropic():
    raise RuntimeError("tests must not reach the Anthropic API; inject a fake with use_anthropic()")


@pytest.fixture(autouse=True)
def fake_clients():
    """Every test runs with the offline fakes injected, so nothing reaches the network."""
    FakeTypeSafe.reset()
    ollama = FakeOllama()
    with use_clients(ollama=ollama, typesafe=FakeTypeSafe, anthropic=_no_anthropic):
        yield {"ollama": ollama, "typesafe": FakeTypeSafe}


@pytest.fixture
def fixture_source(data_dir):
    """Copies the valid fixtures into $WW_DATA_DIR/transcripts/fixtures/."""
    dest = data_dir / "transcripts" / "fixtures"
    dest.mkdir(parents=True)
    for p in VALID_FIXTURES:
        shutil.copy(p, dest / p.name)
    return "fixtures"
