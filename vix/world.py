"""The simulated world: metros, data-center sites, halls, tenants, ports and
interconnections. Everything here is synthetic and seeded, so a fresh install
always starts from the same world. Names are invented; no real company is modeled."""
from __future__ import annotations

import json
import math
import random

from . import db
from .config import SEED

METROS = [
    # code, name, region, lat, lon, grid gCO2/kWh, renewable %, ambient mean C, daily swing C, $/kWh
    ("SJC", "Silicon Valley", "Americas", 37.37, -121.92, 220, 35, 16, 7, 0.18),
    ("ASH", "Ashburn", "Americas", 39.04, -77.49, 330, 15, 13, 9, 0.09),
    ("NYC", "New York", "Americas", 40.71, -74.00, 260, 20, 13, 8, 0.16),
    ("LHR", "London", "EMEA", 51.51, -0.13, 180, 40, 11, 6, 0.28),
    ("FRA", "Frankfurt", "EMEA", 50.11, 8.68, 360, 45, 10, 7, 0.30),
    ("SIN", "Singapore", "APAC", 1.35, 103.82, 410, 5, 28, 3, 0.22),
    ("NRT", "Tokyo", "APAC", 35.68, 139.69, 450, 22, 16, 7, 0.24),
    ("SYD", "Sydney", "APAC", -33.87, 151.21, 620, 32, 18, 6, 0.20),
]

KW_PER_CAB_DESIGN = 6.0
CRAH_KW = 300.0
CAPEX_PER_KW = 10_000.0          # build cost per kW of critical capacity
SITE_OPEX_MONTH = 450_000.0      # staff-equivalent, maintenance, security, insurance, per site

PRICES = {
    "cabinet_month": 600.0,
    "kw_month": 210.0,
    "cross_connect_month": 300.0,
    "port_month": {1: 300.0, 10: 800.0, 100: 2500.0, 400: 6500.0},
    "vc_per_mbps_month": 0.9,
    "metal_month": {"s1.small": 350.0, "m1.large": 1100.0, "g1.gpu": 4200.0},
}
METAL_PLANS = {"s1.small": 120, "m1.large": 60, "g1.gpu": 16}

KINDS = ["carrier", "cloud", "content", "enterprise", "financial"]
_PREFIX = ["Arc", "Blue", "Cedar", "Delta", "Ember", "Fjord", "Granite", "Harbor", "Iris", "Juniper",
           "Kestrel", "Lumen", "Meridian", "Nimbus", "Orbit", "Pioneer", "Quarry", "Ridge", "Summit",
           "Tidal", "Umber", "Vector", "Willow", "Xenon", "Yonder", "Zephyr"]
_SUFFIX = {"carrier": ["Transit", "Fiber", "Telecom", "Networks", "Wave"],
           "cloud": ["Cloud", "Compute", "Stack", "Hosting"],
           "content": ["Media", "Stream", "CDN", "Games"],
           "enterprise": ["Logistics", "Health", "Retail", "Motors", "Pharma", "Energy"],
           "financial": ["Capital", "Markets", "Exchange", "Bank", "Trading"]}


def seeded() -> bool:
    return (db.scalar("select count(*) from metros") or 0) > 0


