"""v0.2 product lines and the simulated calendar.

  * Cloud on-ramps and a cloud router (private connections to invented cloud providers)
  * Network Edge: virtual routers, firewalls and SD-WAN on per-metro compute pools
  * Internet exchange ports (exchange membership drives the BGP simulator)
  * AI colocation in liquid-cooled halls
  * Remote-hands tickets with SLAs and billing
  * Contracts with escalators, renewals, downsizing and churn

All providers, tenants and prices are invented."""
from __future__ import annotations

import datetime as dt
import heapq
import json
import math
import os
import random
import threading
import time

from . import db

# ---------------------------------------------------------------- calendar

START_DATE = "2026-10-16"


def rng(tag: str) -> random.Random:
    """Random stream for a simulation step. Normally fresh each run; when VIX_SIM_SEED is set (sandbox evaluations
    for self-improvement) it is reproducible from the tag, the simulated date and the seed."""
    seed = os.environ.get("VIX_SIM_SEED")
    if seed and os.environ.get("VIX_PLACEBO") == tag and today() <= os.environ.get("VIX_PLACEBO_UNTIL", ""):
        seed += "-placebo"   # a tiny perturbation: measures how far a run drifts on its own
    return random.Random(f"{tag}-{today()}-{seed}" if seed else f"{tag}-{today()}-{time.time()}")


def today() -> str:
    return db.setting("sim_date") or START_DATE


def today_d() -> dt.date:
    return dt.date.fromisoformat(today())


def advance(days: int = 1) -> str:
    d = (today_d() + dt.timedelta(days=days)).isoformat()
    db.set_setting("sim_date", d)
    return d


def add_months(d: dt.date, months: float) -> dt.date:
    return d + dt.timedelta(days=round(months * 30.44))


# ---------------------------------------------------------------- catalogue

CLOUDS = [("STRATUS", "Stratus Cloud", 900), ("CIRRUS", "Cirrus Compute", 600),
          ("CUMULUS", "Cumulus Platform", 400), ("ALTUS", "Altus SaaS", 80)]
ROUTER_PACKAGES = {"lab": 100, "standard": 1000, "advanced": 10_000, "premium": 100_000}
EDGE_SIZES = {"small": 2, "medium": 4, "large": 8, "xlarge": 16}
EDGE_ROLES = ("router", "firewall", "sdwan")
LIQUID_CAB_KW = 60.0     # design density of an AI hall
CDU_KW = 800.0           # heat each coolant distribution unit can move
AI_HALL_CABS = 64
TECH_COST_MONTH = 38_000.0   # one technician post staffed 24x7 (about 4.2 people)

PRICES = {
    "cloud_connection_per_mbps": 1.2,
    "cloud_router": {"lab": 150.0, "standard": 800.0, "advanced": 2200.0, "premium": 6000.0},
    "edge_per_vcpu": 95.0,
    "edge_licence": {"router": 250.0, "firewall": 600.0, "sdwan": 450.0},
    "ix_port": {1: 500.0, 10: 1200.0, 100: 3500.0, 400: 9000.0},
    "ai_kw_month": 190.0,
    "internet_per_mbps": 0.55,
    "internet_port_fee": 300.0,
    "remote_hands_30min": 120.0,
    "install_flat": 400.0,
}

TICKET_KINDS = {
    # kind: (weight, mean work minutes, SLA minutes, billing)
    "smart_hands": (0.35, 35, 240, "per30"),
    "cabling": (0.15, 90, 1440, "per30"),
    "install": (0.10, 180, 2880, "flat"),
    "shipment": (0.20, 15, 120, "free"),
    "access": (0.20, 10, 30, "free"),
}

# ---------------------------------------------------------------- upgrade / seed


