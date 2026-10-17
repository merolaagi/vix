"""Market and ecosystem: competitors, customer personas, win/loss, the customer portal and its API,
the marketplace and partner channel, ecosystem density (the network effect), NPS and CSAT, and the
bare-metal product sunset. Competitors, partners and customers are invented; all numbers are simulated."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import secrets

from . import crm, db, products, world

SCHEMA = """
create table if not exists mkt_competitor (
  id integer primary key, name text, style text, price_index real, reputation real, aggressiveness real, presence text
);
create table if not exists mkt_competitor_event (
  id integer primary key, sim_date text, competitor text, metro text, event text, detail text
);
create table if not exists mkt_customer (
  tenant integer primary key, persona text, nps integer, nps_date text, portal integer default 0, api_key_hash text,
  key_issued text, source text
);
create table if not exists mkt_winloss (
  id integer primary key, opp text, tenant integer, kind text, metro text, sim_date text, outcome text, competitor text,
  reason text, p_win real, discount real, channel text, persona text
);
create table if not exists mkt_opp_channel (opp text primary key, channel text, partner integer);
create table if not exists mkt_partner (
  id integer primary key, name text, kind text, tier text, commission_pct real, deals_won integer default 0, mrr_sourced real default 0
);
create table if not exists mkt_listing (
  id integer primary key, tenant integer, title text, category text, metros text, created text
);
create table if not exists mkt_survey (
  id integer primary key, tenant integer, sim_date text, kind text, score integer, driver text
);
create table if not exists mkt_request (
  id integer primary key, tenant integer, kind text, detail text, status text, created text, done text, channel text,
  result text, csat integer
);
create table if not exists mkt_migration (
  tenant integer primary key, servers integer, mrr_before real, offer text, decide_by text, status text, mrr_after real, note text
);
"""

COMPETITORS = [  # name, style, price index vs vix list, reputation, aggressiveness, metros with presence (capacity score)
    ("Kestrel Colo", "price fighter", 0.84, 0.55, 0.8, {"ASH": .9, "SJC": .6, "NYC": .5, "SYD": .5, "SIN": .3}),
    ("Monolith Data Centers", "wholesale and hyperscale", 0.92, 0.7, 0.5, {"ASH": 1.0, "FRA": .8, "SIN": .6, "NRT": .6, "LHR": .5}),
    ("Arcway Interconnect", "interconnection-dense retail", 1.02, 0.8, 0.4, {"LHR": .9, "FRA": .9, "NYC": .8, "SJC": .6, "NRT": .4})]
PERSONAS = {  # kind weight multipliers, price sensitivity, ecosystem weight, SLA weight, portal affinity
    "cloud-first enterprise": ({"cloud_connection": 2.2, "vc": 1.6, "cross_connect": 1.2, "edge_device": 1.6, "cabinets": .8}, .5, .7, .5, .6),
    "latency-sensitive trader": ({"port": 1.8, "cross_connect": 1.8, "vc": 1.3, "cabinets": 1.1, "metal": .3}, .2, .6, .9, .3),
    "content distributor": ({"ix_port": 2.4, "internet_access": 2.2, "cabinets": 1.2, "port": 1.2}, .8, .5, .4, .7),
    "AI builder": ({"ai_cabinets": 4.0, "metal": 1.8, "cloud_connection": 1.4, "cabinets": .6}, .5, .3, .6, .5),
    "network carrier": ({"port": 1.8, "cross_connect": 2.0, "ix_port": 1.6, "vc": 1.2, "metal": .2}, .3, .9, .5, .4),
    "cost cutter": ({"cabinets": 1.6, "cross_connect": .7, "cloud_connection": .6, "edge_device": .5}, .95, .2, .4, .3)}
PERSONA_BY_KIND = {"carrier": {"network carrier": 6, "content distributor": 1},
                   "cloud": {"cloud-first enterprise": 2, "network carrier": 2, "AI builder": 2},
                   "content": {"content distributor": 5, "cost cutter": 1, "AI builder": 1},
                   "enterprise": {"cloud-first enterprise": 5, "cost cutter": 2, "AI builder": 1},
                   "financial": {"latency-sensitive trader": 5, "cloud-first enterprise": 2}}
PARTNERS = [("Northbeam Systems", "reseller", "gold", 0.10), ("Gridline Integrators", "reseller", "silver", 0.08),
            ("Pallas Consulting", "referral", "gold", 0.06), ("Ferro Networks", "technology", "silver", 0.05),
            ("Lumen Peak Advisory", "referral", "silver", 0.05), ("Quayside IT", "reseller", "bronze", 0.07)]
LISTING = {"carrier": ("IP transit and wavelengths", "network"), "cloud": ("Private cloud on-ramp", "cloud"),
           "content": ("Content delivery and caching", "content"), "financial": ("Market data distribution", "financial"),
           "enterprise": ("Managed SD-WAN hub", "enterprise")}


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
    if not db.scalar("select count(*) from mkt_competitor"):
        for n, st, pi, rep, ag, pres in COMPETITORS:
            db.execute("insert into mkt_competitor(name,style,price_index,reputation,aggressiveness,presence) values(?,?,?,?,?,?)",
                       (n, st, pi, rep, ag, json.dumps({m: {"cap": c, "price": pi} for m, c in pres.items()})))
        for n, k, t, c in PARTNERS:
            db.execute("insert into mkt_partner(name,kind,tier,commission_pct) values(?,?,?,?)", (n, k, t, c))
    ensure_customers(random.Random(11))
    if not db.scalar("select count(*) from mkt_listing"):
        seed_listings(random.Random(12))


def ensure_customers(rnd):
    have = {r["tenant"] for r in db.rows("select tenant from mkt_customer")}
    for t in db.rows("select id, kind from tenants"):
        if t["id"] not in have:
            add_customer(t["id"], rnd, t["kind"])


def add_customer(tid, rnd, kind=None, source="direct"):
    kind = kind or db.scalar("select kind from tenants where id=?", (tid,))
    opts = PERSONA_BY_KIND.get(kind, {"cloud-first enterprise": 1})
    persona = rnd.choices(list(opts), list(opts.values()))[0]
    db.execute("insert or ignore into mkt_customer(tenant,persona,nps,portal,source) values(?,?,?,?,?)",
               (tid, persona, rnd.choices([5, 6, 7, 8, 9, 10], [1, 1, 2, 3, 3, 2])[0], 1 if rnd.random() < PERSONAS[persona][4] else 0, source))


def persona(tid) -> str:
    return db.scalar("select persona from mkt_customer where tenant=?", (tid,)) or "cloud-first enterprise"


def seed_listings(rnd):
    for t in db.rows("select id, kind, name, home_metro from tenants where kind in ('carrier','cloud','content','financial')"):
        if rnd.random() < 0.45:
            title, cat = LISTING[t["kind"]]
            db.execute("insert into mkt_listing(tenant,title,category,metros,created) values(?,?,?,?,?)",
                       (t["id"], f"{t['name']}: {title}", cat, t["home_metro"], products.today()))


# ---------------------------------------------------------------- ecosystem density


def ecosystem() -> list[dict]:
    """Per metro: networks (tenants with ports there), interconnections, interconnections per customer, and a 0-1
    density score relative to the densest metro. Density is what makes customers stay and pay more."""
    rows = []
    for m in db.rows("select code from metros"):
        code = m["code"]
        nets = db.scalar("select count(distinct tenant) from ports where substr(site,1,3)=?", (code,)) or 0
        xc = db.scalar("select count(*) from cross_connects where substr(site,1,3)=?", (code,)) or 0
        vc = db.scalar("select count(*) from virtual_circuits v join ports p on p.id=v.a_port where substr(p.site,1,3)=?", (code,)) or 0
        cl = db.scalar("select count(*) from cloud_connections where metro=?", (code,)) or 0
        ix = db.scalar("select count(distinct tenant) from ix_ports where metro=?", (code,)) or 0
        cust = db.scalar("select count(distinct p.tenant) from placements p join halls h on h.id=p.hall join sites s on s.id=h.site"
                         " where s.metro=?", (code,)) or 1
        ic = xc + vc + cl
        rows.append({"metro": code, "networks": nets, "exchange_members": ix, "interconnections": ic, "customers": cust,
                     "per_customer": round(ic / cust, 1)})
    best = max((r["per_customer"] * math.log1p(r["networks"]) for r in rows), default=1) or 1
    for r in rows:
        r["density"] = round(r["per_customer"] * math.log1p(r["networks"]) / best, 3)
    return rows


def _density():
    cache = db.setting("eco_density") or {}
    if cache.get("date") != products.today():
        cache = {"date": products.today(), "m": {r["metro"]: r["density"] for r in ecosystem()}}
        db.set_setting("eco_density", cache)
    return cache["m"]


# ---------------------------------------------------------------- competition and win/loss


MARKET_BIAS = -0.45


def competitors_in(metro) -> list[dict]:
    out = []
    for c in db.rows("select * from mkt_competitor"):
        pres = json.loads(c["presence"]).get(metro)
        if pres and pres["cap"] > 0:
            out.append({**c, "cap": pres["cap"], "metro_price": pres["price"]})
    return out


def _metro_of(params, tid):
    m = params.get("metro") or params.get("a_metro") or (params.get("site") or "")[:3]
    return m or db.scalar("select home_metro from tenants where id=?", (tid,))


def pressure(t, kind, metro):
    """Deal Desk hook: how much extra discount competition calls for here, and who is bidding."""
    init()
    comps = competitors_in(metro)
    if not comps:
        return 0.0, None
    c = max(comps, key=lambda x: x["cap"] * (1.1 - x["metro_price"]) * x["aggressiveness"])
    sens = PERSONAS[persona(t["id"])][1]
    gap = max(0.0, 1.0 - c["metro_price"])
    return round(sens * gap * 0.3 + 0.01 * c["aggressiveness"], 3), c["name"]


def _free_cabinets(metro, liquid):
    return sum(h["cabinets_total"] * 0.92 - h["cabinets_sold"] for h in world.halls(metro)
               if (h.get("cooling_type") == "liquid") == liquid)


def win_hook(o, rnd):
    """Who wins the deal: vix's price after discount, ecosystem density and service reputation against the
    strongest competitor in the metro, through the customer persona's eyes. Capacity shortfalls lose deals."""
    init()
    tid = int(o["AccountId"][3:])
    params = json.loads(o["Params__c"] or "{}")
    kind = o["Product__c"]
    metro = _metro_of(params, tid)
    pr = persona(tid)
    _, sens, eco_w, sla_w, _ = PERSONAS[pr]
    ch = db.one("select * from mkt_opp_channel where opp=?", (o["Id"],)) or {"channel": "direct", "partner": None}
    q = db.one("select discount_pct from fin_quote where opp=?", (o["Id"],)) if db.one(
        "select name from sqlite_master where name='fin_quote'") else None
    disc = q["discount_pct"] if q else 0.0
    idx = db.scalar("select price_index from tenants where id=?", (tid,)) or 1.0
    our_price = idx * (1 - disc)
    dens = _density().get(metro, 0.5)
    sla = products.sla_window(30)["sla_pct"] or 95
    nk = db.setting("net_kpis") or {}
    rep = 0.5 + (sla - 93) / 20 + ((nk.get("availability_pct") or 99.99) - 99.95) * 2
    comps = competitors_in(metro)
    reason, comp_name, p = None, None, 0.9 if ch["channel"] == "portal" else 0.62
    if comps and ch["channel"] != "portal":
        c = max(comps, key=lambda x: x["cap"] * (1.2 - x["metro_price"]) + x["reputation"] * 0.3)
        comp_name = c["name"]
        u_us = -sens * our_price * 3 + eco_w * dens * 1.5 + sla_w * rep
        u_them = -sens * c["metro_price"] * 3 + eco_w * c["cap"] * 0.6 + sla_w * c["reputation"]
        p = 1 / (1 + math.exp(-(MARKET_BIAS + 1.8 * (u_us - u_them))))
        terms = {"price": -sens * (our_price - c["metro_price"]) * 3, "ecosystem": eco_w * (dens * 1.5 - c["cap"] * 0.6),
                 "reputation": sla_w * (rep - c["reputation"])}
        reason = min(terms, key=terms.get)
    if kind in ("cabinets", "ai_cabinets") and _free_cabinets(metro, kind == "ai_cabinets") < int(params.get("cabinets", 1)):
        p *= 0.35
        reason = "no capacity"
    if ch["channel"] == "partner":
        p = min(0.95, p + 0.08)
    won = rnd.random() < p
    if not won and (not reason or rnd.random() < 0.2):
        reason = rnd.choice(["timing", "budget cut", "project cancelled"]) if not reason or rnd.random() < 0.5 else reason
    db.execute("insert into mkt_winloss(opp,tenant,kind,metro,sim_date,outcome,competitor,reason,p_win,discount,channel,persona)"
               " values(?,?,?,?,?,?,?,?,?,?,?,?)", (o["Id"], tid, kind, metro, products.today(), "won" if won else "lost",
                                                    comp_name, None if won else reason, round(p, 3), disc, ch["channel"], pr))
    if won and ch["channel"] == "partner" and ch["partner"]:
        pa = db.one("select * from mkt_partner where id=?", (ch["partner"],))
        db.execute("update mkt_partner set deals_won=deals_won+1, mrr_sourced=mrr_sourced+? where id=?", (o["MRR__c"] or 0, pa["id"]))
        from . import fin
        if fin.opened():
            fee = round((o["MRR__c"] or 0) * 12 * pa["commission_pct"], 2)
            fin.post(products.today(), [("sga", fee), ("cash", -fee)], f"partner commission {pa['name']} on {o['Id']}", f"partner:{pa['id']}")
    return won, {"p_win": p, "competitor": comp_name, "reason": reason}


