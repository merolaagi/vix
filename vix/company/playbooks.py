"""Department playbooks. Each takes a run context and returns (summary, markdown).
Agents here are rule-based: they read the simulated world, act on it, and write
up what they did. Reasoning beyond the rules happens when Claude reads the
reports through MCP and the founder approves decisions."""
from __future__ import annotations

import datetime as dt
import json
import random
import time

from .. import bi, crm, db, netsim, products, routerlab, thermal, world
from . import charter as charter_mod
from . import engine as _engine
from .engine import decision_add, decisions, kpis, playbook


def table(rows, cols, headers=None):
    if not rows:
        return "_none_\n"
    headers = headers or cols
    out = "| " + " | ".join(headers) + " |\n|" + "---|" * len(cols) + "\n"
    for r in rows:
        out += "| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |\n"
    return out


def _fmt(v):
    if isinstance(v, float):
        return f"{v:,.3f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.1f}"
    if isinstance(v, int):
        return f"{v:,}"
    return "" if v is None else str(v)


def usd(v):
    return f"${v:,.0f}"


# ---------------------------------------------------------------- Sales

@playbook("sales_demand_cycle", "Demand & pipeline", "sales",
          "Work the CRM pipeline: opportunities reaching their close date are won (placing the order) or lost; "
          "then open a day's worth of new opportunities from existing customers and new logos.",
          {"days": {"type": "int", "default": 1}, "orders_per_day": {"type": "int", "default": 5},
           "new_logo_rate": {"type": "float", "default": 0.06}})
def sales_demand_cycle(ctx):
    rnd = random.Random(int(time.time()))
    days = ctx.p("days", 1, int)
    rate = ctx.p("orders_per_day", 5, int)
    new_rate = ctx.p("new_logo_rate", 0.06, float)
    metros = [m[0] for m in world.METROS]
    sites = [s["id"] for s in db.rows("select id from sites")]
    ai_metros = sorted({h["metro"] for h in world.halls() if h.get("cooling_type") == "liquid"}) or metros
    kinds = {"cabinets": .14, "ai_cabinets": .025, "port": .10, "cross_connect": .26, "vc": .12,
             "cloud_connection": .12, "cloud_router": .03, "edge_device": .06, "ix_port": .04, "metal": .07,
             "internet_access": .05}
    placed, new_tenants = [], []
    pipe = {"won": 0, "lost": 0, "advanced": 0}
    for _ in range(days):
        step = crm.advance_pipeline(rnd)
        for k in pipe:
            pipe[k] += step[k]
        rate_opps = rate / crm.WIN_RATE
        n = max(0, int(rnd.gauss(rate_opps, rate_opps ** 0.5)))
        for _ in range(n):
            if rnd.random() < new_rate:
                tid = world.new_tenant(rnd)
                new_tenants.append(tid)
            else:
                tid = db.scalar("select t.id from tenants t join contracts c on c.tenant=t.id"
                                " where c.status='active' order by random() limit 1")
            t = db.one("select * from tenants where id=?", (tid,))
            w = dict(kinds)
            for hook in _engine.DEMAND_HOOKS:
                if w is None:
                    break
                w = hook(t, w)
            if not w:
                continue
            kind = rnd.choices(list(w), list(w.values()))[0]
            home = t["home_metro"] if rnd.random() < 0.6 else rnd.choice(metros)
            if kind == "cabinets":
                params = {"metro": home, "cabinets": rnd.choice([1, 2, 2, 4, 6, 10, 20]),
                          "kw_per_cabinet": rnd.choice([4, 5, 6, 8, 12])}
            elif kind == "port":
                params = {"site": rnd.choice([s for s in sites if s.startswith(home)] or sites),
                          "speed_g": rnd.choice([10, 10, 100, 400] if t["kind"] != "enterprise" else [1, 10])}
            elif kind == "cross_connect":
                ps = db.rows("select site from ports where tenant=?", (tid,))
                params = {"site": rnd.choice(ps)["site"] if ps else rnd.choice(sites)}
            elif kind == "vc":
                z = rnd.choice([m for m in metros if m != home])
                params = {"a_metro": home, "z_metro": z, "mbps": rnd.choice([50, 100, 200, 500, 1000, 5000])}
            elif kind == "ai_cabinets":
                params = {"metro": home if home in ai_metros else rnd.choice(ai_metros),
                          "cabinets": rnd.choice([1, 2, 4, 4, 8]), "kw_per_cabinet": rnd.choice([30, 40, 60, 80])}
            elif kind == "cloud_connection":
                on = (db.one("select * from cloud_onramps where metro=? order by random() limit 1", (home,))
                      or db.one("select * from cloud_onramps order by random() limit 1"))
                params = {"metro": on["metro"], "cloud": on["cloud"], "mbps": rnd.choice([50, 100, 200, 500, 1000, 5000, 10000])}
            elif kind == "cloud_router":
                params = {"metro": home, "package": rnd.choices(list(products.ROUTER_PACKAGES), [0.2, 0.5, 0.25, 0.05])[0]}
            elif kind == "edge_device":
                params = {"metro": home, "role": rnd.choice(products.EDGE_ROLES),
                          "size": rnd.choice(["small", "medium", "medium", "large", "xlarge"]), "ha": rnd.random() < 0.4}
            elif kind == "internet_access":
                params = {"metro": home, "commit_mbps": rnd.choice([100, 200, 500, 1000, 2000, 10000])}
            elif kind == "ix_port":
                params = {"metro": home, "speed_g": rnd.choice([10, 100, 100, 400] if t["kind"] != "enterprise" else [1, 10])}
            else:
                params = {"metro": home, "plan": rnd.choices(list(world.METAL_PLANS), [0.55, 0.35, 0.1])[0],
                          "count": rnd.choice([1, 2, 4, 8])}
            oid = crm.open_opportunity(tid, kind, params, rnd.randint(2, 21),
                                       "New Business" if tid in new_tenants else "Expansion")
            placed.append({"opportunity": oid, "tenant": t["name"], "kind": kind, "params": json.dumps(params)})
    ctx.log(f"pipeline: {pipe['won']} won, {pipe['lost']} lost; opened {len(placed)} opportunities, "
            f"{len(new_tenants)} new logos")
    by_kind = {}
    for p in placed:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    md = (f"Pipeline: **{pipe['won']} won** (orders placed), {pipe['lost']} lost, {pipe['advanced']} moved stage. "
          f"Opened **{len(placed)} opportunities** over {days} day(s); **{len(new_tenants)} new logos**. "
          f"Win rate {crm.WIN_RATE:.0%}, sales cycle 2-21 days.\n\n"
          f"## New opportunities by product\n\n" + table([{"kind": k, "opportunities": v} for k, v in sorted(by_kind.items())],
                                                          ["kind", "opportunities"]) +
          "\n## Opened today\n\n" + table(placed, ["opportunity", "tenant", "kind", "params"]))
    return {"orders": pipe["won"], "lost": pipe["lost"], "opportunities": len(placed), "new_tenants": len(new_tenants),
            "by_kind": by_kind}, md


def _fulfil(ctx, kinds, label):
    res = world.process_orders(kinds)
    changed = [r for r in res if r["changed"]]
    stats = {}
    for r in res:
        stats[r["status"]] = stats.get(r["status"], 0) + 1
    ctx.log(f"{label}: {stats}")
    rows = [{"order": r["id"], "tenant": r["tenant"], "kind": r["kind"], "status": r["status"], "note": r["note"]}
            for r in changed]
    still = [r for r in res if not r["changed"]]
    md = (f"Processed **{len(res)}** {label} orders: " + ", ".join(f"{v} {k}" for k, v in stats.items()) + ".\n\n"
          "## Changed this run\n\n" + table(rows, ["order", "tenant", "kind", "status", "note"]) +
          (f"\n## Still waitlisted ({len(still)})\n\n" + table(
              [{"order": r["id"], "kind": r["kind"], "note": r["note"]} for r in still[:30]], ["order", "kind", "note"])
           if still else ""))
    return {"processed": len(res), **stats}, md


@playbook("ix_fulfill_orders", "Provision interconnection", "ix",
          "Provision pending ports, cross-connects, fabric virtual circuits, cloud connections and "
          "exchange ports; retry the waitlist.")
