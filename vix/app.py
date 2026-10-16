"""Web HQ: JSON API plus the single-page dashboard. Also runs the company scheduler."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from . import __version__, db, world
from .company import charter as charter_mod
from .company import engine
from .config import STATIC_DIR


@asynccontextmanager
async def lifespan(_app):
    db.init()
    world.seed()
    engine.seed_schedules()
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