def on_opportunity(oid):
    """Route a new opportunity: self-serve through the portal, sourced by a partner, or direct sales."""
    init()
    o = db.one("select * from crm_opportunity where Id=?", (oid,))
    tid = int(o["AccountId"][3:])
    rnd = random.Random(oid)
    mc = db.one("select * from mkt_customer where tenant=?", (tid,))
    if not mc:
        add_customer(tid, rnd)
        mc = db.one("select * from mkt_customer where tenant=?", (tid,))
    ch, partner = "direct", None
    from . import harness
    if o["Type"] == "Expansion" and mc["portal"] and (o["MRR__c"] or 0) < float(harness.get("portal.self_serve_max_mrr")) and rnd.random() < 0.55:
        ch = "portal"
        db.execute("update crm_opportunity set CloseDate=? where Id=?", ((products.today_d() + dt.timedelta(days=1)).isoformat(), oid))
    elif o["Type"] == "New Business" and rnd.random() < 0.18:
        ch = "partner"
        partner = rnd.choice(db.rows("select id from mkt_partner"))["id"]
    db.execute("insert or replace into mkt_opp_channel(opp,channel,partner) values(?,?,?)", (oid, ch, partner))


def demand_hook(t, weights):
    pr = persona(t["id"]) if db.one("select name from sqlite_master where name='mkt_customer'") else None
    w = dict(weights)
    if pr:
        for k, m in PERSONAS[pr][0].items():
            if k in w:
                w[k] *= m
    if (db.setting("metal_sunset") or {}).get("announced"):
        w.pop("metal", None)
    return w