def ix_fulfill_orders(ctx):
    return _fulfil(ctx, ["port", "cross_connect", "vc", "cloud_connection", "ix_port", "internet_access"], "interconnection")


@playbook("digital_fulfill_orders", "Provision digital services", "digital",
          "Provision cloud routers and Network Edge virtual devices; report pool and router headroom.")
def digital_fulfill_orders(ctx):
    s, md = _fulfil(ctx, ["cloud_router", "edge_device"], "digital-services")
    pools = db.rows("select metro, vcpu_total, vcpu_used, round(1.0*vcpu_used/vcpu_total,3) util from edge_pool order by metro")
    routers = []
    for r in db.rows("select id, tenant, metro, package from cloud_routers"):
        used, limit = products.router_routes(r["id"])
        if used > 0.8 * limit:
            routers.append({**r, "routes": used, "limit": limit})
    return s, md + "\n## Edge compute pools\n\n" + table(pools, ["metro", "vcpu_total", "vcpu_used", "util"]) + \
        "\n## Cloud routers above 80% of route limit\n\n" + table(routers, ["id", "tenant", "metro", "package", "routes", "limit"])


@playbook("ops_fulfill_space", "Place colocation", "ops",
          "Place pending cabinet orders (air halls) and AI cabinet orders (liquid-cooled halls) in the "
          "least power-loaded hall that fits, under the power cap.")
def ops_fulfill_space(ctx):
    return _fulfil(ctx, ["cabinets", "ai_cabinets"], "colocation")


@playbook("metal_fulfill_orders", "Provision bare metal", "metal",
          "Allocate pending bare-metal orders from each metro's inventory.")
def metal_fulfill_orders(ctx):
    s, md = _fulfil(ctx, ["metal"], "bare-metal")
    inv = db.rows("select metro, plan, total, allocated, total-allocated free from metal_inventory order by metro, plan")
    return s, md + "\n## Inventory\n\n" + table(inv, ["metro", "plan", "total", "allocated", "free"])


# ---------------------------------------------------------------- Customer ops & accounts

@playbook("cx_ticket_desk", "Remote-hands desk", "cx",
          "Run one simulated day of remote-hands tickets at every site (smart hands, cabling, installs, "
          "shipments, access) against SLAs; bill the work; ask for technicians where SLAs slip.",
          {"load": {"type": "float", "default": 1.0}})
def cx_ticket_desk(ctx):
    day = products.today()
    rnd = random.Random(f"cx-{day}-{time.time()}")
    r = products.simulate_tickets(rnd, day, ctx.p("load", 1.0, float))
    since = (products.today_d() - dt.timedelta(days=7)).isoformat()
    week = {w["site"]: w for w in db.rows("select site, count(*) n, sum(met) met, sum(billed_usd) usd from tickets"
                                         " where sim_date>? group by site", (since,))}
    rows, filed = [], []
    for s in r["sites"]:
        w = week.get(s["site"], {"n": 0, "met": 0, "usd": 0})
        pct7 = round(100 * w["met"] / w["n"], 1) if w["n"] else 100.0
        rows.append({**s, "sla_7d": pct7, "billed_7d": round(w["usd"] or 0)})
        if w["n"] >= 60 and pct7 < 92:
            did = decision_add(ctx.agent, "hire_techs", f"Add a technician at {s['site']}",
                               {"site": s["site"], "add": 1, "reason": f"7-day SLA {pct7}% on {w['n']} tickets with {s['techs']} techs"})
            filed.append(did)
    ctx.log(f"{day}: {r['tickets']} tickets, SLA {r['sla_pct']}%")
    md = (f"Simulated day **{day}**: **{r['tickets']} tickets**, SLA met **{r['sla_pct']}%**. "
          f"{len(filed)} staffing decision(s) filed.\n\n" +
          table(sorted(rows, key=lambda x: x["sla_7d"]), ["site", "techs", "tickets", "sla_pct", "util", "sla_7d", "billed_7d"]) +
          "\n_SLAs: access 30 min, shipment 2 h, smart hands 4 h (urgent 1 h), cabling 24 h, install 48 h. "
          "Smart hands and cabling bill per 30 minutes, installs flat._\n")
    return {"day": day, "tickets": r["tickets"], "sla_pct": r["sla_pct"], "decisions": filed}, md


@playbook("accounts_renewals", "Renewals & churn", "accounts",
          "Process contracts that end today: renew with the price escalator, downsize, or churn. "
          "Renewal odds rise with interconnection density and cloud connections and fall with SLA misses.")
def accounts_renewals(ctx):
    rnd = random.Random(f"acct-{products.today()}-{time.time()}")
    opened = crm.open_renewals(90)
    events = products.run_renewals(rnd)
    for e in events:
        crm.close_renewal(e["tenant"], e["event"], e["mrr_after"])
    risk = products.at_risk(90, 10)
    ret = products.retention(90)
    ctx.log(f"{len(events)} contract events, net retention 90d {ret['net_retention']}")
    md = (f"Simulated day **{products.today()}**: {len(events)} contract(s) reached their end date; "
          f"{opened} renewal opportunities opened in the CRM (90-day horizon).\n\n"
          "## Today\n\n" + table(events, ["name", "kind", "event", "p_renew", "mrr_before", "mrr_after", "cross_connects",
                                          "sla_misses_90d", "note"]) +
          "\n## Last 90 days\n\n" + table([ret], ["renewed", "downsized", "churned", "mrr_up_for_renewal", "mrr_retained", "net_retention"]) +
          "\n## Most at risk in the next 90 days\n\n" + table(risk, ["name", "end_date", "p_renew", "mrr", "cross_connects",
                                                                   "cloud_connections", "sla_misses_90d"]))
    return {"events": len(events), "by_event": {e: sum(1 for x in events if x["event"] == e) for e in
                                                ("renewed", "downsized", "churned")},
            "net_retention_90d": ret["net_retention"]}, md


# ---------------------------------------------------------------- Ops

@playbook("ops_daily_health", "Daily site health", "ops",
          "Simulate 24 hours of power and cooling in every hall, apply load drift and random equipment "
          "faults/repairs, and flag halls outside limits.",
          {"fault_rate": {"type": "float", "default": 0.03}})
def ops_daily_health(ctx):
    rnd = random.Random(int(time.time()))
    fault = ctx.p("fault_rate", 0.03, float)
    targets = charter_mod.load()["targets"]
    events = []
    for h in world.halls():
        # tenants ramp toward 60-85% of what they contracted
        goal = h["contracted_kw"] * rnd.uniform(0.6, 0.85)
        it = round(h["it_load_kw"] + 0.15 * (goal - h["it_load_kw"]), 1)
        down = h["crah_down"]
        if down and rnd.random() < 0.5:
            down -= 1
            events.append({"hall": h["id"], "event": "CRAH unit repaired"})
        if rnd.random() < fault:
            down += 1
            events.append({"hall": h["id"], "event": "CRAH unit failed"})
        db.execute("update halls set it_load_kw=?, crah_down=? where id=?", (it, down, h["id"]))
    rows, flags = [], []
    metros = {m["code"]: m for m in db.rows("select * from metros")}
    for h in world.halls():
        d = thermal.day_profile(h, metros[h["metro"]])
        row = {"hall": h["id"], "cooling": h.get("cooling_type", "air"), "it_kw": h["it_load_kw"], "pue": d["pue"], "inlet_c": d["inlet_c"],
               "power_util": h["power_util"], "cooling_headroom_kw": d["cooling_headroom_kw"],
               "crah": f"{h['crah_units'] - h['crah_down']}/{h['crah_units']}"}
        rows.append(row)
        why = []
        lim = thermal.limits(h)
        if d["inlet_c"] > lim["recommended"]:
            why.append(f"{lim['measure']} {d['inlet_c']} C above recommended")
        if d["cooling_headroom_kw"] < h["crah_kw_each"]:
            why.append("lost N+1 cooling redundancy")
        if h["power_util"] > targets["max_hall_power_util"]:
            why.append(f"power {h['power_util']:.0%} contracted")
        if d["pue"] and d["pue"] > targets["max_pue"]:
            why.append(f"PUE {d['pue']}")
        if why:
            flags.append({"hall": h["id"], "issues": "; ".join(why)})
    ctx.log(f"{len(rows)} halls simulated, {len(flags)} flagged, {len(events)} equipment events")
    it = sum(r["it_kw"] for r in rows)
    pue = round(sum(r["it_kw"] * (r["pue"] or 0) for r in rows) / it, 3) if it else None
    md = (f"Simulated 24 h for **{len(rows)} halls**. Fleet IT load **{it / 1000:.2f} MW**, load-weighted PUE "
          f"**{pue}**. {len(flags)} halls flagged.\n\n## Flags\n\n" + table(flags, ["hall", "issues"]) +
          "\n## Equipment events\n\n" + table(events, ["hall", "event"]) +
          "\n## All halls\n\n" + table(rows, ["hall", "cooling", "it_kw", "pue", "inlet_c", "power_util", "cooling_headroom_kw", "crah"]) +
          "\n_Model: hourly outside-air sine per metro; free cooling below 18 C, chiller COP falls 0.15 per C above; "
          "UPS loss 4%, fans 3%, misc 1.5%._\n")
    return {"halls": len(rows), "flagged": len(flags), "fleet_pue": pue, "events": len(events),
            "flags": flags[:10]}, md