def seed(force: bool = False):
    db.init()
    if seeded() and not force:
        from . import crm, netinfra, products
        products.upgrade(SEED)
        crm.upgrade()
        netinfra.upgrade(SEED)
        return
    rnd = random.Random(SEED)
    with db.conn() as c:
        for t in ("metros", "sites", "halls", "tenants", "placements", "ports", "cross_connects",
                  "virtual_circuits", "metal_inventory", "metal_servers", "orders", "telemetry",
                  "clouds", "cloud_onramps", "cloud_routers", "cloud_connections", "edge_pool", "edge_devices",
                  "ix_ports", "tickets", "contracts", "contract_events"):
            c.execute(f"delete from {t}")
        c.execute("delete from settings where key in ('world_version','sim_date','crm_synced','bi_loaded','crm_started_ts')")
        for t in ("crm_account", "crm_contact", "crm_opportunity", "crm_case", "crm_asset", "crm_contract",
                  "net_vendor", "net_firmware", "net_device", "net_interface", "net_link", "net_event", "net_incident",
                  "net_impact", "net_change", "net_capacity_daily", "net_audit"):
            if c.execute("select name from sqlite_master where name=?", (t,)).fetchone():
                c.execute(f"delete from {t}")
        for m in METROS:
            c.execute("insert into metros values(?,?,?,?,?,?,?,?,?,?)", m)
        halls = []
        for code, *_ in METROS:
            n_sites = 3 if code == "ASH" else 2
            for s in range(1, n_sites + 1):
                sid = f"{code}{s}"
                c.execute("insert into sites(id,metro,name,opened,tier) values(?,?,?,?,?)",
                          (sid, code, f"{code}-{s} campus", rnd.randint(2009, 2023), "Tier III"))
                for h in range(1, rnd.randint(2, 4) + 1):
                    cab = rnd.choice([180, 220, 260, 320])
                    util_kw = cab * KW_PER_CAB_DESIGN
                    sold = int(cab * rnd.uniform(0.55, 0.93))
                    contracted = round(sold * 5.5 * rnd.uniform(0.8, 1.15), 1)
                    contracted = min(contracted, util_kw * 0.95)
                    it = round(contracted * rnd.uniform(0.55, 0.8), 1)
                    crah = math.ceil(util_kw / CRAH_KW) + 1
                    hid = f"{sid}-H{h}"
                    halls.append((hid, sid, code, sold))
                    c.execute("insert into halls(id,site,name,cabinets_total,cabinets_sold,utility_kw,contracted_kw,"
                              "it_load_kw,crah_units,crah_kw_each,crah_down,ups_minutes,generators,build_capex_usd,built_ts)"
                              " values(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                              (hid, sid, f"Hall {h}", cab, 0, util_kw, 0, 0, crah, CRAH_KW,
                               rnd.choice([8, 10, 12]), max(2, math.ceil(util_kw / 1500) * 2),
                               util_kw * CAPEX_PER_KW, db.now()))
            for plan, n in METAL_PLANS.items():
                c.execute("insert into metal_inventory values(?,?,?,?)", (code, plan, n, 0))

        # tenants
        names, tenants = set(), []
        asn = 64600
        for i in range(160):
            kind = rnd.choices(KINDS, weights=[2, 1, 1.5, 3, 1.5])[0]
            while True:
                nm = f"{rnd.choice(_PREFIX)} {rnd.choice(_SUFFIX[kind])}"
                if nm not in names:
                    names.add(nm)
                    break
            asn += rnd.randint(1, 40)
            home = rnd.choice(METROS)[0]
            pfx = {"carrier": 40, "cloud": 25, "content": 12, "enterprise": 2, "financial": 3}[kind]
            pfx = max(1, int(pfx * rnd.uniform(0.5, 1.6)))
            tid = c.execute("insert into tenants(name,kind,asn,home_metro,prefixes,since_ts) values(?,?,?,?,?,?)",
                            (nm, kind, asn, home, pfx, db.now())).lastrowid
            tenants.append((tid, kind, home))

        # placements: fill each hall's sold cabinets from tenants that live in or near that metro
        for hid, sid, code, sold in halls:
            left = sold
            local = [t for t in tenants if t[2] == code] or tenants
            while left > 0:
                t = rnd.choice(local if rnd.random() < 0.7 else tenants)
                n = min(left, rnd.randint(2, 30))
                kw = round(n * rnd.uniform(3.5, 8.0), 1)
                c.execute("insert into placements(tenant,hall,cabinets,kw,ts) values(?,?,?,?,?)",
                          (t[0], hid, n, kw, db.now()))
                left -= n
        # derive hall totals from placements
        for hid, *_ in halls:
            n, kw = c.execute("select coalesce(sum(cabinets),0), coalesce(sum(kw),0) from placements where hall=?",
                              (hid,)).fetchone()
            util = c.execute("select utility_kw from halls where id=?", (hid,)).fetchone()[0]
            kw = min(kw, util * 0.95)
            c.execute("update halls set cabinets_sold=?, contracted_kw=?, it_load_kw=? where id=?",
                      (n, round(kw, 1), round(kw * rnd.uniform(0.55, 0.8), 1), hid))

        # ports: every tenant with a placement gets ports at those sites; carriers also land elsewhere
        pres = c.execute("select distinct p.tenant, h.site from placements p join halls h on h.id=p.hall").fetchall()
        site_ids = [r[0] for r in c.execute("select id from sites")]
        port_rows = list(pres)
        for tid, kind, _ in tenants:
            if kind in ("carrier", "cloud"):
                for s in rnd.sample(site_ids, rnd.randint(4, 10)):
                    port_rows.append((tid, s))
        for tid, s in set(port_rows):
            kind = next(t[1] for t in tenants if t[0] == tid)
            speeds = [10, 100, 400] if kind in ("carrier", "cloud", "content") else [1, 10, 10, 100]
            for _ in range(rnd.randint(1, 3)):
                c.execute("insert into ports(tenant,site,speed_g,ts) values(?,?,?,?)",
                          (tid, s, rnd.choice(speeds), db.now()))

        # cross connects within a site; virtual circuits between metros
        by_site = {}
        for pid, tid, s in c.execute("select id, tenant, site from ports"):
            by_site.setdefault(s, []).append((pid, tid))
        for s, plist in by_site.items():
            if len(plist) < 2:
                continue
            for _ in range(len(plist) * 2):
                a, z = rnd.sample(plist, 2)
                if a[1] != z[1]:
                    c.execute("insert into cross_connects(site,a_port,z_port,ts) values(?,?,?,?)",
                              (s, a[0], z[0], db.now()))
        allp = c.execute("select p.id, s.metro from ports p join sites s on s.id=p.site").fetchall()
        for _ in range(500):
            a, z = rnd.sample(allp, 2)
            if a[1] != z[1]:
                c.execute("insert into virtual_circuits(a_port,z_port,mbps,rtt_ms,ts) values(?,?,?,?,?)",
                          (a[0], z[0], rnd.choice([50, 100, 200, 500, 1000, 2000, 10000]),
                           rtt_ms(a[1], z[1]), db.now()))
        for code, *_ in METROS:
            for plan, n in METAL_PLANS.items():
                used = int(n * rnd.uniform(0.3, 0.75))
                c.execute("update metal_inventory set allocated=? where metro=? and plan=?", (used, code, plan))
    db.set_setting("world_seed", SEED)
    db.set_setting("rs_filtering", False)
    from . import crm, netinfra, products
    products.upgrade(SEED)
    crm.upgrade()
    netinfra.upgrade(SEED)