def renewal_adjust(tid):
    if not db.one("select name from sqlite_master where name='mkt_customer'"):
        return 0.0, {}
    mc = db.one("select * from mkt_customer where tenant=?", (tid,))
    if not mc:
        return 0.0, {}
    pr = PERSONAS[mc["persona"]]
    home = db.scalar("select home_metro from tenants where id=?", (tid,))
    dens = _density().get(home, 0.5)
    comps = competitors_in(home)
    idx = db.scalar("select price_index from tenants where id=?", (tid,)) or 1.0
    cheapest = min((c["metro_price"] for c in comps), default=idx)
    nps = mc["nps"] if mc["nps"] is not None else 7
    dp = (nps - 7) * 0.012 + pr[2] * (dens - 0.5) * 0.06 - pr[1] * max(0.0, idx - cheapest) * 0.15
    return dp, {"persona": mc["persona"], "nps": nps, "ecosystem_density": dens, "cheapest_rival_price": cheapest}


# ---------------------------------------------------------------- competitor moves


def competitor_moves(rnd) -> list[dict]:
    out = []
    for c in db.rows("select * from mkt_competitor"):
        pres = json.loads(c["presence"])
        if rnd.random() < 0.12 * c["aggressiveness"] and pres:
            m = rnd.choice(list(pres))
            cut = rnd.uniform(0.03, 0.08)
            pres[m]["price"] = round(pres[m]["price"] * (1 - cut), 3)
            out.append({"competitor": c["name"], "metro": m, "event": "price cut", "detail": f"-{cut:.0%} on retail colocation"})
        if rnd.random() < 0.05:
            m = rnd.choice([x["code"] for x in db.rows("select code from metros")])
            cur = pres.get(m, {"cap": 0.0, "price": c["price_index"]})
            cur["cap"] = round(min(1.0, cur["cap"] + rnd.uniform(0.15, 0.35)), 2)
            pres[m] = cur
            out.append({"competitor": c["name"], "metro": m, "event": "new capacity", "detail": f"capacity score now {cur['cap']}"})
        if rnd.random() < 0.03:
            db.execute("update mkt_competitor set reputation=max(0.2, reputation-0.08) where id=?", (c["id"],))
            out.append({"competitor": c["name"], "metro": None, "event": "major outage", "detail": "reputation hit with customers"})
        for m in pres:  # prices drift back toward their list
            pres[m]["price"] = round(pres[m]["price"] + 0.01 * (c["price_index"] - pres[m]["price"]), 3)
        db.execute("update mkt_competitor set presence=?, reputation=min(0.9, reputation+0.003) where id=?", (json.dumps(pres), c["id"]))
    for e in out:
        db.execute("insert into mkt_competitor_event(sim_date,competitor,metro,event,detail) values(?,?,?,?,?)",
                   (products.today(), e["competitor"], e["metro"], e["event"], e["detail"]))
    return out


