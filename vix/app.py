"""Web HQ: JSON API plus the single-page dashboard. Also runs the company scheduler."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__, armory, bi, chat, council, crm, db, evolve, fin, graph, growth, harness, llm, market, netinfra, products, roles, trust, world
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


# ---------------------------------------------------------------- v0.6-0.9: finance, growth, market, trust, governance, portal

import json as _json  # noqa: E402

from .config import DATA_DIR as _DATA  # noqa: E402


def _closed_rows(n=24):
    return [{"period": m["period"], "revenue": round(m["income"]["revenue"]), "adj_ebitda": round(m["income"]["adj_ebitda"]),
             "net_income": round(m["income"]["net_income"]), "affo": round(m["metrics"]["affo"]),
             "affo_per_share": round(m["metrics"]["affo_per_share"], 3), "net_debt_to_ebitda": m["metrics"]["net_debt_to_ebitda"],
             "cash": round(m["balance"]["assets"]["cash"]), "dso_days": m["metrics"]["dso_days"],
             "balance_check": m["metrics"]["balance_check"]} for m in fin.closed_periods(n)]


@app.get("/api/fin/summary")
def fin_summary():
    fin.init()
    last = fin.closed_periods(1)
    return {"kpis": fin.kpi_hook().get("finance"), "last_close": last[0] if last else None, "periods": _closed_rows(),
            "aging": fin.ar_aging(), "debt": fin.debt_summary(), "forecast": fin.forecast(6),
            "budget_vs_actual": fin.budget_vs_actual(last[0]["period"]) if last else [],
            "dividends": db.rows("select * from fin_dividend order by id desc"),
            "earnings": db.rows("select id, report, summary, started from runs where playbook='ir_earnings_pack' and status='done'"
                                " and json_extract(summary,'$.published') is not null order by id desc limit 8"),
            "fx": db.rows("select ccy, round(rate,4) rate, round(100*(rate/base-1),2) move_pct from fin_fx where ccy!='USD'")}


@app.get("/api/fin/statements")
def fin_statements(period: str = ""):
    fin.init()
    period = period or (fin.closed_periods(1)[0]["period"] if fin.closed_periods(1) else products.today()[:7])
    return fin.statements(period)


@app.get("/api/fin/invoices")
def fin_invoices(status: str = "", tenant: int = 0, limit: int = 50):
    fin.init()
    q, a = ("select i.id, i.number, t.name, i.period, i.issue_date, i.due_date, i.currency, i.amount_usd, i.balance_usd, i.status,"
            " i.disputed, i.dunning from fin_invoice i join tenants t on t.id=i.tenant where 1=1"), []
    if status:
        q, a = q + " and i.status=?", a + [status]
    if tenant:
        q, a = q + " and i.tenant=?", a + [tenant]
    return db.rows(q + " order by i.id desc limit ?", tuple(a + [min(limit, 500)]))


@app.get("/api/fin/quotes")
def fin_quotes(limit: int = 40):
    fin.init()
    return db.rows("select q.sim_date, t.name, q.kind, q.metro, q.list_mrr, q.discount_pct, q.net_mrr, q.gm_pct, q.approval, q.status, q.note"
                   " from fin_quote q join tenants t on t.id=q.tenant order by q.id desc limit ?", (min(limit, 500),))


@app.get("/api/growth/summary")
def growth_summary():
    growth.init()
    projects = db.rows("select * from dev_project order by status, stage_due")
    for p in projects:
        p["history"] = _json.loads(p["history"] or "[]")
        p["roi"] = _json.loads(p["roi"] or "[]")
    return {"summary": growth.summary(), "projects": projects, "outlook": growth.outlook(), "renewables": growth.renewables(),
            "grid": db.rows("select * from dev_power order by metro"), "ppas": db.rows("select * from dev_ppa order by id desc"),
            "rfps": db.rows("select * from dev_rfp order by id desc limit 20"), "stages": growth.STAGES}


@app.get("/api/market/summary")
def market_summary():
    market.init()
    comps = db.rows("select name, style, price_index, round(reputation,2) reputation, aggressiveness, presence from mkt_competitor")
    for c in comps:
        c["presence"] = _json.loads(c["presence"])
    return {"competitors": comps, "events": db.rows("select * from mkt_competitor_event order by id desc limit 25"),
            "winloss": market.winloss(30), "winloss_90": market.winloss(90), "nps": market.nps(), "ecosystem": market.ecosystem(),
            "listings": market.listings()[:25], "partners": db.rows("select * from mkt_partner order by mrr_sourced desc"),
            "personas": db.rows("select m.persona, count(*) n, round(avg(m.nps),1) avg_nps, sum(m.portal) portal from mkt_customer m"
                                " join contracts c on c.tenant=m.tenant where c.status='active' group by m.persona order by n desc"),
            "metal": {"position": market.metal_position(), "sunset": db.setting("metal_sunset"),
                      "migration": db.rows("select status, count(*) n, round(sum(mrr_before)) before, round(sum(coalesce(mrr_after,0))) after"
                                           " from mkt_migration group by status")}}


@app.get("/api/trust/summary")
def trust_summary():
    trust.init()
    latest = db.scalar("select max(sim_date) from cmp_result")
    ctl = db.rows("select c.id, c.family, c.title, c.owner, c.frameworks, r.status, r.evidence, r.path from cmp_control c left join cmp_result r"
                  " on r.id=(select max(id) from cmp_result x where x.control=c.id) order by c.id")
    return {"readiness": db.setting("compliance_readiness"), "controls": ctl, "as_of": latest,
            "audits": db.rows("select * from cmp_audit order by id desc limit 5"),
            "risks": db.rows("select * from risk_register order by score desc"),
            "incidents": db.rows("select * from sec_incident order by id desc limit 25"),
            "alerts": db.rows("select * from soc_alert order by id desc limit 25"),
            "drills": db.rows("select * from bcm_drill order by id desc limit 8"), "backup": db.setting("last_backup"),
            "legal": db.rows("select l.sim_date, t.name, l.request, l.decision, l.risk, l.note from legal_review l join tenants t on t.id=l.tenant"
                             " order by l.id desc limit 20")}


@app.get("/api/trust/evidence/{date}/{control}")
def trust_evidence(date: str, control: str):
    import re as _re
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not _re.fullmatch(r"[A-Z]{2}-\d{2}", control):
        raise HTTPException(400, "bad evidence path")
    f = _DATA / "evidence" / date / f"{control}.json"
    if not f.exists():
        raise HTTPException(404, "no evidence")
    return _json.loads(f.read_text())


@app.get("/api/gov/summary")
def gov_summary():
    trust.init()
    latest = db.scalar("select max(sim_date) from gov_scorecard")
    return {"scorecards": db.rows("select * from gov_scorecard where sim_date=? order by grade, agent", (latest,)), "as_of": latest,
            "outcomes": db.rows("select * from gov_outcome order by approved_on desc limit 60"),
            "by_kind": db.rows("select kind, count(*) n, round(avg(score),2) avg_score from gov_outcome where status='scored' and score is not null group by kind"),
            "budgets": db.rows("select * from gov_budget where period=(select max(period) from gov_budget)")}


# ---- customer portal: the operator issues a key; the customer's own API uses it

@app.get("/portal")
def portal_page():
    return FileResponse(STATIC_DIR / "portal.html")


@app.get("/api/portal/customers")
def portal_customers(q: str = "", limit: int = 30):
    market.init()
    return db.rows("select t.id, t.name, t.kind, t.home_metro, m.persona, m.portal from tenants t join contracts c on c.tenant=t.id"
                   " left join mkt_customer m on m.tenant=t.id where c.status='active' and t.name like ? order by t.name limit ?",
                   (f"%{q}%", min(limit, 200)))


@app.post("/api/portal/key")
def portal_key(body: dict = Body(...)):
    market.init()
    tid = int(body.get("tenant") or 0)
    if not db.one("select id from tenants where id=?", (tid,)):
        raise HTTPException(404, "no such customer")
    return {"tenant": tid, "api_key": market.issue_key(tid), "note": "shown once; the customer uses it as Authorization: Bearer <key>"}


def _cust(authorization: str | None, x_vix_key: str | None) -> int:
    key = (x_vix_key or "").strip() or ((authorization or "")[7:].strip() if (authorization or "").lower().startswith("bearer ") else "")
    tid = market.tenant_for_key(key)
    if not tid:
        raise HTTPException(401, "invalid or missing API key")
    return tid


@app.get("/portal/api/v1/me")
def papi_me(authorization: str | None = Header(None), x_vix_key: str | None = Header(None)):
    return market.portal_view(_cust(authorization, x_vix_key))


@app.get("/portal/api/v1/invoices")
def papi_invoices(authorization: str | None = Header(None), x_vix_key: str | None = Header(None)):
    return market.portal_view(_cust(authorization, x_vix_key))["invoices"]


@app.get("/portal/api/v1/catalog")
def papi_catalog(authorization: str | None = Header(None), x_vix_key: str | None = Header(None)):
    _cust(authorization, x_vix_key)
    metal_off = bool((db.setting("metal_sunset") or {}).get("announced"))
    return {"products": {k: v for k, v in world.order_kinds().items() if not (metal_off and k == "metal")},
            "metros": [m["code"] for m in db.rows("select code from metros order by code")],
            "sites": [s["id"] for s in db.rows("select id from sites order by id")],
            "clouds": [c["id"] for c in db.rows("select id from clouds order by id")],
            "plans": list(world.METAL_PLANS), "request_kinds": market.REQUEST_KINDS}


@app.post("/portal/api/v1/orders")
def papi_order(body: dict = Body(...), authorization: str | None = Header(None), x_vix_key: str | None = Header(None)):
    tid = _cust(authorization, x_vix_key)
    try:
        return market.portal_order(tid, body.get("kind", ""), body.get("params") or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/portal/api/v1/requests")
def papi_request(body: dict = Body(...), authorization: str | None = Header(None), x_vix_key: str | None = Header(None)):
    tid = _cust(authorization, x_vix_key)
    try:
        return market.request(tid, body.get("kind", ""), body.get("detail") or {}, channel="api")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------------------------------------------------------------- v0.10: models, council, self-improvement

@app.get("/api/llm/settings")
def llm_settings():
    return llm.public_settings()


@app.post("/api/llm/settings")
def llm_settings_set(body: dict = Body(...)):
    try:
        return llm.update(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/llm/test/{provider}")
def llm_test(provider: str):
    if provider not in llm.PROVIDERS:
        raise HTTPException(404, "unknown provider")
    return llm.test_provider(provider)


@app.post("/api/llm/try")
def llm_try(body: dict = Body(...)):
    spec = body.get("model") or (llm.models_for("chat") or [None])[0]
    if not spec:
        raise HTTPException(400, "no model configured and Ollama is not running")
    try:
        r = llm.call(spec, [{"role": "user", "content": body.get("prompt") or "In one sentence, what does a data-center interconnection company sell?"}],
                     role="test", max_tokens=200)
        return {"model": spec, "text": r["text"], "latency_s": r["latency_s"]}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {str(e)[:300]}") from e


@app.get("/api/llm/ollama")
def llm_ollama():
    return llm.ollama_status()


@app.post("/api/llm/ollama/pull")
def llm_ollama_pull(body: dict = Body(...)):
    try:
        return llm.ollama_pull(body.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/llm/ollama/delete")
def llm_ollama_delete(body: dict = Body(...)):
    try:
        return llm.ollama_delete(body.get("name", ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@app.get("/api/llm/usage")
def llm_usage():
    llm.init()
    return {"month": llm.usage_month(), "recent": db.rows("select * from llm_usage order by id desc limit 40")}


@app.get("/api/council")
def council_list():
    council.init()
    return {"settings": council.settings(), "members": council.members(), "sessions": council.sessions(), "record": council.record(),
            "pending_for_claude": len(council.pending_for_claude())}


@app.post("/api/council/convene/{decision}")
def council_convene(decision: int):
    from .company.playbooks import _recommend
    d = next((x for x in engine.decisions() if x["id"] == decision), None)
    if not d:
        raise HTTPException(404, "no such decision")
    v, why = _recommend(d)
    return council.convene(d, v, why, force=True)


@app.get("/api/harness")
def harness_view():
    return {"version": harness.version(), "components": harness.catalog(), "history": (db.setting("harness") or {}).get("history", [])[-10:]}


@app.get("/api/evolve")
def evolve_view():
    return {"runs": evolve.runs(), "queue": __import__("json").loads(evolve.QUEUE.read_text()) if evolve.QUEUE.exists() else []}


@app.get("/api/evolve/{run_id}")
def evolve_run_view(run_id: str):
    import re as _re
    if not _re.fullmatch(r"[\w-]+", run_id) or not (evolve.ROOT / run_id / "state.json").exists():
        raise HTTPException(404, "no such run")
    st = evolve._load(run_id)
    st.pop("frontier_rewards", None)
    st.pop("baseline_rewards", None)
    return {"state": st, "history": evolve.history(run_id)}


@app.post("/api/evolve/run")
def evolve_start(body: dict = Body(default={})):
    return {"run_id": engine.start("evolve_run", {"rounds": int(body.get("rounds", 3)), "days": int(body.get("days", 45))}, "web")}


@app.post("/api/evolve/propose")
def evolve_propose(body: dict = Body(...)):
    try:
        return evolve.queue_proposal(body.get("component", ""), body.get("value"), body.get("hypothesis", ""), by="founder")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/evolve/stop/{run_id}")
def evolve_stop(run_id: str):
    evolve.stop(run_id)
    return {"stopping": run_id}