def upgrade(seed: int = 42):
    """Bring a v0.1 world (or a fresh one) up to v0.2 without touching existing records."""
    if (db.setting("world_version") or 1) >= 2:
        return False
    from . import world
    rnd = random.Random(seed + 2)
    if not db.setting("sim_date"):
        db.set_setting("sim_date", START_DATE)
    metros = [m[0] for m in world.METROS]
    with db.conn() as c:
        for cid, name, routes in CLOUDS:
            c.execute("insert or ignore into clouds values(?,?,?)", (cid, name, routes))
            for m in rnd.sample(metros, rnd.randint(5, 8)):
                c.execute("insert or ignore into cloud_onramps(cloud,metro,capacity_mbps,used_mbps) values(?,?,?,0)",
                          (cid, m, rnd.choice([100, 200, 400]) * 1000))
        for m in metros:
            c.execute("insert or ignore into edge_pool(metro,vcpu_total,vcpu_used) values(?,?,0)",
                      (m, 4000 if m == "ASH" else 2000))
        c.execute("update halls set cooling_type='air' where cooling_type is null")
        c.execute("update sites set techs=3 where id in (select site from halls group by site having sum(cabinets_total)>800)")
    # exchange members: everyone who used to be on the route server
    present = db.rows("select distinct t.id, t.kind, s.metro from tenants t join ports p on p.tenant=t.id"
                      " join sites s on s.id=p.site")
    for r in present:
        if r["kind"] in ("carrier", "cloud", "content") or rnd.random() < 0.3:
            sp = rnd.choice([10, 100, 100, 400] if r["kind"] != "enterprise" else [1, 10])
            db.execute("insert into ix_ports(tenant,metro,speed_g,ts) values(?,?,?,?)", (r["id"], r["metro"], sp, db.now()))
    # AI halls
    gpu_buyers = [t["id"] for t in db.rows("select id from tenants where kind in ('cloud','content','enterprise')")]
    for site in ("ASH1", "SJC1", "FRA1", "SIN1"):
        if not db.one("select id from sites where id=?", (site,)):
            continue
        h = world.add_hall(site, AI_HALL_CABS, liquid=True)
        sold = 0
        target = int(AI_HALL_CABS * rnd.uniform(0.4, 0.6))
        while sold < target:
            n = min(target - sold, rnd.choice([4, 8, 8, 16]))
            kw = n * rnd.choice([40, 60, 80])
            fulfill_ai(rnd.choice(gpu_buyers), {"metro": site[:3], "cabinets": n, "kw_per_cabinet": kw / n}, hall=h["hall"])
            sold += n
    # cloud connections, routers, edge devices
    tenants = db.rows("select id, kind from tenants")
    for t in rnd.sample(tenants, 50):
        m = db.one("select s.metro from ports p join sites s on s.id=p.site where p.tenant=? limit 1", (t["id"],))
        if m:
            fulfill_router(t["id"], {"metro": m["metro"], "package": rnd.choice(["standard", "standard", "advanced"])})
    for _ in range(260):
        t = rnd.choice(tenants)
        on = db.one("select o.* from cloud_onramps o join sites s on s.metro=o.metro join ports p on p.site=s.id"
                    " where p.tenant=? order by random() limit 1", (t["id"],))
        if on:
            fulfill_cloud(t["id"], {"metro": on["metro"], "cloud": on["cloud"], "mbps": rnd.choice([50, 100, 500, 1000, 5000])})
    for _ in range(70):
        t = rnd.choice(tenants)
        fulfill_edge(t["id"], {"metro": rnd.choice(metros), "role": rnd.choice(EDGE_ROLES),
                               "size": rnd.choice(["small", "medium", "medium", "large"]), "ha": rnd.random() < 0.4})
    # contracts
    for t in tenants:
        _new_contract(t["id"], rnd, backdate=True)
    db.set_setting("world_version", 2)
    return True


def _new_contract(tid, rnd, backdate=False):
    term = rnd.choice([24, 36, 36, 36, 60])
    start = add_months(today_d(), -rnd.uniform(0, term)) if backdate else today_d()
    db.execute("insert or replace into contracts(tenant,start_date,end_date,term_months,escalator_pct,status)"
               " values(?,?,?,?,?,'active')",
               (tid, start.isoformat(), add_months(start, term).isoformat(), term, rnd.choice([3.0, 3.0, 4.0, 5.0])))


def new_contract(tid):
    _new_contract(tid, random.Random(tid))


# ---------------------------------------------------------------- fulfilment


def _port_in_metro(tid, metro):
    from . import world
    site = world._site_in_metro(metro, tid)
    if not site:
        return None
    pid, _ = world._ensure_port(tid, site)
    return pid