def winloss(days=30) -> dict:
    since = (products.today_d() - dt.timedelta(days=days)).isoformat()
    rows = db.rows("select * from mkt_winloss where sim_date>?", (since,))
    won = [r for r in rows if r["outcome"] == "won"]
    lost = [r for r in rows if r["outcome"] == "lost"]
    reasons = {}
    for r in lost:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    to = {}
    for r in lost:
        if r["competitor"]:
            to[r["competitor"]] = to.get(r["competitor"], 0) + 1
    by_ch = {}
    for r in rows:
        d = by_ch.setdefault(r["channel"], {"channel": r["channel"], "won": 0, "lost": 0})
        d[r["outcome"]] += 1
    by_m = {}
    for r in rows:
        d = by_m.setdefault(r["metro"], {"metro": r["metro"], "won": 0, "lost": 0})
        d[r["outcome"]] += 1
    for d in list(by_ch.values()) + list(by_m.values()):
        d["win_rate"] = round(d["won"] / (d["won"] + d["lost"]), 3) if d["won"] + d["lost"] else None
    return {"deals": len(rows), "won": len(won), "lost": len(lost), "win_rate": round(len(won) / len(rows), 3) if rows else None,
            "loss_reasons": sorted([{"reason": k, "n": v} for k, v in reasons.items()], key=lambda x: -x["n"]),
            "lost_to": sorted([{"competitor": k, "n": v} for k, v in to.items()], key=lambda x: -x["n"]),
            "by_channel": list(by_ch.values()), "by_metro": sorted(by_m.values(), key=lambda d: d["metro"])}


