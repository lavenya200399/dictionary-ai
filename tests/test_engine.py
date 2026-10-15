"""Profiling, relationship discovery and the autopilot dashboard (no network, no LLM)."""
from conftest import upload


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and "row_cap" in body


def test_sample_profile_discovers_relationships(client, sample):
    p = client.get(f"/api/sources/{sample}/profile").json()
    assert {t["name"] for t in p["tables"]} == {"payments", "ratings", "rides", "users", "vehicles"}
    links = {(r["from_table"], r["from_col"], r["to_table"]) for r in p["relationships"]}
    assert ("payments", "ride_id", "rides") in links          # same column name
    assert ("rides", "rider_id", "users") in links            # found by value overlap
    assert ("rides", "driver_id", "users") in links


def test_column_kinds_are_classified(client, sample):
    p = client.get(f"/api/sources/{sample}/profile").json()
    rides = next(t for t in p["tables"] if t["name"] == "rides")
    kinds = {c["name"]: c["kind"] for c in rides["columns"]}
    assert kinds["ride_id"] == "id" and kinds["fare"] == "measure"
    assert kinds["requested_at"] == "temporal" and kinds["status"] == "category"


def test_autopilot_builds_a_dashboard(client, sample):
    a = client.get(f"/api/sources/{sample}/autopilot").json()
    assert a["table"] == "rides"
    assert len(a["kpis"]) >= 2 and len(a["charts"]) >= 4
    assert {c["type"] for c in a["charts"]} & {"line", "bar", "hbar", "doughnut"}
    assert a["insights"], "expected at least one automatic insight"


def test_any_csv_works_not_just_the_sample(client):
    sid = upload(client, "Sales", {"sales.csv": "order_date,region,revenue\n2025-01-03,N,10\n2025-02-03,S,20\n2025-03-03,N,30\n2025-04-03,S,25\n"})
    a = client.get(f"/api/sources/{sid}/autopilot").json()
    assert a["kpis"][0]["value"] == 4 and any("revenue" in k["label"].lower() for k in a["kpis"])


def test_private_urls_are_blocked(client):
    r = client.post("/api/sources/api", json={"name": "x", "url": "http://127.0.0.1:9/data"})
    assert r.status_code == 400 and "blocked" in r.json()["detail"].lower()