@playbook("ops_incident_drill", "Incident drill", "ops",
          "Run a minute-by-minute failure drill in one hall: cooling loss, utility outage or fiber cut.",
          {"hall": {"type": "str", "default": "SIN1-H1"},
           "kind": {"type": "str", "default": "cooling", "choices": ["cooling", "utility", "fiber"]},
           "units_lost": {"type": "int", "default": 2}, "repair_min": {"type": "int", "default": 45}})
def ops_incident_drill(ctx):
    hid, kind = ctx.p("hall"), ctx.p("kind", "cooling")
    h = next((x for x in world.halls() if x["id"] == hid), None)
    if not h:
        raise ValueError(f"no hall {hid}")
    m = world.metro(h["metro"])
    r = thermal.drill(h, m, kind, units_lost=ctx.p("units_lost", 2, int), repair_min=ctx.p("repair_min", 45, int))
    ctx.log(f"drill {kind} in {hid}: peak {r.get('peak_inlet_c')} C, outage {r.get('outage')}")
    if kind == "fiber":
        return r, f"{r['note']}\n"
    # how many units can we lose before breaching the allowable limit?
    tolerance = None
    if kind == "cooling":
        for k in range(1, h["crah_units"] - h["crah_down"] + 1):
            rr = thermal.drill(h, m, "cooling", units_lost=k, repair_min=ctx.p("repair_min", 45, int))
            if rr["breach_min"] is not None:
                tolerance = k - 1
                break
        else:
            tolerance = h["crah_units"] - h["crah_down"]
        if tolerance is not None and tolerance < 2:
            decision_add(ctx.agent, "cooling_upgrade", f"Add a CRAH unit to {hid}",
                         {"hall": hid, "units": 1, "reason": f"hall tolerates only {tolerance} unit failure(s) before 32 C"})
    md = (f"**{kind}** drill in **{hid}** ({h['it_load_kw']:.0f} kW IT, {h['crah_units'] - h['crah_down']} of "
          f"{h['crah_units']} CRAH units available).\n\n"
          f"- Peak inlet: **{r['peak_inlet_c']} C** ({r.get('verdict', 'n/a')})\n"
          f"- Allowable limit breached at: {('minute ' + str(r['breach_min'])) if r['breach_min'] is not None else 'never'}\n"
          f"- Critical load dropped: **{'yes' if r['outage'] else 'no'}**\n" +
          (f"- Units the hall can lose before breaching 32 C: **{tolerance}**\n" if tolerance is not None else "") +
          "\n## Response log\n\n" + "".join(f"- {a}\n" for a in r["actions"]) +
          "\n## Inlet temperature (every 5 min)\n\n" + table(r["timeline"], ["min", "inlet_c"]))
    return {k: r[k] for k in ("hall", "kind", "outage", "breach_min", "peak_inlet_c")} | {"tolerance_units": tolerance}, md


# ---------------------------------------------------------------- Network

@playbook("net_bgp_study", "BGP exchange study", "net",
          "Simulate BGP among the networks at one metro's exchange and inject an event: "
          "withdraw, leak, hijack or fiber_cut. Filtering defaults to the company standard.",
          {"metro": {"type": "str", "default": "ASH"},
           "event": {"type": "str", "default": "leak", "choices": ["baseline", "withdraw", "leak", "hijack", "fiber_cut"]},
           "rs_filtering": {"type": "bool", "default": None}, "edge_filtering": {"type": "bool", "default": None},
           "max_as": {"type": "int", "default": 48}})
def net_bgp_study(ctx):
    r = netsim.study(ctx.p("metro", "ASH"), ctx.p("event", "leak"), rs_filtering=ctx.p("rs_filtering"),
                     edge_filtering=ctx.p("edge_filtering"), max_as=ctx.p("max_as", 48, int))
    if "error" in r:
        raise ValueError(r["error"])
    b, a = r["baseline"], r["after_event"]
    ctx.log(f"{r['event']} at {r['metro']}: {json.dumps(a or b)}")
    md = (f"**{r['networks']} networks** at the {r['metro']} exchange ({r['rs_clients']} on the route server), "
          f"**{r['prefixes']} prefixes**. Route-server filtering **{'on' if r['rs_filtering'] else 'off'}**, "
          f"carrier edge filtering **{'on' if r['edge_filtering'] else 'off'}**.\n\n"
          "## Baseline\n\n" + table([b], list(b)) +
          (f"\n## After `{r['event']}`\n\n" + table([a], list(a)) +
           "\n" + "".join(f"- {k}: {v}\n" for k, v in r["detail"].items()) +
           f"- rejected by route server: {r['rs_rejected']}; rejected at carrier edges: {r['edge_rejected']}\n"
           if a else "") + f"\n_Model: {r['model']}._\n")
    return {k: r[k] for k in ("metro", "event", "rs_filtering", "edge_filtering", "networks", "rs_rejected",
                              "edge_rejected")} | {"baseline": b, "after": a}, md


@playbook("net_latency_map", "Latency map", "net", "Round-trip times between every pair of metros.")
def net_latency_map(ctx):
    lm = world.latency_matrix()
    rows = [{"from": a, **{b: lm["rtt_ms"][i][j] for j, b in enumerate(lm["metros"])}} for i, a in enumerate(lm["metros"])]
    ctx.log("latency map rebuilt")
    return {"metros": lm["metros"]}, "RTT in ms.\n\n" + table(rows, ["from"] + lm["metros"]) + f"\n_Model: {lm['model']}._\n"


# ---------------------------------------------------------------- Energy & Finance

def _latest_telemetry():
    return {t["hall"]: t for t in db.rows(
        "select t.* from telemetry t join (select hall, max(id) id from telemetry group by hall) l on l.id=t.id")}


@playbook("energy_report", "Energy & carbon report", "energy",
          "Daily energy, PUE and carbon per metro from the latest hall telemetry.")