# ---------------------------------------------------------------- customer voice, portal, marketplace


def surveys(rnd) -> dict:
    """Quarterly NPS from a third of accounts, driven by what each customer actually experienced."""
    today = products.today_d()
    out = {"nps_responses": 0}
    if today.day != 1 or today.month not in (1, 4, 7, 10):
        return out
    since = (today - dt.timedelta(days=90)).isoformat()
    for c in db.rows("select c.tenant from contracts c where c.status='active'"):
        if rnd.random() > 0.33:
            continue
        tid = c["tenant"]
        misses = db.scalar("select count(*) from tickets where tenant=? and met=0 and sim_date>?", (tid, since)) or 0
        try:
            outages = db.scalar("select count(distinct i.incident) from net_impact i join net_incident n on n.id=i.incident"
                                " where i.tenant=? and n.sim_date>?", (tid, since)) or 0
            disputes = db.scalar("select count(*) from fin_dispute where tenant=? and opened>?", (tid, since)) or 0
        except Exception:  # noqa: BLE001
            outages = disputes = 0
        rejected = db.scalar("select count(*) from mkt_request where tenant=? and status='failed' and created>?", (tid, since)) or 0
        base = 8.9 - 0.3 * max(0, min(5, misses) - 1) - 0.55 * min(3, outages) - 0.6 * min(2, disputes) - 0.6 * rejected
        score = max(0, min(10, round(rnd.gauss(base, 1.1))))
        drivers = [k for k, v in (("SLA misses", misses), ("network outages", outages), ("billing disputes", disputes)) if v]
        db.execute("insert into mkt_survey(tenant,sim_date,kind,score,driver) values(?,?,?,?,?)",
                   (tid, today.isoformat(), "nps", score, ", ".join(drivers) or "service as expected"))
        db.execute("update mkt_customer set nps=?, nps_date=? where tenant=?", (score, today.isoformat(), tid))
        out["nps_responses"] += 1
    return out


def nps(days=120) -> dict:
    since = (products.today_d() - dt.timedelta(days=days)).isoformat()
    rows = db.rows("select score, driver from mkt_survey where kind='nps' and sim_date>?", (since,))
    if not rows:
        rows = [{"score": r["nps"], "driver": None} for r in db.rows("select nps from mkt_customer where nps is not null")]
    if not rows:
        return {"nps": None, "responses": 0}
    pro = sum(1 for r in rows if r["score"] >= 9)
    det = sum(1 for r in rows if r["score"] <= 6)
    drivers = {}
    for r in rows:
        if r["driver"] and r["score"] <= 6:
            for d in r["driver"].split(", "):
                drivers[d] = drivers.get(d, 0) + 1
    csat = db.scalar("select round(avg(csat),2) from mkt_request where csat is not null and created>?", (since,))
    return {"nps": round(100 * (pro - det) / len(rows)), "responses": len(rows), "promoters": pro, "detractors": det,
            "detractor_drivers": sorted([{"driver": k, "n": v} for k, v in drivers.items()], key=lambda x: -x["n"]),
            "portal_csat": csat}


