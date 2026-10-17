"""Office of the Chief Development Officer (Keystone): Site Selection (Scout), Power Procurement (Volt),
Construction PMO (Mason)."""
from __future__ import annotations

import datetime as dt
import json
import random

from .. import db, fin, growth, products
from . import engine
from .engine import decision_add, playbook
from .fin_playbooks import _sub, usd
from .playbooks import table

engine.add_department({"id": "dev", "name": "Office of the Chief Development Officer", "agent": "Keystone", "tier": "super",
                       "reports_to": "ceo", "mission": "Grow capacity ahead of demand: choose where to build, secure land and "
                       "power, deliver projects on time and budget, and prove each one earns its business case.",
                       "kpis": ["MW under construction", "projects on time", "ROI vs plan"]}, before="exec")
for _d in ({"id": "sites", "name": "Site Selection", "agent": "Scout", "mission": "Forecast when each metro sells out, write "
            "business cases for new halls and campuses, and bid for hyperscale builds.", "kpis": ["months of runway"]},
           {"id": "power", "name": "Power Procurement", "agent": "Volt", "mission": "Grid capacity and interconnection queues, "
            "power purchase agreements, bridge power, and the path to 100% renewable energy.", "kpis": ["renewable %", "hedged %"]},
           {"id": "construction", "name": "Construction PMO", "agent": "Mason", "mission": "Run every project through land, design, "
            "permits, power, construction and commissioning; manage delays and overruns; hand halls to operations.",
            "kpis": ["on-time delivery", "budget variance"]}):
    _d["reports_to"] = "dev"
    engine.add_department(_d, before="exec")


def _boot():
    growth.init()


def _boot_all():
    _boot()
    engine.add_schedules([("dev_review", "weekly tue 06:30")])


engine.BOOT.append(_boot_all)
fin.POWER_COST_HOOK = growth.power_cost
fin.CAPEX_COMMITMENTS_HOOK = growth.committed_capex
fin.JV_INCOME_HOOK = growth.jv_income_month

CLOUD_NAMES = [c[1] for c in products.CLOUDS]
KIND_LABEL = {"hall": "a new hall", "ai_hall": "an AI hall", "new_site": "a new campus", "hyperscale": "a hyperscale build"}
PPA_KIND = {"SJC": "solar", "SYD": "solar", "SIN": "solar", "ASH": "wind", "NYC": "wind", "LHR": "wind", "FRA": "wind", "NRT": "hydro"}


@playbook("dev_daily", "Construction site day", "construction",
          "Mason: daily progress on every project: spend (capitalised as construction in progress), stage gates, permit and "
          "supply-chain delays, cost overruns, waiting for utility power, and handover of finished halls and campuses.")
def dev_daily(ctx):
    _boot()
    rnd = random.Random(f"dev-{products.today()}")
    growth.capacity_snapshot()
    growth.power_step(rnd)
    r = growth.advance(rnd)
    ctx.log(f"spent {usd(r['spent'])}, {len(r['events'])} events, {len(r['live'])} live")
    md = (f"Spent {usd(r['spent'])} today across active projects.\n\n" +
          ("## Events\n\n" + table(r["events"], ["project", "event"]) if r["events"] else "") +
          ("## Handed over to operations\n\n" + table(r["live"], ["project", "hall", "site"]) if r["live"] else ""))
    return {"spent": r["spent"], "events": len(r["events"]), "live": len(r["live"])}, md


@playbook("site_selection", "Site selection", "sites",
          "Scout: months of runway per metro from absorption and the waitlist; files new halls or campuses when a metro "
          "will sell out before a project could deliver; answers hyperscale requests for proposal as joint ventures.")
