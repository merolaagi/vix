"""The company runtime: departments, playbook registry, runs (with super agents
delegating child runs to departments), reports, decisions and schedules."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .. import db, world
from ..config import REPORT_DIR
from . import charter as charter_mod

DEPARTMENTS = [
    {"id": "ceo", "name": "Office of the CEO", "agent": "Helm", "tier": "chief", "reports_to": "founder",
     "mission": "Run the company for the founder: pull reports from the Chief Operator, Chief Scientist and Chief "
                "Data Officer, check them against each other, and deliver a weekly board pack with a recommendation "
                "on every decision that needs the founder.",
     "kpis": ["board packs", "decisions recommended"]},
    {"id": "cdo", "name": "Office of the Chief Data Officer", "agent": "Vega", "tier": "super", "reports_to": "ceo",
     "mission": "Own the company's data: the CRM, the warehouse and the dashboards. Audit that CRM, operations, BI and "
                "Finance agree, verify with REA evidence, self-heal drift, and escalate what can't be fixed.",
     "kpis": ["data quality score", "open findings"]},
    {"id": "cno", "name": "Office of the Chief Network Officer", "agent": "Meridian", "tier": "super", "reports_to": "ceo",
     "mission": "Keep every customer connected: own the routers, switches, optical gear and backbone, the NOC, changes and "
                "capacity; keep the network ready to run real gear; report availability and risk to the CEO.",
     "kpis": ["availability", "MTTR", "critical CVE devices"]},
    {"id": "noc", "name": "Network Operations Center", "agent": "Sentinel", "tier": "department", "reports_to": "cno",
     "mission": "Watch every device 24x7 through the driver layer, diagnose faults, remediate, measure customer impact.",
     "kpis": ["incidents", "MTTD", "MTTR"]},
    {"id": "change", "name": "Change Management", "agent": "Tempo", "tier": "department", "reports_to": "cno",
     "mission": "Plan and run firmware upgrades and config changes in maintenance windows with notice and rollback.",
     "kpis": ["changes done", "rollbacks"]},
    {"id": "capacity", "name": "Network Capacity", "agent": "Gauge", "tier": "department", "reports_to": "cno",
     "mission": "Forecast port and backbone exhaustion and file capex decisions before customers wait.",
     "kpis": ["hot spots"]},
    {"id": "cto", "name": "Office of the CTO", "agent": "Lattice", "tier": "super", "reports_to": "ceo",
     "mission": "Own the software that runs the company: platform APIs, workflows, security, product software, ITSM and the "
                "AI assistant. Report engineering health to the CEO.", "kpis": ["test pass rate", "security findings"]},
    {"id": "platform", "name": "Platform Engineering", "agent": "Anvil", "tier": "department", "reports_to": "cto",
     "mission": "Backend APIs and data stores, durable workflows, end-to-end tests, DevSecOps.", "kpis": ["API p95", "e2e pass"]},
    {"id": "productsw", "name": "Product Software", "agent": "Keel", "tier": "department", "reports_to": "cto",
     "mission": "Product backends including Internet Access; regression tests and delivery quality.", "kpis": ["regression pass"]},
    {"id": "aieng", "name": "AI Engineering", "agent": "Echo", "tier": "department", "reports_to": "cto",
     "mission": "The Ask vix assistant and MCP tools grounded in telemetry, inventory, topology and finance; evaluation.",
     "kpis": ["eval score"]},
    {"id": "itsm", "name": "IT Service Management", "agent": "Tally", "tier": "department", "reports_to": "cto",
     "mission": "ServiceNow-shaped incidents, change requests, problems and CMDB.", "kpis": ["problems open"]},
    {"id": "datacloud", "name": "Data Cloud & Marketing Technology", "agent": "Signal", "tier": "department", "reports_to": "cdo",
     "mission": "Unified customer profiles, consent, segments and next-best-action opportunities.", "kpis": ["actions", "consent"]},
    {"id": "graph", "name": "Knowledge Graph", "agent": "Loom", "tier": "department", "reports_to": "cdo",
     "mission": "Keep the company graph (people, work, customers, money, gear) current in Neo4j and the embedded store.",
     "kpis": ["nodes", "edges"]},
    {"id": "netauto", "name": "Network Automation", "agent": "Cadence", "tier": "department", "reports_to": "cno",
     "mission": "Golden configs, compliance checks and automated remediation through change management.",
     "kpis": ["config compliance"]},
    {"id": "certlab", "name": "Network Certification Lab", "agent": "Proof", "tier": "department", "reports_to": "cno",
     "mission": "Certify every network OS release before rollout: regression, interop, scale, rollback.", "kpis": ["releases certified"]},
    {"id": "sre", "name": "Site Reliability Engineering", "agent": "Beacon", "tier": "department", "reports_to": "cno",
     "mission": "SLOs, error budgets, change freezes and postmortems.", "kpis": ["budget burn", "SLOs met"]},
    {"id": "arch", "name": "Network Architecture", "agent": "Vantage", "tier": "department", "reports_to": "cno",
     "mission": "Measure vendor families, write architecture decision records, request proofs of concept.", "kpis": ["ADRs"]},
    {"id": "sdn", "name": "SDN & AI Networking", "agent": "Mesh", "tier": "department", "reports_to": "rnd",
     "mission": "Inference-traffic routing, L4/L7 proxying and mTLS for AI halls.", "kpis": ["p95 latency"]},
    {"id": "ps", "name": "Professional Services", "agent": "Bridge", "tier": "department", "reports_to": "exec",
     "mission": "Implementation projects for large wins: milestones, delays, CSAT.", "kpis": ["projects", "CSAT"]},
    {"id": "exec", "name": "Office of the Chief Operator", "agent": "Atlas", "tier": "super", "reports_to": "ceo",
     "mission": "Run the business: drive the operating cycle across the departments on the simulated "
                "calendar, review KPIs weekly and file decisions (new air or AI halls, cooling, staffing) for the founder.",
     "kpis": ["decisions filed", "operating cycles"]},
    {"id": "rnd", "name": "Research & Development", "agent": "Euler", "tier": "super", "reports_to": "ceo",
     "mission": "Chief Scientist: design and test network gear and standards in simulation, delegate studies "
                "to Network Engineering, and propose standards when the evidence is strong.",
     "kpis": ["studies run", "standards proposed"]},
    {"id": "sales", "name": "Sales & Customers", "agent": "Broker", "tier": "department",
     "mission": "Bring in synthetic demand from existing and new tenants and turn it into orders.",
     "kpis": ["orders placed", "new tenants"]},
    {"id": "ix", "name": "Interconnection", "agent": "Weaver", "tier": "department",
     "mission": "Provision ports, cross-connects and fabric virtual circuits.",
     "kpis": ["cross-connects", "virtual circuits"]},
    {"id": "ops", "name": "Site Operations", "agent": "Warden", "tier": "department",
     "mission": "Keep halls inside thermal and power limits, place colocation orders, run failure drills.",
     "kpis": ["halls healthy", "drills run"]},
    {"id": "metal", "name": "Compute (Metal)", "agent": "Forge", "tier": "department",
     "mission": "Provision bare-metal servers from mock inventory.",
     "kpis": ["servers allocated"]},
    {"id": "net", "name": "Network Engineering", "agent": "Pathfinder", "tier": "department",
     "mission": "Run BGP at the exchanges, study failure and attack events, keep the latency map.",
     "kpis": ["studies run", "reachability"]},
    {"id": "digital", "name": "Digital Services", "agent": "Relay", "tier": "department",
     "mission": "Run the software-defined products: cloud routers and Network Edge virtual devices.",
     "kpis": ["cloud routers", "edge devices"]},
    {"id": "cx", "name": "Customer Operations", "agent": "Concierge", "tier": "department",
     "mission": "Run the remote-hands desk at every site: tickets, SLAs, billing and staffing.",
     "kpis": ["SLA attainment", "tickets per day"]},
    {"id": "accounts", "name": "Account Management", "agent": "Keeper", "tier": "department",
     "mission": "Renew contracts with escalators, watch accounts at risk, and record downsizing and churn.",
     "kpis": ["net retention", "accounts at risk"]},
    {"id": "crm", "name": "CRM Operations", "agent": "Scribe", "tier": "department", "reports_to": "cdo",
     "mission": "Keep the CRM true to operations: accounts, contracts, assets and cases synced every simulated day.",
     "kpis": ["accounts", "assets", "cases"]},
    {"id": "bi", "name": "Business Intelligence", "agent": "Prism", "tier": "department", "reports_to": "cdo",
     "mission": "Load the warehouse nightly, own the metrics layer and dashboards, and publish Tableau-ready extracts.",
     "kpis": ["days loaded", "dashboards"]},
    {"id": "energy", "name": "Energy & Sustainability", "agent": "Steward", "tier": "department",
     "mission": "Report energy use, PUE and carbon per metro.",
     "kpis": ["fleet PUE", "tCO2 per day"]},
    {"id": "finance", "name": "Finance", "agent": "Ledger", "tier": "department",
     "mission": "Close the month: revenue by product, power cost, opex, depreciation, EBITDA.",
     "kpis": ["MRR", "EBITDA margin"]},
]
for _d in DEPARTMENTS:
    _d.setdefault("reports_to", {"net": "cno"}.get(_d["id"], "exec"))
DEPT = {d["id"]: d for d in DEPARTMENTS}

# ---------- extension points (v0.6+): later modules register here instead of patching the core ----------
DECISION_EFFECTS: dict = {}   # kind -> fn(detail, decision_row) -> effect dict
RECOMMENDERS: dict = {}       # kind -> fn(decision) -> (verdict, why)
ON_DECIDE: list = []          # fn(decision_row, approved, effect) after any decision is resolved
DAILY_STEPS: list = []        # playbooks run every simulated day after the core steps
WEEKLY_STEPS: list = []       # playbooks run every 7th simulated day in fast-forward
BOARD_CHIEFS: list = []       # (playbook, fn(run) -> (bullet markdown, [risks])) for the CEO board pack
KPI_HOOKS: list = []          # fn() -> dict merged into kpis()
BOOT: list = []               # fn() run at startup (create tables, open books, add schedules)
DEMAND_HOOKS: list = []       # fn(tenant_row, kind_weights) -> new weights, or None to skip this tenant today


def add_department(d: dict, before: str | None = None):
    """Register a department (idempotent). Keeps DEPT in sync."""
    if d["id"] in DEPT:
        DEPT[d["id"]].update(d)
        return
    d.setdefault("tier", "department")
    d.setdefault("kpis", [])
    idx = next((i for i, x in enumerate(DEPARTMENTS) if x["id"] == before), len(DEPARTMENTS)) if before else len(DEPARTMENTS)
    DEPARTMENTS.insert(idx, d)
    DEPT[d["id"]] = d


def add_schedules(pairs):
    have = {s["playbook"] for s in db.rows("select playbook from schedules")}
    for name, every in pairs:
        if name not in have and name in PLAYBOOKS:
            schedule_add(name, every)


def org_chart() -> dict:
    """Chain of command: founder -> CEO -> chiefs -> departments."""
    def node(did):
        d = DEPT[did]
        return {"id": did, "name": d["name"], "agent": d["agent"], "tier": d["tier"],
                "reports": [node(x["id"]) for x in DEPARTMENTS if x["reports_to"] == did]}
    return {"id": "founder", "name": "Founder", "agent": "you", "tier": "founder",
            "reports": [node(d["id"]) for d in DEPARTMENTS if d["reports_to"] == "founder"]}


@dataclass
class Playbook:
    name: str
    title: str
    department: str
    description: str
    fn: Callable
    params: dict = field(default_factory=dict)

    def meta(self):
        return {"name": self.name, "title": self.title, "department": self.department,
                "agent": DEPT[self.department]["agent"], "description": self.description, "params": self.params}


PLAYBOOKS: dict[str, Playbook] = {}


def playbook(name, title, department, description, params=None):
    def deco(fn):
        PLAYBOOKS[name] = Playbook(name, title, department, description, fn, params or {})
        return fn
    return deco


class Ctx:
    def __init__(self, run_id, pb: Playbook, params: dict):
        self.run_id, self.pb, self.params = run_id, pb, params
        self.agent = DEPT[pb.department]["agent"]
        self._log = []

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} [{self.agent}] {msg}"
        self._log.append(line)
        db.execute("update runs set log=? where id=?", ("\n".join(self._log), self.run_id))

    def p(self, key, default=None, cast=None):
        v = self.params.get(key, self.pb.params.get(key, {}).get("default", default))
        if v is None:
            return default
        return cast(v) if cast else v

    def inline(self, name, params=None) -> dict:
        """Run a department playbook inside this run (no separate run or report).
        Used when a super agent fast-forwards many simulated days."""
        pb = PLAYBOOKS[name]
        sub = Ctx.__new__(Ctx)
        sub.run_id, sub.pb, sub.params, sub.agent, sub._log = self.run_id, pb, params or {}, DEPT[pb.department]["agent"], []
        sub.log = lambda msg: None
        sub.is_inline = True
        summary, _ = pb.fn(sub)
        return summary

    def child(self, name, params=None) -> dict:
        """Delegate to a department: runs synchronously as a child run and returns it."""
        dept = DEPT[PLAYBOOKS[name].department]
        self.log(f"delegates {name} to {dept['agent']} ({dept['name']})")
        rid = _create(name, params or {}, trigger=f"run {self.run_id}", parent=self.run_id)
        _execute(rid)
        r = run_get(rid)
        self.log(f"{dept['agent']} finished {name}: {r['status']}")
        return r


def _create(name, params, trigger, parent=None):
    if name not in PLAYBOOKS:
        raise ValueError(f"unknown playbook {name}; one of {sorted(PLAYBOOKS)}")
    pb = PLAYBOOKS[name]
    return db.execute("insert into runs(playbook,department,params,status,trigger,parent,started,log)"
                      " values(?,?,?,?,?,?,?,?)",
                      (name, pb.department, json.dumps(params), "running", trigger, parent, time.time(), ""))


def _execute(rid):
    r = db.one("select * from runs where id=?", (rid,))
    pb = PLAYBOOKS[r["playbook"]]
    ctx = Ctx(rid, pb, json.loads(r["params"] or "{}"))
    try:
        summary, md = pb.fn(ctx)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        report = f"{stamp}-{pb.name}-r{rid}.md"
        dept_now = db.scalar("select department from runs where id=?", (rid,)) or pb.department
        head = (f"# {pb.title}\n\n_{DEPT.get(dept_now, DEPT[pb.department])['name']} · agent {ctx.agent} · run {rid} · "
                f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · simulated data_\n\n")
        (REPORT_DIR / report).write_text(head + md)
        db.execute("update runs set status='done', finished=?, report=?, summary=? where id=?",
                   (time.time(), report, json.dumps(summary, default=str), rid))
    except Exception as e:  # noqa: BLE001
        ctx.log(f"failed: {e}")
        db.execute("update runs set status='failed', finished=?, summary=?, log=? where id=?",
                   (time.time(), json.dumps({"error": str(e)}),
                    "\n".join(ctx._log + [traceback.format_exc()[-2000:]]), rid))


def start(name, params=None, trigger="manual") -> int:
    rid = _create(name, params or {}, trigger)
    threading.Thread(target=_execute, args=(rid,), daemon=True).start()
    return rid


def run_now(name, params=None, trigger="manual") -> dict:
    rid = _create(name, params or {}, trigger)
    _execute(rid)
    return run_get(rid)


def run_get(rid):
    r = db.one("select * from runs where id=?", (rid,))
    if r:
        r["params"] = json.loads(r["params"] or "{}")
        r["summary"] = json.loads(r["summary"] or "null")
        r["children"] = [c["id"] for c in db.rows("select id from runs where parent=?", (rid,))]
    return r


def runs(limit=20, department=None):
    q, args = "select * from runs", ()
    if department:
        q, args = q + " where department=?", (department,)
    out = db.rows(q + " order by id desc limit ?", args + (limit,))
    for r in out:
        r["params"] = json.loads(r["params"] or "{}")
        r["summary"] = json.loads(r["summary"] or "null")
    return out


def safe_report(name: str):
    p = (REPORT_DIR / name).resolve()
    if p.parent != REPORT_DIR.resolve() or not p.name.endswith(".md") or not p.exists():
        raise ValueError(f"no report {name}")
    return p


def reports(limit=50):
    files = sorted(REPORT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime} for f in files]


# ---------- decisions ----------

DEDUPE_KEY = {"build_hall": "site", "hire_techs": "site", "cooling_upgrade": "hall", "network_standard": None,
              "network_capex": "ref", "net_change": "change", "net_live_writes": None, "net_device_mode": "hostname"}


def decision_add(agent, kind, title, detail):
    """File a decision for the founder, unless an open one already covers the same thing."""
    for d in db.rows("select id, detail from decisions where status='open' and kind=?", (kind,)):
        key = DEDUPE_KEY.get(kind, "__all__")
        old = json.loads(d["detail"] or "{}")
        if key is None or (key == "__all__" and old == detail) or (key in old and old.get(key) == detail.get(key)):
            return d["id"]
    return db.execute("insert into decisions(ts,agent,kind,title,detail,status) values(?,?,?,?,?,'open')",
                      (time.time(), agent, kind, title, json.dumps(detail)))


def decisions(status=None):
    q, args = "select * from decisions", ()
    if status:
        q, args = q + " where status=?", (status,)
    out = db.rows(q + " order by id desc", args)
    for d in out:
        d["detail"] = json.loads(d["detail"] or "{}")
    return out


def decide(did: int, approve: bool, note: str = "") -> dict:
    d = db.one("select * from decisions where id=?", (did,))
    if not d:
        raise ValueError(f"no decision {did}")
    if d["status"] != "open":
        raise ValueError(f"decision {did} is already {d['status']}")
    detail = json.loads(d["detail"] or "{}")
    effect = None
    if approve and d["kind"] in DECISION_EFFECTS:
        effect = DECISION_EFFECTS[d["kind"]](detail, d)
    elif approve:
        if d["kind"] == "build_hall":
            effect = world.add_hall(detail["site"], int(detail.get("cabinets", 260)), bool(detail.get("liquid")))
        elif d["kind"] == "network_capex":
            from .. import netinfra
            act = detail["action"]
            if act == "leaf_pair":
                effect = netinfra.add_leaf_pair(detail["ref"])
            elif act == "backbone_400g":
                effect = netinfra.upgrade_link(detail["ref"], 400)
            elif act == "ix_switch":
                effect = netinfra.add_ix_switch(detail["ref"])
        elif d["kind"] == "net_change":
            db.execute("update net_change set status='approved', approval='founder' where id=?", (detail["change"],))
            effect = {"change": detail["change"], "status": "approved"}
        elif d["kind"] == "net_live_writes":
            db.set_setting("net_live_writes", bool(detail.get("enable", True)))
            effect = {"net_live_writes": bool(detail.get("enable", True))}
        elif d["kind"] == "net_device_mode":
            db.execute("update net_device set mode=? where hostname=? and mode!='sim'", (detail["mode"], detail["hostname"]))
            effect = {"hostname": detail["hostname"], "mode": detail["mode"]}
        elif d["kind"] == "hire_techs":
            db.execute("update sites set techs=techs+? where id=?", (int(detail.get("add", 1)), detail["site"]))
            effect = {"site": detail["site"], "techs": db.scalar("select techs from sites where id=?", (detail["site"],))}
        elif d["kind"] == "network_standard":
            for k in ("rs_filtering", "edge_filtering"):
                if k in detail:
                    db.set_setting(k, bool(detail[k]))
            effect = {k: db.setting(k) for k in ("rs_filtering", "edge_filtering")}
        elif d["kind"] == "cooling_upgrade":
            db.execute("update halls set crah_units=crah_units+? where id=?", (int(detail.get("units", 1)), detail["hall"]))
            effect = {"hall": detail["hall"], "added_units": int(detail.get("units", 1))}
    db.execute("update decisions set status=?, resolved_ts=?, note=? where id=?",
               ("approved" if approve else "rejected", time.time(),
                (note + (f" | applied: {json.dumps(effect, default=str)}" if effect else "")).strip(" |"), did))
    for hook in ON_DECIDE:
        try:
            hook({**d, "detail": detail}, approve, effect)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return {"decision": did, "status": "approved" if approve else "rejected", "effect": effect}


# ---------- schedules ----------

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_run(every: str, after: float) -> float:
    now = dt.datetime.fromtimestamp(after)
    parts = every.strip().lower().split()
    if parts == ["hourly"]:
        return (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).timestamp()
    if parts and parts[0] == "daily" and len(parts) == 2:
        h, m = map(int, parts[1].split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return (t if t > now else t + dt.timedelta(days=1)).timestamp()
    if parts and parts[0] == "weekly" and len(parts) == 3 and parts[1] in DAYS:
        h, m = map(int, parts[2].split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        t += dt.timedelta(days=(DAYS.index(parts[1]) - t.weekday()) % 7)
        return (t if t > now else t + dt.timedelta(days=7)).timestamp()
    raise ValueError("every must be 'hourly', 'daily HH:MM' or 'weekly mon HH:MM'")


def schedules():
    out = db.rows("select * from schedules order by id")
    for s in out:
        s["params"] = json.loads(s["params"] or "{}")
        s["next_run"] = next_run(s["every"], s["last_run"] or time.time()) if s["enabled"] else None
    return out


def schedule_add(name, every, params=None):
    if name not in PLAYBOOKS:
        raise ValueError(f"unknown playbook {name}")
    next_run(every, time.time())
    return db.execute("insert into schedules(playbook,params,every,enabled,last_run) values(?,?,?,1,?)",
                      (name, json.dumps(params or {}), every, time.time()))


def schedule_set(sid, enabled=None, delete=False):
    if delete:
        db.execute("delete from schedules where id=?", (sid,))
    elif enabled is not None:
        db.execute("update schedules set enabled=? where id=?", (1 if enabled else 0, sid))


def seed_schedules():
    if db.scalar("select count(*) from schedules"):
        return
    for name, every in (("exec_operating_cycle", "daily 02:10"),
                        ("exec_weekly_review", "weekly mon 06:40"),
                        ("rnd_research_cycle", "weekly sat 03:20"),
                        ("cdo_data_audit", "weekly sun 05:50"),
                        ("ceo_board_pack", "weekly mon 07:20")):
        schedule_add(name, every)


def seed_v5_schedules():
    from .. import graph
    from . import eng_playbooks
    eng_playbooks.init()
    graph.init()
    for fn in BOOT:
        fn()
    have = {s["playbook"] for s in db.rows("select playbook from schedules")}
    for name, every in (("cto_engineering_review", "weekly sun 05:20"), ("sre_slo_review", "weekly sun 05:35"),
                        ("aiops_predict", "daily 03:40"), ("crm_data_cloud", "weekly wed 04:10"),
                        ("net_inventory_audit", "weekly fri 04:25"), ("arch_review", "weekly sat 04:55"),
                        ("graph_sync", "daily 04:50")):
        if name not in have:
            schedule_add(name, every)


def seed_v4_schedules():
    have = {s["playbook"] for s in db.rows("select playbook from schedules")}
    if "cno_network_review" not in have:
        schedule_add("cno_network_review", "weekly sun 06:10")


def seed_v3_schedules():
    have = {s["playbook"] for s in db.rows("select playbook from schedules")}
    for name, every in (("cdo_data_audit", "weekly sun 05:50"), ("ceo_board_pack", "weekly mon 07:20")):
        if name not in have:
            schedule_add(name, every)


def tick():
    now = time.time()
    for s in schedules():
        if s["enabled"] and s["next_run"] and s["next_run"] <= now:
            db.execute("update schedules set last_run=? where id=?", (now, s["id"]))
            start(s["playbook"], s["params"], trigger=f"schedule {s['id']}")


def scheduler_loop(stop: threading.Event):
    while not stop.wait(30):
        try:
            tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


# ---------- overview ----------

def kpis():
    from .. import products
    hs = world.halls()
    cab_t = sum(h["cabinets_total"] for h in hs)
    cab_s = sum(h["cabinets_sold"] for h in hs)
    tele = db.rows("select t.* from telemetry t join (select hall, max(id) id from telemetry group by hall) l on l.id=t.id")
    it = sum(t["it_kw"] for t in tele)
    fac = sum(t["facility_kw"] for t in tele)
    fin = db.setting("fin_last") or {}
    week = time.time() - 7 * 86400
    out = {
        "metros": db.scalar("select count(*) from metros"),
        "sites": db.scalar("select count(*) from sites"),
        "halls": len(hs),
        "cabinet_util": round(cab_s / cab_t, 3) if cab_t else 0,
        "it_load_mw": round(sum(h["it_load_kw"] for h in hs) / 1000, 2),
        "fleet_pue": round(fac / it, 3) if it else None,
        "tenants": db.scalar("select count(*) from tenants"),
        "cross_connects": db.scalar("select count(*) from cross_connects"),
        "virtual_circuits": db.scalar("select count(*) from virtual_circuits"),
        "mrr_usd": world.revenue_breakdown()["total"],
        "ebitda_margin": fin.get("ebitda_margin"),
        "orders_pending": db.scalar("select count(*) from orders where status='pending'"),
        "orders_waitlisted": db.scalar("select count(*) from orders where status='waitlisted'"),
        "open_decisions": db.scalar("select count(*) from decisions where status='open'"),
        "runs_7d": db.scalar("select count(*) from runs where started>?", (week,)),
        "failed_7d": db.scalar("select count(*) from runs where started>? and status='failed'", (week,)),
        "sim_date": products.today(),
        "ai_it_mw": round(sum(h["it_load_kw"] for h in hs if h.get("cooling_type") == "liquid") / 1000, 2),
        "cloud_connections": db.scalar("select count(*) from cloud_connections"),
        "edge_devices": db.scalar("select count(*) from edge_devices"),
        "ix_members": db.scalar("select count(distinct tenant) from ix_ports"),
        "remote_hands_sla_30d": products.sla_window(30)["sla_pct"],
        "net_retention_90d": products.retention(90)["net_retention"],
        "active_customers": db.scalar("select count(*) from contracts where status='active'"),
        "data_quality_score": (db.setting("data_quality") or {}).get("score"),
        "network": _net_kpis(),
        "crm_open_pipeline_mrr": db.scalar("select round(coalesce(sum(MRR__c),0)) from crm_opportunity where IsClosed=0"
                                           " and Type!='Renewal'") if db.one("select name from sqlite_master where name='crm_opportunity'") else None,
        "network_standard": {"rs_filtering": bool(db.setting("rs_filtering", False)),
                             "edge_filtering": bool(db.setting("edge_filtering", False))},
    }
    for hook in KPI_HOOKS:
        try:
            out.update(hook() or {})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return out


def _net_kpis():
    if not db.one("select name from sqlite_master where name='net_device'"):
        return None
    k = db.setting("net_kpis") or {}
    return {"devices": db.scalar("select count(*) from net_device"),
            "devices_down": db.scalar("select count(*) from net_device where status in ('down','unreachable')"),
            "live_devices": db.scalar("select count(*) from net_device where mode!='sim'"),
            "availability_30d": k.get("availability_pct"), "mttr_30d": k.get("mttr_min"),
            "credits_30d": k.get("credits_usd"), "readiness": (db.setting("net_readiness") or {}).get("level")}


def overview():
    deps = []
    for d in DEPARTMENTS:
        last = db.one("select id, playbook, status, started, finished, report, summary from runs"
                      " where department=? order by id desc limit 1", (d["id"],))
        if last:
            last["summary"] = json.loads(last["summary"] or "null")
        deps.append({**d, "playbooks": [p.meta() for p in PLAYBOOKS.values() if p.department == d["id"]],
                     "runs_total": db.scalar("select count(*) from runs where department=?", (d["id"],)),
                     "last_run": last})
    sched = sorted([{"playbook": s["playbook"], "every": s["every"], "next_run": s["next_run"]}
                    for s in schedules() if s["enabled"]], key=lambda s: s["next_run"] or 0)
    return {"charter": charter_mod.load(), "org_chart": org_chart(), "departments": deps, "kpis": kpis(),
            "open_decisions": decisions("open")[:10], "next_scheduled": sched}


from . import playbooks  # noqa: E402,F401  (registers every playbook)
from . import net_playbooks  # noqa: E402,F401
from . import eng_playbooks  # noqa: E402,F401
from . import graph_playbooks  # noqa: E402,F401
from . import fin_playbooks  # noqa: E402,F401
from . import growth_playbooks  # noqa: E402,F401
from . import market_playbooks  # noqa: E402,F401
from . import trust_playbooks  # noqa: E402,F401
from . import evolve_playbooks  # noqa: E402,F401
from . import assign_playbooks  # noqa: E402,F401
