import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def pipeline_artifacts():
    """Make sure data + model exist (CI runs the pipeline first; this is a fallback)."""
    from src.config import MODEL_FILE
    if not MODEL_FILE.exists():
        from src import pipeline  # noqa: F401
        from src.data import generate, prepare
        from src.features import build
        from src.models import train
        for step in (generate, prepare, build, train):
            step.main()


@pytest.fixture()
def store_db(tmp_path, monkeypatch):
    """Isolated, freshly seeded store database for each test."""
    from app import db
    monkeypatch.setattr(db, "PHARMACY_DB", tmp_path / "test.db")
    conn = db.connect()
    conn.executescript(db.SCHEMA)
    conn.close()
    db.seed(days=45)
    return db
