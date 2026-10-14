"""Dictionary engine: source registry, profiling, autopilot analytics and guarded SQL execution."""
import io, ipaddress, json, os, re, socket, time, uuid
from pathlib import Path
from urllib.parse import urlparse
from io import StringIO

import numpy as np
import pandas as pd
import requests
import sqlglot
from sqlalchemy import bindparam, create_engine, inspect, text
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "workspace"; WS.mkdir(exist_ok=True)
REG, PINS = WS / "sources.json", WS / "pins.json"
CONTRACTS, SCHEMAS = WS / "contracts.json", WS / "schema_baselines.json"
CAP = int(os.getenv("ROW_CAP", 1000)); TMO = int(os.getenv("QUERY_TIMEOUT_S", 15))
HINT = ("amount", "revenue", "fare", "price", "total", "cost", "sales", "qty", "quantity", "spend", "profit", "income", "salary")
_eng, _prof = {}, {}


class Blocked(Exception): ...
class BadSQL(Exception): ...


def _rd(p, d): return json.loads(p.read_text()) if p.exists() else d
def _wr(p, v): p.write_text(json.dumps(v, indent=1, default=str))
def q(eng, n): return eng.dialect.identifier_preparer.quote(n)
def _f(v):
    try: return round(float(v), 4)
    except Exception: return None
def _j(v):
    if v is None or isinstance(v, (str, bool, int)): return v
    if isinstance(v, float): return None if v != v else v
    if hasattr(v, "item"): return _j(v.item())
    try: return float(v) if v.__class__.__name__ == "Decimal" else str(v)
    except Exception: return str(v)
def _isstr(s): return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


# ───────────────────────── sources ─────────────────────────
def sources():
    return [{"id": i, **{k: v for k, v in s.items() if k != "url"}} for i, s in _rd(REG, {}).items()]

def _get(sid):
    s = _rd(REG, {}).get(sid)
    if not s: raise KeyError("Unknown source")
    return s

def _put(name, kind, dialect, url):
    reg = _rd(REG, {}); sid = uuid.uuid4().hex[:8]
    reg[sid] = {"name": name, "kind": kind, "dialect": dialect, "url": url, "glossary": [], "created": time.strftime("%Y-%m-%d %H:%M")}
    _wr(REG, reg); return sid

def engine(sid):
    if sid not in _eng:
        s = _get(sid)
        url = s["url"] if s["kind"] == "db" else f"sqlite:///{Path(s['url']).resolve().as_uri()}?mode=ro&uri=true"
        _eng[sid] = create_engine(url, pool_pre_ping=True)
    return _eng[sid]

def add_db(name, url):
    try:
        e = create_engine(url)
        with e.connect() as c: c.execute(text("SELECT 1"))
        n = len(inspect(e).get_table_names()); e.dispose()
    except Exception as ex:
        raise ValueError("Could not connect: " + re.sub(r"://[^@/]*@", "://***@", str(ex))[:240])
    if n == 0: raise ValueError("Connected, but no tables were found")
    return _put(name, "db", url.split(":")[0].split("+")[0], url)

def _clean(c): return re.sub(r"\W+", "_", str(c).strip().lower()).strip("_") or "col"
def _cols(cols):
    seen, out = {}, []
    for c in map(_clean, cols):
        n = seen.get(c, 0); seen[c] = n + 1; out.append(c if n == 0 else f"{c}_{n}")
    return out

