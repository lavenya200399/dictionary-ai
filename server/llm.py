"""Groq client + the question→SQL→answer pipeline (schema-agnostic)."""
import json, os, re, time

import requests

from . import engine as E

PROVIDER = "groq"
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


class NoKey(Exception): ...


def enabled():
    return bool(os.getenv("GROQ_API_KEY"))


def gen(prompt, system=None, json_mode=False, temp=0.1):
    key = os.getenv("GROQ_API_KEY")
    if not key: raise NoKey("Add GROQ_API_KEY=your_groq_api_key_here to your .env file to enable questions and briefs.")
    messages = []
    if system: messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": MODEL, "messages": messages, "temperature": temp, "max_completion_tokens": 4096,
            "reasoning_effort": os.getenv("GROQ_REASONING_EFFORT", "low"), "store": False}
    if json_mode: body["response_format"] = {"type": "json_object"}
    for i in range(4):
        try: r = requests.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=(10, 180))
        except requests.exceptions.Timeout as e: raise ValueError("Groq took too long to answer. Please retry.") from e
        except requests.exceptions.ConnectionError as e: raise ValueError("Could not connect to Groq. Check outbound HTTPS access or configure HTTPS_PROXY in .env.") from e
        if r.status_code in (429, 500, 502, 503): time.sleep(1.5 * 2 ** i); continue
        if r.status_code != 200: raise ValueError(f"Groq error {r.status_code}: {r.text[:300]}")
        try:
            output = r.json()["choices"][0]["message"]["content"]
            if output and output.strip(): return output
        except (KeyError, IndexError, TypeError): pass
        raise ValueError("Groq returned no text. Please retry.")
    raise ValueError("Groq is busy. Please retry in a moment.")


def jparse(s):
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    try: return json.loads(s)
    except Exception: raise ValueError("The model returned an unreadable plan. Please rephrase and try again.")


SYS = """You are a careful senior data analyst. Write ONE read-only {dialect} SQL query that answers the user's question.
Rules: SELECT/WITH only. Use only the tables and columns listed (never invent any). Qualify columns when joining.
Use clear aliases (total_fare, avg_rating). Add ORDER BY and a sensible LIMIT for rankings. For trends return the period label
as the FIRST column (YYYY-MM or YYYY-MM-DD, use SUBSTR on timestamps) and the measure(s) after it. Filter on the exact category
values shown. Apply the business glossary exactly. If the question cannot be answered from this data, set sql to null.
Return JSON only: {"sql": string|null, "title": short chart title, "approach": one sentence on how you interpreted the question,
"followups": up to 3 short follow-up questions, "reason": string (only when sql is null)}"""


def schema_text(sid):
    p = E.profile(sid); rel = {}
    for r in p["relationships"]: rel.setdefault((r["from_table"], r["from_col"]), f"{r['to_table']}.{r['to_col']}")
    lines = []
    for t in p["tables"]:
        cs = []
        for c in t["columns"]:
            s = f"{c['name']} ({c['kind']}"
            if (t["name"], c["name"]) in rel: s += f" → {rel[(t['name'], c['name'])]}"
            if c["kind"] in ("category", "boolean"): s += "; values: " + ", ".join(k for k, _ in c["top"])
            elif c["kind"] == "measure": s += f"; {c['min']}..{c['max']}"
            elif c["kind"] == "temporal": s += f"; {c['min'][:10]}..{c['max'][:10]}"
            cs.append(s + ")")
        lines.append(f"TABLE {t['name']} ({t['rows']:,} rows): " + "; ".join(cs))
    g = E.glossary(sid)
    if g: lines.append("BUSINESS GLOSSARY (apply exactly):\n" + "\n".join(f"- {x['term']}: {x['definition']}" for x in g))
    return "\n".join(lines)


