"""Office of the Chief Commercial Officer (Summit): Sales (Broker) and Account Management (Keeper) move here,
plus Competitive Intelligence (Lens), Customer Experience & Portal (Portico) and Marketplace & Partners (Nexus).
Compute (Forge) runs the bare-metal sunset."""
from __future__ import annotations

import random

from .. import crm, db, fin, market, products, world
from . import engine
from .engine import decision_add, playbook
from .fin_playbooks import _sub, usd
from .playbooks import table

engine.add_department({"id": "cco", "name": "Office of the Chief Commercial Officer", "agent": "Summit", "tier": "super",
                       "reports_to": "ceo", "mission": "Win in the market: sales, renewals, competitive position, the customer "
                       "experience and portal, the marketplace and partners, and the product portfolio.",
                       "kpis": ["win rate", "NPS", "ecosystem density"]}, before="exec")
for _d in ({"id": "intel", "name": "Competitive Intelligence", "agent": "Lens", "mission": "Track competitors' prices, builds and "
            "outages metro by metro; explain every win and loss.", "kpis": ["win rate", "losses to price"]},
           {"id": "portal", "name": "Customer Experience & Portal", "agent": "Portico", "mission": "The customer portal and API, "
            "self-service orders and requests, letters of authorization, NPS and CSAT.", "kpis": ["NPS", "portal CSAT"]},
           {"id": "ecosystem", "name": "Marketplace & Partners", "agent": "Nexus", "mission": "Grow ecosystem density: marketplace "
            "listings that connect buyers to sellers, and the partner channel.", "kpis": ["interconnections per customer", "partner MRR"]}):
    _d["reports_to"] = "cco"
    engine.add_department(_d, before="exec")
for _id in ("sales", "accounts"):
    engine.DEPT[_id]["reports_to"] = "cco"


def _boot():
    market.init()


def _boot_all():
    _boot()
    engine.add_schedules([("cco_review", "weekly tue 07:10")])


engine.BOOT.append(_boot_all)


@playbook("market_daily", "Customer day", "portal",
          "Portico: customers raise portal requests (letters of authorization, remote hands, access, reports) that the team "
          "works; quarterly NPS surveys; marketplace buyers connect to sellers; the bare-metal migration moves forward.")
def market_daily(ctx):
    _boot()
    rnd = random.Random(f"mkt-{products.today()}")
    pa = market.portal_activity(rnd)
    sv = market.surveys(rnd)
    leads = market.marketplace_leads(rnd)
    ss = market.sunset_step(rnd)
    ctx.log(f"{pa['new_requests']} portal requests, {leads} marketplace leads")
    md = (f"Portal: {pa['new_requests']} new requests, {pa['done']} done, {pa['failed']} failed. Marketplace: {leads} new "
          f"cross-connect deals. " + (f"NPS survey: {sv['nps_responses']} responses. " if sv.get("nps_responses") else "") +
          (f"Metal migration: {ss.get('migrated', 0)} moved, {ss.get('lost', 0)} lost, {ss.get('decommissioned', 0)} decommissioned." if ss else "") + "\n")
    return {**pa, "marketplace_leads": leads, **sv, **{f"metal_{k}": v for k, v in (ss or {}).items()}}, md


@playbook("market_intel", "Competitive intelligence", "intel",
          "Lens: competitor moves this week (price cuts, new capacity, outages), our price position by metro, and win/loss "
          "analysis for the last 30 days: why we lose, and to whom.")
def market_intel(ctx):
    _boot()
    rnd = random.Random(f"intel-{products.today()}")
    moves = market.competitor_moves(rnd)
    wl = market.winloss(30)
    pos = []
    for m in db.rows("select code from metros"):
        cs = market.competitors_in(m["code"])
        pos.append({"metro": m["code"], "rivals": ", ".join(c["name"] for c in cs) or "none",
                    "cheapest_rival_price": min((c["metro_price"] for c in cs), default=None),
                    "ecosystem_density": market._density().get(m["code"])})
    ctx.log(f"{len(moves)} moves, win rate {wl['win_rate']}")
    md = (f"Win rate over 30 days **{(wl['win_rate'] or 0):.0%}** on {wl['deals']} decided deals.\n\n## Competitor moves\n\n" +
          table(moves, ["competitor", "metro", "event", "detail"]) + "\n## Why we lose\n\n" + table(wl["loss_reasons"], ["reason", "n"]) +
          "\n## Who we lose to\n\n" + table(wl["lost_to"], ["competitor", "n"]) + "\n## By channel\n\n" +
          table(wl["by_channel"], ["channel", "won", "lost", "win_rate"]) + "\n## Price position (vix list = 1.00)\n\n" +
          table(pos, ["metro", "rivals", "cheapest_rival_price", "ecosystem_density"]))
    return {"moves": len(moves), "win_rate": wl["win_rate"], "top_loss_reason": wl["loss_reasons"][0]["reason"] if wl["loss_reasons"] else None,
            "lost_to_price_pct": round(sum(r["n"] for r in wl["loss_reasons"] if r["reason"] == "price") / wl["lost"], 3) if wl["lost"] else 0}, md