def energy_report(ctx):
    tele = _latest_telemetry()
    if not tele:
        raise ValueError("no telemetry yet: run ops_daily_health first")
    metros = {m["code"]: m for m in db.rows("select * from metros")}
    per = {}
    for h in world.halls():
        t = tele.get(h["id"])
        if not t:
            continue
        p = per.setdefault(h["metro"], {"metro": h["metro"], "it_mwh": 0.0, "facility_mwh": 0.0})
        p["it_mwh"] += t["it_kw"] * 24 / 1000
        p["facility_mwh"] += t["facility_kw"] * 24 / 1000
    rows = []
    for code, p in per.items():
        m = metros[code]
        p["pue"] = round(p["facility_mwh"] / p["it_mwh"], 3) if p["it_mwh"] else None
        p["grid_g_per_kwh"] = m["grid_gco2_kwh"]
        p["renewable_pct"] = m["renewable_pct"]
        p["tco2_location"] = round(p["facility_mwh"] * m["grid_gco2_kwh"] / 1000, 2)
        p["tco2_market"] = round(p["tco2_location"] * (1 - m["renewable_pct"] / 100), 2)
        p["it_mwh"], p["facility_mwh"] = round(p["it_mwh"], 2), round(p["facility_mwh"], 2)
        rows.append(p)
    rows.sort(key=lambda r: -r["tco2_market"])
    it = sum(r["it_mwh"] for r in rows)
    fac = sum(r["facility_mwh"] for r in rows)
    co2 = sum(r["tco2_market"] for r in rows)
    ctx.log(f"fleet PUE {fac / it:.3f}, {co2:.1f} tCO2/day market-based")
    md = (f"Fleet: **{fac:.1f} MWh/day** facility energy, PUE **{fac / it:.3f}**, **{co2:.1f} tCO2/day** "
          f"(market-based, after renewable supply).\n\n" +
          table(rows, ["metro", "it_mwh", "facility_mwh", "pue", "grid_g_per_kwh", "renewable_pct", "tco2_location", "tco2_market"]) +
          f"\n**Biggest lever:** {rows[0]['metro']} emits the most; more renewable supply there moves the total most.\n"
          "\n_Grid intensities and renewable shares are illustrative inputs, not measured data._\n")
    return {"fleet_pue": round(fac / it, 3), "facility_mwh_day": round(fac, 1), "tco2_day": round(co2, 1),
            "top_emitter": rows[0]["metro"]}, md


# fin_monthly_close moved to fin_playbooks.py (v0.6)


# ---------------------------------------------------------------- R&D

@playbook("rnd_router_study", "Switch buffer study", "rnd",
          "Simulate one switch output port under bursty traffic across loads and buffer-sizing rules, "
          "then size a switch design.",
          {"link_gbps": {"type": "float", "default": 100}, "rtt_ms": {"type": "float", "default": 10},
           "flows": {"type": "int", "default": 400}, "ports": {"type": "int", "default": 32},
           "design_load": {"type": "float", "default": 0.9}})
def rnd_router_study(ctx):
    r = routerlab.study(link_gbps=ctx.p("link_gbps", 100, float), rtt_ms=ctx.p("rtt_ms", 10, float),
                        flows=ctx.p("flows", 400, int), ports=ctx.p("ports", 32, int),
                        design_load=ctx.p("design_load", 0.9, float))
    d = r["design"]
    ctx.log(f"design: {d['buffer_rule']} buffer, {d['buffer_total_mb']} MB, {d['memory']}")
    md = (f"{r['link_gbps']:.0f}G output port, {r['sources']} bursty sources, {r['simulated_ms']} ms simulated "
          f"per point; loss target {r['loss_target_pct']}% at {d['design_load']:.0%} load.\n\n"
          "## Buffer rules\n\n" + table([{"rule": k, "packets": v} for k, v in r["rules"].items()], ["rule", "packets"]) +
          "\n## Results\n\n" + table(r["grid"], ["load", "rule", "buffer_mb", "loss_pct", "delay_mean_us", "delay_p99_us"]) +
          f"\n## Proposed design\n\n" + "".join(f"- {k}: {v}\n" for k, v in d.items()) +
          f"\n_Caveat: {r['caveat']}_\n")
    return {"design": d}, md


@playbook("rnd_cooling_study", "AI cooling study", "rnd",
          "Compare air and direct-to-chip liquid cooling for 1 MW of AI load in every metro: PUE, "
          "energy cost, carbon; rank where the next AI hall should go.",
          {"carbon_usd_per_t": {"type": "float", "default": 100}})
def rnd_cooling_study(ctx):
    cprice = ctx.p("carbon_usd_per_t", 100, float)
    rows = []
    for m in db.rows("select * from metros"):
        res = {}
        for kind in ("air", "liquid"):
            h = {"id": f"model-{kind}", "it_load_kw": 1000.0, "cooling_type": kind}
            fac = sum(thermal.hall_power(h, thermal.ambient_c(m, hr))["facility_kw"] for hr in range(24)) / 24
            res[kind] = fac / 1000
        mwh = res["liquid"] * 8760
        cost = mwh * 1000 * m["power_usd_kwh"]
        tco2 = mwh * m["grid_gco2_kwh"] / 1000 * (1 - m["renewable_pct"] / 100)
        rows.append({"metro": m["code"], "pue_air": round(res["air"], 3), "pue_liquid": round(res["liquid"], 3),
                     "saving_pct": round(100 * (res["air"] - res["liquid"]) / res["air"], 1),
                     "energy_usd_yr": round(cost), "tco2_yr": round(tco2), "score_usd": round(cost + tco2 * cprice)})
    rows.sort(key=lambda r: r["score_usd"])
    top = [r["metro"] for r in rows[:3]]
    ctx.log(f"best metros for AI halls: {top}")
    md = (f"1 MW of IT for a year, air versus direct-to-chip liquid (85% of heat to warm water, dry coolers up "
          f"to 30 C outside). Score = energy cost + carbon at ${cprice:.0f}/t (market-based).\n\n" +
          table(rows, ["metro", "pue_air", "pue_liquid", "saving_pct", "energy_usd_yr", "tco2_yr", "score_usd"]) +
          f"\n**Recommendation:** next AI halls in {', '.join(top)}. Air cooling cannot actually hold "
          f"40-100 kW racks; the air column shows what the same load would cost if it could.\n")
    return {"best_metros": top, "rows": rows}, md


@playbook("rnd_research_cycle", "Research cycle", "rnd",
          "Chief Scientist: delegate a 2x2 route-leak study (route-server x carrier filtering) to Network "
          "Engineering, run the switch buffer study, write a research memo and propose a network standard "
          "if filtering clearly helps.",
          {"metro": {"type": "str", "default": "ASH"}})
def rnd_research_cycle(ctx):
    metro = ctx.p("metro", "ASH")
    grid = []
    for rs in (False, True):
        for edge in (False, True):
            c = ctx.child("net_bgp_study", {"metro": metro, "event": "leak", "rs_filtering": rs, "edge_filtering": edge})
            s = c["summary"] or {}
            a = (s.get("after") or {})
            grid.append({"rs_filtering": rs, "edge_filtering": edge, "leaked_routes": a.get("leaked_routes_in_use"),
                         "updates": a.get("updates"), "report": c["report"]})
    hj = []
    for rs, edge in ((False, False), (True, True)):
        c = ctx.child("net_bgp_study", {"metro": metro, "event": "hijack", "rs_filtering": rs, "edge_filtering": edge})
        a = ((c["summary"] or {}).get("after") or {})
        hj.append({"filtering": "both" if rs else "none", "hijacked_routes": a.get("hijacked_routes_in_use")})
    sw = ctx.child("rnd_router_study", {})
    sdn = ctx.child("sdn_inference_study", {})
    cool = ctx.child("rnd_cooling_study", {})
    best = (cool["summary"] or {}).get("best_metros", [])
    design = (sw["summary"] or {}).get("design", {})
    base = grid[0]["leaked_routes"] or 0
    both = grid[3]["leaked_routes"] or 0
    std = {"rs_filtering": bool(db.setting("rs_filtering", False)), "edge_filtering": bool(db.setting("edge_filtering", False))}
    proposal = None
    if base and both <= base * 0.5 and not (std["rs_filtering"] and std["edge_filtering"]):
        proposal = decision_add(ctx.agent, "network_standard",
                                "Adopt route-server and carrier-edge route filtering as the network standard",
                                {"rs_filtering": True, "edge_filtering": True,
                                 "evidence": f"leaked routes {base} -> {both} at {metro}"})
    ctx.log(f"leak grid done; proposal {proposal}")
    md = ("## Route leaks: which filtering stops them?\n\n" + table(grid, ["rs_filtering", "edge_filtering", "leaked_routes", "updates", "report"]) +
          f"\nLeaked routes in use: none {base}, route server only {grid[2]['leaked_routes']}, carrier edges only "
          f"{grid[1]['leaked_routes']}, both {both}. Route-server filtering only covers routes that travel over the "
          f"exchange; leaks that reach networks through the leaker's own transit providers need carrier edge filtering.\n"
          "\n## Hijacks\n\n" + table(hj, ["filtering", "hijacked_routes"]) +
          f"\n## Switch design\n\nBuffer rule **{design.get('buffer_rule')}**, {design.get('buffer_total_mb')} MB total, "
          f"{design.get('memory')}, about {design.get('est_power_w')} W. Details in {sw['report']}.\n"
          f"\n## AI cooling\n\nDirect-to-chip liquid is the cheapest and cleanest way to run AI load; best metros "
          f"for the next AI hall: **{', '.join(best)}**. Details in {cool['report']}.\n"
          f"\n## AI inference networking\n\nMesh recommends `{(sdn['summary'] or {}).get('best')}` routing for inference traffic. "
          f"Details in {sdn['report']}.\n"
          f"\n## Standard\n\nCurrent: {std}. " +
          (f"Filed decision **#{proposal}** to adopt both filters; the founder approves it." if proposal else
           "No change proposed.") + "\n")
    return {"leak_grid": grid, "hijack": hj, "design": design, "best_ai_metros": best, "proposal": proposal}, md