def site_selection(ctx):
    _boot()
    rnd = random.Random(f"scout-{products.today()}")
    ol = growth.outlook()
    filed = []
    for o in ol:
        if not o["needs_capacity"]:
            continue
        liquid = o["type"].startswith("AI")
        site = growth.site_for_new_hall(o["metro"])
        kind = ("ai_hall" if liquid else "hall") if site else "new_site"
        cabs = 64 if liquid else growth.HALL_CABS
        budget = growth.budget_for(kind, cabs, liquid)
        case = growth.business_case(kind, o["metro"], cabs, liquid, budget, growth.lead_days(kind) // 30)
        title = (f"Build a {'liquid-cooled AI ' if liquid else ''}hall at {site}" if site else f"Open a new campus in {o['metro']}")
        did = decision_add(ctx.agent, "dev_project", title,
                           {"key": f"{o['metro']}:{'ai' if liquid else 'air'}", "kind": kind, "metro": o["metro"], "site": site,
                            "cabinets": cabs, "liquid": liquid, "budget_usd": round(budget), "irr": case["irr"],
                            "stabilised_mrr": case["stabilised_mrr"], "months_to_live": case["months_to_live"],
                            "reason": f"{o['metro']} {o['type']} sells out in {o['months_to_full']} months at {o['absorption_per_month']} "
                                      f"cabinets/month; {KIND_LABEL[kind]} takes about {growth.lead_days(kind) // 30} months"})
        filed.append({"decision": did, "what": title, "irr": case["irr"]})
    # hyperscale requests for proposal
    today = products.today()
    if rnd.random() < 0.07 and db.scalar("select count(*) from dev_rfp where status in ('open','bid')") < 2:
        grid = {g["metro"]: g for g in db.rows("select * from dev_power")}
        metro = rnd.choice(["ASH", "ASH", "FRA", "NRT", "SYD", "SJC", "LHR"])
        mw = rnd.choice([12, 18, 24, 36])
        rent = round(rnd.uniform(135, 175), 1)
        cloud = rnd.choice(CLOUD_NAMES)
        capex = mw * growth.HYPERSCALE_CAPEX_PER_MW * growth.JV_SHARE
        monthly = mw * 1000 * rent * growth.JV_SHARE * 0.62
        months = growth.lead_days("hyperscale") // 30
        cfs = [-capex / months] * months + [monthly] * (180 - months)
        cfs[-1] += monthly * 12 * 11
        irr = growth.irr(cfs)
        rid = db.execute("insert into dev_rfp(cloud,metro,mw,rent_kw,opened,decide_by,status,irr) values(?,?,?,?,?,?,'open',?)",
                         (cloud, metro, mw, rent, today, (products.today_d() + dt.timedelta(days=21)).isoformat(), irr))
        did = decision_add(ctx.agent, "hyperscale_bid", f"Bid for a {mw} MW build-to-suit for {cloud} in {metro}",
                           {"rfp": rid, "cloud": cloud, "metro": metro, "mw": mw, "rent_kw_month": rent, "vix_share": growth.JV_SHARE,
                            "vix_capex_usd": round(capex), "irr": round(irr, 4) if irr else None,
                            "grid_mw_available": round(grid[metro]["grid_mw_available"], 1), "lease_years": 15})
        db.execute("update dev_rfp set decision=? where id=?", (did, rid))
        filed.append({"decision": did, "what": f"hyperscale bid {cloud} {metro}", "irr": irr})
    won = []
    for r in db.rows("select * from dev_rfp where status='open' and decide_by<?", (today,)):
        db.execute("update dev_rfp set status='lapsed' where id=?", (r["id"],))
    for r in db.rows("select * from dev_rfp where status='bid' and decide_by<=?", (today,)):
        grid = db.one("select * from dev_power where metro=?", (r["metro"],))
        p = 0.45 + (0.15 if grid["grid_mw_available"] >= r["mw"] else -0.1)
        if rnd.random() < p:
            db.execute("update dev_rfp set status='won' where id=?", (r["id"],))
            pr = growth.start_project("hyperscale", r["metro"], mw=r["mw"], partner=r["cloud"], rent_kw=r["rent_kw"],
                                      decision=r["decision"], case={"irr": r["irr"], "stabilised_mrr": None})
            won.append({"rfp": r["id"], "cloud": r["cloud"], "metro": r["metro"], "mw": r["mw"], "project": pr["project"]})
        else:
            comp = (growth.LOSS_HOOK(r) if getattr(growth, "LOSS_HOOK", None) else None) or "a competitor"
            db.execute("update dev_rfp set status='lost', competitor=? where id=?", (comp, r["id"]))
    ctx.log(f"{len(filed)} decisions filed, {len(won)} hyperscale wins")
    md = ("## Runway by metro\n\n" + table(ol, ["metro", "type", "cabinets", "sold", "waitlisted", "free", "absorption_per_month",
                                                "months_to_full", "pipeline_cabinets", "lead_months"]) +
          ("\n## Filed for the founder\n\n" + table([{**f, "irr": f"{f['irr']:.1%}" if f["irr"] else "n/a"} for f in filed],
                                                    ["decision", "what", "irr"]) if filed else "\nNo metro needs new capacity yet.\n") +
          ("\n## Hyperscale wins\n\n" + table(won, ["cloud", "metro", "mw", "project"]) if won else "") +
          "\n## Requests for proposal\n\n" + table(db.rows("select cloud, metro, mw, rent_kw, opened, status, competitor from dev_rfp order by id desc limit 10"),
                                                   ["cloud", "metro", "mw", "rent_kw", "opened", "status", "competitor"]))
    return {"filed": len(filed), "hyperscale_won": len(won), "metros_short": sum(1 for o in ol if o["needs_capacity"])}, md


@playbook("power_procurement", "Power procurement", "power",
          "Volt: renewable coverage against the path to 100% by 2030, power purchase agreements, grid capacity and queues by "
          "metro, bridge power for projects stuck waiting on the utility.")
def power_procurement(ctx):
    _boot()
    rnd = random.Random(f"volt-{products.today()}")
    rn = growth.renewables()
    filed = []
    if rn["renewable_pct"] is not None and rn["renewable_pct"] < rn["target_pct"]:
        cands = [m for m in rn["metros"] if m["ppa_mwh"] < 0.7 * m["mwh_month"]
                 and not db.one("select 1 from dev_ppa where metro=? and start>?", (m["metro"], products.today()))]
        gap = sorted(cands, key=lambda m: -(m["mwh_month"] - m["ppa_mwh"]) * (1 - (m["renewable_pct"] or 0) / 100))
        m = gap[0] if gap else None
        kind = PPA_KIND.get(m["metro"], "wind") if m else "wind"
        need_mwh = (rn["target_pct"] - rn["renewable_pct"]) / 100 * rn["mwh_month"]
        room = (0.7 * m["mwh_month"] - m["ppa_mwh"]) if m else 0
        mw = round(min(need_mwh, room, (m["mwh_month"] if m else 0) * 0.35) / (730 * growth.CF[kind]), 1) if m else 0
        if mw >= 1:
            price = round(m["grid_price"] * 1000 * rnd.uniform(0.82, 1.06), 2)
            did = decision_add(ctx.agent, "sign_ppa", f"Sign a {mw} MW {kind} power purchase agreement for {m['metro']}",
                               {"metro": m["metro"], "mw": mw, "kind": kind, "price_mwh": price, "term_years": 12,
                                "grid_price_mwh": round(m["grid_price"] * 1000, 2),
                                "covers_mwh_month": round(mw * 730 * growth.CF[kind]),
                                "renewable_now_pct": rn["renewable_pct"], "target_pct": rn["target_pct"]})
            filed.append({"decision": did, "what": f"{mw} MW {kind} PPA in {m['metro']} at ${price}/MWh"})
    for p in db.rows("select * from dev_project where status='active' and stage='power'"):
        if p["stage_due"] < (products.today_d() - dt.timedelta(days=30)).isoformat():
            cost = round(p["mw"] * 1_800_000)
            did = decision_add(ctx.agent, "onsite_power", f"Install {p['mw']} MW of bridge power for {p['name']}",
                               {"project": p["id"], "name": p["name"], "metro": p["metro"], "mw": p["mw"], "capex_usd": cost,
                                "waiting_days": (products.today_d() - dt.date.fromisoformat(p["stage_due"])).days,
                                "stabilised_mrr": p["expected_mrr"]})
            filed.append({"decision": did, "what": f"bridge power for {p['name']}"})
    grid = db.rows("select metro, round(grid_mw_available,1) grid_mw_available, refill_mw_month, onsite_mw from dev_power order by metro")
    ppas = db.rows("select metro, mw, kind, price_mwh, start, end_date, status from dev_ppa order by id desc limit 15")
    ctx.log(f"renewable {rn['renewable_pct']}% vs {rn['target_pct']}%")
    md = (f"Renewable energy **{rn['renewable_pct']}%** of {rn['mwh_month']:,} MWh a month; target this year {rn['target_pct']}%.\n\n"
          "## By metro\n\n" + table(rn["metros"], ["metro", "mwh_month", "ppa_mwh", "renewable_pct", "grid_price", "ppa_price"]) +
          "\n## Grid capacity\n\n" + table(grid, ["metro", "grid_mw_available", "refill_mw_month", "onsite_mw"]) +
          "\n## Power purchase agreements\n\n" + table(ppas, ["metro", "mw", "kind", "price_mwh", "start", "end_date", "status"]) +
          ("\n## Filed for the founder\n\n" + table(filed, ["decision", "what"]) if filed else ""))
    return {"renewable_pct": rn["renewable_pct"], "target_pct": rn["target_pct"], "filed": len(filed)}, md


@playbook("construction_report", "Construction report", "construction",
          "Mason: every active project with stage, due date, budget, spend, delays and overruns.")
def construction_report(ctx):
    _boot()
    rows = []
    for p in db.rows("select * from dev_project order by status, stage_due"):
        h = json.loads(p["history"] or "[]")
        rows.append({"project": p["name"], "kind": p["kind"], "stage": p["stage"], "due": p["stage_due"] if p["status"] == "active" else p["live_date"],
                     "budget": usd(p["budget_usd"]), "spent": usd(p["spent_usd"] or 0),
                     "irr_at_approval": f"{p['irr']:.1%}" if p["irr"] else "n/a",
                     "delays": sum(1 for e in h if "delay" in e["event"] or "slipped" in e["event"] or "waiting" in e["event"]),
                     "overruns": sum(1 for e in h if "overrun" in e["event"]), "status": p["status"]})
    s = growth.summary()
    ctx.log(f"{s['active']} active")
    md = (f"**{s['active']} projects** active, {s['mw_under_construction']} MW under construction, {usd(s['budget_active'] - s['spent_active'])} "
          f"still to spend (vix share). {s['live']} delivered.\n\n" +
          table(rows, ["project", "kind", "stage", "due", "budget", "spent", "irr_at_approval", "delays", "overruns", "status"]))
    return s, md


@playbook("dev_review", "Development review", "dev",
          "Keystone: weekly development review. Delegates to Site Selection, Power Procurement and the Construction PMO, runs "
          "post-build ROI checks at 6, 12 and 24 months, and escalates late, over-budget or under-earning projects.")
def dev_review(ctx):
    _boot()
    sc = _sub(ctx, "site_selection")
    pw = _sub(ctx, "power_procurement")
    cr = _sub(ctx, "construction_report")
    roi = growth.roi_checks()
    esc = []
    for p in db.rows("select * from dev_project where status='active'"):
        first = json.loads(p["history"] or "[]")
        orig = db.scalar("select json_extract(detail,'$.budget_usd') from decisions where id=?", (p["decision"],)) if p["decision"] else None
        if orig and p["budget_usd"] > float(orig) * 1.1:
            esc.append(f"{p['name']} is {100 * (p['budget_usd'] / float(orig) - 1):.0f}% over its approved budget")
        late = (products.today_d() - dt.date.fromisoformat(p["created"])).days - growth.lead_days(p["kind"])
        if p["status"] == "active" and late > 60:
            esc.append(f"{p['name']} is {late} days behind its original schedule (stage: {p['stage']})")
    for r in roi:
        if r["ratio"] is not None and r["ratio"] < 0.7:
            esc.append(f"{r['project']} earns {r['ratio']:.0%} of plan after {r['months']} months")
    ctx.log(f"{len(esc)} escalations")
    md = ("Weekly development review by Keystone.\n\n"
          f"- Scout: {(sc['summary'] or {}).get('metros_short')} metro segments short of capacity, {(sc['summary'] or {}).get('filed')} decisions filed, "
          f"{(sc['summary'] or {}).get('hyperscale_won')} hyperscale wins\n"
          f"- Volt: renewable {(pw['summary'] or {}).get('renewable_pct')}% vs target {(pw['summary'] or {}).get('target_pct')}%\n"
          f"- Mason: {(cr['summary'] or {}).get('active')} active projects, {(cr['summary'] or {}).get('mw_under_construction')} MW under construction\n" +
          ("\n## ROI checks\n\n" + table(roi, ["project", "months", "plan_mrr", "actual_mrr", "ratio", "cabinets_sold"]) if roi else "") +
          "\n## Escalations\n\n" + ("".join(f"- {e}\n" for e in esc) or "- none\n"))
    return {"escalations": esc, "active": (cr["summary"] or {}).get("active"), "renewable_pct": (pw["summary"] or {}).get("renewable_pct"),
            "roi_checks": len(roi)}, md


# ---------------------------------------------------------------- decisions

def _eff_dev(det, d):
    return growth.start_project(det["kind"], det["metro"], site=det.get("site"), cabinets=int(det.get("cabinets", growth.HALL_CABS)),
                                liquid=bool(det.get("liquid")), decision=d["id"],
                                case={"irr": det.get("irr"), "stabilised_mrr": det.get("stabilised_mrr")})


def _eff_build_hall(det, d):
    """Atlas's waitlist-driven build_hall decisions now start a construction project instead of a hall appearing at once."""
    site = det["site"]
    metro = db.scalar("select metro from sites where id=?", (site,)) or site[:3]
    n = db.scalar("select count(*) from halls where site=?", (site,)) or 0
    liquid = bool(det.get("liquid"))
    kind = ("ai_hall" if liquid else "hall") if n < growth.SITE_MAX_HALLS else "new_site"
    return growth.start_project(kind, metro, site=site if kind != "new_site" else None, cabinets=int(det.get("cabinets", growth.HALL_CABS)),
                                liquid=liquid, decision=d["id"])


def _eff_bid(det, d):
    db.execute("update dev_rfp set status='bid', decide_by=? where id=?",
               ((products.today_d() + dt.timedelta(days=30)).isoformat(), det["rfp"]))
    return {"rfp": det["rfp"], "status": "bid submitted; award in 30 days"}


def _eff_ppa(det, d):
    start = (products.today_d() + dt.timedelta(days=60)).isoformat()
    end = products.add_months(dt.date.fromisoformat(start), 12 * int(det.get("term_years", 12))).isoformat()
    pid = db.execute("insert into dev_ppa(metro,mw,kind,price_mwh,term_years,start,end_date,status,decision) values(?,?,?,?,?,?,?,'active',?)",
                     (det["metro"], det["mw"], det["kind"], det["price_mwh"], det.get("term_years", 12), start, end, d["id"]))
    return {"ppa": pid, "starts": start}


def _eff_onsite(det, d):
    db.execute("update dev_power set onsite_mw=onsite_mw+? where metro=?", (det["mw"], det["metro"]))
    if fin.opened():
        fin.post(products.today(), [("ppe", det["capex_usd"]), ("cash", -det["capex_usd"])], f"bridge power: {det['name']}", f"decision:{d['id']}")
    return {"metro": det["metro"], "onsite_mw": det["mw"]}


def _declined(d, approved, effect):
    if d["kind"] == "hyperscale_bid" and not approved:
        db.execute("update dev_rfp set status='declined' where id=?", (d["detail"]["rfp"],))


engine.ON_DECIDE.append(_declined)
engine.DECISION_EFFECTS.update({"dev_project": _eff_dev, "build_hall": _eff_build_hall, "hyperscale_bid": _eff_bid,
                                "sign_ppa": _eff_ppa, "onsite_power": _eff_onsite})
engine.DEDUPE_KEY.update({"dev_project": "key", "hyperscale_bid": "rfp", "sign_ppa": "metro", "onsite_power": "project"})


def _leverage_after(capex):
    last = fin.closed_periods(1)
    if not last or not last[0]["metrics"]["net_debt_to_ebitda"]:
        return None
    m, i = last[0]["metrics"], last[0]["income"]
    return (m["net_debt"] + capex) / (i["adj_ebitda"] * 12) if i["adj_ebitda"] > 0 else None


def _rec_dev(d):
    det = d["detail"]
    irr = det.get("irr")
    lev = _leverage_after(det.get("budget_usd", 0))
    if irr is None or irr < growth.HURDLE_IRR:
        return "hold", f"IRR {irr:.1%} is below the {growth.HURDLE_IRR:.0%} hurdle" if irr is not None else "no positive IRR"
    if lev and lev > 6:
        return "hold", f"IRR {irr:.1%} clears the hurdle but leverage would reach {lev:.1f}x; fund it first"
    return "approve", f"{det['reason']}; IRR {irr:.1%} vs {growth.HURDLE_IRR:.0%} hurdle" + (f", leverage after {lev:.1f}x" if lev else "")


def _rec_build_hall(d):
    det = d["detail"]
    metro = det["site"][:3]
    liquid = bool(det.get("liquid"))
    cabs = int(det.get("cabinets", growth.HALL_CABS))
    kind = "ai_hall" if liquid else "hall"
    budget = growth.budget_for(kind, cabs, liquid)
    case = growth.business_case(kind, metro, cabs, liquid, budget, growth.lead_days(kind) // 30)
    pipe = db.scalar("select count(*) from dev_project where status='active' and metro=? and liquid=?", (metro, int(liquid)))
    if pipe:
        return "reject", f"{pipe} project(s) already under way in {metro}; Scout's pipeline covers this"
    if (case["irr"] or 0) >= growth.HURDLE_IRR:
        return "approve", f"{det['reason']}; IRR {case['irr']:.1%}; starts a {growth.lead_days(kind) // 30}-month project"
    return "hold", f"IRR {(case['irr'] or 0):.1%} below the hurdle at today's absorption"


def _rec_bid(d):
    det = d["detail"]
    if (det.get("irr") or 0) >= 0.09:
        return "approve", f"IRR {det['irr']:.1%} on {usd(det['vix_capex_usd'])} for a 15-year {det['cloud']} lease" + (
            "" if det["grid_mw_available"] >= det["mw"] else f"; power is tight ({det['grid_mw_available']} MW free)")
    return "reject", f"IRR {(det.get('irr') or 0):.1%} is too thin for a single-tenant build"


def _rec_ppa(d):
    det = d["detail"]
    prem = det["price_mwh"] / det["grid_price_mwh"] - 1
    if prem <= 0.05:
        return "approve", (f"{'saves' if prem < 0 else 'costs'} {abs(prem):.0%} vs grid, fixes the price for 12 years, lifts renewable "
                           f"share toward {det['target_pct']}%")
    return "hold", f"{prem:.0%} premium over the grid; ask Volt to re-tender"


def _rec_onsite(d):
    det = d["detail"]
    if det["waiting_days"] >= 30:
        return "approve", f"project has waited {det['waiting_days']} days for the utility; {usd(det['capex_usd'])} brings it forward"
    return "hold", "the utility may still deliver; revisit in two weeks"


engine.RECOMMENDERS.update({"dev_project": _rec_dev, "build_hall": _rec_build_hall, "hyperscale_bid": _rec_bid, "sign_ppa": _rec_ppa,
                            "onsite_power": _rec_onsite})
engine.DAILY_STEPS.append("dev_daily")
engine.WEEKLY_STEPS.append("dev_review")
engine.KPI_HOOKS.append(growth.kpi_hook)


def _board(r):
    s = r["summary"] or {}
    return (f"- **Keystone (development):** {s.get('active')} projects active, renewable energy {s.get('renewable_pct')}%, "
            f"{s.get('roi_checks')} ROI checks this week. Report: {r['report']}\n",
            [f"Development escalation from Keystone: {e}" for e in s.get("escalations", [])])


engine.BOARD_CHIEFS.append(("dev_review", _board))