@playbook("customer_voice", "Customer voice", "portal",
          "Portico: NPS and what drives detractors, portal adoption and CSAT, request volumes by kind.")
def customer_voice(ctx):
    _boot()
    n = market.nps()
    adoption = db.scalar("select round(100.0*sum(portal)/count(*),1) from mkt_customer m join contracts c on c.tenant=m.tenant where c.status='active'")
    reqs = db.rows("select kind, count(*) n, round(avg(csat),2) csat, sum(status='failed') failed from mkt_request group by kind")
    by_p = db.rows("select m.persona, count(*) n, round(avg(m.nps),1) avg_score from mkt_customer m join contracts c on c.tenant=m.tenant"
                   " where c.status='active' group by m.persona order by n desc")
    ctx.log(f"NPS {n.get('nps')}")
    md = (f"NPS **{n.get('nps')}** ({n.get('responses')} responses: {n.get('promoters')} promoters, {n.get('detractors')} detractors). "
          f"Portal adoption {adoption}% of active customers; portal CSAT {n.get('portal_csat')} / 5.\n\n## What detractors cite\n\n" +
          table(n.get("detractor_drivers") or [], ["driver", "n"]) + "\n## Requests\n\n" + table(reqs, ["kind", "n", "csat", "failed"]) +
          "\n## Personas\n\n" + table(by_p, ["persona", "n", "avg_score"]))
    return {"nps": n.get("nps"), "portal_adoption_pct": adoption, "portal_csat": n.get("portal_csat")}, md


@playbook("ecosystem_review", "Ecosystem review", "ecosystem",
          "Nexus: ecosystem density by metro (networks, exchange members, interconnections per customer), the busiest "
          "marketplace listings, and partner-sourced business.")
def ecosystem_review(ctx):
    _boot()
    eco = market.ecosystem()
    li = market.listings()[:12]
    pa = db.rows("select name, kind, tier, commission_pct, deals_won, round(mrr_sourced) mrr_sourced from mkt_partner order by mrr_sourced desc")
    ctx.log("ecosystem reviewed")
    md = ("## Ecosystem density\n\n" + table(sorted(eco, key=lambda r: -r["density"]),
                                             ["metro", "networks", "exchange_members", "interconnections", "customers", "per_customer", "density"]) +
          "\n_Density raises win rates and renewals for customers who value the ecosystem._\n\n## Marketplace\n\n" +
          table(li, ["title", "category", "seller", "metros", "connections"]) + "\n## Partners\n\n" +
          table(pa, ["name", "kind", "tier", "commission_pct", "deals_won", "mrr_sourced"]))
    return {"densest_metro": max(eco, key=lambda r: r["density"])["metro"] if eco else None, "listings": len(market.listings()),
            "partner_mrr": round(sum(p["mrr_sourced"] or 0 for p in pa))}, md


@playbook("metal_sunset_review", "Bare-metal sunset", "metal",
          "Forge: the bare-metal product's revenue, margin and customers; proposes retiring it, then tracks the migration of "
          "each customer to colocation or a partner cloud until end of life.")
def metal_sunset_review(ctx):
    _boot()
    pos = market.metal_position()
    s = db.setting("metal_sunset") or {}
    filed = None
    if not s.get("announced") and pos["share_of_revenue"] < 0.06:
        filed = decision_add(ctx.agent, "sunset_metal", "Retire the bare-metal product",
                             {"metal_mrr": round(pos["metal_mrr"]), "share_of_revenue": pos["share_of_revenue"],
                              "customers": pos["customers"], "servers": pos["servers_allocated"], "gross_margin": pos["gross_margin"],
                              "plan": "stop new sales now; offer each customer colocation or a partner cloud; end of life in 180 days"})
    mig = db.rows("select m.*, t.name from mkt_migration m join tenants t on t.id=m.tenant order by m.status, m.decide_by")
    before = sum(m["mrr_before"] or 0 for m in mig)
    kept = sum(m["mrr_after"] or 0 for m in mig if m["status"] == "migrated")
    decided = sum(m["mrr_before"] or 0 for m in mig if m["status"] != "offered")
    ctx.log(f"metal share {pos['share_of_revenue']:.1%}")
    md = (f"Bare metal earns {usd(pos['metal_mrr'])}/month ({pos['share_of_revenue']:.1%} of revenue) at about {pos['gross_margin']:.0%} gross "
          f"margin from {pos['customers']} customers.\n\n" +
          (f"**Sunset announced {s['announced']}, end of life {s['eol']}.** " if s.get("announced") else "") +
          (f"Filed decision #{filed} to retire it.\n\n" if filed else "") +
          (f"Migration: {sum(1 for m in mig if m['status'] == 'migrated')} moved, {sum(1 for m in mig if m['status'] == 'lost')} lost, "
           f"{sum(1 for m in mig if m['status'] == 'offered')} still deciding. Revenue kept on decided accounts: "
           f"{(kept / decided if decided else 0):.0%} of {usd(decided)}.\n\n" + table(mig[:30], ["name", "servers", "mrr_before", "offer", "decide_by", "status", "mrr_after", "note"])
           if mig else ""))
    return {"metal_mrr": round(pos["metal_mrr"]), "announced": s.get("announced"), "retention": round(kept / decided, 3) if decided else None,
            "filed": filed}, md