def _receipt(sid, res=None, attempts=1, checks=None, t0=0):
    s = next((x for x in E.sources() if x["id"] == sid), {})
    r = {"source": s.get("name"), "dialect": s.get("dialect"), "model": MODEL, "attempts": attempts, "total_ms": int((time.time() - t0) * 1000)}
    if res: r.update(checks=res["checks"], tables=res["tables"], rows=len(res["rows"]), truncated=res["truncated"], ms=res["ms"], safety="passed")
    else: r.update(checks=checks or [], tables=[], rows=0, truncated=False, ms=0, safety="blocked")
    return r


def ask(sid, question, history=None):
    t0 = time.time(); dia = E.engine(sid).dialect.name
    hist = "".join(f"\nPrevious question: {h.get('q', '')}\nPrevious SQL: {h.get('sql', '')}\n" for h in (history or [])[-2:])
    prompt = f"DATABASE ({dia}):\n{schema_text(sid)}\n{hist}\nQUESTION: {question}"
    sysm = SYS.replace("{dialect}", dia)
    plan, attempts = jparse(gen(prompt, sysm, True)), 0
    while True:
        attempts += 1; sql = (plan.get("sql") or "").strip()
        base = {"question": question, "title": plan.get("title") or question, "approach": plan.get("approach", ""), "followups": (plan.get("followups") or [])[:3], "sql": sql}
        if not sql:
            return {**base, "answered": False, "answer": plan.get("reason") or "I couldn't turn that into a query on this data. Try rephrasing, or check the columns in Data.", "receipt": _receipt(sid, t0=t0, checks=[])}
        try:
            res = E.run(sid, sql); break
        except E.Blocked as e:
            return {**base, "answered": False, "blocked": True, "answer": f"I didn't run this. {e}. Dictionary only reads data; it never changes it.",
                    "receipt": _receipt(sid, t0=t0, checks=[{"label": str(e), "ok": False}])}
        except Exception as e:
            err = str(e)[:300]
            if attempts >= 2:
                return {**base, "answered": False, "answer": f"I couldn't produce a query that runs on this data ({err}). Try rephrasing the question.", "receipt": _receipt(sid, t0=t0, attempts=attempts, checks=[])}
            plan = jparse(gen(prompt + f"\nYour previous SQL failed.\nSQL: {sql}\nERROR: {err}\nReturn corrected JSON.", sysm, True))
    head = ", ".join(res["cols"]); sample = "\n".join(" | ".join(str(v) for v in r) for r in res["rows"][:15])
    note = " (only the top rows are shown)" if res["truncated"] or len(res["rows"]) > 15 else ""
    ans = gen(f"Question: {question}\nColumns: {head}\nRows{note}:\n{sample or '(no rows)'}\n\nWrite 2-3 plain-English sentences for a business reader. Lead with the answer, cite specific numbers, avoid jargon, never mention SQL, and do not state anything the rows do not show. If there are no rows, say no data matched.", None, False, 0.3).strip()
    return {**base, "answered": True, "answer": ans, "columns": res["cols"], "rows": res["rows"], "truncated": res["truncated"], "chart": res["chart"], "receipt": _receipt(sid, res, attempts, t0=t0)}


def brief(sid):
    a = E.autopilot(sid)
    if a.get("empty"): raise ValueError("This source has no tables yet")
    facts = [f"Focus table: {a['table']} ({a['rows']:,} rows)"] + [f"KPI {k['label']}: {k['value']:,.2f}" + (f" ({k['delta']:+.1f}% vs previous period)" if k["delta"] is not None else "") for k in a["kpis"]]
    facts += [f"Insight: {i['title']}. {i['text']}" for i in a["insights"]]
    fallback = "\n".join("- " + f.replace("Insight: ", "").replace("KPI ", "") for f in facts[1:])
    if not enabled(): return {"text": fallback, "ai": False}
    txt = gen("Facts computed from the data:\n" + "\n".join(facts) + "\n\nWrite an executive brief for a non-technical manager: one short headline sentence, then 4-5 bullets (each starting with '- ') covering what matters and what to look into next. Use only these facts; no invented numbers.", None, False, 0.3)
    return {"text": txt.strip(), "ai": True}