# ---------------------------------------------------------------- Data: CRM, BI, CDO, CEO

@playbook("crm_sync", "CRM sync", "crm",
          "Scribe brings the CRM in line with operations: accounts and contacts, contracts, assets "
          "(cabinets, ports, cross-connects, circuits, cloud, edge, exchange, metal) and cases from tickets.")
def crm_sync(ctx):
    r = crm.sync()
    s = crm.summary()
    ctx.log(f"synced: {r}")
    md = ("## Changes this sync\n\n" + table([r], list(r)) +
          "\n## CRM now\n\n" + table(s["accounts"], ["Type", "n"]) +
          f"\n{s['contacts']} contacts, {s['cases']:,} cases.\n\n## Open pipeline\n\n" +
          table(s["open_pipeline"], ["Type", "StageName", "n", "mrr"]) +
          "\n## Assets\n\n" + table(s["assets"], ["Status", "n"]))
    return {**r, "last_sync": s["last_sync"]}, md


@playbook("bi_nightly_load", "Warehouse load", "bi",
          "Prism loads the simulated day into the warehouse (revenue, operations, tickets, pipeline, bookings, "
          "contract events, energy) so every dashboard and metric reads the same numbers.")
def bi_nightly_load(ctx):
    r = bi.load()
    st = bi.status()
    ctx.log(f"loaded {r['date']}: {r['rows']} rows in {r['seconds']} s")
    md = (f"Loaded **{r['date']}**: {r['rows']:,} fact rows in {r['seconds']} s. {st['days_loaded']} days in the warehouse.\n\n" +
          table([{"table": k, "rows": v} for k, v in st["tables"].items()], ["table", "rows"]))
    return r, md


@playbook("bi_export", "Tableau export", "bi",
          "Prism exports the warehouse to CSV and a Tableau .hyper extract (when Tableau's Hyper API is installed).")
def bi_export(ctx):
    r = bi.export()
    ctx.log(f"exported to {r['dir']}")
    hy = r["hyper"]
    md = (f"Export written to `{r['dir']}`.\n\n" + table([{"table": k, "rows": v} for k, v in r["tables"].items()], ["table", "rows"]) +
          ("\n**Tableau extract:** `vix_warehouse.hyper` " + f"({hy['bytes']:,} bytes). Open it in Tableau Desktop or Tableau Public."
           if hy.get("written") else f"\nNo .hyper extract: {hy.get('reason')}. CSV files are ready for any tool.") + "\n")
    return {"dir": r["dir"], "hyper": hy.get("written", False), "rows": sum(r["tables"].values())}, md


def _audit_checks(use_rea: bool):
    from .. import armory
    today = products.today()
    checks = []

    def add(name, ok, detail, severity="high", warn=False):
        checks.append({"check": name, "status": "pass" if ok else ("warn" if warn else "fail"),
                       "severity": severity, "detail": detail})
    active = db.scalar("select count(*) from contracts where status='active'")
    cust = db.scalar("select count(*) from crm_account where Type='Customer'")
    tenants = db.scalar("select count(*) from tenants")
    accts = db.scalar("select count(*) from crm_account")
    add("Accounts match tenants", accts == tenants and cust == active,
        f"{tenants} tenants / {accts} accounts; {active} active contracts / {cust} customer accounts")
    for src, sql in crm.ASSET_SOURCES:
        ops_n = db.scalar(f"select count(*) from ({sql})")
        crm_n = db.scalar("select count(*) from crm_asset where Status='Installed' and Source__c like ?", (src + ":%",))
        add(f"Assets: {src.replace('_', ' ')}", ops_n == crm_n, f"operations {ops_n} / CRM installed {crm_n}")
    t, cs = db.scalar("select count(*) from tickets"), db.scalar("select count(*) from crm_case where Type!='network_outage'")
    add("Every ticket has a case", t == cs, f"{t:,} tickets / {cs:,} cases")
    outages = db.scalar("select count(*) from net_impact where incident>0")
    ncases = db.scalar("select count(*) from crm_case where Type='network_outage'")
    add("Every network outage has a customer case", outages == ncases, f"{outages} impacted services / {ncases} cases")
    broken = db.scalar("select count(*) from crm_opportunity o where IsWon=1 and Type!='Renewal'"
                       " and not exists (select 1 from orders r where r.id=o.Order__c)")
    add("Won opportunities have orders", broken == 0, f"{broken} won opportunities without an order")
    since = db.setting("crm_started_ts") or 0
    direct = db.scalar("select count(*) from orders r where r.ts>? and not exists"
                       " (select 1 from crm_opportunity o where o.Order__c=r.id)", (since,))
    add("Orders came through the CRM", direct == 0, f"{direct} orders placed outside the pipeline since the CRM went live",
        severity="low", warn=True)
    live = products.revenue_breakdown()["total"]
    wh_mrr = bi.metric("mrr")["value"] or 0
    crm_mrr = db.scalar("select round(coalesce(sum(MRR__c),0),2) from crm_account") or 0
    add("BI MRR = live billing", abs(wh_mrr - live) <= 1, f"warehouse ${wh_mrr:,.0f} / live ${live:,.0f}")
    add("CRM MRR = live billing", abs(crm_mrr - live) <= 1, f"CRM ${crm_mrr:,.0f} / live ${live:,.0f}")
    fin = db.setting("fin_last") or {}
    if fin.get("sim_date") == today:
        gap = abs(fin["mrr"] - live)
        add("Finance close = live billing", gap <= 1,
            f"close ${fin['mrr']:,.0f} / live ${live:,.0f}" + (" (activity after today's close)" if 1 < gap <= 0.005 * live else ""),
            severity="high" if gap > 0.005 * live else "low", warn=gap <= 0.005 * live)
    else:
        add("Finance close is current", False, f"last close {fin.get('sim_date', 'never')}, today {today}",
            severity="low", warn=True)
    ops_nrr = products.retention(90)["net_retention"]
    bi_nrr = bi.metric("net_retention", 90)["value"]
    add("BI net retention = operations", (ops_nrr is None and bi_nrr is None) or
        (ops_nrr is not None and bi_nrr is not None and abs(ops_nrr * 100 - bi_nrr) <= 0.1),
        f"warehouse {bi_nrr}% / operations {None if ops_nrr is None else round(ops_nrr * 100, 1)}%")
    sla_ops = products.sla_window(30)["sla_pct"]
    s = bi.metric("sla_by_site", 30)["value"]
    tk = sum(x["tickets"] for x in s)
    sla_bi = round(100 * sum(x["tickets"] * x["sla_pct"] / 100 for x in s) / tk, 2) if tk else None
    add("BI SLA = operations", sla_ops is None or (sla_bi is not None and abs(sla_ops - sla_bi) <= 0.1),
        f"warehouse {sla_bi}% / operations {sla_ops}% (30 days)")
    add("CRM synced today", db.setting("crm_synced") == today, f"last sync {db.setting('crm_synced')}")
    add("Warehouse loaded today", db.setting("bi_loaded") == today, f"last load {db.setting('bi_loaded')}")
    evidence = {}
    if use_rea:
        for which, prefix in (("crm", "crm_"), ("warehouse", "fact_")):
            r = armory.run("rea_inspect_db", {"db": which})
            if r.get("ok"):
                names = [x["name"] for x in r["tables"]]
                expected = list(crm.OBJECTS.values()) if which == "crm" else bi.FACT_TABLES
                missing = [x for x in expected if x not in names]
                add(f"REA schema evidence: {which}", not missing,
                    f"evidence {r['evidence_id'][:18]}..., {len(names)} tables" + (f", missing {missing}" if missing else ""))
                evidence[which] = r["evidence_id"]
            else:
                add(f"REA schema evidence: {which}", False, f"REA unavailable: {str(r.get('error'))[:120]}",
                    severity="low", warn=True)
        r = armory.run("api_contract_check", {})
        if "checks" in r:
            add("API contract (Salesforce-shaped + BI)", r["ok"], f"{r['passed']}/{r['total']} endpoint checks pass; REA HAR "
                + (r["rea"].get("evidence_id", "")[:18] + "..." if r.get("rea", {}).get("evidence_id") else "not inspected"))
            evidence["api_har"] = r.get("har")
        else:
            add("API contract (Salesforce-shaped + BI)", False, str(r.get("error"))[:160], severity="low", warn=True)
    return checks, evidence


