"""Department playbooks. Each takes a run context and returns (summary, markdown).
Agents here are rule-based: they read the simulated world, act on it, and write
up what they did. Reasoning beyond the rules happens when Claude reads the
reports through MCP and the founder approves decisions."""
from __future__ import annotations

import json
import random
import time

from .. import db, netsim, routerlab, thermal, world
from . import charter as charter_mod
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

@playbook("sales_demand_cycle", "Demand cycle", "sales",
          "Generate a day's worth of synthetic customer demand (existing tenants and new logos) as orders.",
          {"days": {"type": "int", "default": 1}, "orders_per_day": {"type": "int", "default": 9},
           "new_logo_rate": {"type": "float", "default": 0.15}})
def sales_demand_cycle(ctx):
    rnd = random.Random(int(time.time()))
    days = ctx.p("days", 1, int)
    rate = ctx.p("orders_per_day", 9, int)
    new_rate = ctx.p("new_logo_rate", 0.15, float)
    metros = [m[0] for m in world.METROS]
    sites = [s["id"] for s in db.rows("select id from sites")]
    placed, new_tenants = [], []
    for _ in range(days):
        n = max(0, int(rnd.gauss(rate, rate ** 0.5)))
        for _ in range(n):
            if rnd.random() < new_rate:
                tid = world.new_tenant(rnd)
                new_tenants.append(tid)
            else:
                tid = db.scalar("select id from tenants order by random() limit 1")
            t = db.one("select * from tenants where id=?", (tid,))
            kind = rnd.choices(["cabinets", "port", "cross_connect", "vc", "metal"], [0.2, 0.15, 0.35, 0.2, 0.1])[0]
            home = t["home_metro"] if rnd.random() < 0.6 else rnd.choice(metros)
            if kind == "cabinets":
                params = {"metro": home, "cabinets": rnd.choice([2, 4, 6, 10, 20, 40]),
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
            else:
                params = {"metro": home, "plan": rnd.choices(list(world.METAL_PLANS), [0.55, 0.35, 0.1])[0],
                          "count": rnd.choice([1, 2, 4, 8])}
            o = world.place_order(tid, kind, params)
            placed.append({"order": o["order_id"], "tenant": t["name"], "kind": kind, "params": json.dumps(params)})
    ctx.log(f"placed {len(placed)} orders, signed {len(new_tenants)} new tenants")
    by_kind = {}
    for p in placed:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    md = (f"Placed **{len(placed)} orders** over {days} day(s); **{len(new_tenants)} new tenants** signed.\n\n"
          f"## By product\n\n" + table([{"kind": k, "orders": v} for k, v in sorted(by_kind.items())], ["kind", "orders"]) +
          "\n## Orders\n\n" + table(placed, ["order", "tenant", "kind", "params"]))
    return {"orders": len(placed), "new_tenants": len(new_tenants), "by_kind": by_kind}, md


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
          "Provision pending ports, cross-connects and fabric virtual circuits; retry the waitlist.")
def ix_fulfill_orders(ctx):
    return _fulfil(ctx, ["port", "cross_connect", "vc"], "interconnection")


@playbook("ops_fulfill_space", "Place colocation", "ops",
          "Place pending cabinet orders in the least power-loaded hall that fits, under the power cap.")
def ops_fulfill_space(ctx):
    return _fulfil(ctx, ["cabinets"], "colocation")


@playbook("metal_fulfill_orders", "Provision bare metal", "metal",
          "Allocate pending bare-metal orders from each metro's inventory.")
def metal_fulfill_orders(ctx):
    s, md = _fulfil(ctx, ["metal"], "bare-metal")
    inv = db.rows("select metro, plan, total, allocated, total-allocated free from metal_inventory order by metro, plan")
    return s, md + "\n## Inventory\n\n" + table(inv, ["metro", "plan", "total", "allocated", "free"])


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
        row = {"hall": h["id"], "it_kw": h["it_load_kw"], "pue": d["pue"], "inlet_c": d["inlet_c"],
               "power_util": h["power_util"], "cooling_headroom_kw": d["cooling_headroom_kw"],
               "crah": f"{h['crah_units'] - h['crah_down']}/{h['crah_units']}"}
        rows.append(row)
        why = []
        if d["inlet_c"] > thermal.RECOMMENDED_MAX_C:
            why.append(f"inlet {d['inlet_c']} C above recommended")
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
          "\n## All halls\n\n" + table(rows, ["hall", "it_kw", "pue", "inlet_c", "power_util", "cooling_headroom_kw", "crah"]) +
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


@playbook("fin_monthly_close", "Monthly close", "finance",
          "Revenue by product and metro, power cost from telemetry, site opex, depreciation, EBITDA.")