REQUEST_KINDS = {"loa": "Letter of authorization for a cross-connect", "remote_hands": "Remote-hands request",
                 "access": "Site access for visitors", "report": "Usage report"}


def portal_activity(rnd) -> dict:
    """Customers who use the portal raise their own requests; Portico's team works them."""
    today = products.today()
    n = 0
    for c in db.rows("select m.tenant from mkt_customer m join contracts c on c.tenant=m.tenant where m.portal=1 and c.status='active'"):
        if rnd.random() < 0.035:
            kind = rnd.choices(list(REQUEST_KINDS), [3, 4, 2, 1])[0]
            try:
                request(c["tenant"], kind, {}, channel="portal", rnd=rnd)
            except ValueError:
                request(c["tenant"], "remote_hands", {}, channel="portal", rnd=rnd)
            n += 1
    done = failed = 0
    for r in db.rows("select * from mkt_request where status='open'"):
        if rnd.random() < 0.85:
            ok = rnd.random() > 0.04
            res = r["result"]
            db.execute("update mkt_request set status=?, done=?, csat=? where id=?",
                       ("done" if ok else "failed", today, (rnd.choice([4, 5, 5, 5]) if ok else rnd.choice([1, 2])), r["id"]))
            done += ok
            failed += not ok
    return {"new_requests": n, "done": done, "failed": failed}


def request(tid, kind, detail, channel="api", rnd=None) -> dict:
    """A customer request through the portal or its API. LOAs are generated immediately."""
    init()
    rnd = rnd or random.Random()
    if kind not in REQUEST_KINDS:
        raise ValueError(f"kind must be one of {list(REQUEST_KINDS)}")
    result = None
    if kind == "loa":
        port = db.one("select * from ports where tenant=? order by random() limit 1", (tid,))
        if not port:
            raise ValueError("you need a port at the site before a cross-connect can be ordered")
        name = db.scalar("select name from tenants where id=?", (tid,))
        panel = f"{port['site']}-MMR{rnd.randint(1, 3)}-PP{rnd.randint(10, 48):02d}"
        result = (f"LETTER OF AUTHORIZATION / CONNECTING FACILITY ASSIGNMENT (simulated)\n\nCustomer: {name}\nSite: {port['site']}\n"
                  f"Demarcation: patch panel {panel}, position {rnd.randint(1, 24)} (single-mode fibre, LC)\n"
                  f"Customer port: {port.get('leaf') or 'unassigned'} {port.get('leaf_if') or ''} ({port['speed_g']} G)\n"
                  f"Valid until: {(products.today_d() + dt.timedelta(days=90)).isoformat()}\n"
                  f"{name} authorises the holder of this letter to order a cross-connect to the demarcation above.\n")
    rid = db.execute("insert into mkt_request(tenant,kind,detail,status,created,channel,result) values(?,?,?,?,?,?,?)",
                     (tid, kind, json.dumps(detail or {}), "open", products.today(), channel, result))
    return {"request": rid, "kind": kind, "status": "open", "document": result}


def marketplace_leads(rnd) -> int:
    """Buyers find sellers in the marketplace and connect to them: new cross-connect and virtual-circuit deals."""
    n = 0
    listings = db.rows("select * from mkt_listing")
    if not listings:
        return 0
    for _ in range(max(0, int(rnd.gauss(1.2, 0.8)))):
        li = rnd.choice(listings)
        buyer = db.scalar("select t.id from tenants t join contracts c on c.tenant=t.id where c.status='active' and t.id!=?"
                          " order by random() limit 1", (li["tenant"],))
        if not buyer:
            continue
        site = db.scalar("select site from ports where tenant=? order by random() limit 1", (li["tenant"],))
        if not site:
            continue
        oid = crm.open_opportunity(buyer, "cross_connect", {"site": site}, rnd.randint(2, 7), "Expansion")
        db.execute("insert or replace into mkt_opp_channel(opp,channel,partner) values(?,?,?)", (oid, "marketplace", None))
        n += 1
    return n


def listings() -> list[dict]:
    out = []
    for li in db.rows("select l.*, t.name seller from mkt_listing l join tenants t on t.id=l.tenant"):
        buyers = db.scalar("select count(distinct p2.tenant) from cross_connects x join ports p1 on p1.id=x.a_port join ports p2 on p2.id=x.z_port"
                           " where p1.tenant=? or p2.tenant=?", (li["tenant"], li["tenant"])) or 0
        out.append({"id": li["id"], "title": li["title"], "category": li["category"], "seller": li["seller"], "metros": li["metros"],
                    "connections": buyers})
    return sorted(out, key=lambda x: -x["connections"])