def add_frames(name, frames, kind):
    sid = uuid.uuid4().hex[:8]; path = WS / f"{sid}.db"; w = create_engine(f"sqlite:///{path}")
    try:
        for t, df in frames.items():
            df = df.copy(); df.columns = _cols(df.columns)
            for c in df.columns:
                s = df[c]
                if pd.api.types.is_datetime64_any_dtype(s): df[c] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
                elif pd.api.types.is_bool_dtype(s): df[c] = s.astype(int)
                elif _isstr(s):
                    iso = s.dropna().astype(str).str.match(r"^\d{4}-\d{2}-\d{2}").mean() > .9
                    if re.search(r"(date|time|_at|_on)$", c) and not iso:
                        p = pd.to_datetime(s, errors="coerce")
                        if p.notna().mean() > .9: df[c] = p.dt.strftime("%Y-%m-%d %H:%M:%S")
                    df[c] = df[c].map(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
            tn = _clean(t); tn = "t_" + tn if tn[0].isdigit() else tn
            df.to_sql(tn, w, index=False, if_exists="replace", chunksize=5000)
    finally: w.dispose()
    reg = _rd(REG, {}); reg[sid] = {"name": name, "kind": kind, "dialect": "sqlite", "url": str(path), "glossary": [], "created": time.strftime("%Y-%m-%d %H:%M")}
    _wr(REG, reg); return sid

def read_upload(filename, data):
    ext, stem, b = Path(filename).suffix.lower(), Path(filename).stem, io.BytesIO(data)
    if ext in (".csv", ".txt"):
        try: return {stem: pd.read_csv(b)}
        except Exception: b.seek(0); return {stem: pd.read_csv(b, sep=None, engine="python")}
    if ext == ".tsv": return {stem: pd.read_csv(b, sep="\t")}
    if ext in (".xlsx", ".xls"):
        sh = pd.read_excel(b, sheet_name=None); return {(f"{stem}_{k}" if len(sh) > 1 else stem): v for k, v in sh.items()}
    if ext == ".parquet": return {stem: pd.read_parquet(b)}
    if ext == ".json": return {stem: _records(json.loads(data))}
    raise ValueError(f"Unsupported file type: {ext or filename}")

def _guard(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname: raise ValueError("Enter a valid http(s) URL")
    if os.getenv("ALLOW_PRIVATE_URLS") == "1": return
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Private/internal addresses are blocked (set ALLOW_PRIVATE_URLS=1 to allow)")

def fetch_web_tables(url, table_index=None):
    """Import static HTML tables from a public web page as queryable data."""
    _guard(url)
    r = requests.get(url, headers={"User-Agent": "DictionaryDataImporter/1.0"}, timeout=20,
                     allow_redirects=False, stream=True)
    if 300 <= r.status_code < 400: raise ValueError("The page redirected; use the final URL")
    r.raise_for_status()
    if int(r.headers.get("content-length", 0) or 0) > 10 * 2**20: raise ValueError("Web pages are limited to 10 MB")
    raw = r.raw.read(10 * 2**20 + 1)
    if len(raw) > 10 * 2**20: raise ValueError("Web pages are limited to 10 MB")
    html = raw.decode(r.encoding or "utf-8", errors="replace")
    try: tables = pd.read_html(StringIO(html))
    except ValueError: tables = []
    except ImportError as ex: raise ValueError("HTML table import needs the lxml package") from ex
    if table_index is not None:
        if table_index < 1 or table_index > len(tables): raise ValueError(f"Choose a table from 1 to {len(tables)}")
        tables = [tables[table_index - 1]]
    if not tables: raise ValueError("No HTML tables found. This importer supports tables present in the page HTML.")
    frames = {}
    for i, df in enumerate(tables[:20], 1):
        if df.empty: continue
        df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed:")]
        if not df.empty: frames[f"table_{i}"] = df.head(100000)
    if not frames: raise ValueError("The page has no usable table data")
    return frames

def _records(j, path=None):
    for p in filter(None, (path or "").split(".")): j = j[int(p)] if isinstance(j, list) else j[p]
    if isinstance(j, dict):
        lists = [v for v in j.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        j = lists[0] if lists else [j]
    return pd.json_normalize(j, sep="_")

def fetch_api(url, headers, path):
    _guard(url)
    r = requests.get(url, headers=headers or {}, timeout=20, allow_redirects=False)
    if 300 <= r.status_code < 400: raise ValueError("The API redirected; use the final URL")
    r.raise_for_status()
    return _records(r.json(), path).head(100000)

def remove_source(sid):
    reg = _rd(REG, {}); s = reg.pop(sid, None)
    if not s: raise KeyError("Unknown source")
    _wr(REG, reg)
    if sid in _eng: _eng.pop(sid).dispose()
    _prof.pop(sid, None)
    if s["kind"] != "db": Path(s["url"]).unlink(missing_ok=True)
    _wr(PINS, [p for p in _rd(PINS, []) if p["source_id"] != sid])
    for path in (CONTRACTS, SCHEMAS):
        values=_rd(path,{})
        if sid in values: values.pop(sid); _wr(path,values)

def glossary(sid): return _get(sid).get("glossary", [])
def set_glossary(sid, g):
    reg = _rd(REG, {}); reg[sid]["glossary"] = g; _wr(REG, reg)


# ───────────────────────── profiling ─────────────────────────
GEO = ("latitude", "longitude", "_lat", "_lng", "_lon")

def classify(name, nn):
    l = name.lower()
    if nn.empty: return "text"
    if pd.api.types.is_bool_dtype(nn): return "boolean"
    if l == "id" or l.endswith("_id") or l.endswith("uuid"): return "id"
    num = pd.api.types.is_numeric_dtype(nn)
    if num and (any(g in l for g in GEO) or l in ("lat", "lng", "lon")): return "geo"
    st = nn.astype(str)
    if pd.api.types.is_datetime64_any_dtype(nn) or (not num and st.head(200).str.match(r"^\d{4}-\d{2}-\d{2}").mean() > .9): return "temporal"
    if num:
        if nn.nunique() <= 2: return "boolean"
        return "category" if any(w in l for w in ("year", "zip", "postal", "phone")) else "measure"
    k = st.nunique()
    return "category" if k <= 12 or (k <= 40 and k / len(nn) < .5) else "text"

def colprof(df, col):
    s = df[col]; nn = s.dropna(); n = len(nn); kind = classify(col, nn)
    d = {"name": col, "kind": kind, "nulls": round(100 * (1 - n / max(len(s), 1)), 1), "distinct": int(nn.astype(str).nunique()) if n else 0}
    d["unique"] = n > 0 and d["distinct"] == n
    if kind in ("measure", "geo"): d.update(min=_f(nn.min()), max=_f(nn.max()), mean=_f(nn.mean()))
    elif kind == "temporal": d.update(min=str(nn.astype(str).min()), max=str(nn.astype(str).max()))
    elif kind in ("category", "boolean"): d["top"] = [[str(k), int(v)] for k, v in nn.astype(str).value_counts().head(5).items()]
    else: d["sample"] = [str(x)[:40] for x in nn.head(3)]
    return d

def profile(sid, refresh=False):
    if sid in _prof and not refresh: return _prof[sid]
    eng = engine(sid); insp = inspect(eng); tables = []
    with eng.connect() as c:
        for t in (insp.get_table_names() + insp.get_view_names())[:40]:
            try:
                n = c.execute(text(f"SELECT COUNT(*) FROM {q(eng, t)}")).scalar()
                df = pd.read_sql(text(f"SELECT * FROM {q(eng, t)} LIMIT 5000"), c)
                tables.append({"name": t, "rows": int(n), "columns": [colprof(df, x) for x in df.columns]})
            except Exception: c.rollback()
        rels = _rels(eng, c, insp, tables)
    _prof[sid] = {"tables": tables, "relationships": rels}
    return _prof[sid]

def _rels(eng, c, insp, tables):
    out, seen = [], set()
    def add(ft, fc, tt, tc, how, evidence=None):
        if ft != tt and (ft, fc, tt) not in seen:
            seen.add((ft, fc, tt)); out.append({"from_table": ft, "from_col": fc, "to_table": tt, "to_col": tc, "how": how, "evidence": evidence or {"kind": "declared_foreign_key", "observed": True}})
    names = {t["name"] for t in tables}
    for t in tables:
        try:
            for fk in insp.get_foreign_keys(t["name"]):
                if fk["referred_table"] in names:
                    for a, b in zip(fk["constrained_columns"], fk["referred_columns"]): add(t["name"], a, fk["referred_table"], b, "foreign key")
        except Exception: pass
    pks = []
    for t in tables:
        for i, col in enumerate(t["columns"]):
            if col["kind"] == "id" and (col["unique"] or col["distinct"] >= .98 * min(t["rows"], 5000)) and (i == 0 or col["name"] == "id"): pks.append((t, col)); break
    for t in tables:
        for col in t["columns"]:
            if col["kind"] != "id" or any(p[1] is col for p in pks): continue
            cands = [(pt, pc) for pt, pc in pks if pt is not t]
            exact = [(pt, pc) for pt, pc in cands if pc["name"] == col["name"]]
            if exact: add(t["name"], col["name"], exact[0][0]["name"], exact[0][1]["name"], "same column name", {"kind": "name_match", "observed": False}); continue
            best = None
            try:
                vals = [r[0] for r in c.execute(text(f"SELECT DISTINCT {q(eng, col['name'])} FROM {q(eng, t['name'])} WHERE {q(eng, col['name'])} IS NOT NULL LIMIT 100"))]
                if len(vals) < 3: continue
                for pt, pc in cands:
                    k = c.execute(text(f"SELECT COUNT(DISTINCT {q(eng, pc['name'])}) FROM {q(eng, pt['name'])} WHERE {q(eng, pc['name'])} IN :v").bindparams(bindparam("v", expanding=True)), {"v": vals}).scalar()
                    if k / len(vals) >= .95:
                        cov = col["distinct"] / max(pt["rows"], 1)
                        if best is None or cov > best[0]: best = (cov, pt, pc, int(k), len(vals))
            except Exception: c.rollback(); continue
            if best: add(t["name"], col["name"], best[1]["name"], best[2]["name"], "values match", {"kind": "value_overlap", "observed": True, "sample_size": best[4], "matched_values": best[3], "match_rate": round(best[3] / best[4], 4)})
    return out


# ───────────────────────── guarded SQL ─────────────────────────
DIALECT = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql", "mssql": "tsql", "oracle": "oracle"}
BAD = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Merge", "Into", "TruncateTable", "Pragma", "Set") if hasattr(exp, n))
NOFUNC = {"pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export", "load_extension", "readfile", "writefile", "sleep", "benchmark", "dblink", "xp_cmdshell", "load_file"}
SYSDB = {"information_schema", "pg_catalog", "mysql", "sys", "performance_schema"}

def check_sql(sid, sql):
    eng = engine(sid); d = DIALECT.get(eng.dialect.name)
    try: trees = [t for t in sqlglot.parse(sql, read=d) if t]
    except sqlglot.errors.ParseError as e: raise BadSQL(f"Could not parse SQL: {str(e)[:160]}")
    if len(trees) != 1: raise Blocked("Only a single statement is allowed")
    t = trees[0]
    if not isinstance(t, exp.Query) or (BAD and t.find(*BAD)): raise Blocked("Only read-only SELECT queries are allowed")
    fns = {(f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower() for f in t.find_all(exp.Func)}
    if fns & NOFUNC: raise Blocked("Query uses a function that is not allowed")
    known = {x["name"].lower() for x in profile(sid)["tables"]}
    cte = {c.alias_or_name.lower() for c in t.find_all(exp.CTE)}
    tabs = list(t.find_all(exp.Table))
    if any((tb.db or "").lower() in SYSDB for tb in tabs): raise Blocked("System tables are not accessible")
    bad = {tb.name for tb in tabs if tb.name.lower() not in known | cte}
    if bad: raise BadSQL("Unknown table(s): " + ", ".join(sorted(bad)))
    return t, sorted({tb.name for tb in tabs if tb.name.lower() not in cte}), d

def run(sid, sql):
    eng = engine(sid); t, used, d = check_sql(sid, sql)
    lim = t.args.get("limit") if isinstance(t, exp.Select) else None
    if lim is not None and lim.expression.name.isdigit() and int(lim.expression.name) <= CAP: final = t.sql(dialect=d)
    elif isinstance(t, exp.Select) and lim is None: final = t.limit(CAP + 1).sql(dialect=d)
    else: final = f"SELECT * FROM ({t.sql(dialect=d)}) AS _q LIMIT {CAP + 1}"
    t0, dn = time.time(), eng.dialect.name
    with eng.connect() as c:
        if dn == "postgresql": c.execute(text("SET TRANSACTION READ ONLY")); c.execute(text(f"SET LOCAL statement_timeout = {TMO * 1000}"))
        elif dn == "mysql": c.execute(text("SET SESSION TRANSACTION READ ONLY")); c.execute(text(f"SET SESSION MAX_EXECUTION_TIME={TMO * 1000}"))
        elif dn == "sqlite":
            dl = time.time() + TMO; c.connection.driver_connection.set_progress_handler(lambda: 1 if time.time() > dl else 0, 20000)
        res = c.execute(text(final.replace(":", r"\:"))); cols = list(res.keys()); rows = res.fetchall()
    trunc = len(rows) > CAP; rows = [[_j(v) for v in r] for r in rows[:CAP]]
    checks = [{"label": "Single statement", "ok": True}, {"label": "Read-only SELECT", "ok": True}, {"label": "Known tables only", "ok": True},
              {"label": "No unsafe functions", "ok": True}, {"label": f"Row cap {CAP:,}", "ok": True},
              {"label": "Read-only session" if dn != "sqlite" else "Read-only file", "ok": True}]
    return {"cols": cols, "rows": rows, "truncated": trunc, "ms": int((time.time() - t0) * 1000), "tables": used, "checks": checks, "chart": pick_chart(cols, rows)}

_LBL = re.compile(r"(^|_)(id|year|month|day|hour|week|quarter|rank)$")
def pick_chart(cols, rows):
    if not rows: return None
    df = pd.DataFrame(rows, columns=cols)
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not _LBL.search(c.lower())]
    if not num: return None
    lab = [c for c in cols if c not in num]
    if len(rows) == 1 and not lab: return {"type": "kpi", "items": [{"label": c, "value": df[c].iloc[0]} for c in num[:4]]}
    if lab:
        x = df[lab[0]].astype(str).str.slice(0, 28).tolist()[:60]
        temporal = df[lab[0]].astype(str).str.match(r"^\d{4}(-\d{2}){0,2}").mean() > .8
        typ = "line" if temporal and len(rows) >= 3 else "doughnut" if len(rows) <= 5 and len(num) == 1 and (df[num[0]] >= 0).all() else "hbar" if len(rows) > 6 or max(map(len, x)) > 12 else "bar"
        return {"type": typ, "x": x, "series": [{"name": c, "data": [_j(v) for v in df[c].tolist()[:60]]} for c in num[:3]]}
    if len(num) >= 2 and len(rows) >= 8:
        return {"type": "scatter", "points": [{"x": _j(a), "y": _j(b)} for a, b in zip(df[num[0]], df[num[1]])][:500], "series": [{"name": f"{num[0]} vs {num[1]}"}]}
    return None


# ───────────────────────── autopilot ─────────────────────────
def _ins(kind, title, text_, ask): return {"kind": kind, "title": title, "text": text_, "ask": ask}

def autopilot(sid, table=None):
    p = profile(sid); eng = engine(sid)
    if not p["tables"]: return {"empty": True, "tables": [], "kpis": [], "charts": [], "insights": []}
    T = {t["name"]: t for t in p["tables"]}
    def score(t):
        k = [c["kind"] for c in t["columns"]]
        return t["rows"] ** .5 * (1 + 2 * ("temporal" in k) + k.count("measure") + sum(r["from_table"] == t["name"] for r in p["relationships"]))
    fact = T.get(table) or max(T.values(), key=score)
    name, cols = fact["name"], fact["columns"]; Q = lambda n: q(eng, n); ft = Q(name)
    low = lambda c: c["name"].lower(); nice = lambda s: s.replace("_", " ")
    tcol = next(iter(sorted([c for c in cols if c["kind"] == "temporal"], key=lambda c: not any(w in low(c) for w in ("creat", "request", "order", "date", "start", "sign")))), None)
    allm = [c for c in cols if c["kind"] == "measure"]
    meas = sorted(allm, key=lambda c: not any(h in low(c) for h in HINT))[:3]
    cats = [c for c in cols if c["kind"] in ("category", "boolean") and 2 <= c["distinct"] <= 30 and c["nulls"] < 50][:3]
    agg = lambda c: "SUM" if any(h in low(c) for h in HINT) else "AVG"
    out = {"table": name, "tables": [t["name"] for t in p["tables"]], "rows": fact["rows"], "kpis": [], "charts": [], "insights": []}
    ins, ser, partial, per, tsql = out["insights"], None, False, "period", ""
    with eng.connect() as c:
        df_ = lambda s: pd.read_sql(text(s), c)
        def _try(fn, *a):
            try: return fn(*a)
            except Exception: c.rollback()
        sel = ["COUNT(*) AS n"] + [f"{agg(m)}({Q(m['name'])}) AS m{i}" for i, m in enumerate(meas)]
        tot = df_(f"SELECT {', '.join(sel)} FROM {ft}").iloc[0]
        if tcol:
            def mk():
                tc = Q(tcol["name"]); span = c.execute(text(f"SELECT MIN({tc}), MAX({tc}) FROM {ft}")).one()
                d0, d1 = pd.to_datetime(str(span[0])[:10], errors="coerce"), pd.to_datetime(str(span[1])[:10], errors="coerce")
                if pd.isna(d0) or pd.isna(d1): return None
                n = 7 if (d1 - d0).days > 75 else 10; cast = "CHAR" if eng.dialect.name == "mysql" else "TEXT"
                sql = f"SELECT SUBSTR(CAST({tc} AS {cast}),1,{n}) AS period, {', '.join(sel)} FROM {ft} WHERE {tc} IS NOT NULL GROUP BY 1 ORDER BY 1"
                return df_(sql).tail(48).reset_index(drop=True), n, d1.day < 27 and n == 7, sql
            r = _try(mk)
            if r and len(r[0]) >= 2: ser, n, partial, tsql = r; per = "month" if n == 7 else "day"
        def kp(label, key, v):
            k = {"label": label, "value": float(v) if pd.notna(v) else 0, "delta": None, "spark": []}
            if ser is not None and len(ser) >= 3:
                vals = pd.to_numeric(ser[key], errors="coerce").fillna(0).tolist(); lf = -2 if partial else -1
                k["spark"] = vals[-24:]
                if vals[lf - 1]: k["delta"] = round((vals[lf] - vals[lf - 1]) / abs(vals[lf - 1]) * 100, 1)
            out["kpis"].append(k)
        kp(f"{nice(name)} records", "n", tot["n"])
        for i, m in enumerate(meas): kp(f"{'Total' if agg(m) == 'SUM' else 'Avg'} {nice(m['name'])}", f"m{i}", tot[f"m{i}"])
        def chart(title, sql, typ="bar", dim=None):
            d = df_(sql)
            if d.empty: return None
            out["charts"].append({"title": title, "type": typ, "x": d.iloc[:, 0].astype(str).tolist(), "sql": sql, "dim": dim,
                                  "series": [{"name": str(d.columns[1]), "data": pd.to_numeric(d.iloc[:, 1]).astype(float).round(2).tolist()}]})
            return d
        if ser is not None:
            out["charts"].append({"title": f"{nice(name)} per {per}", "type": "line", "x": ser.period.tolist(), "sql": tsql, "dim": None, "series": [{"name": "Records", "data": ser.n.astype(float).tolist()}]})
            if meas:
                m = meas[0]
                out["charts"].append({"title": f"{'Total' if agg(m) == 'SUM' else 'Avg'} {nice(m['name'])} per {per}", "type": "line", "x": ser.period.tolist(), "sql": tsql, "dim": None,
                                      "series": [{"name": nice(m["name"]), "data": pd.to_numeric(ser["m0"]).astype(float).round(2).tolist()}]})
        for j, cc in enumerate(cats[:2]):
            col = Q(cc["name"])
            d = _try(chart, f"{nice(name)} by {nice(cc['name'])}", f"SELECT {col} AS label, COUNT(*) AS records FROM {ft} WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10", "doughnut" if cc["distinct"] <= 5 and j == 0 else "bar", f"{name}.{cc['name']}")
            if j == 0 and d is not None and len(d) > 1 and fact["rows"]:
                share = float(d.iloc[0, 1]) / fact["rows"]
                if share >= .45: ins.append(_ins("concentration", f"“{d.iloc[0, 0]}” dominates {nice(cc['name'])}", f"{share:.0%} of all {nice(name)} are {d.iloc[0, 0]}.", f"Why is {d.iloc[0, 0]} so common in {nice(cc['name'])}?"))
        if meas and cats:
            m, cc = meas[0], cats[0]; col = Q(cc["name"])
            _try(chart, f"{'Total' if agg(m) == 'SUM' else 'Avg'} {nice(m['name'])} by {nice(cc['name'])}", f"SELECT {col} AS label, {agg(m)}({Q(m['name'])}) AS value FROM {ft} WHERE {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10", "hbar", f"{name}.{cc['name']}")
        used = set()
        for r in p["relationships"]:
            if r["from_table"] != name or r["to_table"] not in T or len(used) >= 2: continue
            dim = T[r["to_table"]]
            dc = next((x for x in dim["columns"] if x["kind"] == "category" and 2 <= x["distinct"] <= 30 and (r["to_table"], x["name"]) not in used), None)
            if not dc: continue
            used.add((r["to_table"], dc["name"])); dcq = Q(dc["name"])
            sql = f"SELECT d.{dcq} AS label, COUNT(*) AS records FROM {ft} f JOIN {Q(dim['name'])} d ON f.{Q(r['from_col'])} = d.{Q(r['to_col'])} WHERE d.{dcq} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
            _try(chart, f"{nice(name)} by {nice(r['to_table'])} {nice(dc['name'])} (via {r['from_col']})", sql, "hbar", f"{r['to_table']}.{dc['name']}")
        if meas:
            mc = Q(meas[0]["name"])
            v = _try(lambda: df_(f"SELECT {mc} AS v FROM {ft} WHERE {mc} IS NOT NULL LIMIT 20000")["v"].astype(float))
            if v is not None and len(v) > 5 and v.nunique() > 3:
                h, e = np.histogram(v, bins=12)
                out["charts"].append({"title": f"Distribution of {nice(meas[0]['name'])}", "type": "bar", "x": [f"{e[i]:.3g}–{e[i + 1]:.3g}" for i in range(12)], "series": [{"name": "Records", "data": h.tolist()}], "sql": f"SELECT {mc} FROM {ft}", "dim": None, "pin": False})
        if len(allm) >= 2:
            ms = allm[:4]; cs = ", ".join(Q(m["name"]) for m in ms)
            d = _try(lambda: df_(f"SELECT {cs} FROM {ft} LIMIT 20000").astype(float).corr())
            if d is not None:
                best = max(((abs(d.iloc[a, b]), a, b) for a in range(len(ms)) for b in range(a + 1, len(ms)) if d.iloc[a, b] == d.iloc[a, b]), default=None)
                if best and best[0] >= .7:
                    a, b = ms[best[1]]["name"], ms[best[2]]["name"]
                    ins.append(_ins("relationship", f"{nice(a)} and {nice(b)} move together", f"Correlation r = {d.iloc[best[1], best[2]]:.2f} across {fact['rows']:,} records.", f"How does {nice(a)} relate to {nice(b)}?"))
    if ser is not None and len(ser) >= 4:
        v = ser.n.astype(float).values; use = v[:-1] if partial else v; pr = ser.period.values; mu, sd = use.mean(), use.std()
        if sd > 0:
            z = (use - mu) / sd; k = int(np.argmax(np.abs(z)))
            if abs(z[k]) >= 2:
                w = "spike" if z[k] > 0 else "drop"
                ins.append(_ins("anomaly", f"Unusual {w} in {pr[k]}", f"{nice(name)} reached {use[k]:,.0f}, {abs(z[k]):.1f}σ {'above' if z[k] > 0 else 'below'} the typical {mu:,.0f}.", f"What explains the {w} in {nice(name)} in {pr[k]}?"))
        dl = out["kpis"][0]["delta"]
        if dl is not None and abs(dl) >= 10: ins.append(_ins("trend", f"{nice(name)} {'up' if dl > 0 else 'down'} {abs(dl):.0f}% vs previous {per}", f"Latest full {per} compared with the one before it.", f"Why did {nice(name)} go {'up' if dl > 0 else 'down'} recently?"))
        kx = int(np.argmax(use)); ins.append(_ins("peak", f"Busiest {per}: {pr[kx]}", f"{use[kx]:,.0f} {nice(name)}, the highest of {len(use)} periods.", f"What drove the peak in {pr[kx]}?"))
    for col in sorted(cols, key=lambda c: -c["nulls"])[:2]:
        if col["nulls"] >= 20: ins.append(_ins("quality", f"{col['nulls']:.0f}% of {nice(col['name'])} is empty", "Large gaps can distort averages and totals.", f"Why is {nice(col['name'])} empty for so many {nice(name)}?"))
    order = ["anomaly", "trend", "concentration", "relationship", "quality", "peak"]
    ins.sort(key=lambda i: order.index(i["kind"])); del ins[6:]
    return out


# ───────────────────────── board ─────────────────────────
def pins(): return _rd(PINS, [])
def add_pin(sid, title, sql):
    _get(sid); check_sql(sid, sql)
    ps = pins(); ps.append({"id": uuid.uuid4().hex[:8], "source_id": sid, "title": title[:120], "sql": sql}); _wr(PINS, ps); return ps[-1]
def del_pin(pid): _wr(PINS, [p for p in pins() if p["id"] != pid])
def board():
    names = {s["id"]: s["name"] for s in sources()}; out = []
    for p in pins():
        if p["source_id"] not in names: continue
        item = {**p, "source": names[p["source_id"]]}
        try: r = run(p["source_id"], p["sql"]); item.update(chart=r["chart"], rows=len(r["rows"]), cols=r["cols"], data=r["rows"][:200])
        except Exception as e: item["error"] = str(e)[:200]
        out.append(item)
    return out


# ───────────────────────── data quality ─────────────────────────
FIX = {
    "empty_col": "A column with no values carries no information. Drop it, or fix the pipeline that should fill it.",
    "nulls": "Decide if blanks are expected. If not, fix it at the source or fill a default, and exclude blanks from averages.",
    "blank": "Empty text is not the same as missing. Treat blank strings as NULL when loading the data.",
    "dup_rows": "Remove exact duplicates before reporting, and add a unique key at the source so they cannot recur.",
    "dup_keys": "A key must be unique. Deduplicate the rows and add a UNIQUE constraint.",
    "outliers": "Check whether these are real events or entry errors before they distort averages. Consider medians.",
    "negative": "Negative values look wrong for this field. Check for refunds, sign errors or bad imports.",
    "future": "Dates in the future usually mean a time-zone or entry error.",
    "order": "The end is earlier than the start. Check timestamps and time zones.",
    "orphans": "These rows point to records that do not exist. Fix the source or load the missing parent records.",
    "whitespace": "Trim leading/trailing spaces when loading, otherwise identical values count as different.",
    "case": "Standardise casing (for example Title Case) so one value is not split into several groups.",
    "numtext": "Numbers stored as text cannot be summed or sorted properly. Convert the column to a numeric type.",
    "constant": "A column with one value carries no information. Consider dropping it from reports.",
    "stale": "The newest record is old. Check that the data feed is still running.",
}
_DW = {"completeness": 0, "uniqueness": 3, "validity": 2, "consistency": 1, "freshness": 0}
_NEG = ("fare", "amount", "price", "qty", "quantity", "revenue", "cost", "distance", "total", "salary", "age")
_PAIRS = (("pickup", "dropoff"), ("start", "end"), ("created", "updated"), ("request", "pickup"), ("open", "close"), ("begin", "finish"), ("order", "ship"))
_SEV = {"high": 0, "medium": 1, "low": 2, "info": 3}

def quality(sid):
    p = profile(sid, refresh=True); eng = engine(sid); Q = lambda n: q(eng, n)
    today = time.strftime("%Y-%m-%d"); issues, tabs, checks = [], [], [0]
    with eng.connect() as c:
        one = lambda s: int(c.execute(text(s)).scalar() or 0)
        def safe(fn):
            try: fn()
            except Exception: c.rollback()
        for t in p["tables"]:
            name, n, cols = t["name"], t["rows"], t["columns"]; ft = Q(name); pen = dict.fromkeys(_DW, 0.0); box = {}
            def add(sev, check, dim, col, title, detail, count, sql, w=None):
                pct = count / max(n, 1) * 100; pen[dim] += pct * (_DW[dim] if w is None else w)
                issues.append({"sev": sev, "check": check, "dim": dim, "table": name, "column": col, "title": title, "detail": detail,
                               "count": int(count), "pct": round(pct, 2), "sql": sql, "fix": FIX[check]})
            if n == 0:
                add("high", "empty_col", "completeness", None, "Table is empty", f"“{name}” has no rows.", 0, f"SELECT * FROM {ft} LIMIT 100", 0)
                tabs.append({"name": name, "rows": 0, "score": 0, "dims": dict.fromkeys(("completeness", "uniqueness", "validity", "consistency"), 0), "cols": [], "issues": 1}); continue
            def sec_null():
                nn = c.execute(text("SELECT " + ", ".join(f"COUNT({Q(x['name'])})" for x in cols) + f" FROM {ft}")).one()
                miss = [n - int(v) for v in nn]; box["miss"] = miss; checks[0] += len(cols)
                for j, x in enumerate(cols):
                    if x["kind"] in ("category", "text"):
                        blanks = one(f"SELECT COUNT(*) FROM {ft} WHERE {Q(x['name'])} IS NOT NULL AND TRIM(CAST({Q(x['name'])} AS TEXT)) = ''")
                        miss[j] += blanks
                pen["completeness"] = sum(miss) / max(n * len(cols), 1) * 150
                for x, m in zip(cols, miss):
                    pct = m / n * 100
                    if m == n: add("high", "empty_col", "completeness", x["name"], "Column is completely empty", f"Every row of “{x['name']}” is blank.", m, f"SELECT * FROM {ft} LIMIT 100", 0)
                    elif pct >= .5:
                        condition=f"{Q(x['name'])} IS NULL"
                        if x["kind"] in ("category","text"): condition+=f" OR TRIM({Q(x['name'])}) = ''"
                        add("high" if pct >= 30 else "medium" if pct >= 5 else "low", "nulls", "completeness", x["name"], f"{pct:.1f}% missing", f"{m:,} of {n:,} rows have no value for “{x['name']}”.", m, f"SELECT * FROM {ft} WHERE {condition} LIMIT 100", 0)
            def sec_dups():
                box["df"] = df = pd.read_sql(text(f"SELECT * FROM {ft} LIMIT 50000"), c); ns = len(df); checks[0] += 2
                allc = ", ".join(Q(x["name"]) for x in cols)
                # Count exact duplicates over the full table; the sample is only used for preview.
                d = one(f"SELECT COALESCE(SUM(copies - 1), 0) FROM (SELECT COUNT(*) AS copies FROM {ft} GROUP BY {allc} HAVING COUNT(*) > 1) AS duplicate_groups")
                if d:
                    add("high" if d / n > .01 else "medium", "dup_rows", "uniqueness", None, f"{d:,} duplicate rows", f"{d:,} rows are exact copies of another row.", d, f"SELECT {allc}, COUNT(*) AS copies FROM {ft} GROUP BY {allc} HAVING COUNT(*) > 1 ORDER BY copies DESC LIMIT 100")
                k = cols[0]
                if k["kind"] == "id" and (k["unique"] or k["distinct"] >= .98 * min(n, 5000)):
                    kq = Q(k["name"]); dk = one(f"SELECT COUNT(*) - COUNT(DISTINCT {kq}) FROM {ft}")
                    if dk: add("high", "dup_keys", "uniqueness", k["name"], f"{dk:,} repeated key values", f"“{k['name']}” looks like a unique ID, but {dk:,} values repeat.", dk, f"SELECT {kq}, COUNT(*) AS copies FROM {ft} GROUP BY {kq} HAVING COUNT(*) > 1 ORDER BY copies DESC LIMIT 100")
            def sec_measures():
                df = box["df"]; ns = len(df)
                for x in cols:
                    if x["kind"] != "measure": continue
                    s = pd.to_numeric(df[x["name"]], errors="coerce").dropna(); checks[0] += 2
                    if len(s) < 20: continue
                    xq = Q(x["name"]); q1, q3 = s.quantile(.25), s.quantile(.75); iqr = q3 - q1
                    if iqr > 0:
                        lo, hi = q1 - 3 * iqr, q3 + 3 * iqr; k = one(f"SELECT COUNT(*) FROM {ft} WHERE {xq} < {lo:.12g} OR {xq} > {hi:.12g}")
                        if k: add("medium" if k / n >= .01 else "low", "outliers", "validity", x["name"], f"{k / n * 100:.1f}% extreme values", f"Values outside {lo:.4g} to {hi:.4g} (3× the interquartile range); thresholds were estimated from up to 50,000 rows.", k, f"SELECT * FROM {ft} WHERE {xq} < {lo:.12g} OR {xq} > {hi:.12g} LIMIT 100")
                    neg = one(f"SELECT COUNT(*) FROM {ft} WHERE {xq} < 0")
                    if neg and any(w in x["name"].lower() for w in _NEG): add("high", "negative", "validity", x["name"], f"{neg:,} negative values", f"“{x['name']}” should not normally be negative.", neg, f"SELECT * FROM {ft} WHERE {xq} < 0 LIMIT 100")
            def sec_dates():
                tc = [x for x in cols if x["kind"] == "temporal"]
                for x in tc:
                    xq = Q(x["name"]); checks[0] += 1
                    if not any(w in x["name"].lower() for w in ("expir", "due", "end", "until", "schedul", "renew", "deliver", "next")):
                        k = one(f"SELECT COUNT(*) FROM {ft} WHERE {xq} > '{today}'")
                        if k: add("medium", "future", "validity", x["name"], f"{k:,} dates in the future", f"“{x['name']}” has values after today ({today}).", k, f"SELECT * FROM {ft} WHERE {xq} > '{today}' LIMIT 100")
                for a, b in _PAIRS:
                    ca = next((x for x in tc if a in x["name"].lower()), None); cb = next((x for x in tc if b in x["name"].lower() and x is not ca), None)
                    if ca and cb:
                        checks[0] += 1; aq, bq = Q(ca["name"]), Q(cb["name"]); k = one(f"SELECT COUNT(*) FROM {ft} WHERE {bq} < {aq}")
                        if k: add("high", "order", "validity", cb["name"], f"{k:,} rows end before they start", f"“{cb['name']}” is earlier than “{ca['name']}” in {k:,} rows.", k, f"SELECT * FROM {ft} WHERE {bq} < {aq} LIMIT 100")
                if tc:
                    x = sorted(tc, key=lambda x: not any(w in x["name"].lower() for w in ("creat", "request", "order", "update", "date", "time")))[0]
                    mx = c.execute(text(f"SELECT MAX({Q(x['name'])}) FROM {ft}")).scalar(); d = pd.to_datetime(str(mx)[:10], errors="coerce")
                    if pd.notna(d) and (pd.Timestamp.now().normalize() - d).days > 90:
                        add("info", "stale", "freshness", x["name"], f"Latest record is {(pd.Timestamp.now().normalize() - d).days} days old", f"Newest “{x['name']}” value is {str(mx)[:10]}.", 0, f"SELECT * FROM {ft} ORDER BY {Q(x['name'])} DESC LIMIT 100")
            def sec_text():
                df = box["df"]; ns = len(df)
                for x in cols:
                    if x["distinct"] == 1 and n > 1 and x["nulls"] < 50: add("low", "constant", "consistency", x["name"], "Only one value", f"Every filled row of “{x['name']}” is “{(x.get('top') or [['?']])[0][0]}”.", n, f"SELECT {Q(x['name'])}, COUNT(*) AS rows FROM {ft} GROUP BY 1 LIMIT 100", 0)
                    if x["kind"] not in ("category", "text"): continue
                    s = df[x["name"]].dropna()
                    if s.empty or pd.api.types.is_numeric_dtype(s): continue
                    s = s.astype(str); nm = x["name"].lower(); xq = Q(x["name"]); checks[0] += 3
                    ws = int((s != s.str.strip()).sum()); bl = int((s.str.strip() == "").sum())
                    if ws: add("medium" if ws / ns > .01 else "low", "whitespace", "consistency", x["name"], f"{ws:,} values with stray spaces", "Leading or trailing spaces make identical values look different.", round(ws / ns * n), f"SELECT * FROM {ft} WHERE {xq} <> TRIM({xq}) LIMIT 100")
                    if bl: add("medium", "blank", "completeness", x["name"], f"{bl:,} blank text values", "Empty strings are filled cells, but hold no information.", round(bl / ns * n), f"SELECT * FROM {ft} WHERE TRIM({xq}) = '' LIMIT 100", 1.5)
                    if x["kind"] == "category":
                        vc = s.value_counts(); d2 = pd.DataFrame({"v": vc.index, "c": vc.values}); d2["k"] = d2.v.str.lower().str.strip(); d2["r"] = d2.groupby("k")["c"].rank(ascending=False, method="first")
                        aff = int(d2.loc[d2.r > 1, "c"].sum())
                        if aff:
                            ex = d2[d2.duplicated("k", keep=False)].head(2).v.tolist()
                            add("medium", "case", "consistency", x["name"], "Same value written differently", f"For example “{ex[0]}” and “{ex[1]}” are counted as separate groups.", round(aff / ns * n), f"SELECT {xq}, COUNT(*) AS rows FROM {ft} GROUP BY {xq} ORDER BY LOWER({xq}) LIMIT 100")
                    if not any(w in nm for w in ("id", "zip", "postal", "phone", "code")) and s.nunique() > 12 and pd.to_numeric(s, errors="coerce").notna().mean() >= .95:
                        add("low", "numtext", "consistency", x["name"], "Numbers stored as text", f"“{x['name']}” looks numeric but is stored as text.", n, f"SELECT {xq} FROM {ft} LIMIT 100", 0)
            def sec_fk():
                for r in p["relationships"]:
                    if r["from_table"] != name: continue
                    fc, tt, tc = Q(r["from_col"]), Q(r["to_table"]), Q(r["to_col"]); checks[0] += 1
                    j = f"FROM {ft} f LEFT JOIN {tt} p ON f.{fc} = p.{tc} WHERE f.{fc} IS NOT NULL AND p.{tc} IS NULL"; k = one(f"SELECT COUNT(*) {j}")
                    if k: add("high", "orphans", "validity", r["from_col"], f"{k:,} rows point to missing {r['to_table']}", f"“{r['from_col']}” has values with no matching {r['to_table']}.{r['to_col']}.", k, f"SELECT f.* {j} LIMIT 100")
            for fn in (sec_null, sec_dups, sec_measures, sec_dates, sec_text, sec_fk): safe(fn)
            dims = {k: round(max(0, 100 - pen[k]), 1) for k in ("completeness", "uniqueness", "validity", "consistency")}
            miss = box.get("miss") or [0] * len(cols)
            tabs.append({"name": name, "rows": n, "dims": dims, "score": round(sum(dims.values()) / 4, 1), "issues": sum(i["table"] == name for i in issues),
                         "cols": [{"name": x["name"], "kind": x["kind"], "null_pct": round(m / n * 100, 1)} for x, m in zip(cols, miss)]})
    dims = {k: round(sum(t["dims"][k] for t in tabs) / max(len(tabs), 1), 1) for k in ("completeness", "uniqueness", "validity", "consistency")}
    issues.sort(key=lambda i: (_SEV[i["sev"]], -i["pct"]))
    schema = schema_status(sid, p); contracts = contract_status(sid, p)
    checks[0]+=sum(len(c.get("required",[]))+len(c.get("unique",[]))+len(c.get("allowed",{}))+len(c.get("types",{})) for c in contracts["specs"].values())
    checks[0]+=sum(len(t["columns"]) for t in schema["tables"])
    for finding in schema["issues"] + contracts["issues"]:
        issues.append(finding)
        dim="completeness" if finding["check"]=="required" else "uniqueness" if finding["check"]=="unique" else "validity"
        penalty=2 if not finding.get("pct") else min(25,float(finding["pct"])*.5)
        dims[dim]=round(max(0,dims[dim]-penalty),1)
        tab=next((t for t in tabs if t["name"]==finding["table"]),None)
        if tab:
            tab["dims"][dim]=round(max(0,tab["dims"][dim]-penalty),1)
            tab["score"]=round(sum(tab["dims"].values())/4,1);tab["issues"]+=1
    issues.sort(key=lambda i: (_SEV[i["sev"]], -i["pct"]))
    return {"overall": {"score": round(sum(dims.values()) / 4, 1), "dims": dims}, "tables": tabs, "issues": issues, "checks": checks[0],
            "counts": {k: sum(i["sev"] == k for i in issues) for k in _SEV}, "sampled": any(t["rows"] > 50000 for t in p["tables"]),
            "schema": schema["tables"], "contracts": contracts}


def _schema(sid): return _rd(SCHEMAS, {}).get(sid)

def set_schema_baseline(sid):
    p = profile(sid, refresh=True)
    baselines = _rd(SCHEMAS, {})
    baselines[sid] = {t["name"]: {"columns":[c["name"] for c in t["columns"]],"types":{c["name"]:c["kind"] for c in t["columns"]}} for t in p["tables"]}
    _wr(SCHEMAS, baselines)
    return schema_status(sid, p)["tables"]

def schema_status(sid, p=None):
    p = p or profile(sid); baseline = _schema(sid)
    current = {t["name"]: {"columns":[c["name"] for c in t["columns"]],"types":{c["name"]:c["kind"] for c in t["columns"]}} for t in p["tables"]}
    if baseline is None:
        baselines = _rd(SCHEMAS, {}); baselines[sid] = current; _wr(SCHEMAS, baselines)
        baseline = current
    issues = []
    for table in sorted(set(baseline) | set(current)):
        old_def, now_def = baseline.get(table, []), current.get(table, [])
        old_cols = old_def.get("columns",[]) if isinstance(old_def,dict) else old_def
        now_cols = now_def.get("columns",[]) if isinstance(now_def,dict) else now_def
        old_types = old_def.get("types",{}) if isinstance(old_def,dict) else {}
        now_types = now_def.get("types",{}) if isinstance(now_def,dict) else {}
        old, now = set(old_cols), set(now_cols)
        for col in sorted(old - now):
            issues.append({"sev":"high", "check":"schema", "dim":"validity", "table":table, "column":col, "title":"Column missing from schema", "detail":f"{table}.{col} existed in the saved schema but is no longer present.", "count":1, "pct":0, "sql":f"SELECT * FROM {q(engine(sid), table)} LIMIT 100" if table in current else "SELECT 1", "fix":"Check whether the upstream schema changed intentionally, then review and update the saved baseline."})
        for col in sorted(now - old):
            issues.append({"sev":"medium", "check":"schema", "dim":"validity", "table":table, "column":col, "title":"New column detected", "detail":f"{table}.{col} was not present in the saved schema.", "count":1, "pct":0, "sql":f"SELECT * FROM {q(engine(sid), table)} LIMIT 100", "fix":"Confirm the new field is expected and update the saved schema baseline."})
        for col in sorted(old & now):
            if col in old_types and col in now_types and old_types[col] != now_types[col]:
                issues.append({"sev":"medium", "check":"schema", "dim":"validity", "table":table, "column":col, "title":"Column type changed", "detail":f"{table}.{col} changed from {old_types[col]} to {now_types[col]} in the current profile.", "count":1, "pct":0, "sql":f"SELECT * FROM {q(engine(sid), table)} LIMIT 100", "fix":"Verify the upstream type change and its impact, then update the saved schema baseline if expected."})
        if table in baseline and table not in current:
            issues.append({"sev":"high", "check":"schema", "dim":"validity", "table":table, "column":None, "title":"Table missing from schema", "detail":f"Table {table} existed in the saved schema but is no longer present.", "count":1, "pct":0, "sql":"SELECT 1", "fix":"Check the upstream table and confirm whether its removal was expected."})
    table_status=[]
    for name,value in current.items():
        old_def=baseline.get(name,[]); old_cols=old_def.get("columns",[]) if isinstance(old_def,dict) else old_def; old_types=old_def.get("types",{}) if isinstance(old_def,dict) else {}
        changed=set(value["columns"])!=set(old_cols) or any(c in old_types and old_types[c]!=value["types"].get(c) for c in value["columns"])
        table_status.append({"name":name,"columns":value["columns"],"status":"changed" if changed else "stable"})
    return {"tables":table_status, "issues":issues, "has_baseline":True}

def get_contracts(sid):
    _get(sid); return _rd(CONTRACTS, {}).get(sid, {})

def set_contract(sid, table, contract):
    p = profile(sid); t = next((x for x in p["tables"] if x["name"] == table), None)
    if not t: raise ValueError("Choose a table in this data source")
    names = {c["name"] for c in t["columns"]}
    required = list(dict.fromkeys(str(x).strip() for x in contract.get("required", []) if str(x).strip()))
    unique = list(dict.fromkeys(str(x).strip() for x in contract.get("unique", []) if str(x).strip()))
    if set(required + unique) - names: raise ValueError("Contract columns must exist in the selected table")
    allowed = contract.get("allowed", {}); types=contract.get("types", {})
    if not isinstance(allowed, dict) or set(allowed) - names or any(not isinstance(v, list) for v in allowed.values()): raise ValueError("Allowed values must map existing columns to lists")
    if not isinstance(types,dict) or set(types)-names or any(v not in ("text","number","date","boolean") for v in types.values()): raise ValueError("Types must map existing columns to text, number, date, or boolean")
    data = _rd(CONTRACTS, {}); data.setdefault(sid, {})[table] = {"required":required, "unique":unique, "allowed":allowed,"types":types}
    _wr(CONTRACTS, data); return data[sid][table]

def contract_status(sid, p=None):
    p = p or profile(sid); specs = get_contracts(sid); by = {t["name"]:t for t in p["tables"]}; issues=[]
    for table, spec in specs.items():
        if table not in by:
            issues.append({"table":table,"column":None,"check":"contract","sev":"high","title":"Contract table missing","detail":f"Required table {table} is not available.","count":1,"pct":0,"sql":"SELECT 1","fix":"Restore the table or update its data contract."}); continue
        t=by[table]; cols={c["name"]:c for c in t["columns"]}; eng=engine(sid); ft=q(eng,table); n=t["rows"]
        def count_rows(sql):
            with eng.connect() as conn: return int(conn.execute(text(sql)).scalar() or 0)
        for col in spec.get("required",[]):
            if col not in cols: continue
            cq=q(eng,col); condition=f"{cq} IS NULL"
            if cols[col]["kind"] in ("category","text"): condition+=f" OR TRIM({cq}) = ''"
            count=count_rows(f"SELECT COUNT(*) FROM {ft} WHERE {condition}")
            if count: issues.append({"table":table,"column":col,"check":"required","sev":"high","title":f"Required field {col} is missing","detail":f"Contract requires a value; {count:,} of {n:,} rows are null or blank.","count":count,"pct":round(count/max(n,1)*100,2),"sql":f"SELECT * FROM {ft} WHERE {condition} LIMIT 100","fix":"Provide a valid value or revise the contract if blanks are allowed."})
        for col in spec.get("unique",[]):
            if col not in cols: continue
            cq=q(eng,col); count=max(0,count_rows(f"SELECT COUNT({cq}) - COUNT(DISTINCT {cq}) FROM {ft}"))
            if count: issues.append({"table":table,"column":col,"check":"unique","sev":"high","title":f"Unique field {col} has duplicates","detail":f"The uniqueness contract is violated by {count:,} repeated non-null values.","count":count,"pct":round(count/max(n,1)*100,2),"sql":f"SELECT {cq}, COUNT(*) AS copies FROM {ft} WHERE {cq} IS NOT NULL GROUP BY {cq} HAVING COUNT(*) > 1 LIMIT 100","fix":"Resolve duplicate keys or revise the uniqueness contract."})
        for col, values in spec.get("allowed",{}).items():
            if col not in cols or not values: continue
            # Compare as strings to avoid database-specific literal formatting.
            allowed=set(map(str,values))
            with eng.connect() as conn: sample=pd.read_sql(text(f"SELECT DISTINCT {q(eng,col)} AS v FROM {ft} WHERE {q(eng,col)} IS NOT NULL LIMIT 50000"),conn).v
            invalid=sample[~sample.astype(str).isin(allowed)].astype(str).tolist()
            if invalid:
                literals=", ".join("'"+str(v).replace("'","''")+"'" for v in values)
                issues.append({"table":table,"column":col,"check":"allowed","sev":"medium","title":f"Unexpected values in {col}","detail":f"Found values outside the allowed set, including {', '.join(invalid[:3])!r}. Distinct values checked up to 50,000.","count":len(invalid),"pct":0,"sql":f"SELECT * FROM {ft} WHERE {q(eng,col)} NOT IN ({literals}) LIMIT 100","fix":"Correct the unexpected values or update the allowed-value list."})
        for col, expected in spec.get("types",{}).items():
            if col not in cols: continue
            with eng.connect() as conn: values_sample=pd.read_sql(text(f"SELECT {q(eng,col)} AS v FROM {ft} WHERE {q(eng,col)} IS NOT NULL LIMIT 50000"),conn).v
            if expected=="number": valid=pd.to_numeric(values_sample,errors="coerce").notna()
            elif expected=="date": valid=pd.to_datetime(values_sample,errors="coerce").notna()
            elif expected=="boolean": valid=values_sample.astype(str).str.lower().isin(("true","false","0","1","yes","no"))
            else: valid=values_sample.map(lambda x:isinstance(x,str))
            invalid=int((~valid).sum())
            if invalid: issues.append({"table":table,"column":col,"check":"type","sev":"medium","title":f"Values do not match expected {expected} type","detail":f"{invalid:,} values failed the {expected} check in a sample of {len(values_sample):,} non-null values.","count":invalid,"pct":round(invalid/max(len(values_sample),1)*100,2),"sql":f"SELECT * FROM {ft} WHERE {q(eng,col)} IS NOT NULL LIMIT 100","fix":f"Normalize {col} to the contract's expected {expected} format or revise the contract."})
    return {"specs":specs,"issues":issues}


def _transform_frame(sid, spec):
    table=str(spec.get("table", "")); p=profile(sid); meta=next((t for t in p["tables"] if t["name"]==table),None)
    if not meta: raise ValueError("Choose a table in this data source")
    if meta["rows"] > 200000: raise ValueError("Transformations are limited to 200,000 rows. Filter or export a smaller source first.")
    eng=engine(sid)
    with eng.connect() as conn: df=pd.read_sql(text(f"SELECT * FROM {q(eng,table)}"),conn)
    original=len(df); actions=spec.get("actions",{}) or {}
    col=actions.get("column")
    if col is not None and col not in df.columns: raise ValueError("Choose a column in the selected table")
    null_action=actions.get("nulls","keep")
    if null_action not in ("keep","drop_rows","fill_value","fill_mean","fill_median","fill_mode","drop_column"): raise ValueError("Unknown null handling action")
    if null_action=="drop_column" and col: df=df.drop(columns=[col])
    elif null_action=="drop_rows" and col: df=df[df[col].notna()]
    elif null_action.startswith("fill_") and col:
        val=actions.get("fill_value","")
        if null_action=="fill_mean": val=pd.to_numeric(df[col],errors="coerce").mean()
        elif null_action=="fill_median": val=pd.to_numeric(df[col],errors="coerce").median()
        elif null_action=="fill_mode":
            modes=df[col].mode(dropna=True); val=modes.iloc[0] if len(modes) else ""
        if pd.isna(val): raise ValueError("Could not calculate a fill value for this column")
        df[col]=df[col].fillna(val)
    dup=actions.get("duplicates","keep")
    if dup not in ("keep","drop"): raise ValueError("Unknown duplicate handling action")
    if dup=="drop": df=df.drop_duplicates()
    text_op=actions.get("text","none")
    if text_op not in ("none","trim","lower","upper","title","normalize_spaces"): raise ValueError("Unknown text standardization action")
    if text_op!="none" and col and col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]): raise ValueError("Choose a text column for text standardization")
        vals=df[col].astype("string")
        if text_op=="trim": vals=vals.str.strip()
        elif text_op=="lower": vals=vals.str.lower()
        elif text_op=="upper": vals=vals.str.upper()
        elif text_op=="title": vals=vals.str.title()
        elif text_op=="normalize_spaces": vals=vals.str.split().str.join(" ")
        df[col]=vals
    group=actions.get("group_by") or ""; metric=actions.get("aggregate_column") or ""; fn=actions.get("aggregate","count")
    if group:
        if group not in df.columns: raise ValueError("Group-by column must exist after the selected transformations")
        if fn not in ("count","sum","mean","median","min","max"): raise ValueError("Unsupported aggregate")
        if fn=="count": df=df.groupby(group,dropna=False).size().reset_index(name="count")
        else:
            if metric not in df.columns: raise ValueError("Choose a numeric aggregate column")
            series=pd.to_numeric(df[metric],errors="coerce")
            if series.notna().sum()==0: raise ValueError("The aggregate column must contain numeric values")
            grouped=pd.DataFrame({group:df[group],metric:series}).groupby(group,dropna=False)[metric]
            df=grouped.agg(fn).reset_index(name=f"{fn}_{metric}")
    aggregated=bool(group)
    return df, {"input_rows":original,"output_rows":len(df),"removed_rows":0 if aggregated else original-len(df),"aggregated":aggregated,"columns":list(df.columns)}

def transform_preview(sid, spec):
    df,stats=_transform_frame(sid,spec)
    return {**stats,"preview":json.loads(df.head(50).to_json(orient="split",date_format="iso"))}

def transform_csv(sid,spec):
    df,stats=_transform_frame(sid,spec)
    return df.to_csv(index=False), stats

def investigate(sid, table, column, check):
    p=profile(sid); meta=next((t for t in p["tables"] if t["name"]==table),None)
    if not meta or column not in {c["name"] for c in meta["columns"]}: raise ValueError("Choose a valid table and column")
    eng=engine(sid)
    with eng.connect() as conn: df=pd.read_sql(text(f"SELECT * FROM {q(eng,table)} LIMIT 50000"),conn)
    if column not in df: raise ValueError("Column is not available")
    s=df[column]; check=str(check)
    if check in ("nulls","blank","required"): bad=s.isna() | s.astype("string").str.strip().eq("").fillna(False)
    elif check=="whitespace": bad=s.notna() & s.astype(str).ne(s.astype(str).str.strip())
    elif check=="negative": bad=pd.to_numeric(s,errors="coerce")<0
    elif check=="unique": bad=s.notna() & s.duplicated(keep=False)
    elif check=="allowed":
        allowed=get_contracts(sid).get(table,{}).get("allowed",{}).get(column,[]); bad=s.notna() & ~s.astype(str).isin(set(map(str,allowed)))
    elif check=="type":
        typ=get_contracts(sid).get(table,{}).get("types",{}).get(column,"text")
        if typ=="number": bad=s.notna() & pd.to_numeric(s,errors="coerce").isna()
        elif typ=="date": bad=s.notna() & pd.to_datetime(s,errors="coerce").isna()
        elif typ=="boolean": bad=s.notna() & ~s.astype(str).str.lower().isin(("true","false","0","1","yes","no"))
        else: bad=s.notna() & ~s.map(lambda x:isinstance(x,str))
    elif check=="outliers":
        x=pd.to_numeric(s,errors="coerce"); q1,q3=x.quantile(.25),x.quantile(.75); iqr=q3-q1; bad=(x<q1-3*iqr)|(x>q3+3*iqr) if iqr else pd.Series(False,index=df.index)
    else: bad=pd.Series(False,index=df.index)
    bad=bad.fillna(False); good=~bad; evidence=[]
    if int(bad.sum())<3 or int(good.sum())<3: return {"table":table,"column":column,"sampled":len(df)>=50000,"affected":int(bad.sum()),"evidence":[],"summary":"Too few affected and comparison rows in the 50,000-row sample to compare reliably."}
    for other in df.columns:
        if other==column: continue
        z=df[other]
        if z.nunique(dropna=True)<2: continue
        if pd.api.types.is_numeric_dtype(z):
            a=pd.to_numeric(z[bad],errors="coerce").median(); b=pd.to_numeric(z[good],errors="coerce").median()
            if pd.notna(a) and pd.notna(b) and a!=b:
                evidence.append({"column":other,"kind":"median","affected":round(float(a),4),"comparison":round(float(b),4),"difference_pct":round((float(a)-float(b))/max(abs(float(b)),1e-9)*100,1),"n_affected":int(pd.to_numeric(z[bad],errors="coerce").notna().sum()),"n_comparison":int(pd.to_numeric(z[good],errors="coerce").notna().sum())})
        elif z.nunique(dropna=True)<=30:
            rates=pd.DataFrame({"value":z.astype("string").fillna("(null)"),"affected":bad}).groupby("value").affected.agg(["mean","count"])
            rates=rates[rates["count"]>=3].sort_values("mean",ascending=False)
            for value,row in rates.head(2).iterrows():
                if 0<row["mean"]<1: evidence.append({"column":other,"kind":"category_rate","value":str(value),"affected_rate":round(float(row["mean"])*100,1),"n":int(row["count"])})
    evidence.sort(key=lambda e: abs(e.get("difference_pct",e.get("affected_rate",0))),reverse=True)
    return {"table":table,"column":column,"sampled":len(df)>=50000,"affected":int(bad.sum()),"evidence":evidence[:8],"summary":"Associations in this sample can point to where the issue clusters; they do not establish cause."}