@playbook("cdo_data_audit", "Data audit", "cdo",
          "Chief Data Officer: have Scribe sync and Prism load, then reconcile CRM, operations, the warehouse and "
          "Finance; collect REA evidence on both databases and the API; self-heal drift; escalate the rest to the CEO.",
          {"use_rea": {"type": "bool", "default": True}})
def cdo_data_audit(ctx):
    sync = ctx.child("crm_sync")
    load = ctx.child("bi_nightly_load")
    use_rea = ctx.p("use_rea", True)
    checks, evidence = _audit_checks(bool(use_rea))
    healed = []
    if any(c["status"] == "fail" and (c["check"].startswith("Assets") or c["check"].startswith("Accounts")
                                      or c["check"].startswith("Every ticket")) for c in checks):
        ctx.log("drift found: re-syncing CRM and re-checking")
        crm.sync()
        bi.load()
        before = {c["check"] for c in checks if c["status"] == "fail"}
        checks, evidence = _audit_checks(False) if not use_rea else _audit_checks(True)
        healed = sorted(before - {c["check"] for c in checks if c["status"] == "fail"})
    n = len(checks)
    passed = sum(c["status"] == "pass" for c in checks)
    warns = sum(c["status"] == "warn" for c in checks)
    score = round(100 * (passed + 0.5 * warns) / n, 1)
    escalations = [c for c in checks if c["status"] == "fail"]
    db.set_setting("data_quality", {"score": score, "date": products.today(), "fails": len(escalations), "warns": warns})
    ctx.log(f"data quality {score}: {passed} pass, {warns} warn, {len(escalations)} fail")
    md = (f"Data quality score **{score}** on {products.today()}: {passed} pass, {warns} warn, {len(escalations)} fail"
          + (f"; self-healed: {', '.join(healed)}" if healed else "") + ".\n\n" +
          f"Delegated: {sync['report']} (Scribe), {load['report']} (Prism).\n\n## Checks\n\n" +
          table(checks, ["check", "status", "severity", "detail"]) +
          ("\n## Escalated to the CEO\n\n" + table(escalations, ["check", "detail"]) if escalations else
           "\nNothing to escalate: CRM, operations, warehouse and billing agree.\n") +
          ("\n## REA evidence\n\n" + "".join(f"- {k}: `{v}`\n" for k, v in evidence.items()) if evidence else ""))
    return {"score": score, "pass": passed, "warn": warns, "fail": len(escalations), "healed": healed,
            "escalations": [e["check"] for e in escalations], "evidence": evidence}, md


def _recommend(d):
    det = d["detail"]
    if d["kind"] in _engine.RECOMMENDERS:
        try:
            return _engine.RECOMMENDERS[d["kind"]](d)
        except Exception as e:  # noqa: BLE001
            return "review", f"rule failed: {e}"
    if d["kind"] == "build_hall":
        metro = det["site"][:3]
        kind = "ai_cabinets" if det.get("liquid") else "cabinets"
        demand = 0.0
        for w in db.rows("select params from orders where status='waitlisted' and kind=?", (kind,)):
            pm = json.loads(w["params"])
            if pm.get("metro") == metro:
                demand += crm._order_mrr(kind, pm)
        capex = det.get("capex_usd") or 0
        payback = capex / demand if demand else None
        if payback and payback <= 72:
            return "approve", f"waitlisted demand worth ${demand:,.0f}/mo pays back ${capex:,.0f} in {payback:.0f} months"
        return "hold", (f"waitlisted demand only ${demand:,.0f}/mo against ${capex:,.0f} capex" if demand
                        else "no waitlisted demand behind it yet; the halls are tight on power but customers are not queuing")
    if d["kind"] == "network_capex":
        days = det.get("days_to_80")
        if (det.get("util") or 0) >= 0.8 or (days is not None and days <= 60):
            return "approve", f"{det.get('reason')}; ${det.get('capex_usd', 0):,.0f}"
        return "hold", f"{det.get('reason')}; not urgent yet, revisit in two weeks"
    if d["kind"] == "net_change":
        dev = db.one("select status, ha_peer from net_device where hostname=?", (det["device"],))
        peer = db.one("select status from net_device where hostname=?", (dev["ha_peer"],)) if dev and dev["ha_peer"] else None
        if dev and dev["status"] == "up" and (not peer or peer["status"] == "up"):
            return "approve", f"{det.get('reason')}; HA peer healthy, customers notified, window {det.get('window')}"
        return "hold", "device or its HA peer is not healthy right now"
    if d["kind"] in ("net_live_writes", "net_device_mode"):
        rd = db.setting("net_readiness") or {}
        lvl, since = rd.get("level"), db.setting("net_monitoring_since")
        if d["kind"] == "net_live_writes" and not det.get("enable", True):
            return "approve", "turning automated changes off is always safe"
        if lvl not in ("read-only monitoring", "supervised changes"):
            return "hold", f"readiness is '{lvl or 'not assessed'}'; run net_golive_readiness until it reaches read-only monitoring"
        days = (products.today_d() - dt.date.fromisoformat(since)).days if since else 0
        if days < 14:
            return "hold", (f"real gear has only been watched read-only for {days} day(s); run 14 days of read-only "
                            "monitoring with clean NOC results before allowing automated changes")
        return "approve", (f"{days} days of read-only monitoring passed; every change still needs approval, "
                           "a window, commit-confirm and post-checks")
    if d["kind"] == "hire_techs":
        site = det["site"]
        s30 = next((s for s in bi.metric("sla_by_site", 30)["value"] if s["site"] == site), None)
        at_risk = db.scalar("select coalesce(sum(o.MRR__c),0) from crm_opportunity o join crm_account a on a.Id=o.AccountId"
                            " where o.Type='Renewal' and o.IsClosed=0 and exists (select 1 from crm_asset x where"
                            " x.AccountId=a.Id and x.Location__c like ? and x.Status='Installed')", (site + "%",)) or 0
        if s30 and s30["sla_pct"] < 93:
            return "approve", (f"30-day SLA {s30['sla_pct']}% on {s30['tickets']} tickets, not a one-off; "
                               f"${products.TECH_COST_MONTH:,.0f}/mo against ${at_risk:,.0f}/mo of renewals with gear on site")
        return "hold", (f"7-day dip only; 30-day SLA is {s30['sla_pct'] if s30 else 'n/a'}%. Re-check next week before "
                        f"adding ${products.TECH_COST_MONTH:,.0f}/mo")
    if d["kind"] == "cooling_upgrade":
        h = db.one("select crah_units, crah_down from halls where id=?", (det["hall"],))
        if h and h["crah_down"] >= 1:
            return "approve", f"{h['crah_down']} of {h['crah_units']} units still down; restores redundancy"
        return "hold", "units were repaired since this was filed; redundancy is back without new capex"
    if d["kind"] == "network_standard":
        return "approve", f"research evidence: {det.get('evidence', '')}"
    if d["kind"] in _engine.RECOMMENDERS:
        try:
            return _engine.RECOMMENDERS[d["kind"]](d)
        except Exception as e:  # noqa: BLE001
            return "review", f"rule failed: {e}"
    return "review", "no rule for this kind"


