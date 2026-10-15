"""Shared fixtures: every test gets its own throw-away workspace, so tests never touch real data."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from server import engine as E

    monkeypatch.setattr(E, "WS", tmp_path)
    for attr, name in (("REG", "sources.json"), ("PINS", "pins.json"), ("CONTRACTS", "contracts.json"), ("SCHEMAS", "schema_baselines.json")):
        monkeypatch.setattr(E, attr, tmp_path / name)
    monkeypatch.setattr(E, "_eng", {})
    monkeypatch.setattr(E, "_prof", {})
    from server.main import app

    return TestClient(app)


@pytest.fixture()
def sample(client):
    """The bundled 5-table ride-share dataset, loaded as a source."""
    return client.post("/api/sources/sample").json()["id"]


def upload(client, name, files):
    """Upload {filename: csv_text} as one source and return its id."""
    payload = [("files", (fn, txt.encode(), "text/csv")) for fn, txt in files.items()]
    r = client.post("/api/sources/file", data={"name": name}, files=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]
