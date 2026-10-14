import json
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import engine as E

load_dotenv(E.ROOT / ".env")
from . import llm  # noqa: E402  (import after env is loaded)

app = FastAPI(title="Dictionary", docs_url="/api/docs", openapi_url="/api/openapi.json")


class DB(BaseModel): name: str = "Database"; url: str
class Api(BaseModel): name: str = "API data"; url: str; headers: dict = {}; path: str = ""
class Web(BaseModel): name: str = "Web tables"; url: str; table: int | None = None
class Contract(BaseModel): table: str; required: list[str] = []; unique: list[str] = []; allowed: dict = {}; types: dict = {}
class Transform(BaseModel): table: str; actions: dict = {}
class Investigation(BaseModel): table: str; column: str; check: str
class Ask(BaseModel): question: str; history: list[dict] = []
class Pin(BaseModel): source_id: str; title: str; sql: str
class Sql(BaseModel): sql: str
class Term(BaseModel): term: str; definition: str


def call(fn, *a, **k):
    try: return fn(*a, **k)
    except KeyError as e: raise HTTPException(404, str(e).strip("'\""))
    except llm.NoKey as e: raise HTTPException(503, str(e))
    except (ValueError, E.BadSQL, E.Blocked) as e: raise HTTPException(400, str(e))
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, f"{type(e).__name__}: {str(e)[:300]}")


@app.get("/api/health")
def health(): return {"ok": True, "llm": llm.enabled(), "provider": llm.PROVIDER, "model": llm.MODEL, "row_cap": E.CAP}

@app.get("/api/sources")
def sources(): return E.sources()

@app.post("/api/sources/db")
def add_db(b: DB): return {"id": call(E.add_db, b.name, b.url)}

@app.post("/api/sources/file")
async def add_file(name: str = Form(""), files: list[UploadFile] = File(...)):
    frames = {}
    for f in files:
        data = await f.read()
        if len(data) > 100 * 2 ** 20: raise HTTPException(400, f"{f.filename} is larger than 100 MB")
        frames.update(call(E.read_upload, f.filename, data))
    label = name.strip() or (Path(files[0].filename).stem if len(files) == 1 else "Uploaded files")
    return {"id": call(E.add_frames, label, frames, "file")}

@app.post("/api/sources/api")
def add_api(b: Api):
    df = call(E.fetch_api, b.url, b.headers, b.path)
    if df.empty: raise HTTPException(400, "The API returned no records")
    stem = (b.name.strip() or "api_data")
    return {"id": call(E.add_frames, stem, {stem: df}, "api")}

@app.post("/api/sources/web")
def add_web(b: Web):
    frames = call(E.fetch_web_tables, b.url, b.table)
    return {"id": call(E.add_frames, b.name.strip() or "Web tables", frames, "web")}

@app.post("/api/sources/sample")
def add_sample():
    for s in E.sources():
        if s["kind"] == "sample": return {"id": s["id"]}
    d = E.ROOT / "sample"; frames = {p.stem: pd.read_csv(p) for p in sorted(d.glob("*.csv"))}
    if not frames: raise HTTPException(404, "No sample CSVs found in /sample")
    return {"id": call(E.add_frames, "Sample · ride-share", frames, "sample")}

@app.delete("/api/sources/{sid}")
def del_source(sid: str): call(E.remove_source, sid); return {"ok": True}

@app.get("/api/sources/{sid}/profile")
def profile(sid: str, refresh: bool = False): return call(E.profile, sid, refresh)

@app.get("/api/sources/{sid}/autopilot")
def autopilot(sid: str, table: str | None = None): return call(E.autopilot, sid, table)

@app.post("/api/sources/{sid}/ask")
def ask(sid: str, b: Ask):
    if not b.question.strip(): raise HTTPException(400, "Ask a question")
    return call(llm.ask, sid, b.question.strip()[:600], b.history)

@app.post("/api/sources/{sid}/brief")
def brief(sid: str): return call(llm.brief, sid)

@app.get("/api/sources/{sid}/glossary")
def get_gl(sid: str): return call(E.glossary, sid)

@app.post("/api/sources/{sid}/glossary")
def add_gl(sid: str, b: Term):
    g = call(E.glossary, sid) + [{"term": b.term.strip()[:80], "definition": b.definition.strip()[:400]}]
    call(E.set_glossary, sid, g); return g

@app.delete("/api/sources/{sid}/glossary/{i}")
def del_gl(sid: str, i: int):
    g = call(E.glossary, sid)
    if 0 <= i < len(g): g.pop(i)
    call(E.set_glossary, sid, g); return g

@app.get("/api/sources/{sid}/quality")
def quality(sid: str): return call(E.quality, sid)

@app.get("/api/sources/{sid}/contracts")
def get_contracts(sid: str): return call(E.get_contracts, sid)

@app.put("/api/sources/{sid}/contracts")
def put_contract(sid: str, b: Contract): return call(E.set_contract, sid, b.table, b.dict(exclude={"table"}))

@app.post("/api/sources/{sid}/schema-baseline")
def schema_baseline(sid: str): return call(E.set_schema_baseline, sid)

@app.post("/api/sources/{sid}/transform/preview")
def transform_preview(sid: str, b: Transform): return call(E.transform_preview, sid, {"table":b.table,"actions":b.actions})

@app.post("/api/sources/{sid}/transform/download")
def transform_download(sid: str, b: Transform):
    csv, stats = call(E.transform_csv, sid, {"table":b.table,"actions":b.actions})
    return Response(csv, media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=dictionary-transformed.csv", "X-Output-Rows":str(stats["output_rows"])})

@app.post("/api/sources/{sid}/investigate")
def investigate(sid: str, b: Investigation): return call(E.investigate, sid, b.table, b.column, b.check)

@app.post("/api/sources/{sid}/query")
def query(sid: str, b: Sql):
    r = call(E.run, sid, b.sql); return {"cols": r["cols"], "rows": r["rows"], "truncated": r["truncated"], "ms": r["ms"]}

@app.get("/api/board")
def board(): return call(E.board)

@app.post("/api/board")
def pin(b: Pin): return call(E.add_pin, b.source_id, b.title, b.sql)

@app.delete("/api/board/{pid}")
def unpin(pid: str): E.del_pin(pid); return {"ok": True}


app.mount("/", StaticFiles(directory=E.ROOT / "web", html=True), name="web")