def fulfill_ai(tid, p, hall=None):
    from . import world
    m, n = p.get("metro"), int(p.get("cabinets", 4))
    kw = n * float(p.get("kw_per_cabinet", 60))
    cands = [h for h in world.halls(m) if h.get("cooling_type") == "liquid"
             and (hall is None or h["id"] == hall)
             and h["cabinets_total"] - h["cabinets_sold"] >= n
             and h["utility_kw"] * 0.92 - h["contracted_kw"] >= kw]
    if not cands:
        return "waitlisted", f"no liquid-cooled hall in {m} with {n} cabinets and {kw:.0f} kW free"
    h = min(cands, key=lambda h: h["power_util"])
    db.execute("insert into placements(tenant,hall,cabinets,kw,ts) values(?,?,?,?,?)", (tid, h["id"], n, kw, db.now()))
    db.execute("update halls set cabinets_sold=cabinets_sold+?, contracted_kw=contracted_kw+?, it_load_kw=it_load_kw+?"
               " where id=?", (n, kw, kw * 0.7, h["id"]))
    world._ensure_port(tid, h["site"], 100)
    return "done", f"{n} AI cabinets, {kw:.0f} kW liquid-cooled in {h['id']}"


def router_routes(router_id):
    r = db.one("select * from cloud_routers where id=?", (router_id,))
    used = db.scalar("select coalesce(sum(c.route_count),0) from cloud_connections x join clouds c on c.id=x.cloud"
                     " where x.router=?", (router_id,)) or 0
    own = db.scalar("select prefixes from tenants where id=?", (r["tenant"],)) or 0
    return used + own, ROUTER_PACKAGES[r["package"]]


def fulfill_cloud(tid, p):
    m, cloud, mbps = p.get("metro"), p.get("cloud"), int(p.get("mbps", 100))
    on = db.one("select * from cloud_onramps where cloud=? and metro=?", (cloud, m))
    if not on:
        return "rejected", f"{cloud} has no on-ramp in {m}"
    if on["used_mbps"] + mbps > on["capacity_mbps"]:
        return "waitlisted", f"{cloud} on-ramp in {m} is full ({on['used_mbps']}/{on['capacity_mbps']} Mbps)"
    router = db.one("select id from cloud_routers where tenant=? and metro=?", (tid, m))
    note = ""
    if router:
        used, limit = router_routes(router["id"])
        add = db.scalar("select route_count from clouds where id=?", (cloud,)) or 0
        already = db.one("select id from cloud_connections where router=? and cloud=?", (router["id"], cloud))
        if not already and used + add > limit:
            return "waitlisted", f"cloud router {router['id']} would carry {used + add} routes, package allows {limit}: upgrade it"
        note = f" via cloud router {router['id']}"
    if _port_in_metro(tid, m) is None:
        return "rejected", f"no site in {m}"
    db.execute("insert into cloud_connections(tenant,cloud,metro,mbps,router,ts) values(?,?,?,?,?,?)",
               (tid, cloud, m, mbps, router["id"] if router else None, db.now()))
    db.execute("update cloud_onramps set used_mbps=used_mbps+? where cloud=? and metro=?", (mbps, cloud, m))
    return "done", f"{mbps} Mbps to {cloud} in {m}{note}"


def fulfill_router(tid, p):
    m, pkg = p.get("metro"), p.get("package", "standard")
    if pkg not in ROUTER_PACKAGES:
        return "rejected", f"package must be one of {list(ROUTER_PACKAGES)}"
    ex = db.one("select * from cloud_routers where tenant=? and metro=?", (tid, m))
    if ex:
        if ROUTER_PACKAGES[pkg] <= ROUTER_PACKAGES[ex["package"]]:
            return "rejected", f"already has a {ex['package']} cloud router in {m}"
        db.execute("update cloud_routers set package=? where id=?", (pkg, ex["id"]))
        return "done", f"cloud router {ex['id']} upgraded {ex['package']} -> {pkg}"
    rid = db.execute("insert into cloud_routers(tenant,metro,package,ts) values(?,?,?,?)", (tid, m, pkg, db.now()))
    return "done", f"{pkg} cloud router {rid} in {m}"


def fulfill_edge(tid, p):
    m, role, size = p.get("metro"), p.get("role", "router"), p.get("size", "medium")
    ha = bool(p.get("ha", False))
    if role not in EDGE_ROLES or size not in EDGE_SIZES:
        return "rejected", f"role in {EDGE_ROLES}, size in {list(EDGE_SIZES)}"
    v = EDGE_SIZES[size] * (2 if ha else 1)
    pool = db.one("select * from edge_pool where metro=?", (m,))
    if not pool:
        return "rejected", f"no edge pool in {m}"
    if pool["vcpu_used"] + v > pool["vcpu_total"]:
        return "waitlisted", f"edge pool in {m} full ({pool['vcpu_used']}/{pool['vcpu_total']} vCPU)"
    db.execute("update edge_pool set vcpu_used=vcpu_used+? where metro=?", (v, m))
    did = db.execute("insert into edge_devices(tenant,metro,role,size,vcpu,ha,ts) values(?,?,?,?,?,?,?)",
                     (tid, m, role, size, EDGE_SIZES[size], int(ha), db.now()))
    return "done", f"{size} virtual {role}{' (HA pair)' if ha else ''} #{did} in {m}"


