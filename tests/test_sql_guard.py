"""The safety gate: the model (or a user) can only ever run one read-only query on known tables."""
import pytest


@pytest.mark.parametrize("sql", [
    "DELETE FROM rides",
    "DROP TABLE rides",
    "UPDATE rides SET fare = 0",
    "INSERT INTO rides (ride_id) VALUES (1)",
    "SELECT * FROM rides; DROP TABLE rides",
    "SELECT * FROM sqlite_master",
    "PRAGMA table_info(rides)",
    "WITH x AS (SELECT 1) DELETE FROM rides",
])
def test_unsafe_sql_is_rejected(client, sample, sql):
    r = client.post(f"/api/sources/{sample}/query", json={"sql": sql})
    assert r.status_code == 400, f"should have been blocked: {sql}"


def test_safe_select_runs_and_is_row_capped(client, sample):
    r = client.post(f"/api/sources/{sample}/query", json={"sql": "SELECT status, COUNT(*) AS n FROM rides GROUP BY 1"})
    assert r.status_code == 200 and {row[0] for row in r.json()["rows"]} >= {"completed", "cancelled"}
    big = client.post(f"/api/sources/{sample}/query", json={"sql": "SELECT * FROM rides"}).json()
    assert len(big["rows"]) <= 1000 and big["truncated"] is True


def test_literals_with_colons_and_percents_survive(client, sample):
    sql = "SELECT COUNT(*) FROM users WHERE signup_date > '2025:01' AND city LIKE '%ax%'"
    assert client.post(f"/api/sources/{sample}/query", json={"sql": sql}).status_code == 200
