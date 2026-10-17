"""Development and power: the growth engine.

Projects move through stages with real lead times and costs: land (new sites), design, permits, utility power,
construction and commissioning. Spending is capitalised as construction in progress and moves to PP&E when a
hall goes live. Grid capacity is finite and queues; power purchase agreements fix the price of part of the
load and make it renewable. Hyperscale builds are joint ventures: vix funds its share and books its share of
the lease. Every project carries the business case it was approved on, and is checked against what it
actually earns once it is live. All numbers are simulated."""
from __future__ import annotations

import datetime as dt
import json
import math
import random

from . import db, fin, products, world

SCHEMA = """
create table if not exists dev_project (
  id integer primary key, name text, kind text, metro text, site text, cabinets integer, liquid integer, mw real,
  stage text, stage_started text, stage_due text, budget_usd real, spent_usd real default 0, status text, decision integer,
  created text, live_date text, hall text, expected_mrr real, irr real, partner text, share real, rent_kw real,
  history text default '[]', roi text default '[]'
);
create table if not exists dev_power (
  metro text primary key, grid_mw_available real, refill_mw_month real, onsite_mw real default 0, renewable_target real
);
create table if not exists dev_ppa (
  id integer primary key, metro text, mw real, kind text, price_mwh real, term_years integer, start text, end_date text,
  status text, decision integer
);
create table if not exists dev_capacity (
  sim_date text, metro text, cab_total integer, cab_sold integer, ai_total integer, ai_sold integer,
  primary key (sim_date, metro)
);
create table if not exists dev_rfp (
  id integer primary key, cloud text, metro text, mw real, rent_kw real, opened text, decide_by text, status text,
  decision integer, irr real, competitor text
);
"""

GRID_SEED = {  # MW of new utility capacity available now, MW released per month (Singapore and Frankfurt are tight)
    "SJC": (9, 0.6), "ASH": (45, 3.0), "NYC": (12, 0.8), "LHR": (8, 0.5), "FRA": (6, 0.3), "SIN": (2, 0.1),
    "NRT": (10, 0.6), "SYD": (16, 1.0)}
RENEWABLE_PATH = {2026: 0.45, 2027: 0.55, 2028: 0.65, 2029: 0.8, 2030: 1.0}
STAGES = {  # kind -> [(stage, min days, max days, share of budget)]
    "hall": [("design", 25, 40, .04), ("permits", 40, 80, .01), ("power", 15, 45, .07), ("construction", 150, 220, .83),
             ("commissioning", 20, 35, .05)],
    "ai_hall": [("design", 35, 50, .05), ("permits", 40, 80, .01), ("power", 30, 90, .09), ("construction", 120, 180, .79),
                ("commissioning", 25, 40, .06)],
    "new_site": [("land", 60, 120, .09), ("design", 50, 80, .04), ("permits", 90, 180, .01), ("power", 90, 240, .08),
                 ("construction", 270, 380, .73), ("commissioning", 40, 60, .05)],
    "hyperscale": [("land", 45, 90, .08), ("design", 40, 60, .04), ("permits", 60, 120, .01), ("power", 90, 210, .09),
                   ("construction", 240, 330, .73), ("commissioning", 30, 45, .05)]}
SITE_MAX_HALLS = 6
HALL_CABS = 260
NEW_SITE_SHELL = 20_000_000.0
HURDLE_IRR = 0.10
JV_SHARE = 0.20
HYPERSCALE_CAPEX_PER_MW = 11_000_000.0


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
    if not db.scalar("select count(*) from dev_power"):
        for m, (mw, refill) in GRID_SEED.items():
            db.execute("insert into dev_power(metro,grid_mw_available,refill_mw_month,renewable_target) values(?,?,?,?)",
                       (m, mw, refill, 1.0))


def _d(s):
    return dt.date.fromisoformat(s)


def _hist(p, event):
    h = json.loads(p["history"] or "[]")
    h.append({"date": products.today(), "event": event})
    db.execute("update dev_project set history=? where id=?", (json.dumps(h), p["id"]))