def fulfill_internet(tid, p):
    """Internet Access: committed bandwidth with burst, over the tenant's port, optional BGP."""
    m, commit = p.get("metro"), int(p.get("commit_mbps", 1000))
    if commit < 50:
        return "rejected", "commit at least 50 Mbps"
    if _port_in_metro(tid, m) is None:
        return "rejected", f"no site in {m}"
    n = (db.scalar("select count(*) from internet_access") or 0) + 1
    db.execute("insert into internet_access(tenant,metro,commit_mbps,burst_mbps,ipv4_block,bgp,ts) values(?,?,?,?,?,?,?)",
               (tid, m, commit, commit * 2, f"203.0.113.{(n * 8) % 256}/29", int(bool(p.get("bgp", commit >= 1000))), db.now()))
    return "done", f"{commit} Mbps Internet Access in {m} (burst {commit * 2}){' with BGP' if commit >= 1000 else ''}"


def fulfill_ix(tid, p):
    m, sp = p.get("metro"), int(p.get("speed_g", 10))
    if sp not in PRICES["ix_port"]:
        return "rejected", f"speed in {list(PRICES['ix_port'])}"
    if _port_in_metro(tid, m) is None:
        return "rejected", f"no site in {m}"
    db.execute("insert into ix_ports(tenant,metro,speed_g,ts) values(?,?,?,?)", (tid, m, sp, db.now()))
    return "done", f"{sp}G exchange port in {m}; joins the route server"


HANDLERS = {"ai_cabinets": fulfill_ai, "cloud_connection": fulfill_cloud, "cloud_router": fulfill_router,
            "edge_device": fulfill_edge, "ix_port": fulfill_ix, "internet_access": fulfill_internet}

ORDER_KINDS = {
    "ai_cabinets": "AI colocation in a liquid-cooled hall: {metro, cabinets, kw_per_cabinet}",
    "cloud_connection": "Private connection to a cloud on-ramp: {metro, cloud, mbps}",
    "cloud_router": "Cloud router: {metro, package: lab|standard|advanced|premium}",
    "edge_device": "Network Edge virtual device: {metro, role: router|firewall|sdwan, size, ha}",
    "ix_port": "Internet exchange port: {metro, speed_g}",
    "internet_access": "Internet Access: {metro, commit_mbps, bgp}",
}


# ---------------------------------------------------------------- revenue


_LB_CACHE = threading.local()


def lines_by_tenant() -> dict:
    """Monthly charges by tenant and product line. Cached per thread until this thread writes to the database
    or two seconds pass, so loops that ask for one tenant at a time do not recompute everyone."""
    c = db.conn()
    key = (c.total_changes, today())
    hit = getattr(_LB_CACHE, "v", None)
    if hit and hit[0] == key and time.monotonic() - hit[1] < 2.0:
        return hit[2]
    out = _lines_by_tenant()
    _LB_CACHE.v = (key, time.monotonic(), out)
    return out


