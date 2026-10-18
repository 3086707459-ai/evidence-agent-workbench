from pathlib import Path

import pytest

from evidence_agent.store import Store

CORPUS = Path(__file__).resolve().parents[1] / "corpus"


class Clock:
    def __init__(self):
        self.value = 1700000000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    result = Store(tmp_path / "agent.db", clock=clock)
    result.ingest(CORPUS)
    return result