# ---------------------------------------------------------------- business case


def avg_cab_mrr(metro: str, liquid: bool) -> float:
    """Average monthly revenue per sold cabinet in this metro, from what customers actually pay."""
    lb = products.lines_by_tenant()
    rows = db.rows("select p.tenant, sum(p.cabinets) cab from placements p join halls h on h.id=p.hall join sites s on s.id=h.site"
                   " where s.metro=? and h.cooling_type=? group by p.tenant", (metro, "liquid" if liquid else "air"))
    rev = cab = 0.0
    for r in rows:
        ln = lb.get(r["tenant"], {})
        rev += ln.get("ai_colocation_power" if liquid else "colocation_power", 0) + ln.get("colocation_space", 0)
        cab += r["cab"]
    tot_cab = {r["tenant"]: r["c"] for r in db.rows("select tenant, sum(cabinets) c from placements group by tenant")}
    share = sum(r["cab"] / tot_cab[r["tenant"]] * (sum(v for k, v in lb.get(r["tenant"], {}).items()
                                                        if k not in ("colocation_space", "colocation_power", "ai_colocation_power")))
                for r in rows if tot_cab.get(r["tenant"]))
    if cab:
        return (rev + 0.5 * share) / cab
    return (world.PRICES["cabinet_month"] + world.PRICES["kw_month"] * (40 if liquid else 5)) * 1.3


def absorption(metro: str, liquid: bool = False, days: int = 60) -> float:
    """Net cabinets sold per month over the last `days`, from the daily capacity snapshots."""
    col = "ai_sold" if liquid else "cab_sold"
    rows = db.rows(f"select sim_date, {col} v from dev_capacity where metro=? order by sim_date desc limit ?", (metro, days))
    if len(rows) < 10:
        return 6.0 if not liquid else 1.0
    span = max(1, (_d(rows[0]["sim_date"]) - _d(rows[-1]["sim_date"])).days)
    return max(0.0, (rows[0]["v"] - rows[-1]["v"]) / span * 30.4)


def irr(cashflows: list[float]) -> float | None:
    """Annualised IRR of monthly cash flows by bisection."""
    def npv(r):
        return sum(cf / (1 + r) ** i for i, cf in enumerate(cashflows))
    lo, hi = -0.2, 0.2   # monthly rates
    try:
        if npv(lo) * npv(hi) > 0:
            return None
    except (ZeroDivisionError, OverflowError):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (1 + (lo + hi) / 2) ** 12 - 1


def business_case(kind, metro, cabinets, liquid, budget, months_to_live, fill_per_month=None) -> dict:
    """15-year monthly model: capex during the build, fill at the metro's absorption rate up to 85%, site-level
    margin after power and opex, terminal value at 12x the final year's EBITDA."""
    price = avg_cab_mrr(metro, liquid)
    fill = fill_per_month or max(3.0, absorption(metro, liquid) * 0.7)
    margin = 0.42 if not liquid else 0.36
    cfs, sold = [], 0.0
    build = max(1, months_to_live)
    for m in range(180):
        cf = -budget / build if m < build else 0.0
        if m >= build:
            sold = min(cabinets * 0.85, sold + fill)
            cf += sold * price * margin
        cfs.append(cf)
    cfs[-1] += cfs[-1] * 12 * 12
    r = irr(cfs)
    stab = cabinets * 0.85 * price
    return {"irr": round(r, 4) if r is not None else None, "price_per_cab": round(price), "fill_per_month": round(fill, 1),
            "stabilised_mrr": round(stab), "months_to_live": build, "months_to_85pct": round(build + cabinets * 0.85 / fill)}