def _lines_by_tenant() -> dict:
    from . import world
    P = world.PRICES
    out: dict[int, dict[str, float]] = {}

    def add(tid, k, v):
        if tid is None or not v:
            return
        d = out.setdefault(tid, {})
        d[k] = d.get(k, 0.0) + v

    for r in db.rows("select p.tenant, h.cooling_type ct, sum(p.cabinets) cab, sum(p.kw) kw from placements p"
                     " join halls h on h.id=p.hall group by p.tenant, h.cooling_type"):
        add(r["tenant"], "colocation_space", r["cab"] * P["cabinet_month"])
        if r["ct"] == "liquid":
            add(r["tenant"], "ai_colocation_power", r["kw"] * PRICES["ai_kw_month"])
        else:
            add(r["tenant"], "colocation_power", r["kw"] * P["kw_month"])
    for r in db.rows("select tenant, speed_g, count(*) n from ports group by tenant, speed_g"):
        add(r["tenant"], "ports", P["port_month"].get(r["speed_g"], 800) * r["n"])
    for r in db.rows("select p.tenant, count(*) n from cross_connects x join ports p on p.id=x.a_port group by p.tenant"):
        add(r["tenant"], "cross_connects", r["n"] * P["cross_connect_month"])
    for r in db.rows("select p.tenant, sum(v.mbps) mb from virtual_circuits v join ports p on p.id=v.a_port group by p.tenant"):
        add(r["tenant"], "fabric_virtual_circuits", r["mb"] * P["vc_per_mbps_month"])
    for r in db.rows("select tenant, plan, count(*) n from metal_servers group by tenant, plan"):
        add(r["tenant"], "metal", P["metal_month"][r["plan"]] * r["n"])
    for r in db.rows("select tenant, sum(mbps) mb from cloud_connections group by tenant"):
        add(r["tenant"], "cloud_connections", r["mb"] * PRICES["cloud_connection_per_mbps"])
    for r in db.rows("select tenant, package from cloud_routers"):
        add(r["tenant"], "cloud_routers", PRICES["cloud_router"][r["package"]])
    for r in db.rows("select tenant, role, vcpu, ha from edge_devices"):
        mult = 2 if r["ha"] else 1
        add(r["tenant"], "network_edge", mult * (r["vcpu"] * PRICES["edge_per_vcpu"] + PRICES["edge_licence"][r["role"]]))
    for r in db.rows("select tenant, speed_g, count(*) n from ix_ports group by tenant, speed_g"):
        add(r["tenant"], "internet_exchange", PRICES["ix_port"].get(r["speed_g"], 1200) * r["n"])
    try:
        for r in db.rows("select tenant, sum(commit_mbps) mb, count(*) n from internet_access group by tenant"):
            add(r["tenant"], "internet_access", r["mb"] * PRICES["internet_per_mbps"] + r["n"] * PRICES["internet_port_fee"])
    except Exception:  # noqa: BLE001  (pre-v0.5 databases)
        pass
    idx = {r["id"]: r["price_index"] or 1.0 for r in db.rows("select id, price_index from tenants")}
    for tid, d in out.items():
        for k in d:
            d[k] *= idx.get(tid, 1.0)
    since = (today_d() - dt.timedelta(days=30)).isoformat()
    for r in db.rows("select tenant, sum(billed_usd) b from tickets where sim_date>? group by tenant", (since,)):
        add(r["tenant"], "remote_hands", r["b"])
    return out


def revenue_breakdown() -> dict:
    from . import world
    lines: dict[str, float] = {}
    for d in lines_by_tenant().values():
        for k, v in d.items():
            lines[k] = lines.get(k, 0.0) + v
    # metal allocated at seed time has no tenant attached
    for r in db.rows("select plan, sum(allocated) n from metal_inventory group by plan"):
        owned = db.scalar("select count(*) from metal_servers where plan=?", (r["plan"],)) or 0
        if r["n"] > owned:
            lines["metal"] = lines.get("metal", 0.0) + (r["n"] - owned) * world.PRICES["metal_month"][r["plan"]]
    order = ["colocation_space", "colocation_power", "ai_colocation_power", "ports", "cross_connects",
             "fabric_virtual_circuits", "cloud_connections", "cloud_routers", "network_edge", "internet_exchange", "internet_access",
             "metal", "remote_hands"]
    out = {k: round(lines.get(k, 0.0), 2) for k in order}
    out["total"] = round(sum(out.values()), 2)
    return out


def tenant_mrr(tid) -> float:
    return sum(lines_by_tenant().get(tid, {}).values())


# ---------------------------------------------------------------- remote hands