# ---------------------------------------------------------------- API keys


def issue_key(tid) -> str:
    init()
    key = "vixk_" + secrets.token_urlsafe(24)
    db.execute("update mkt_customer set api_key_hash=?, key_issued=? where tenant=?",
               (hashlib.sha256(key.encode()).hexdigest(), products.today(), tid))
    return key


def tenant_for_key(key: str) -> int | None:
    if not key:
        return None
    return db.scalar("select tenant from mkt_customer where api_key_hash=?", (hashlib.sha256(key.encode()).hexdigest(),))


def portal_view(tid) -> dict:
    init()
    t = db.one("select * from tenants where id=?", (tid,))
    if not t:
        raise ValueError("no such customer")
    lines = products.lines_by_tenant().get(tid, {})
    c = db.one("select * from contracts where tenant=?", (tid,))
    mc = db.one("select * from mkt_customer where tenant=?", (tid,))
    try:
        inv = db.rows("select number, period, issue_date, due_date, currency, round(amount_usd*fx_rate,2) amount_local, amount_usd,"
                      " balance_usd, status from fin_invoice where tenant=? order by id desc limit 12", (tid,))
    except Exception:  # noqa: BLE001
        inv = []
    svc = {
        "cabinets": db.rows("select p.hall, p.cabinets, round(p.kw,1) kw from placements p where p.tenant=?", (tid,)),
        "ports": db.rows("select id, site, speed_g, leaf, leaf_if from ports where tenant=?", (tid,)),
        "cross_connects": db.scalar("select count(*) from cross_connects x join ports p on p.id=x.a_port where p.tenant=?", (tid,)),
        "virtual_circuits": db.scalar("select count(*) from virtual_circuits v join ports p on p.id=v.a_port where p.tenant=?", (tid,)),
        "cloud_connections": db.rows("select cloud, metro, mbps from cloud_connections where tenant=?", (tid,)),
        "edge_devices": db.rows("select metro, role, size, ha from edge_devices where tenant=?", (tid,)),
        "exchange_ports": db.rows("select metro, speed_g from ix_ports where tenant=?", (tid,)),
        "metal_servers": db.rows("select metro, plan, count(*) n from metal_servers where tenant=? group by metro, plan", (tid,)),
        "internet_access": db.rows("select metro, commit_mbps from internet_access where tenant=?", (tid,)),
    }
    return {"customer": {"id": tid, "name": t["name"], "kind": t["kind"], "home_metro": t["home_metro"], "asn": t["asn"],
                         "persona": mc["persona"] if mc else None},
            "contract": c, "monthly_charges": {k: round(v, 2) for k, v in lines.items()}, "monthly_total": round(sum(lines.values()), 2),
            "services": svc, "invoices": inv,
            "tickets": db.rows("select sim_date, site, kind, priority, met, billed_usd from tickets where tenant=? order by id desc limit 15", (tid,)),
            "requests": db.rows("select id, kind, status, created, done, csat from mkt_request where tenant=? order by id desc limit 15", (tid,)),
            "opportunities": db.rows("select Id, Name, StageName, MRR__c, CloseDate from crm_opportunity where AccountId=? and IsClosed=0", (crm.account_id(tid),)),
            "catalog": world.order_kinds()}


def portal_order(tid, kind, params) -> dict:
    """Self-service order: priced at list, approved instantly, provisioned on the next simulated day."""
    init()
    if kind not in world.order_kinds():
        raise ValueError(f"unknown product {kind}")
    if kind == "metal" and (db.setting("metal_sunset") or {}).get("announced"):
        raise ValueError("bare metal is being retired and no longer sold")
    try:
        if db.scalar("select credit_hold from fin_customer where tenant=?", (tid,)):
            raise ValueError("your account is on credit hold: please contact billing")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001
        pass
    oid = crm.open_opportunity(tid, kind, params, 1, "Expansion")
    db.execute("insert or replace into mkt_opp_channel(opp,channel,partner) values(?,?,?)", (oid, "portal", None))
    mrr = db.scalar("select MRR__c from crm_opportunity where Id=?", (oid,))
    return {"opportunity": oid, "kind": kind, "params": params, "mrr_usd": mrr, "status": "accepted: provisioning starts on the next simulated day"}


# ---------------------------------------------------------------- bare-metal sunset