# ---------- geography ----------

_METRO_GEO = {m[0]: {"lat": m[3], "lon": m[4]} for m in METROS}


def metro(code):
    return db.one("select * from metros where code=?", (code,))


def great_circle_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def rtt_ms(a_code, z_code, route_factor=1.45, per_hop_ms=0.25, hops=6):
    """Round trip over fiber: light in glass ~200,000 km/s, real routes ~1.45x the
    great-circle distance, plus a little switching delay per hop."""
    if a_code == z_code:
        return 0.5
    a, z = _METRO_GEO[a_code], _METRO_GEO[z_code]
    km = great_circle_km(a, z) * route_factor
    return round(2 * km / 200.0 + hops * per_hop_ms, 2)


def latency_matrix():
    codes = [m[0] for m in METROS]
    return {"metros": codes, "rtt_ms": [[rtt_ms(a, b) for b in codes] for a in codes],
            "model": "RTT = 2 x great-circle km x 1.45 route factor / 200 km per ms + 6 hops x 0.25 ms"}


# ---------- views ----------

def halls(metro_code=None):
    q = ("select h.*, s.metro from halls h join sites s on s.id=h.site")
    args = ()
    if metro_code:
        q += " where s.metro=?"
        args = (metro_code,)
    out = db.rows(q + " order by h.id", args)
    for h in out:
        h["cab_util"] = round(h["cabinets_sold"] / h["cabinets_total"], 3)
        h["power_util"] = round(h["contracted_kw"] / h["utility_kw"], 3)
        h["cooling_kw_available"] = (h["crah_units"] - h["crah_down"]) * h["crah_kw_each"]
    return out