def simulate_tickets(rnd: random.Random, day: str, load: float = 1.0) -> dict:
    """One day of the remote-hands desk at every site: Poisson ticket arrivals, a priority
    queue (urgent first, then earliest SLA deadline) served by the site's technicians."""
    kinds = list(TICKET_KINDS)
    weights = [TICKET_KINDS[k][0] for k in kinds]
    per_site = []
    for s in db.rows("select s.id, s.techs, coalesce(sum(h.cabinets_sold),0) sold from sites s"
                     " left join halls h on h.site=s.id group by s.id"):
        lam = (1 + 0.02 * s["sold"]) * load
        n = _poisson(rnd, lam)
        tenants = [r["tenant"] for r in db.rows("select distinct p.tenant from placements p join halls h on h.id=p.hall"
                                                 " where h.site=?", (s["id"],))]
        if not tenants or n == 0:
            per_site.append({"site": s["id"], "tickets": 0, "met": 0, "techs": s["techs"], "util": 0.0})
            continue
        tix = []
        for _ in range(n):
            k = rnd.choices(kinds, weights)[0]
            _, mean, sla, _ = TICKET_KINDS[k]
            urgent = k == "smart_hands" and rnd.random() < 0.2
            tix.append({"kind": k, "arr": rnd.uniform(0, 1440), "work": rnd.expovariate(1 / mean),
                        "sla": 60 if urgent else sla, "prio": "urgent" if urgent else "normal",
                        "tenant": rnd.choice(tenants)})
        tix.sort(key=lambda t: t["arr"])
        techs = [0.0] * max(1, s["techs"])
        heapq.heapify(techs)
        pending, i, busy = [], 0, 0.0
        while i < len(tix) or pending:
            free = heapq.heappop(techs)
            now = free if pending else max(free, tix[i]["arr"])
            while i < len(tix) and tix[i]["arr"] <= now:
                pending.append(tix[i])
                i += 1
            pending.sort(key=lambda t: (t["prio"] != "urgent", t["arr"] + t["sla"]))
            t = pending.pop(0)
            start = max(now, t["arr"])
            t["done"] = start + t["work"]
            busy += t["work"]
            heapq.heappush(techs, t["done"])
        met = 0
        batch = []
        for t in tix:
            ok = t["done"] - t["arr"] <= t["sla"]
            met += ok
            bill = TICKET_KINDS[t["kind"]][3]
            usd = (math.ceil(t["work"] / 30) * PRICES["remote_hands_30min"] if bill == "per30"
                   else PRICES["install_flat"] if bill == "flat" else 0.0)
            batch.append((t["tenant"], s["id"], t["kind"], t["prio"], day, round(t["arr"], 1), t["sla"],
                          round(t["work"], 1), round(t["done"], 1), int(ok), usd))
        with db.conn() as c:
            c.executemany("insert into tickets(tenant,site,kind,priority,sim_date,opened_min,sla_min,work_min,done_min,met,billed_usd)"
                          " values(?,?,?,?,?,?,?,?,?,?,?)", batch)
        per_site.append({"site": s["id"], "tickets": len(tix), "met": met, "techs": s["techs"],
                         "util": round(busy / (1440 * len(techs)), 3)})
    total = sum(p["tickets"] for p in per_site)
    met = sum(p["met"] for p in per_site)
    for p in per_site:
        p["sla_pct"] = round(100 * p["met"] / p["tickets"], 1) if p["tickets"] else 100.0
    return {"day": day, "tickets": total, "sla_pct": round(100 * met / total, 2) if total else 100.0, "sites": per_site}


def _poisson(rnd, lam):
    if lam > 30:
        return max(0, int(rnd.gauss(lam, lam ** 0.5)))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1


def sla_window(days: int = 30) -> dict:
    since = (today_d() - dt.timedelta(days=days)).isoformat()
    r = db.one("select count(*) n, coalesce(sum(met),0) met, coalesce(sum(billed_usd),0) usd from tickets where sim_date>?",
               (since,))
    return {"tickets": r["n"], "sla_pct": round(100 * r["met"] / r["n"], 2) if r["n"] else None, "billed_usd": round(r["usd"], 2)}


# ---------------------------------------------------------------- renewals & churn


RENEWAL_ADJUST = []   # fn(tid) -> (delta probability, {reason: value})
ON_CONTRACT_EVENT = []  # fn(tid, event, mrr_before, mrr_after)


def renewal_probability(tid, escalator):
    xc = db.scalar("select count(*) from cross_connects x join ports p on p.id=x.a_port where p.tenant=?", (tid,)) or 0
    since = (today_d() - dt.timedelta(days=90)).isoformat()
    misses = db.scalar("select count(*) from tickets where tenant=? and met=0 and sim_date>?", (tid, since)) or 0
    clouds = db.scalar("select count(*) from cloud_connections where tenant=?", (tid,)) or 0
    try:
        outages = db.scalar("select count(distinct i.incident) from net_impact i join net_incident n on n.id=i.incident"
                            " where i.tenant=? and n.sim_date>?", (tid, since)) or 0
    except Exception:  # noqa: BLE001  (pre-v0.4 databases)
        outages = 0
    p = 0.86 + 0.015 * min(6, xc / 10) + 0.01 * min(3, clouds) - 0.04 * min(5, misses) - 0.03 * min(4, outages) - 0.02 * max(0, escalator - 3)
    why = {"cross_connects": xc, "sla_misses_90d": misses, "cloud_connections": clouds, "network_outages_90d": outages}
    for hook in RENEWAL_ADJUST:
        try:
            dp, w = hook(tid)
            p += dp
            why.update(w)
        except Exception:  # noqa: BLE001
            pass
    return max(0.4, min(0.99, p)), why