@playbook("cco_review", "Commercial review", "cco",
          "Summit: weekly commercial review. Delegates to Competitive Intelligence, Customer Experience, Marketplace & Partners "
          "and the bare-metal sunset; escalates falling win rates, price-driven losses, low NPS and poor migration retention.")
def cco_review(ctx):
    _boot()
    mi = _sub(ctx, "market_intel")
    cv = _sub(ctx, "customer_voice")
    ec = _sub(ctx, "ecosystem_review")
    ms = _sub(ctx, "metal_sunset_review")
    s1, s2, s3, s4 = (mi["summary"] or {}), (cv["summary"] or {}), (ec["summary"] or {}), (ms["summary"] or {})
    esc = []
    if s1.get("win_rate") is not None and s1["win_rate"] < 0.5:
        esc.append(f"win rate {s1['win_rate']:.0%} over 30 days")
    if (s1.get("lost_to_price_pct") or 0) > 0.4:
        esc.append(f"{s1['lost_to_price_pct']:.0%} of losses are on price")
    if s2.get("nps") is not None and s2["nps"] < 20:
        esc.append(f"NPS {s2['nps']}")
    if s4.get("retention") is not None and s4["retention"] < 0.5:
        esc.append(f"bare-metal migration keeps only {s4['retention']:.0%} of revenue")
    ctx.log(f"{len(esc)} escalations")
    md = ("Weekly commercial review by Summit.\n\n"
          f"- Lens: win rate {(s1.get('win_rate') or 0):.0%}, top loss reason {s1.get('top_loss_reason')}, {s1.get('moves')} competitor moves\n"
          f"- Portico: NPS {s2.get('nps')}, portal adoption {s2.get('portal_adoption_pct')}%, CSAT {s2.get('portal_csat')}\n"
          f"- Nexus: densest ecosystem {s3.get('densest_metro')}, {s3.get('listings')} marketplace listings, partner MRR {usd(s3.get('partner_mrr') or 0)}\n"
          f"- Forge: bare metal {usd(s4.get('metal_mrr') or 0)}/mo; " + (f"sunset announced {s4['announced']}" if s4.get("announced") else "not retired") + "\n"
          "\n## Escalations\n\n" + ("".join(f"- {e}\n" for e in esc) or "- none\n"))
    return {"escalations": esc, "win_rate": s1.get("win_rate"), "nps": s2.get("nps")}, md


def _eff_sunset(det, d):
    return market.start_sunset(180)


def _rec_sunset(d):
    det = d["detail"]
    return "approve", (f"{det['share_of_revenue']:.1%} of revenue at {det['gross_margin']:.0%} margin; capacity and capital are better "
                       f"spent on AI halls; {det['customers']} customers get a migration offer")


engine.DECISION_EFFECTS["sunset_metal"] = _eff_sunset
engine.RECOMMENDERS["sunset_metal"] = _rec_sunset
engine.DEDUPE_KEY["sunset_metal"] = None
engine.DAILY_STEPS.append("market_daily")
engine.WEEKLY_STEPS.append("cco_review")
engine.KPI_HOOKS.append(market.kpi_hook)
engine.DEMAND_HOOKS.append(market.demand_hook)
products.RENEWAL_ADJUST.append(market.renewal_adjust)
crm.WIN_HOOK = market.win_hook
crm.ON_OPPORTUNITY.insert(0, lambda oid: market.on_opportunity(oid) if db.one("select name from sqlite_master where name='mkt_opp_channel'") else None)
fin.PRESSURE_HOOK = market.pressure
world.ON_NEW_TENANT.append(lambda tid, rnd: market.add_customer(tid, rnd) if db.one(
    "select name from sqlite_master where name='mkt_customer'") else None)


def _board(r):
    s = r["summary"] or {}
    return (f"- **Summit (commercial):** win rate {(s.get('win_rate') or 0):.0%}, NPS {s.get('nps')}. Report: {r['report']}\n",
            [f"Commercial escalation from Summit: {e}" for e in s.get("escalations", [])])


engine.BOARD_CHIEFS.append(("cco_review", _board))