def sites():
    out = db.rows("select * from sites order by id")
    hs = halls()
    for s in out:
        mine = [h for h in hs if h["site"] == s["id"]]
        s["halls"] = len(mine)
        s["cabinets_total"] = sum(h["cabinets_total"] for h in mine)
        s["cabinets_sold"] = sum(h["cabinets_sold"] for h in mine)
        s["it_load_kw"] = round(sum(h["it_load_kw"] for h in mine), 1)
        s["utility_kw"] = sum(h["utility_kw"] for h in mine)
        s["ports"] = db.scalar("select count(*) from ports where site=?", (s["id"],))
        s["cross_connects"] = db.scalar("select count(*) from cross_connects where site=?", (s["id"],))
    return out


def metros_summary():
    out = db.rows("select * from metros")
    ss = sites()
    for m in out:
        mine = [s for s in ss if s["metro"] == m["code"]]
        m["sites"] = len(mine)
        m["cabinets_total"] = sum(s["cabinets_total"] for s in mine)
        m["cabinets_sold"] = sum(s["cabinets_sold"] for s in mine)
        m["it_load_kw"] = round(sum(s["it_load_kw"] for s in mine), 1)
        m["cross_connects"] = sum(s["cross_connects"] for s in mine)
        m["tenants_present"] = db.scalar(
            "select count(distinct p.tenant) from ports p join sites s on s.id=p.site where s.metro=?", (m["code"],))
    return out


def tenants(kind=None, limit=200):
    q = ("select t.*, (select coalesce(sum(cabinets),0) from placements where tenant=t.id) cabinets,"
         " (select count(*) from ports where tenant=t.id) ports from tenants t")
    args = ()
    if kind:
        q += " where t.kind=?"
        args = (kind,)
    return db.rows(q + " order by t.id limit ?", args + (limit,))


def tenant(tid):
    t = db.one("select * from tenants where id=?", (tid,))
    if not t:
        return None
    t["placements"] = db.rows("select * from placements where tenant=?", (tid,))
    t["ports"] = db.rows("select * from ports where tenant=?", (tid,))
    t["metal"] = db.rows("select metro, plan, count(*) n from metal_servers where tenant=? group by metro, plan", (tid,))
    t["mrr"] = round(tenant_mrr(tid), 2)
    from . import products
    t.update(products.tenant_detail(tid))
    return t


def tenant_mrr(tid):
    from . import products
    return products.tenant_mrr(tid)


def revenue_breakdown():
    from . import products
    return products.revenue_breakdown()


# ---------- orders ----------

ORDER_KINDS = {
    "cabinets": "Colocation: {metro, cabinets, kw_per_cabinet}",
    "port": "Interconnection port: {site, speed_g}",
    "cross_connect": "Physical cross-connect in one site: {site, z_tenant}",
    "vc": "Fabric virtual circuit between metros: {a_metro, z_metro, mbps}",
    "metal": "Bare-metal servers: {metro, plan, count}",
}


def order_kinds():
    from . import products
    return {**ORDER_KINDS, **products.ORDER_KINDS}


def place_order(tenant_id: int, kind: str, params: dict) -> dict:
    if kind not in order_kinds():
        raise ValueError(f"unknown order kind {kind}; one of {list(order_kinds())}")
    if not db.one("select id from tenants where id=?", (tenant_id,)):
        raise ValueError(f"no tenant {tenant_id}")
    oid = db.execute("insert into orders(ts,tenant,kind,params,status,note) values(?,?,?,?,?,?)",
                     (db.now(), tenant_id, kind, json.dumps(params), "pending", ""))
    return {"order_id": oid, "status": "pending"}


def new_tenant(rnd: random.Random, kind=None, home=None):
    kind = kind or rnd.choices(KINDS, weights=[1, 1, 1.5, 3, 1.5])[0]
    home = home or rnd.choice(METROS)[0]
    for _ in range(50):
        nm = f"{rnd.choice(_PREFIX)} {rnd.choice(_SUFFIX[kind])}"
        if not db.one("select id from tenants where name=?", (nm,)):
            break
    else:
        nm = f"{rnd.choice(_PREFIX)} {rnd.choice(_SUFFIX[kind])} {rnd.randint(2, 99)}"
    asn = (db.scalar("select max(asn) from tenants") or 64600) + rnd.randint(1, 40)
    pfx = {"carrier": 40, "cloud": 25, "content": 12, "enterprise": 2, "financial": 3}[kind]
    tid = db.execute("insert into tenants(name,kind,asn,home_metro,prefixes,since_ts) values(?,?,?,?,?,?)",
                     (nm, kind, asn, home, max(1, int(pfx * rnd.uniform(0.5, 1.6))), db.now()))
    from . import products
    products.new_contract(tid)
    return tid