@playbook("ceo_board_pack", "Board pack", "ceo",
          "CEO: get the weekly review from the Chief Operator, the research memo from the Chief Scientist and the data "
          "audit from the Chief Data Officer; check their numbers against the warehouse; recommend on every open "
          "decision; deliver one board pack to the founder.")
def ceo_board_pack(ctx):
    atlas = ctx.child("exec_weekly_review")
    euler = ctx.child("rnd_research_cycle")
    vega = ctx.child("cdo_data_audit")
    mer = ctx.child("cno_network_review")
    cto = ctx.child("cto_engineering_review")
    a, e, v, n = atlas["summary"] or {}, euler["summary"] or {}, vega["summary"] or {}, mer["summary"] or {}
    mrr = bi.metric("mrr")["value"] or 0
    trend = bi.metric("mrr_trend", 30)["value"]
    wk = [x for x in trend if x["x"] <= (products.today_d() - dt.timedelta(days=7)).isoformat()]
    wk_mrr = wk[-1]["y"] if wk else None
    head = [
        {"metric": "MRR", "value": usd(mrr), "vs 7 days": (f"{100 * (mrr / wk_mrr - 1):+.2f}%" if wk_mrr else "n/a")},
        {"metric": "Net retention (90d)", "value": f"{bi.metric('net_retention', 90)['value'] or 0}%", "vs 7 days": ""},
        {"metric": "Win rate (30d)", "value": f"{bi.metric('win_rate', 30)['value'] or 0}%", "vs 7 days": ""},
        {"metric": "Churned/downsized MRR (90d)", "value": usd(bi.metric('churned_mrr', 90)['value'] or 0), "vs 7 days": ""},
        {"metric": "EBITDA margin", "value": f"{(a.get('ebitda_margin') or 0):.1%}", "vs 7 days": ""},
        {"metric": "Data quality", "value": f"{v.get('score')}", "vs 7 days": ""},
        {"metric": "Network availability (30d)", "value": f"{n.get('availability_30d')}%", "vs 7 days": f"7d {n.get('availability_7d')}%"},
        {"metric": "Network SLA credits (30d)", "value": usd(n.get('credits_30d') or 0), "vs 7 days": ""},
    ]
    consistent = abs((a.get("mrr") or 0) - mrr) <= 1
    risks = []
    for o in db.rows("select o.Name, o.CloseDate, o.Probability, o.MRR__c from crm_opportunity o where o.Type='Renewal'"
                     " and o.IsClosed=0 order by o.Probability asc, o.MRR__c desc limit 3"):
        risks.append(f"Renewal at risk: {o['Name']} closes {o['CloseDate']}, {o['Probability']:.0f}% odds, {usd(o['MRR__c'])}/mo")
    sla = [s for s in bi.metric("sla_by_site", 7)["value"] if s["sla_pct"] is not None and s["sla_pct"] < 92]
    if sla:
        risks.append("Remote-hands SLA below 92% at " + ", ".join(f"{s['site']} ({s['sla_pct']}%)" for s in sla[:5]))
    wl = db.scalar("select count(*) from orders where status='waitlisted'")
    if wl:
        risks.append(f"{wl} orders waitlisted for capacity")
    for x in v.get("escalations", []):
        risks.append(f"Data escalation from Vega: {x}")
    for x in n.get("escalations", []):
        risks.append(f"Network escalation from Meridian: {x}")
    for x in (cto["summary"] or {}).get("escalations", []):
        risks.append(f"Engineering escalation from Lattice: {x}")
    extra_lines = []
    for pb_name, fn in _engine.BOARD_CHIEFS:
        try:
            r = ctx.child(pb_name)
            line, rk = fn(r)
            extra_lines.append(line)
            risks.extend(rk)
        except Exception as e:  # noqa: BLE001
            extra_lines.append(f"- **{pb_name}** failed: {e}\n")
    recs = []
    for d in decisions("open"):
        verdict, why = _recommend(d)
        recs.append({"id": d["id"], "from": d["agent"], "decision": d["title"], "Helm recommends": verdict, "why": why})
    ctx.log(f"board pack: {len(recs)} decisions, {len(risks)} risks, numbers consistent={consistent}")
    md = (f"Week ending **{products.today()}** (simulated). Prepared by Helm (CEO) from the Chief Operator, Chief Scientist "
          f"and the other chiefs. " + ("Atlas's figures tie to the warehouse." if consistent else
                                         "**Atlas's MRR does not tie to the warehouse; Vega has been asked to explain.**") +
          "\n\n## Headline\n\n" + table(head, ["metric", "value", "vs 7 days"]) +
          f"\n## From the chiefs\n\n- **Atlas (operations):** {a.get('filed', 0)} decisions filed this review, "
          f"{a.get('open_decisions', 0)} open. Report: {atlas['report']}\n"
          f"- **Euler (research):** next AI halls in {', '.join(e.get('best_ai_metros') or []) or 'n/a'}; "
          + (f"proposed network standard (decision #{e['proposal']})" if e.get("proposal") else "no new standard proposed")
          + f". Report: {euler['report']}\n"
          f"- **Meridian (network):** availability {n.get('availability_30d')}% over 30 days, {n.get('incidents_30d')} incidents, "
          f"MTTR {n.get('mttr_30d')} min, {n.get('critical_cve_devices')} devices on vulnerable firmware, live readiness: "
          f"{n.get('readiness') or 'not assessed'}. Report: {mer['report']}\n"
          f"- **Lattice (engineering):** {(cto['summary'] or {}).get('steps')} reviews, {(cto['summary'] or {}).get('failed')} failed; "
          f"escalations: {', '.join((cto['summary'] or {}).get('escalations') or []) or 'none'}. Report: {cto['report']}\n"
          f"- **Vega (data):** quality {v.get('score')}, {v.get('fail', 0)} fail, {v.get('warn', 0)} warn"
          + (f", self-healed {', '.join(v['healed'])}" if v.get("healed") else "") + f". Report: {vega['report']}\n" + "".join(extra_lines) +
          "\n## Risks\n\n" + ("".join(f"- {r}\n" for r in risks) or "- None worth your time this week.\n") +
          "\n## Decisions for you\n\n" + table(recs, ["id", "from", "decision", "Helm recommends", "why"]) +
          "\nApprove or reject each one in HQ (Decisions) or ask Claude to call company_decide.\n")
    return {"week_ending": products.today(), "mrr": mrr, "decisions": len(recs),
            "recommend_approve": [r["id"] for r in recs if r["Helm recommends"] == "approve"],
            "risks": len(risks), "data_quality": v.get("score"), "consistent": consistent}, md


# ---------------------------------------------------------------- Chief Operator

@playbook("exec_operating_cycle", "Operating cycle", "exec",
          "Chief Operator: run simulated day(s) across departments: demand, colocation, interconnection, "
          "digital services, metal, remote hands, renewals, site health, energy. days>1 fast-forwards the "
          "calendar (weekly reviews included) and writes one summary report.",
          {"days": {"type": "int", "default": 1}})