def lead_days(kind) -> int:
    return sum((a + b) // 2 for _, a, b, _ in STAGES[kind])


# ---------------------------------------------------------------- projects


def budget_for(kind, cabinets, liquid, mw=0.0):
    if kind == "hyperscale":
        return mw * HYPERSCALE_CAPEX_PER_MW
    density = products.LIQUID_CAB_KW if liquid else world.KW_PER_CAB_DESIGN
    hall = cabinets * density * world.CAPEX_PER_KW * (1.25 if liquid else 1.0)
    return hall + (NEW_SITE_SHELL if kind == "new_site" else 0.0)


def start_project(kind, metro, site=None, cabinets=HALL_CABS, liquid=False, decision=None, mw=0.0, partner=None,
                  rent_kw=None, case=None, name=None) -> dict:
    init()
    rnd = random.Random(f"proj-{metro}-{products.today()}-{kind}-{site}")
    stage, a, b, _ = STAGES[kind][0]
    budget = budget_for(kind, cabinets, liquid, mw)
    if not mw:
        mw = round(cabinets * (products.LIQUID_CAB_KW if liquid else world.KW_PER_CAB_DESIGN) * 1.35 / 1000, 2)
    name = name or {"hall": f"{site} new hall", "ai_hall": f"{site} liquid-cooled AI hall", "new_site": f"{metro} new campus",
                    "hyperscale": f"{metro} {mw:.0f} MW hyperscale (JV with {partner})"}[kind]
    case = case or business_case(kind, metro, cabinets, liquid, budget, lead_days(kind) // 30)
    due = (products.today_d() + dt.timedelta(days=rnd.randint(a, b))).isoformat()
    pid = db.execute("insert into dev_project(name,kind,metro,site,cabinets,liquid,mw,stage,stage_started,stage_due,budget_usd,status,"
                     "decision,created,expected_mrr,irr,partner,share,rent_kw) values(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?)",
                     (name, kind, metro, site, cabinets, int(liquid), mw, stage, products.today(), due, round(budget), decision,
                      products.today(), case.get("stabilised_mrr"), case.get("irr"), partner,
                      JV_SHARE if kind == "hyperscale" else 1.0, rent_kw))
    p = db.one("select * from dev_project where id=?", (pid,))
    _hist(p, f"approved; {stage} until {due}; budget ${budget:,.0f}")
    return {"project": pid, "name": name, "stage": stage, "budget_usd": round(budget), "irr": case.get("irr"),
            "live_in_days": lead_days(kind)}


def _stage_info(kind, stage):
    for i, (s, a, b, share) in enumerate(STAGES[kind]):
        if s == stage:
            return i, a, b, share
    return None


def _spend(p, usd, memo):
    if usd <= 0 or not fin.opened():
        db.execute("update dev_project set spent_usd=spent_usd+? where id=?", (usd, p["id"]))
        return
    acct = "jv_investment" if p["kind"] == "hyperscale" else "cip"
    ours = usd * (p["share"] or 1.0)
    fin.post(products.today(), [(acct, ours), ("cash", -ours)], memo, f"proj:{p['id']}")
    db.execute("update dev_project set spent_usd=spent_usd+? where id=?", (usd, p["id"]))


def advance(rnd: random.Random) -> dict:
    """Mason's daily site meeting: spend, risks, stage gates, power queues, handovers."""
    init()
    today = products.today()
    out = {"spent": 0.0, "events": [], "live": []}
    for p in db.rows("select * from dev_project where status='active'"):
        i, a, b, share = _stage_info(p["kind"], p["stage"])
        span = max(1, (_d(p["stage_due"]) - _d(p["stage_started"])).days)
        daily = p["budget_usd"] * share / span
        if p["stage"] == "construction" and rnd.random() < 0.0025:
            pct = rnd.uniform(0.04, 0.15)
            db.execute("update dev_project set budget_usd=budget_usd*? where id=?", (1 + pct, p["id"]))
            ev = f"cost overrun +{pct:.0%} ({rnd.choice(['steel and copper prices', 'contractor claim', 'design change for higher density', 'ground conditions'])})"
            _hist(p, ev)
            out["events"].append({"project": p["name"], "event": ev})
        if p["stage"] == "power" and _d(today) >= _d(p["stage_due"]):
            grid = db.one("select * from dev_power where metro=?", (p["metro"],))
            avail = grid["grid_mw_available"] + grid["onsite_mw"]
            if avail < p["mw"]:
                last = (json.loads(p["history"] or "[]") or [{}])[-1].get("event", "")
                if not last.startswith("waiting for utility power"):
                    _hist(p, f"waiting for utility power: needs {p['mw']} MW, {avail:.1f} MW available in {p['metro']}")
                continue
            take_grid = min(grid["grid_mw_available"], p["mw"])
            db.execute("update dev_power set grid_mw_available=grid_mw_available-?, onsite_mw=max(0,onsite_mw-?) where metro=?",
                       (take_grid, p["mw"] - take_grid, p["metro"]))
        _spend(p, daily, f"{p['name']}: {p['stage']}")
        out["spent"] += daily * (p["share"] or 1.0)
        if _d(today) < _d(p["stage_due"]):
            continue
        if p["stage"] in ("permits", "land") and rnd.random() < 0.22 and "delayed" not in (p["history"] or "")[-200:]:
            extra = rnd.randint(25, 90)
            due = (_d(today) + dt.timedelta(days=extra)).isoformat()
            db.execute("update dev_project set stage_due=? where id=?", (due, p["id"]))
            ev = f"{p['stage']} delayed {extra} days ({'planning objection' if p['stage'] == 'permits' else 'title and zoning'})"
            _hist(p, ev + " delayed")
            out["events"].append({"project": p["name"], "event": ev})
            continue
        if p["stage"] == "construction" and rnd.random() < 0.15 and "supply" not in (p["history"] or "")[-300:]:
            extra = rnd.randint(20, 60)
            db.execute("update dev_project set stage_due=? where id=?", ((_d(today) + dt.timedelta(days=extra)).isoformat(), p["id"]))
            ev = f"construction slipped {extra} days (supply: {rnd.choice(['switchgear', 'generators', 'chillers', 'transformers'])} lead time)"
            _hist(p, ev)
            out["events"].append({"project": p["name"], "event": ev})
            continue
        stages = STAGES[p["kind"]]
        if i + 1 < len(stages):
            ns, na, nb, _ = stages[i + 1]
            due = (_d(today) + dt.timedelta(days=rnd.randint(na, nb))).isoformat()
            db.execute("update dev_project set stage=?, stage_started=?, stage_due=? where id=?", (ns, today, due, p["id"]))
            _hist(p, f"{p['stage']} complete; {ns} until {due}")
        else:
            out["live"].append(go_live(p))
    # utility releases more capacity over time
    for g in db.rows("select * from dev_power"):
        db.execute("update dev_power set grid_mw_available=grid_mw_available+? where metro=?", (g["refill_mw_month"] / 30.4, g["metro"]))
    out["spent"] = round(out["spent"])
    return out


def go_live(p) -> dict:
    today = products.today()
    db.execute("update dev_project set status='live', stage='live', live_date=? where id=?", (today, p["id"]))
    spent = db.scalar("select spent_usd from dev_project where id=?", (p["id"],))
    if p["kind"] == "hyperscale":
        _hist(p, f"handed over to {p['partner']}; vix owns {p['share']:.0%}")
        return {"project": p["name"], "kind": p["kind"]}
    site = p["site"]
    if p["kind"] == "new_site":
        n = (db.scalar("select count(*) from sites where metro=?", (p["metro"],)) or 0) + 1
        site = f"{p['metro']}{n}"
        while db.one("select id from sites where id=?", (site,)):
            n += 1
            site = f"{p['metro']}{n}"
        db.execute("insert into sites(id,metro,name,opened,tier,techs) values(?,?,?,?,?,2)",
                   (site, p["metro"], f"{p['metro']}-{n} campus", products.today_d().year, "Tier III"))
        build_site_network(site, p["metro"])
    h = world.add_hall(site, p["cabinets"], bool(p["liquid"]))
    db.execute("update halls set build_capex_usd=? where id=?", (spent, h["hall"]))
    db.execute("update dev_project set hall=?, site=? where id=?", (h["hall"], site, p["id"]))
    if fin.opened():
        cip = db.scalar("select coalesce(sum(amount),0) from fin_gl where account='cip' and ref=?", (f"proj:{p['id']}",)) or 0
        if cip:
            fin.post(today, [("ppe", cip), ("cip", -cip)], f"{p['name']} in service", f"proj:{p['id']}")
    _hist(p, f"live: {h['hall']} with {p['cabinets']} cabinets")
    return {"project": p["name"], "hall": h["hall"], "site": site}


def build_site_network(site, metro):
    """Core, spine and firewall pairs come up with the building; the leaf pair is staged for acceptance tests."""
    from . import netinfra
    ver = {}
    for role in ("core", "spine", "firewall", "leaf"):
        vendor = netinfra.MODELS[role][0]
        ver[role] = db.scalar("select version from net_firmware where vendor=? and released<=? order by bug_factor, released desc limit 1",
                              (vendor, products.today())) or "1.0"
    p = site.lower()
    for role, names in (("core", ["core1", "core2"]), ("spine", ["spine1", "spine2"]), ("firewall", ["firewall1", "firewall2"]),
                        ("leaf", ["leafa", "leafb"])):
        vendor, model, nports, speed = netinfra.MODELS[role]
        for k, nm in enumerate(names):
            h = f"{p}-{nm}" if role != "leaf" else f"{p}-h1-{nm}"
            peer = f"{p}-{names[1 - k]}" if role != "leaf" else f"{p}-h1-{names[1 - k]}"
            if db.one("select hostname from net_device where hostname=?", (h,)):
                continue
            db.execute("insert into net_device values(" + ",".join("?" * 20) + ")",
                       (h, site, metro, role, vendor, model, "sim-" + vendor, "sim",
                        f"198.19.{random.randint(0, 250)}.{random.randint(1, 250)}", None, ver[role],
                        f"{vendor[:2].upper()}{random.randint(10**8, 10**9 - 1)}", "staged" if role == "leaf" else "up", None, None,
                        products.today(), peer, nports, netinfra._config(h, role, ver[role]), db.now()))
            ifs = []
            for i in range(1, nports + 1):
                if role == "leaf":
                    kind, sp = ("customer", 100) if i <= netinfra.LEAF_CUSTOMER_PORTS else ("uplink", 400)
                    name = f"Ethernet{i}"
                elif role == "core":
                    kind, sp, name = ("uplink" if i <= 4 else "backbone" if i <= 12 else "spare"), 400, f"et-0/0/{i - 1}"
                elif role == "spine":
                    kind, sp, name = ("uplink" if i <= 2 else "fabric"), 400, f"Ethernet{i}"
                else:
                    kind, sp, name = "inside" if i <= 4 else "outside", 100, f"port{i}"
                ifs.append((h, name, kind, sp, 1, 1, -2.5, 0, "", None))
            with db.conn() as c:
                c.executemany("insert or ignore into net_interface values(?,?,?,?,?,?,?,?,?,?)", ifs)


def capacity_snapshot():
    init()
    today = products.today()
    for m in db.rows("select s.metro, sum(case when h.cooling_type!='liquid' then h.cabinets_total else 0 end) ct,"
                     " sum(case when h.cooling_type!='liquid' then h.cabinets_sold else 0 end) cs,"
                     " sum(case when h.cooling_type='liquid' then h.cabinets_total else 0 end) at,"
                     " sum(case when h.cooling_type='liquid' then h.cabinets_sold else 0 end) asold"
                     " from halls h join sites s on s.id=h.site group by s.metro"):
        db.execute("insert or replace into dev_capacity values(?,?,?,?,?,?)", (today, m["metro"], m["ct"], m["cs"], m["at"], m["asold"]))


def outlook() -> list[dict]:
    """Per metro: free cabinets, absorption, months to sell out, capacity already in the pipeline, lead time."""
    init()
    out = []
    for m in db.rows("select code from metros"):
        code = m["code"]
        for liquid in (False, True):
            hs = [h for h in world.halls(code) if (h.get("cooling_type") == "liquid") == liquid]
            if liquid and not hs:
                continue
            total = sum(h["cabinets_total"] for h in hs)
            sold = sum(h["cabinets_sold"] for h in hs)
            wait = 0
            for w in db.rows("select params from orders where status='waitlisted' and kind=?", ("ai_cabinets" if liquid else "cabinets",)):
                pm = json.loads(w["params"])
                if pm.get("metro") == code:
                    wait += int(pm.get("cabinets", 0))
            pipe = db.scalar("select coalesce(sum(cabinets),0) from dev_project where status='active' and metro=? and liquid=? and kind!='hyperscale'",
                             (code, int(liquid))) or 0
            ab = absorption(code, liquid)
            free = total * 0.92 - sold - wait
            months = free / ab if ab > 0 else 99
            lead = lead_days("ai_hall" if liquid else "hall") / 30.4
            out.append({"metro": code, "type": "AI (liquid)" if liquid else "air", "cabinets": total, "sold": sold, "waitlisted": wait,
                        "free": round(free), "absorption_per_month": round(ab, 1), "months_to_full": round(months, 1),
                        "pipeline_cabinets": pipe, "lead_months": round(lead, 1),
                        "needs_capacity": months < lead + 2 and pipe == 0})
    return out


def site_for_new_hall(metro) -> str | None:
    for s in db.rows("select s.id, (select count(*) from halls h where h.site=s.id) n,"
                     " (select count(*) from dev_project p where p.site=s.id and p.status='active') q"
                     " from sites s where s.metro=? order by n", (metro,)):
        if s["n"] + s["q"] < SITE_MAX_HALLS:
            return s["id"]
    return None


def roi_checks() -> list[dict]:
    """6 and 12 months after go-live: what the hall earns vs the business case at the same age."""
    init()
    out = []
    lb = None
    for p in db.rows("select * from dev_project where status='live' and kind!='hyperscale' and hall is not null"):
        months = (products.today_d() - _d(p["live_date"])).days / 30.4
        done = {r["months"] for r in json.loads(p["roi"] or "[]")}
        for mark in (6, 12, 24):
            if months >= mark and mark not in done:
                lb = lb or products.lines_by_tenant()
                h = db.one("select * from halls where id=?", (p["hall"],))
                price = avg_cab_mrr(p["metro"], bool(p["liquid"]))
                actual = h["cabinets_sold"] * price
                case_fill = min(p["cabinets"] * 0.85, max(3.0, absorption(p["metro"], bool(p["liquid"])) * 0.7) * mark)
                plan = case_fill * price
                roi = json.loads(p["roi"] or "[]")
                rec = {"months": mark, "date": products.today(), "plan_mrr": round(plan), "actual_mrr": round(actual),
                       "ratio": round(actual / plan, 2) if plan else None, "cabinets_sold": h["cabinets_sold"]}
                roi.append(rec)
                db.execute("update dev_project set roi=? where id=?", (json.dumps(roi), p["id"]))
                out.append({"project": p["name"], **rec})
    return out


# ---------------------------------------------------------------- power: grid price, PPAs, renewables


def power_step(rnd: random.Random):
    idx = db.setting("power_index") or {}
    for m in db.rows("select code from metros"):
        v = idx.get(m["code"], 1.0)
        v *= 1 + 0.02 * (1.0 - v) + rnd.gauss(0.0002, 0.006)
        idx[m["code"]] = round(min(1.8, max(0.7, v)), 4)
    db.set_setting("power_index", idx)


CF = {"solar": 0.24, "wind": 0.36, "hydro": 0.55}


def ppa_kwh_month(metro) -> tuple[float, float]:
    """Contracted renewable energy per month in a metro and its weighted price per kWh."""
    kwh = cost = 0.0
    for p in db.rows("select * from dev_ppa where metro=? and status='active' and start<=? and end_date>?",
                     (metro, products.today(), products.today())):
        k = p["mw"] * 1000 * 730 * CF.get(p["kind"], 0.3)
        kwh += k
        cost += k * p["price_mwh"] / 1000
    return kwh, (cost / kwh if kwh else 0.0)


def power_cost(metro, kwh, grid_price) -> float:
    """Fin hook: PPA energy at the contract price (up to the load), the rest at the grid price."""
    try:
        pk, pp = ppa_kwh_month(metro)
    except Exception:  # noqa: BLE001
        return kwh * grid_price
    used = min(kwh, pk)
    return used * pp + (kwh - used) * grid_price


def renewables() -> dict:
    rr = fin.run_rate()
    rows, tot, ren = [], 0.0, 0.0
    metros = {m["code"]: m for m in db.rows("select * from metros")}
    for m, kwh in rr["kwh_by_metro"].items():
        pk, pp = ppa_kwh_month(m)
        ppa = min(kwh, pk)
        grid_ren = (kwh - ppa) * metros[m]["renewable_pct"] / 100
        tot += kwh
        ren += ppa + grid_ren
        rows.append({"metro": m, "mwh_month": round(kwh / 1000), "ppa_mwh": round(ppa / 1000),
                     "renewable_pct": round(100 * (ppa + grid_ren) / kwh, 1) if kwh else None,
                     "grid_price": round(fin.metro_power_price(m), 4), "ppa_price": round(pp, 4) if pp else None})
    target = RENEWABLE_PATH.get(products.today_d().year, 1.0)
    return {"metros": rows, "renewable_pct": round(100 * ren / tot, 1) if tot else None, "target_pct": round(100 * target),
            "mwh_month": round(tot / 1000)}


def committed_capex() -> list[dict]:
    """FP&A hook: remaining spend on active projects by month (vix's share)."""
    init()
    by = {}
    for p in db.rows("select * from dev_project where status='active'"):
        remaining = max(0.0, p["budget_usd"] - (p["spent_usd"] or 0)) * (p["share"] or 1.0)
        days = max(30, lead_days(p["kind"]) - (products.today_d() - _d(p["created"])).days)
        per_day = remaining / days
        d = products.today_d()
        for k in range(0, days, 30):
            per = (d + dt.timedelta(days=k + 15)).isoformat()[:7]
            by[per] = by.get(per, 0.0) + per_day * min(30, days - k)
    return [{"period": k, "usd": v} for k, v in sorted(by.items())]


def jv_income_month() -> float:
    total = 0.0
    for p in db.rows("select * from dev_project where kind='hyperscale' and status='live'"):
        total += p["mw"] * 1000 * (p["rent_kw"] or 150) * (p["share"] or JV_SHARE) * 0.62   # share of JV EBITDA
    return total


def summary() -> dict:
    init()
    act = db.rows("select * from dev_project where status='active' order by stage_due")
    return {"active": len(act), "mw_under_construction": round(sum(p["mw"] for p in act if p["kind"] != "hyperscale"), 1),
            "budget_active": round(sum(p["budget_usd"] * (p["share"] or 1) for p in act)),
            "spent_active": round(sum((p["spent_usd"] or 0) * (p["share"] or 1) for p in act)),
            "next_delivery": min((p["stage_due"] for p in act if p["stage"] == "commissioning"), default=None),
            "live": db.scalar("select count(*) from dev_project where status='live'"),
            "renewable_pct": None}


def kpi_hook() -> dict:
    if not db.one("select name from sqlite_master where name='dev_project'"):
        return {}
    s = summary()
    return {"development": {"projects_active": s["active"], "mw_under_construction": s["mw_under_construction"],
                            "capex_remaining": s["budget_active"] - s["spent_active"], "projects_live": s["live"]}}