def metal_position() -> dict:
    rev = products.revenue_breakdown()
    servers = db.scalar("select count(*) from metal_servers") or 0
    alloc = db.scalar("select coalesce(sum(allocated),0) from metal_inventory") or 0
    cust = db.scalar("select count(distinct tenant) from metal_servers") or 0
    return {"metal_mrr": rev.get("metal", 0), "share_of_revenue": round(rev.get("metal", 0) / rev["total"], 4) if rev["total"] else 0,
            "servers_with_customers": servers, "servers_allocated": alloc, "customers": cust, "gross_margin": 0.22}


def start_sunset(eol_days=180) -> dict:
    eol = (products.today_d() + dt.timedelta(days=eol_days)).isoformat()
    db.set_setting("metal_sunset", {"announced": products.today(), "eol": eol})
    rnd = random.Random("sunset")
    lb = products.lines_by_tenant()
    for r in db.rows("select tenant, count(*) n from metal_servers group by tenant"):
        offer = "colocation" if persona(r["tenant"]) in ("AI builder", "network carrier", "latency-sensitive trader") else rnd.choice(
            ["colocation", "partner cloud", "partner cloud"])
        db.execute("insert or replace into mkt_migration(tenant,servers,mrr_before,offer,decide_by,status) values(?,?,?,?,?,'offered')",
                   (r["tenant"], r["n"], round(lb.get(r["tenant"], {}).get("metal", 0), 2), offer,
                    (products.today_d() + dt.timedelta(days=rnd.randint(30, eol_days - 10))).isoformat()))
    return {"eol": eol, "customers": db.scalar("select count(*) from mkt_migration")}


def sunset_step(rnd) -> dict:
    s = db.setting("metal_sunset") or {}
    if not s.get("announced"):
        return {}
    today = products.today()
    out = {"migrated": 0, "lost": 0, "decommissioned": 0}
    for m in db.rows("select * from mkt_migration where status='offered' and decide_by<=?", (today,)):
        p_stay = {"colocation": 0.62, "partner cloud": 0.5}[m["offer"]] + 0.1 * (persona(m["tenant"]) == "AI builder")
        _release_metal(m["tenant"])
        if rnd.random() < p_stay and db.scalar("select status from contracts where tenant=?", (m["tenant"],)) == "active":
            home = db.scalar("select home_metro from tenants where id=?", (m["tenant"],))
            if m["offer"] == "colocation":
                kw = max(6, round(m["servers"] * 2.0))
                params = {"metro": home, "cabinets": max(1, kw // 6), "kw_per_cabinet": 6}
                kind = "cabinets"
            else:
                on = db.one("select * from cloud_onramps where metro=? order by random() limit 1", (home,)) or db.one(
                    "select * from cloud_onramps limit 1")
                params, kind = {"metro": on["metro"], "cloud": on["cloud"], "mbps": 2000 if m["servers"] < 6 else 10000}, "cloud_connection"
            world.place_order(m["tenant"], kind, params)
            from . import crm as _crm
            mrr_after = _crm._order_mrr(kind, params)
            db.execute("update mkt_migration set status='migrated', mrr_after=?, note=? where tenant=?",
                       (round(mrr_after, 2), f"moved to {kind}", m["tenant"]))
            out["migrated"] += 1
        else:
            db.execute("update mkt_migration set status='lost', mrr_after=0, note='took workloads elsewhere' where tenant=?", (m["tenant"],))
            out["lost"] += 1
    if today >= s["eol"] and not s.get("done"):
        for r in db.rows("select distinct tenant from metal_servers"):
            _release_metal(r["tenant"])
            db.execute("update mkt_migration set status='lost', mrr_after=0, note='decommissioned at end of life' where tenant=? and status='offered'",
                       (r["tenant"],))
            out["decommissioned"] += 1
        db.execute("update metal_inventory set allocated=0, total=0")
        s["done"] = today
        db.set_setting("metal_sunset", s)
    return out


def _release_metal(tid):
    for m in db.rows("select metro, plan, count(*) n from metal_servers where tenant=? group by metro, plan", (tid,)):
        db.execute("update metal_inventory set allocated=max(0,allocated-?) where metro=? and plan=?", (m["n"], m["metro"], m["plan"]))
    db.execute("delete from metal_servers where tenant=?", (tid,))


def kpi_hook() -> dict:
    if not db.one("select name from sqlite_master where name='mkt_winloss'"):
        return {}
    wl = winloss(30)
    n = nps()
    return {"market": {"win_rate_30d": wl["win_rate"], "nps": n["nps"],
                       "portal_customers": db.scalar("select count(*) from mkt_customer where portal=1"),
                       "top_loss_reason": wl["loss_reasons"][0]["reason"] if wl["loss_reasons"] else None}}