def _ensure_port(tid, site, speed=10):
    p = db.one("select id from ports where tenant=? and site=? order by speed_g desc", (tid, site))
    if p:
        return p["id"], False
    return db.execute("insert into ports(tenant,site,speed_g,ts) values(?,?,?,?)", (tid, site, speed, db.now())), True


def _site_in_metro(metro_code, tid=None):
    if tid:
        r = db.one("select p.site from ports p join sites s on s.id=p.site where p.tenant=? and s.metro=?",
                   (tid, metro_code))
        if r:
            return r["site"]
    r = db.one("select id from sites where metro=? order by id", (metro_code,))
    return r["id"] if r else None


def fulfill(order: dict, headroom: float = 0.92) -> tuple[str, str]:
    """Try to deliver one order against the simulated inventory. Returns (status, note)."""
    from . import products
    p = json.loads(order["params"] or "{}")
    tid, kind = order["tenant"], order["kind"]
    if kind in products.HANDLERS:
        return products.HANDLERS[kind](tid, p)
    if kind == "cabinets":
        m, n = p.get("metro"), int(p.get("cabinets", 1))
        kwpc = float(p.get("kw_per_cabinet", 5.0))
        need_kw = n * kwpc
        cands = [h for h in halls(m) if h.get("cooling_type", "air") != "liquid"
                 and h["cabinets_total"] - h["cabinets_sold"] >= n
                 and h["utility_kw"] * headroom - h["contracted_kw"] >= need_kw]
        if not cands:
            return "waitlisted", f"no hall in {m} has {n} cabinets and {need_kw:.0f} kW free under the {headroom:.0%} power cap"
        h = min(cands, key=lambda h: h["power_util"])
        db.execute("insert into placements(tenant,hall,cabinets,kw,ts) values(?,?,?,?,?)",
                   (tid, h["id"], n, need_kw, db.now()))
        db.execute("update halls set cabinets_sold=cabinets_sold+?, contracted_kw=contracted_kw+?,"
                   " it_load_kw=it_load_kw+? where id=?", (n, need_kw, need_kw * 0.35, h["id"]))
        _ensure_port(tid, h["site"])
        return "done", f"{n} cabinets, {need_kw:.0f} kW in {h['id']}"
    if kind == "port":
        site, speed = p.get("site"), int(p.get("speed_g", 10))
        if not db.one("select id from sites where id=?", (site,)):
            return "rejected", f"no site {site}"
        db.execute("insert into ports(tenant,site,speed_g,ts) values(?,?,?,?)", (tid, site, speed, db.now()))
        return "done", f"{speed}G port at {site}"
    if kind == "cross_connect":
        site, z = p.get("site"), p.get("z_tenant")
        if not db.one("select id from sites where id=?", (site,)):
            return "rejected", f"no site {site}"
        if not z:
            zr = db.one("select tenant from ports where site=? and tenant!=? order by random() limit 1", (site, tid))
            if not zr:
                return "rejected", f"no counterparty present at {site}"
            z = zr["tenant"]
        a, na = _ensure_port(tid, site)
        zp, nz = _ensure_port(int(z), site)
        db.execute("insert into cross_connects(site,a_port,z_port,ts) values(?,?,?,?)", (site, a, zp, db.now()))
        extra = " (new ports provisioned)" if na or nz else ""
        return "done", f"cross-connect at {site} to tenant {z}{extra}"
    if kind == "vc":
        am, zm, mbps = p.get("a_metro"), p.get("z_metro"), int(p.get("mbps", 100))
        sa, sz = _site_in_metro(am, tid), _site_in_metro(zm, tid)
        if not sa or not sz:
            return "rejected", "unknown metro"
        a, _ = _ensure_port(tid, sa)
        z, _ = _ensure_port(tid, sz)
        cap = (db.scalar("select speed_g from ports where id=?", (a,)) or 10) * 1000
        used = db.scalar("select coalesce(sum(mbps),0) from virtual_circuits where a_port=? or z_port=?", (a, a)) or 0
        if used + mbps > cap:
            return "waitlisted", f"port {a} at {sa} is full ({used} of {cap} Mbps); needs a faster port"
        rtt = rtt_ms(am, zm)
        db.execute("insert into virtual_circuits(a_port,z_port,mbps,rtt_ms,ts) values(?,?,?,?,?)",
                   (a, z, mbps, rtt, db.now()))
        return "done", f"{mbps} Mbps {am}->{zm}, RTT {rtt} ms"
    if kind == "metal":
        m, plan, n = p.get("metro"), p.get("plan", "s1.small"), int(p.get("count", 1))
        inv = db.one("select * from metal_inventory where metro=? and plan=?", (m, plan))
        if not inv:
            return "rejected", f"no {plan} in {m}"
        if inv["total"] - inv["allocated"] < n:
            return "waitlisted", f"{m} has {inv['total'] - inv['allocated']} {plan} free, asked {n}"
        db.execute("update metal_inventory set allocated=allocated+? where metro=? and plan=?", (n, m, plan))
        for _ in range(n):
            db.execute("insert into metal_servers(tenant,metro,plan,ts) values(?,?,?,?)", (tid, m, plan, db.now()))
        return "done", f"{n} x {plan} in {m}"
    return "rejected", "unhandled kind"


