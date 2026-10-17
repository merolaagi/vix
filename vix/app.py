"""Web HQ: JSON API plus the single-page dashboard. Also runs the company scheduler."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__, armory, bi, chat, crm, db, graph, netinfra, products, roles, world
from .company import charter as charter_mod
from .company import engine
from .config import STATIC_DIR


@asynccontextmanager
async def lifespan(_app):
    db.init()
    world.seed()
    engine.seed_schedules()
    engine.seed_v3_schedules()
    engine.seed_v4_schedules()
    engine.seed_v5_schedules()
    stop = threading.Event()
    t = threading.Thread(target=engine.scheduler_loop, args=(stop,), daemon=True)
    t.start()
    yield
    stop.set()


app = FastAPI(title="vix", version=__version__, lifespan=lifespan)


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "hq.html")


@app.get("/api/info")
def info():
    return {"name": "vix", "version": __version__}


# ---------- company ----------

@app.get("/api/company")
def company():
    return engine.overview()


@app.get("/api/company/charter")
def get_charter():
    return charter_mod.load()


@app.put("/api/company/charter")
def put_charter(update: dict = Body(...)):
    return _guard(charter_mod.save, update)


@app.get("/api/company/playbooks")
def playbooks():
    return [p.meta() for p in engine.PLAYBOOKS.values()]


@app.post("/api/company/run/{name}")
def run(name: str, params: dict = Body(default={})):
    return {"run_id": _guard(engine.start, name, params, "web")}


@app.get("/api/company/runs")
def runs(limit: int = 40, department: str | None = None):
    out = engine.runs(limit, department)
    for r in out:
        r.pop("log", None)
    return out


@app.get("/api/company/runs/{rid}")
def run_get(rid: int):
    r = engine.run_get(rid)
    if not r:
        raise HTTPException(404, "no such run")
    return r


@app.get("/api/company/reports")
def reports(limit: int = 60):
    return engine.reports(limit)


@app.get("/api/company/reports/{name}", response_class=PlainTextResponse)
def report(name: str):
    return _guard(engine.safe_report, name).read_text()


@app.get("/api/company/decisions")
def get_decisions(status: str | None = None):
    return engine.decisions(status)


@app.post("/api/company/decisions/{did}")
def decide(did: int, body: dict = Body(...)):
    return _guard(engine.decide, did, bool(body.get("approve")), body.get("note", ""))


@app.get("/api/company/schedules")
def schedules():
    return engine.schedules()


@app.post("/api/company/schedules")
def schedule_add(body: dict = Body(...)):
    _guard(engine.schedule_add, body.get("playbook"), body.get("every", ""), body.get("params") or {})
    return engine.schedules()


@app.patch("/api/company/schedules/{sid}")
def schedule_patch(sid: int, body: dict = Body(...)):
    engine.schedule_set(sid, enabled=bool(body.get("enabled")))
    return engine.schedules()


@app.delete("/api/company/schedules/{sid}")
def schedule_delete(sid: int):
    engine.schedule_set(sid, delete=True)
    return engine.schedules()


# ---------- world ----------

@app.get("/api/world/metros")
def metros():
    return world.metros_summary()


@app.get("/api/world/sites")
def sites():
    return world.sites()


@app.get("/api/world/halls")
def halls(metro: str | None = None):
    return world.halls(metro)


@app.get("/api/world/tenants")
def tenants(kind: str | None = None, limit: int = 200):
    return world.tenants(kind, limit)


@app.get("/api/world/tenants/{tid}")
def tenant(tid: int):
    t = world.tenant(tid)
    if not t:
        raise HTTPException(404, "no such tenant")
    return t


@app.get("/api/world/products")
def world_products():
    return {**products.catalogue_summary(), "revenue_by_product": products.revenue_breakdown()}


@app.get("/api/world/accounts")
def world_accounts(days: int = 90):
    return {"at_risk": products.at_risk(days, 20), "retention_90d": products.retention(90),
            "recent_events": db.rows("select e.*, t.name from contract_events e join tenants t on t.id=e.tenant"
                                     " order by e.id desc limit 20")}


@app.get("/api/world/latency")
def latency():
    return world.latency_matrix()


@app.get("/api/world/orders")
def orders(status: str | None = None, limit: int = 100):
    q, args = "select * from orders", ()
    if status:
        q, args = q + " where status=?", (status,)
    return db.rows(q + " order by id desc limit ?", args + (limit,))


@app.post("/api/world/orders")
def order(body: dict = Body(...)):
    return _guard(world.place_order, int(body.get("tenant", 0)), body.get("kind", ""), body.get("params") or {})


# ---------- CRM: Salesforce-shaped REST ----------

SF = f"/services/data/{crm.API_VERSION}"


def _sf_error(msg, code="MALFORMED_QUERY", status=400):
    return JSONResponse([{"message": msg, "errorCode": code}], status_code=status)


@app.get("/services/data")
def sf_versions():
    return [{"label": "vix CRM", "url": SF, "version": crm.API_VERSION[1:]}]


@app.get(SF)
def sf_resources():
    return {"sobjects": f"{SF}/sobjects", "query": f"{SF}/query"}


@app.get(SF + "/sobjects")
def sf_sobjects():
    return {"encoding": "UTF-8", "sobjects": [{"name": o, "label": crm.LABELS[o], "keyPrefix": crm.PREFIX[o],
                                                "queryable": True, "urls": {"sobject": f"{SF}/sobjects/{o}",
                                                                             "describe": f"{SF}/sobjects/{o}/describe"}}
                                               for o in crm.OBJECTS]}


@app.get(SF + "/sobjects/{obj}/describe")
def sf_describe(obj: str):
    try:
        return crm.describe(obj)
    except ValueError as e:
        return _sf_error(str(e), "NOT_FOUND", 404)


@app.get(SF + "/sobjects/{obj}/{rid}")
def sf_get(obj: str, rid: str):
    try:
        r = crm.get(obj, rid)
    except ValueError as e:
        return _sf_error(str(e), "NOT_FOUND", 404)
    return r if r else _sf_error(f"no {obj} {rid}", "NOT_FOUND", 404)


@app.get(SF + "/query")
def sf_query(q: str):
    try:
        return crm.query(q)
    except ValueError as e:
        return _sf_error(str(e))


@app.get("/api/crm/summary")
def crm_summary():
    return crm.summary()


# ---------- BI ----------

@app.get("/api/bi/status")
def bi_status():
    return bi.status()


@app.get("/api/bi/metrics")
def bi_metrics():
    return bi.metrics_catalog()


@app.get("/api/bi/metric/{name}")
def bi_metric(name: str, days: int = 90):
    return _guard(bi.metric, name, days)


@app.get("/api/bi/dashboards")
def bi_dashboards():
    return {k: {"title": v["title"], "tiles": len(v["tiles"])} for k, v in bi.dashboards().items()}


@app.get("/api/bi/dashboard/{slug}")
def bi_dashboard(slug: str, days: int = 90):
    return _guard(bi.dashboard, slug, days)


@app.get("/api/bi/exports")
def bi_exports():
    return bi.exports()


@app.get("/api/bi/exports/{date}/{name}")
def bi_export_file(date: str, name: str):
    p = (bi.EXPORT_DIR / date / name).resolve()
    if bi.EXPORT_DIR.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, "no such export file")
    return FileResponse(p, filename=name)


# ---------- armory ----------

@app.get("/api/armory")
def armory_tools():
    return armory.tools()


@app.get("/api/net/summary")
def net_summary():
    return netinfra.summary()


@app.get("/api/net/devices")
def net_devices(site: str | None = None, role: str | None = None):
    return netinfra.inventory(site, role)


@app.get("/api/net/incidents")
def net_incidents(limit: int = 40):
    return db.rows("select * from net_incident order by id desc limit ?", (limit,))


@app.get("/api/net/changes")
def net_changes(limit: int = 40):
    return db.rows("select * from net_change order by id desc limit ?", (limit,))


@app.get("/api/net/capacity")
def net_capacity():
    day = db.scalar("select max(date) from net_capacity_daily")
    rows = db.rows("select * from net_capacity_daily where date=? order by util desc limit 20", (day,))
    for r in rows:
        r["days_to_80"] = netinfra.forecast_days(r["kind"], r["ref"])
    return rows


@app.get("/api/net/kpis")
def net_kpis():
    from .company.net_playbooks import network_kpis
    return {"d7": network_kpis(7), "d30": network_kpis(30), "readiness": db.setting("net_readiness")}


@app.get("/api/org")
def org():
    return engine.org_chart()


# ---------------------------------------------------------------- v0.5: roles, ServiceNow, graph, Explore, Ask vix


@app.get("/api/roles")
def roles_catalog():
    return roles.catalog()


@app.get("/api/now/table/{table}")
def snow(table: str, sysparm_query: str = "", sysparm_limit: int = 50):
    from .company import eng_playbooks
    try:
        return eng_playbooks.snow_table(table, sysparm_query, sysparm_limit)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/graph/stats")
def graph_stats():
    return graph.stats()


@app.get("/api/graph/search")
def graph_search(q: str, limit: int = 15):
    return graph.search(q, limit)


@app.get("/api/graph/neighborhood")
def graph_neighborhood(id: str, depth: int = 1, limit: int = 80):
    return graph.neighborhood(id, depth, limit)


@app.get("/api/graph/path")
def graph_path(a: str, b: str):
    return graph.path(a, b)


@app.get("/api/graph/money")
def graph_money():
    return graph.money_map()


@app.post("/api/graph/rebuild")
def graph_rebuild():
    return {"run_id": engine.start("graph_sync", {}, "web")}


@app.post("/api/graph/cypher")
def graph_cypher(body: dict = Body(...)):
    try:
        return graph.cypher(body.get("query", ""), body.get("params") or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/bi/explore/model")
def explore_model():
    return bi.explore_model()


@app.post("/api/bi/explore")
def explore_run(spec: dict = Body(...)):
    try:
        return bi.explore(spec)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/bi/explore/pin")
def explore_pin(body: dict = Body(...)):
    try:
        return bi.pin_explore(body["spec"], body.get("title") or "Exploration", body.get("dashboard") or "explore",
                              body.get("chart") or "bar")
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/chat/engine")
def chat_engine():
    return {"engine": chat.engine_name()}


@app.post("/api/chat")
def chat_ask(body: dict = Body(...)):
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(400, "message is empty")
    return chat.answer(msg[:2000], body.get("history") or [])
