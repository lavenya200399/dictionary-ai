"""Data-quality scan, contracts, schema drift, root-cause investigation and the transform engine."""
from conftest import upload

DIRTY_CUSTOMERS = "customer_id,name,city\n" + "\n".join(f"{i},Cust {i},{'Halifax' if i % 2 else 'halifax '}" for i in range(1, 201)) + "\n5,Dup,Halifax\n"
DIRTY_ORDERS = "order_id,customer_id,amount,ship_date,order_date\n" + "\n".join(
    f"{i},{(i % 200) + 1},{-5 if i == 3 else 20 + i % 7},2025-03-{(i % 20) + 2:02d},2025-03-{(i % 20) + 1:02d}" for i in range(1, 201)) + "\n201,999,25,2025-02-01,2025-03-05\n"


def _dirty(client):
    return upload(client, "Dirty", {"customers.csv": DIRTY_CUSTOMERS, "orders.csv": DIRTY_ORDERS})


def test_quality_scan_finds_real_problems(client):
    q = client.get(f"/api/sources/{_dirty(client)}/quality").json()
    found = {i["check"] for i in q["issues"]}
    assert {"dup_keys", "negative", "orphans", "order", "case", "whitespace"} <= found
    assert 0 <= q["overall"]["score"] < 100 and q["checks"] > 20


def test_every_finding_has_runnable_inspect_sql(client):
    sid = _dirty(client)
    for issue in client.get(f"/api/sources/{sid}/quality").json()["issues"]:
        if issue.get("sql"):
            assert client.post(f"/api/sources/{sid}/query", json={"sql": issue["sql"]}).status_code == 200, issue["check"]


def test_data_contract_violations_are_reported(client):
    sid = _dirty(client)
    r = client.put(f"/api/sources/{sid}/contracts", json={"table": "orders", "required": ["order_id"], "unique": ["amount"], "allowed": {}, "types": {"amount": "number"}})
    assert r.status_code == 200
    q = client.get(f"/api/sources/{sid}/quality").json()
    assert any(i["check"] == "unique" and i["column"] == "amount" for i in q["issues"])
    assert client.put(f"/api/sources/{sid}/contracts", json={"table": "orders", "required": ["nope"]}).status_code == 400


def test_schema_drift_is_detected_after_baseline(client):
    sid = upload(client, "Drift", {"t.csv": "a,b\n1,2\n3,4\n"})
    assert client.post(f"/api/sources/{sid}/schema-baseline").status_code == 200
    from server import engine as E
    from sqlalchemy import create_engine, text
    path = E._get(sid)["url"]
    w = create_engine(f"sqlite:///{path}")
    with w.begin() as c: c.execute(text("ALTER TABLE t ADD COLUMN c TEXT"))
    w.dispose(); E._eng.pop(sid, None); E._prof.pop(sid, None)
    titles = {i["title"] for i in client.get(f"/api/sources/{sid}/quality").json()["issues"] if i["check"] == "schema"}
    assert "New column detected" in titles


def test_transform_preview_never_modifies_the_source(client):
    sid = upload(client, "T", {"t.csv": "id,city,score\n1, Halifax ,10\n2,Toronto,\n2,Toronto,\n3,Halifax,30\n"})
    spec = {"table": "t", "actions": {"column": "score", "nulls": "fill_value", "fill_value": "0", "duplicates": "drop"}}
    r = client.post(f"/api/sources/{sid}/transform/preview", json=spec).json()
    assert r["input_rows"] == 4 and r["output_rows"] == 3 and r["removed_rows"] == 1
    again = client.post(f"/api/sources/{sid}/query", json={"sql": "SELECT COUNT(*) FROM t"}).json()
    assert again["rows"][0][0] == 4, "the original table must stay untouched"
    csv = client.post(f"/api/sources/{sid}/transform/download", json={"table": "t", "actions": {"group_by": "city", "aggregate": "count"}})
    assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"] and "city,count" in csv.text


def test_investigate_points_at_where_the_problem_clusters(client):
    rows = "id,region,score\n" + "\n".join(f"{i},{'north' if i < 30 else 'south'},{'' if i < 25 else i}" for i in range(1, 61))
    sid = upload(client, "Inv", {"t.csv": rows})
    r = client.post(f"/api/sources/{sid}/investigate", json={"table": "t", "column": "score", "check": "nulls"}).json()
    assert r["affected"] == 24 and any(e["column"] == "region" for e in r["evidence"])