def process_orders(kinds: list[str], include_waitlist: bool = True) -> list[dict]:
    states = ("pending", "waitlisted") if include_waitlist else ("pending",)
    q = (f"select * from orders where status in ({','.join('?' * len(states))})"
         f" and kind in ({','.join('?' * len(kinds))}) order by ts")
    out = []
    for o in db.rows(q, states + tuple(kinds)):
        status, note = fulfill(o)
        if status == "waitlisted" and o["status"] == "waitlisted":
            out.append({**o, "status": status, "note": note, "changed": False})
            continue
        db.execute("update orders set status=?, note=?, done_ts=? where id=?",
                   (status, note, db.now() if status == "done" else None, o["id"]))
        out.append({**o, "status": status, "note": note, "changed": True})
    return out


def add_hall(site_id: str, cabinets: int = 260, liquid: bool = False) -> dict:
    """Build a hall. Liquid halls are AI halls: high density, coolant distribution units
    (stored in the crah_* columns) instead of air handlers."""
    from . import products
    s = db.one("select * from sites where id=?", (site_id,))
    if not s:
        raise ValueError(f"no site {site_id}")
    n = (db.scalar("select count(*) from halls where site=?", (site_id,)) or 0) + 1
    hid = f"{site_id}-{'AI' if liquid else 'H'}{n}"
    while db.one("select id from halls where id=?", (hid,)):
        n += 1
        hid = f"{site_id}-{'AI' if liquid else 'H'}{n}"
    density = products.LIQUID_CAB_KW if liquid else KW_PER_CAB_DESIGN
    unit_kw = products.CDU_KW if liquid else CRAH_KW
    util = cabinets * density
    db.execute("insert into halls(id,site,name,cabinets_total,cabinets_sold,utility_kw,contracted_kw,it_load_kw,"
               "crah_units,crah_kw_each,crah_down,ups_minutes,generators,build_capex_usd,built_ts,cooling_type,kw_per_cab_design)"
               " values(?,?,?,?,0,?,0,0,?,?,0,10,?,?,?,?,?)",
               (hid, site_id, f"{'AI hall' if liquid else 'Hall'} {n}", cabinets, util, math.ceil(util / unit_kw) + 1,
                unit_kw, max(2, math.ceil(util * 1.3 / 1500) * 2), util * CAPEX_PER_KW * (1.25 if liquid else 1.0),
                db.now(), "liquid" if liquid else "air", density))
    return {"hall": hid, "cabinets": cabinets, "utility_kw": util, "cooling": "liquid" if liquid else "air",
            "capex_usd": util * CAPEX_PER_KW * (1.25 if liquid else 1.0)}