def release_tenant(tid, fraction: float) -> dict:
    """Give back a fraction of a tenant's footprint (1.0 = full exit)."""
    freed = {"cabinets": 0, "kw": 0.0, "cross_connects": 0}
    for p in db.rows("select * from placements where tenant=?", (tid,)):
        n = p["cabinets"] if fraction >= 1 else p["cabinets"] // 2
        if n <= 0:
            continue
        kw = p["kw"] * n / p["cabinets"]
        h = db.one("select it_load_kw, contracted_kw from halls where id=?", (p["hall"],))
        it_drop = h["it_load_kw"] * kw / h["contracted_kw"] if h["contracted_kw"] else 0
        db.execute("update halls set cabinets_sold=max(0,cabinets_sold-?), contracted_kw=max(0,contracted_kw-?),"
                   " it_load_kw=max(0,it_load_kw-?) where id=?", (n, kw, it_drop, p["hall"]))
        if n == p["cabinets"]:
            db.execute("delete from placements where id=?", (p["id"],))
        else:
            db.execute("update placements set cabinets=cabinets-?, kw=kw-? where id=?", (n, kw, p["id"]))
        freed["cabinets"] += n
        freed["kw"] += kw
    xcs = db.rows("select x.id from cross_connects x join ports p on p.id=x.a_port or p.id=x.z_port where p.tenant=?", (tid,))
    drop = xcs if fraction >= 1 else xcs[: len(xcs) // 2]
    for x in drop:
        db.execute("delete from cross_connects where id=?", (x["id"],))
    freed["cross_connects"] = len(drop)
    if fraction >= 1:
        for c in db.rows("select * from cloud_connections where tenant=?", (tid,)):
            db.execute("update cloud_onramps set used_mbps=max(0,used_mbps-?) where cloud=? and metro=?",
                       (c["mbps"], c["cloud"], c["metro"]))
        for e in db.rows("select * from edge_devices where tenant=?", (tid,)):
            db.execute("update edge_pool set vcpu_used=max(0,vcpu_used-?) where metro=?",
                       (e["vcpu"] * (2 if e["ha"] else 1), e["metro"]))
        for m in db.rows("select metro, plan, count(*) n from metal_servers where tenant=? group by metro, plan", (tid,)):
            db.execute("update metal_inventory set allocated=max(0,allocated-?) where metro=? and plan=?",
                       (m["n"], m["metro"], m["plan"]))
        db.execute("delete from virtual_circuits where a_port in (select id from ports where tenant=?)"
                   " or z_port in (select id from ports where tenant=?)", (tid, tid))
        for t in ("cloud_connections", "cloud_routers", "edge_devices", "metal_servers", "ix_ports", "internet_access", "ports"):
            db.execute(f"delete from {t} where tenant=?", (tid,))
    return freed


def run_renewals(rnd: random.Random) -> list[dict]:
    """Every active contract whose end date has arrived renews, downsizes or churns."""
    events = []
    for c in db.rows("select c.*, t.name, t.kind from contracts c join tenants t on t.id=c.tenant"
                     " where c.status='active' and c.end_date<=?", (today(),)):
        tid = c["tenant"]
        before = tenant_mrr(tid)
        p, why = renewal_probability(tid, c["escalator_pct"])
        new_end = add_months(today_d(), c["term_months"]).isoformat()
        if rnd.random() < p:
            db.execute("update tenants set price_index=price_index*? where id=?", (1 + c["escalator_pct"] / 100, tid))
            from . import harness
            esc = float(harness.get("accounts.escalator_pct"))
            db.execute("update contracts set start_date=?, end_date=?, escalator_pct=? where tenant=?", (today(), new_end, esc, tid))
            ev, note = "renewed", f"+{c['escalator_pct']}% escalator; next term {esc}%"
        elif rnd.random() < 0.5:
            freed = release_tenant(tid, 0.5)
            db.execute("update contracts set start_date=?, end_date=? where tenant=?", (today(), new_end, tid))
            ev, note = "downsized", f"released {freed['cabinets']} cabinets, {freed['cross_connects']} cross-connects"
        else:
            freed = release_tenant(tid, 1.0)
            db.execute("update contracts set status='ended' where tenant=?", (tid,))
            ev, note = "churned", f"left: {freed['cabinets']} cabinets, {freed['cross_connects']} cross-connects freed"
        after = tenant_mrr(tid)
        db.execute("insert into contract_events(ts,sim_date,tenant,event,mrr_before,mrr_after,note) values(?,?,?,?,?,?,?)",
                   (db.now(), today(), tid, ev, round(before, 2), round(after, 2), note))
        for hook in ON_CONTRACT_EVENT:
            hook(tid, ev, before, after)
        events.append({"tenant": tid, "name": c["name"], "kind": c["kind"], "event": ev, "p_renew": round(p, 3),
                       "mrr_before": round(before), "mrr_after": round(after), "note": note, **why})
    return events


def retention(days: int = 90) -> dict:
    since = (today_d() - dt.timedelta(days=days)).isoformat()
    ev = db.rows("select event, count(*) n, sum(mrr_before) b, sum(mrr_after) a from contract_events"
                 " where sim_date>? group by event", (since,))
    by = {e["event"]: e for e in ev}
    b = sum(e["b"] or 0 for e in ev)
    a = sum(e["a"] or 0 for e in ev)
    return {"window_days": days, "renewed": by.get("renewed", {}).get("n", 0),
            "downsized": by.get("downsized", {}).get("n", 0), "churned": by.get("churned", {}).get("n", 0),
            "mrr_up_for_renewal": round(b), "mrr_retained": round(a),
            "net_retention": round(a / b, 3) if b else None}


def at_risk(days: int = 90, limit: int = 15) -> list[dict]:
    horizon = (today_d() + dt.timedelta(days=days)).isoformat()
    out = []
    for c in db.rows("select c.*, t.name, t.kind from contracts c join tenants t on t.id=c.tenant"
                     " where c.status='active' and c.end_date<=? order by c.end_date", (horizon,)):
        p, why = renewal_probability(c["tenant"], c["escalator_pct"])
        out.append({"tenant": c["tenant"], "name": c["name"], "end_date": c["end_date"], "p_renew": round(p, 3),
                    "mrr": round(tenant_mrr(c["tenant"])), **why})
    out.sort(key=lambda r: (r["p_renew"], -r["mrr"]))
    return out[:limit]


def catalogue_summary() -> dict:
    return {
        "sim_date": today(),
        "clouds": db.rows("select c.id, c.name, c.route_count, count(o.metro) onramps,"
                          " coalesce(sum(o.used_mbps),0) used_mbps, coalesce(sum(o.capacity_mbps),0) capacity_mbps"
                          " from clouds c left join cloud_onramps o on o.cloud=c.id group by c.id"),
        "cloud_connections": db.scalar("select count(*) from cloud_connections"),
        "cloud_routers": db.rows("select package, count(*) n from cloud_routers group by package"),
        "edge": db.rows("select p.metro, p.vcpu_total, p.vcpu_used, (select count(*) from edge_devices d where d.metro=p.metro) devices"
                        " from edge_pool p order by p.metro"),
        "ix": db.rows("select metro, count(distinct tenant) members, sum(speed_g) capacity_g from ix_ports group by metro"),
        "ai_halls": db.rows("select h.id, h.cabinets_total, h.cabinets_sold, h.contracted_kw, h.it_load_kw from halls h"
                            " where h.cooling_type='liquid' order by h.id"),
        "contracts": db.rows("select status, count(*) n from contracts group by status"),
        "remote_hands_30d": sla_window(30),
        "retention_90d": retention(90),
    }


def tenant_detail(tid) -> dict:
    return {
        "contract": db.one("select * from contracts where tenant=?", (tid,)),
        "cloud_connections": db.rows("select * from cloud_connections where tenant=?", (tid,)),
        "cloud_routers": db.rows("select * from cloud_routers where tenant=?", (tid,)),
        "edge_devices": db.rows("select * from edge_devices where tenant=?", (tid,)),
        "ix_ports": db.rows("select * from ix_ports where tenant=?", (tid,)),
        "revenue_lines": {k: round(v, 2) for k, v in lines_by_tenant().get(tid, {}).items()},
        "events": db.rows("select * from contract_events where tenant=? order by id desc limit 10", (tid,)),
    }


def dumps(o):
    return json.dumps(o, default=str)
