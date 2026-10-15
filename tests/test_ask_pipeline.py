"""The natural-language pipeline, with the LLM replaced by a scripted fake (no key, no network).

This proves the part that matters: whatever the model returns, only safe SQL ever executes.
"""
import json

from server import llm


class FakeLLM:
    def __init__(self, *plans): self.plans, self.calls = list(plans), 0
    def __call__(self, prompt, system=None, json_mode=False, temp=0.1):
        self.calls += 1
        return json.dumps(self.plans.pop(0)) if json_mode else "Most rides are completed."


def ask(client, sid, q="how many rides per status?"):
    return client.post(f"/api/sources/{sid}/ask", json={"question": q}).json()


def test_happy_path_returns_answer_chart_and_trust_receipt(client, sample, monkeypatch):
    monkeypatch.setattr(llm, "gen", FakeLLM({"sql": "SELECT status, COUNT(*) AS rides FROM rides GROUP BY 1 ORDER BY 2 DESC", "title": "Rides by status", "approach": "Group by status"}))
    r = ask(client, sample)
    assert r["answered"] and r["chart"]["type"] in ("doughnut", "bar", "hbar")
    assert r["receipt"]["safety"] == "passed" and all(c["ok"] for c in r["receipt"]["checks"]) and r["receipt"]["attempts"] == 1


def test_a_destructive_query_from_the_model_is_blocked(client, sample, monkeypatch):
    monkeypatch.setattr(llm, "gen", FakeLLM({"sql": "DELETE FROM rides WHERE status = 'cancelled'", "title": "oops"}))
    r = ask(client, sample, "delete all cancelled rides")
    assert r["blocked"] is True and not r["answered"] and r["receipt"]["safety"] == "blocked"
    count = client.post(f"/api/sources/{sample}/query", json={"sql": "SELECT COUNT(*) FROM rides"}).json()["rows"][0][0]
    assert count == 20000, "no rows may be deleted"


def test_a_bad_query_is_self_corrected_once(client, sample, monkeypatch):
    fake = FakeLLM({"sql": "SELECT * FROM ride_table", "title": "wrong table"}, {"sql": "SELECT COUNT(*) AS n FROM rides", "title": "Total rides"})
    monkeypatch.setattr(llm, "gen", fake)
    r = ask(client, sample, "how many rides?")
    assert r["answered"] and r["receipt"]["attempts"] == 2 and r["chart"]["type"] == "kpi"


def test_missing_key_gives_a_clear_error(client, sample, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    r = client.post(f"/api/sources/{sample}/ask", json={"question": "hi"})
    assert r.status_code == 503 and "GROQ_API_KEY" in r.json()["detail"]