def exec_operating_cycle(ctx):
    steps = ["sales_demand_cycle", "ops_fulfill_space", "ix_fulfill_orders", "digital_fulfill_orders",
             "metal_fulfill_orders", "cx_ticket_desk", "noc_watch", "nos_certify", "netauto_compliance", "change_manage",
             "net_deploy_migrate", "ps_projects", "accounts_renewals", "ops_daily_health", "energy_report", "crm_sync",
             "bi_nightly_load"]
    steps = steps[:-2] + [x for x in _engine.DAILY_STEPS if x in _engine.PLAYBOOKS] + steps[-2:]
    days = max(1, min(365, ctx.p("days", 1, int)))

    def physics():
        from .. import netinfra
        r = random.Random(f"net-{products.today()}-{time.time()}")
        netinfra.assign_ports()
        netinfra.simulate_day(r)
        netinfra.capacity_snapshot()
    if days == 1:
        day = products.advance(1)
        physics()
        ctx.log(f"simulated day {day}")
        rows = []
        for name in steps:
            c = ctx.child(name)
            s = c["summary"] or {}
            brief = ", ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, (list, dict)))[:160]
            rows.append({"step": name, "status": c["status"], "result": brief, "report": c["report"]})
        k = kpis()
        md = (f"Simulated day **{day}**.\n\n## Delegated steps\n\n" + table(rows, ["step", "status", "result", "report"]) +
              "\n## KPIs after the cycle\n\n" + table([{"kpi": a, "value": json.dumps(b) if isinstance(b, dict) else b}
                                                        for a, b in k.items()], ["kpi", "value"]))
        return {"day": day, "steps": len(rows), "failed": sum(r["status"] != "done" for r in rows),
                "waitlisted": k["orders_waitlisted"], "mrr_usd": k["mrr_usd"]}, md
    start_mrr = world.revenue_breakdown()["total"]
    start = products.today()
    log = []
    week = {"net_incidents": 0, "credits": 0.0, "upgrades": 0}
    for d in range(days):
        day = products.advance(1)
        physics()
        out = {name: ctx.inline(name) for name in steps}
        if (d + 1) % 7 == 0:
            ctx.inline("exec_weekly_review")
            for wk in ["cno_network_review", "crm_data_cloud", "itsm_sync", "net_inventory_audit"] + list(_engine.WEEKLY_STEPS):
                ctx.inline(wk)
        row = {"day": day, "orders": out["sales_demand_cycle"]["orders"],
               "new_logos": out["sales_demand_cycle"]["new_tenants"],
               "tickets": out["cx_ticket_desk"]["tickets"], "sla_pct": out["cx_ticket_desk"]["sla_pct"],
               **out["accounts_renewals"]["by_event"], "pue": out["ops_daily_health"]["fleet_pue"],
               }
        week["net_incidents"] += out["noc_watch"]["incidents"]
        week["credits"] += out["noc_watch"]["credits_usd"]
        week["upgrades"] += out["change_manage"]["executed"]
        if (d + 1) % 7 == 0 or d == days - 1:
            row["mrr"] = round(world.revenue_breakdown()["total"])
            row.update({"net_incidents": week["net_incidents"], "credits": round(week["credits"]), "upgrades": week["upgrades"]})
            week = {"net_incidents": 0, "credits": 0.0, "upgrades": 0}
        log.append(row)
        if d % 10 == 0:
            ctx.log(f"day {day}")
    k = kpis()
    end_mrr = world.revenue_breakdown()["total"]

    def tot(key):
        return sum(r.get(key, 0) or 0 for r in log)
    weekly = [r for r in log if "mrr" in r]
    md = (f"Fast-forwarded **{days} days** ({start} to {products.today()}). MRR {usd(start_mrr)} to **{usd(end_mrr)}** "
          f"({100 * (end_mrr / start_mrr - 1):+.1f}%). {tot('orders')} orders, {tot('new_logos')} new logos, "
          f"{tot('renewed')} renewals, {tot('downsized')} downsizes, {tot('churned')} churned. "
          f"{k['open_decisions']} decisions await the founder.\n\n## Week by week\n\n" +
          "_Network columns are totals for the week; the others are that day's figures._\n\n" +
          table(weekly, ["day", "orders", "new_logos", "tickets", "sla_pct", "net_incidents", "credits", "upgrades",
                         "renewed", "downsized", "churned", "pue", "mrr"]) +
          "\n## KPIs now\n\n" + table([{"kpi": a, "value": json.dumps(b) if isinstance(b, dict) else b}
                                          for a, b in k.items()], ["kpi", "value"]) +
          "\n_Department steps ran inline (no separate reports) to keep a long fast-forward readable._\n")
    return {"days": days, "from": start, "to": products.today(), "mrr_start": round(start_mrr), "mrr_end": round(end_mrr),
            "orders": tot("orders"), "churned": tot("churned"), "open_decisions": k["open_decisions"]}, md


@playbook("exec_weekly_review", "Weekly review", "exec",
          "Chief Operator: close the books, review capacity, cooling and demand, and file decisions "
          "(new halls, cooling upgrades) for the founder.")
def exec_weekly_review(ctx):
    fin = ctx.child("fin_monthly_close")
    targets = charter_mod.load()["targets"]
    hs = world.halls()
    filed = []
    # capacity: metros where waitlisted cabinet demand exists or halls are near the power cap
    wait = db.rows("select params from orders where status='waitlisted' and kind='cabinets'")
    demand = {}
    for w in wait:
        p = json.loads(w["params"])
        demand[p["metro"]] = demand.get(p["metro"], 0) + int(p.get("cabinets", 0))
    air = [h for h in hs if h.get("cooling_type", "air") != "liquid"]
    for code in sorted({h["metro"] for h in air}):
        mine = [h for h in air if h["metro"] == code]
        hot = [h for h in mine if h["power_util"] > targets["max_hall_power_util"]]
        free = sum(h["cabinets_total"] - h["cabinets_sold"] for h in mine)
        if demand.get(code, 0) > 0 or (len(hot) >= max(1, len(mine) // 2)):
            site = min({h["site"] for h in mine}, key=lambda s: sum(1 for h in mine if h["site"] == s))
            did = decision_add(ctx.agent, "build_hall", f"Build a new hall at {site}",
                               {"site": site, "cabinets": 260,
                                "reason": f"{demand.get(code, 0)} cabinets waitlisted in {code}; "
                                          f"{len(hot)}/{len(mine)} halls above {targets['max_hall_power_util']:.0%} power; "
                                          f"{free} cabinets free metro-wide",
                                "capex_usd": 260 * world.KW_PER_CAB_DESIGN * world.CAPEX_PER_KW})
            filed.append({"decision": did, "kind": "build_hall", "where": site})
    ai_wait = {}
    for w in db.rows("select params from orders where status='waitlisted' and kind='ai_cabinets'"):
        pm = json.loads(w["params"])
        ai_wait[pm["metro"]] = ai_wait.get(pm["metro"], 0) + int(pm.get("cabinets", 0))
    for code, n in ai_wait.items():
        liquid = [h for h in hs if h["metro"] == code and h.get("cooling_type") == "liquid"]
        site = liquid[0]["site"] if liquid else f"{code}1"
        did = decision_add(ctx.agent, "build_hall", f"Build a liquid-cooled AI hall at {site}",
                           {"site": site, "cabinets": products.AI_HALL_CABS, "liquid": True,
                            "reason": f"{n} AI cabinets waitlisted in {code}",
                            "capex_usd": products.AI_HALL_CABS * products.LIQUID_CAB_KW * world.CAPEX_PER_KW * 1.25})
        filed.append({"decision": did, "kind": "build_hall (AI)", "where": site})
    for h in hs:
        if h["crah_down"] >= 2:
            did = decision_add(ctx.agent, "cooling_upgrade", f"Add a CRAH unit to {h['id']}",
                               {"hall": h["id"], "units": 1, "reason": f"{h['crah_down']} units down"})
            filed.append({"decision": did, "kind": "cooling_upgrade", "where": h["id"]})
    k = kpis()
    fs = fin["summary"] or {}
    ctx.log(f"filed {len(filed)} decisions")
    open_d = decisions("open")
    md = (f"MRR **{usd(fs.get('mrr', 0))}**, EBITDA margin **{(fs.get('ebitda_margin') or 0):.1%}**, cabinet "
          f"utilisation **{k['cabinet_util']:.0%}**, fleet PUE **{k['fleet_pue']}**, "
          f"**{k['orders_waitlisted']}** orders waitlisted.\n\n## Decisions filed this review\n\n" +
          table(filed, ["decision", "kind", "where"]) +
          "\n## Open decisions awaiting the founder\n\n" +
          table([{"id": d["id"], "by": d["agent"], "title": d["title"], "reason": d["detail"].get("reason") or d["detail"].get("evidence")}
                 for d in open_d], ["id", "by", "title", "reason"]) +
          "\n## Customers\n\n" + table([{**products.retention(90), "sla_30d": products.sla_window(30)["sla_pct"]}],
                                          ["renewed", "downsized", "churned", "net_retention", "sla_30d"]) +
          "\nMost at risk: " + (", ".join(f"{r['name']} ({r['p_renew']:.0%}, {usd(r['mrr'])}/mo)"
                                          for r in products.at_risk(90, 5)) or "none") +
          f"\n\nFinance detail: {fin['report']}.\n")
    return {"filed": len(filed), "open_decisions": len(open_d), "mrr": fs.get("mrr"),
            "ebitda_margin": fs.get("ebitda_margin")}, md