def fin_monthly_close(ctx):
    rev = world.revenue_breakdown()
    tele = _latest_telemetry()
    metros = {m["code"]: m for m in db.rows("select * from metros")}
    power_cost, capex, per_metro = 0.0, 0.0, {}
    for h in world.halls():
        t = tele.get(h["id"])
        fac_kw = t["facility_kw"] if t else h["it_load_kw"] * 1.45
        c = fac_kw * 730 * metros[h["metro"]]["power_usd_kwh"]
        power_cost += c
        capex += h["build_capex_usd"]
        pm = per_metro.setdefault(h["metro"], {"metro": h["metro"], "power_cost": 0.0, "cabinets_sold": 0, "cabinets_total": 0})
        pm["power_cost"] += c
        pm["cabinets_sold"] += h["cabinets_sold"]
        pm["cabinets_total"] += h["cabinets_total"]
    for pm in per_metro.values():
        pm["revenue"] = sum(world.tenant_mrr(r["tenant"]) * r["share"] for r in db.rows(
            "select p.tenant, cast(sum(p.cabinets) as real) / (select sum(cabinets) from placements where tenant=p.tenant) share"
            " from placements p join halls h on h.id=p.hall join sites s on s.id=h.site where s.metro=? group by p.tenant",
            (pm["metro"],)))
        pm["util"] = round(pm["cabinets_sold"] / pm["cabinets_total"], 3)
        pm["revenue"], pm["power_cost"] = round(pm["revenue"]), round(pm["power_cost"])
    sites = db.scalar("select count(*) from sites")
    opex = sites * world.SITE_OPEX_MONTH
    dep = capex / 240
    ebitda = rev["total"] - power_cost - opex
    margin = round(ebitda / rev["total"], 3) if rev["total"] else None
    snap = {"mrr": rev["total"], "power_cost": round(power_cost), "opex": opex, "ebitda": round(ebitda),
            "ebitda_margin": margin, "depreciation": round(dep), "ebit": round(ebitda - dep), "ts": time.time()}
    db.set_setting("fin_last", snap)
    ctx.log(f"MRR {usd(rev['total'])}, EBITDA margin {margin:.1%}")
    md = ("## P&L (one month)\n\n" + table([
        {"line": "Revenue", "usd": usd(rev["total"])},
        {"line": "Power", "usd": "-" + usd(power_cost)},
        {"line": "Site opex", "usd": "-" + usd(opex)},
        {"line": "**EBITDA**", "usd": f"**{usd(ebitda)}** ({margin:.1%})"},
        {"line": "Depreciation (20-yr straight line)", "usd": "-" + usd(dep)},
        {"line": "EBIT", "usd": usd(ebitda - dep)}], ["line", "usd"]) +
        "\n## Revenue by product\n\n" + table([{"product": k, "usd": usd(v)} for k, v in rev.items() if k != "total"],
                                              ["product", "usd"]) +
        "\n## By metro\n\n" + table(sorted(per_metro.values(), key=lambda r: -r["revenue"]),
                                    ["metro", "revenue", "power_cost", "util"]) +
        "\n_List prices are illustrative; metro revenue allocates each tenant's MRR by where its cabinets sit._\n")
    return snap, md


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
          f"\nRoute-server filtering alone cuts leaked routes from {base} to {grid[2]['leaked_routes']}; the rest "
          f"reach networks through the leaker's own transit providers, which only carrier edge filtering stops "
          f"(both on: {both}).\n\n## Hijacks\n\n" + table(hj, ["filtering", "hijacked_routes"]) +
          f"\n## Switch design\n\nBuffer rule **{design.get('buffer_rule')}**, {design.get('buffer_total_mb')} MB total, "
          f"{design.get('memory')}, about {design.get('est_power_w')} W. Details in {sw['report']}.\n\n"
          f"## Standard\n\nCurrent: {std}. " +
          (f"Filed decision **#{proposal}** to adopt both filters; the founder approves it." if proposal else
           "No change proposed.") + "\n")
    return {"leak_grid": grid, "hijack": hj, "design": design, "proposal": proposal}, md


# ---------------------------------------------------------------- Chief Operator

@playbook("exec_operating_cycle", "Daily operating cycle", "exec",
          "Chief Operator: delegate the day across departments: demand, colocation, interconnection, "
          "metal, site health, energy. Summarise what happened.")
def exec_operating_cycle(ctx):
    steps = ["sales_demand_cycle", "ops_fulfill_space", "ix_fulfill_orders", "metal_fulfill_orders",
             "ops_daily_health", "energy_report"]
    rows = []
    for name in steps:
        c = ctx.child(name)
        s = c["summary"] or {}
        brief = ", ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, (list, dict)))[:160]
        rows.append({"step": name, "status": c["status"], "result": brief, "report": c["report"]})
    k = kpis()
    md = ("## Delegated steps\n\n" + table(rows, ["step", "status", "result", "report"]) +
          "\n## KPIs after the cycle\n\n" + table([{"kpi": a, "value": json.dumps(b) if isinstance(b, dict) else b}
                                                    for a, b in k.items()], ["kpi", "value"]))
    return {"steps": len(rows), "failed": sum(r["status"] != "done" for r in rows),
            "waitlisted": k["orders_waitlisted"], "mrr_usd": k["mrr_usd"]}, md


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
    for code in sorted({h["metro"] for h in hs}):
        mine = [h for h in hs if h["metro"] == code]
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
          f"\nFinance detail: {fin['report']}.\n")
    return {"filed": len(filed), "open_decisions": len(open_d), "mrr": fs.get("mrr"),
            "ebitda_margin": fs.get("ebitda_margin")}, md
