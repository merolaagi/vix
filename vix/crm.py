"""Headless CRM: vix's system of record for customers, modeled on Salesforce's
published standard objects (Account, Contact, Opportunity, Case, Asset, Contract)
so tools that speak Salesforce's REST shapes and a subset of SOQL can read it.

Sales and renewals now run through Opportunities. Scribe (CRM Operations) syncs
accounts, contracts, assets and cases from operations every simulated day."""
from __future__ import annotations

import json
import random
import re

from . import db

API_VERSION = "v60.0"

SCHEMA = """
create table if not exists crm_account (
  Id text primary key, Name text, Type text, Industry text, BillingCity text, Tenant__c integer unique,
  ASN__c integer, MRR__c real, CreatedDate text, LastModifiedDate text
);
create table if not exists crm_contact (
  Id text primary key, AccountId text, FirstName text, LastName text, Title text, Email text, CreatedDate text
);
create table if not exists crm_opportunity (
  Id text primary key, AccountId text, Name text, Type text, StageName text, Probability real,
  Amount real, MRR__c real, CloseDate text, Product__c text, Params__c text, Order__c integer,
  IsClosed integer default 0, IsWon integer default 0, CreatedDate text, LastModifiedDate text
);
create table if not exists crm_case (
  Id text primary key, AccountId text, Subject text, Type text, Priority text, Status text, Origin text,
  Site__c text, SLA_Minutes__c real, SLA_Met__c integer, Billed__c real, CreatedDate text, Ticket__c integer unique
);
create table if not exists crm_asset (
  Id text primary key, AccountId text, Name text, ProductCode text, Location__c text, Quantity real,
  Status text, Source__c text unique, InstallDate text, UsageEndDate text
);
create table if not exists crm_contract (
  Id text primary key, AccountId text, StartDate text, EndDate text, ContractTerm integer, Status text,
  Escalator__c real
);
create index if not exists crm_opp_account on crm_opportunity(AccountId);
create index if not exists crm_opp_open on crm_opportunity(IsClosed, CloseDate);
create index if not exists crm_case_account on crm_case(AccountId);
create index if not exists crm_asset_account on crm_asset(AccountId);
"""

# Salesforce-style key prefixes
PREFIX = {"Account": "001", "Contact": "003", "Opportunity": "006", "Case": "500", "Asset": "02i", "Contract": "800"}
OBJECTS = {
    "Account": "crm_account", "Contact": "crm_contact", "Opportunity": "crm_opportunity",
    "Case": "crm_case", "Asset": "crm_asset", "Contract": "crm_contract",
}
LABELS = {"Account": "Account", "Contact": "Contact", "Opportunity": "Opportunity", "Case": "Case",
          "Asset": "Asset", "Contract": "Contract"}
INDUSTRY = {"carrier": "Telecommunications", "cloud": "Technology", "content": "Media",
            "enterprise": "Manufacturing", "financial": "Finance"}
STAGES = [("Qualification", 20), ("Proposal", 50), ("Negotiation", 75)]
WIN_RATE = 0.62


def sfid(obj: str, n: int) -> str:
    return PREFIX[obj] + str(int(n)).zfill(12)


def account_id(tenant: int) -> str:
    return sfid("Account", tenant)


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)


def _today():
    from . import products
    return products.today()


# ---------------------------------------------------------------- sync (Scribe)

ASSET_SOURCES = [
    # (source table, SQL selecting: source id, tenant, name, product code, location, quantity)
    ("placements", "select p.id, p.tenant, 'Colocation ' || p.cabinets || ' cab / ' || cast(round(p.kw) as int) || ' kW',"
                   " case h.cooling_type when 'liquid' then 'AI-COLO' else 'COLO' end, p.hall, p.cabinets"
                   " from placements p join halls h on h.id=p.hall"),
    ("ports", "select id, tenant, 'Port ' || speed_g || 'G', 'PORT-' || speed_g || 'G', site, 1 from ports"),
    ("cross_connects", "select x.id, p.tenant, 'Cross-connect ' || x.site, 'XCONNECT', x.site, 1"
                       " from cross_connects x join ports p on p.id=x.a_port"),
    ("virtual_circuits", "select v.id, p.tenant, 'Fabric VC ' || v.mbps || ' Mbps', 'FABRIC-VC', p.site, v.mbps"
                         " from virtual_circuits v join ports p on p.id=v.a_port"),
    ("cloud_connections", "select id, tenant, 'Cloud connection ' || cloud || ' ' || mbps || ' Mbps', 'CLOUD-' || cloud,"
                          " metro, mbps from cloud_connections"),
    ("cloud_routers", "select id, tenant, 'Cloud router (' || package || ')', 'CLOUD-ROUTER', metro, 1 from cloud_routers"),
    ("edge_devices", "select id, tenant, 'Edge ' || role || ' ' || size || case ha when 1 then ' HA' else '' end,"
                     " 'EDGE-' || upper(role), metro, vcpu from edge_devices"),
    ("ix_ports", "select id, tenant, 'Exchange port ' || speed_g || 'G', 'IX-' || speed_g || 'G', metro, 1 from ix_ports"),
    ("metal_servers", "select id, tenant, 'Metal ' || plan, 'METAL-' || upper(plan), metro, 1 from metal_servers"),
    ("internet_access", "select id, tenant, 'Internet Access ' || commit_mbps || ' Mbps', 'INTERNET', metro, commit_mbps"
                        " from internet_access"),
]


def sync(rnd: random.Random | None = None) -> dict:
    """Bring CRM records in line with operations. Returns counts of what changed."""
    from . import products
    init()
    rnd = rnd or random.Random(7)
    today = _today()
    out = {"accounts_new": 0, "contacts_new": 0, "assets_new": 0, "assets_retired": 0, "cases_new": 0}
    with db.conn() as c:
        metro_name = {r[0]: r[1] for r in c.execute("select code, name from metros")}
        have = {r[0] for r in c.execute("select Tenant__c from crm_account")}
        lines = products.lines_by_tenant()
        for t in c.execute("select t.id, t.name, t.kind, t.asn, t.home_metro, coalesce(k.status,'active') st"
                           " from tenants t left join contracts k on k.tenant=t.id").fetchall():
            aid = account_id(t[0])
            typ = "Customer" if t[5] == "active" else "Former Customer"
            mrr = round(sum(lines.get(t[0], {}).values()), 2)
            if t[0] not in have:
                c.execute("insert into crm_account values(?,?,?,?,?,?,?,?,?,?)",
                          (aid, t[1], typ, INDUSTRY.get(t[2], "Other"), metro_name.get(t[4], t[4]), t[0], t[3], mrr,
                           today, today))
                out["accounts_new"] += 1
                first = rnd.choice(["Avery", "Jordan", "Riley", "Morgan", "Casey", "Quinn", "Rowan", "Sasha", "Devi", "Kiran"])
                last = rnd.choice(["Patel", "Garcia", "Nguyen", "Okafor", "Silva", "Kim", "Moreau", "Haddad", "Sato", "Novak"])
                slug = re.sub(r"[^a-z]", "", t[1].lower())
                for title in ("Head of Infrastructure", "Network Architect"):
                    n = c.execute("select count(*) from crm_contact").fetchone()[0] + 1
                    c.execute("insert into crm_contact(Id,AccountId,FirstName,LastName,Title,Email,CreatedDate) values(?,?,?,?,?,?,?)",
                              (sfid("Contact", n), aid, first, last, title, f"{first.lower()}.{last.lower()}@{slug}.example", today))
                    out["contacts_new"] += 1
                    first, last = rnd.choice(["Lee", "Sam", "Noor", "Ira"]), rnd.choice(["Berg", "Rossi", "Adeyemi", "Chen"])
            else:
                c.execute("update crm_account set Type=?, MRR__c=?, LastModifiedDate=? where Tenant__c=?", (typ, mrr, today, t[0]))
        for k in c.execute("select tenant, start_date, end_date, term_months, status, escalator_pct from contracts").fetchall():
            c.execute("insert into crm_contract values(?,?,?,?,?,?,?) on conflict(Id) do update set StartDate=excluded.StartDate,"
                      " EndDate=excluded.EndDate, Status=excluded.Status", (sfid("Contract", k[0]), account_id(k[0]), k[1], k[2],
                                                                            k[3], "Activated" if k[4] == "active" else "Expired", k[5]))
        existing = {r[0]: r[1] for r in c.execute("select Source__c, Status from crm_asset")}
        next_n = c.execute("select coalesce(max(cast(substr(Id,4) as integer)),0) from crm_asset").fetchone()[0] + 1
        inserts, updates, retire = [], [], []
        for src, sql in ASSET_SOURCES:
            keys = set()
            for sid, tid, name, code, loc, qty in c.execute(sql).fetchall():
                key = f"{src}:{sid}"
                keys.add(key)
                if key in existing:
                    updates.append((name, account_id(tid), qty, key))
                else:
                    inserts.append((sfid("Asset", next_n), account_id(tid), name, code, loc, qty, key, today))
                    next_n += 1
            retire += [(today, k) for k, st in existing.items()
                       if st == "Installed" and k.startswith(src + ":") and k not in keys]
        c.executemany("insert into crm_asset(Id,AccountId,Name,ProductCode,Location__c,Quantity,Status,Source__c,InstallDate)"
                      " values(?,?,?,?,?,?,'Installed',?,?)", inserts)
        c.executemany("update crm_asset set Name=?, AccountId=?, Quantity=?, Status='Installed', UsageEndDate=null"
                      " where Source__c=?", updates)
        c.executemany("update crm_asset set Status='Retired', UsageEndDate=? where Source__c=?", retire)
        out["assets_new"], out["assets_retired"] = len(inserts), len(retire)
        last = c.execute("select coalesce(max(Ticket__c),0) from crm_case").fetchone()[0]
        rows = c.execute("select id, tenant, site, kind, priority, sim_date, sla_min, met, billed_usd from tickets where id>?",
                         (last,)).fetchall()
        c.executemany("insert into crm_case values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      [(sfid("Case", r[0]), account_id(r[1]), f"{r[3].replace('_', ' ').title()} at {r[2]}", r[3],
                        "High" if r[4] == "urgent" else "Medium", "Closed", "Remote Hands Portal", r[2], r[6], r[7], r[8],
                        r[5], r[0]) for r in rows])
        out["cases_new"] = len(rows)
    with db.conn() as c:
        if c.execute("select name from sqlite_master where name='net_impact'").fetchone():
            done = {r[0] for r in c.execute("select -Ticket__c from crm_case where Type='network_outage'")}
            rows = c.execute("select i.rowid, i.tenant, i.service_kind, i.service_id, i.minutes, i.credit_usd, n.sim_date,"
                             " n.target, n.cause, n.severity from net_impact i join net_incident n on n.id=i.incident"
                             " where i.incident>0").fetchall()
            new = [r for r in rows if r[0] not in done]
            c.executemany("insert into crm_case values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(sfid("Case", 9_000_000_000 + r[0]), account_id(r[1]),
                            f"Network outage: {r[2].replace('_', ' ')} {r[3]} ({r[8]} on {r[7]}, {r[4]:.0f} min)",
                            "network_outage", "High" if r[9] == "sev1" else "Medium", "Closed", "NOC",
                            r[7].split("-")[0].upper()[:4], None, 0, -r[5], r[6], -r[0]) for r in new])
            out["network_cases_new"] = len(new)
    db.set_setting("crm_synced", today)
    return out


def claim_orphan_metal(seed: int = 11) -> int:
    """Bare metal allocated at world seed had no customer attached, so it billed without an account.
    Give each orphan server to a customer present in that metro."""
    rnd = random.Random(seed)
    n = 0
    for r in db.rows("select metro, plan, allocated from metal_inventory"):
        owned = db.scalar("select count(*) from metal_servers where metro=? and plan=?", (r["metro"], r["plan"])) or 0
        missing = r["allocated"] - owned
        if missing <= 0:
            continue
        local = [x["tenant"] for x in db.rows("select distinct p.tenant from ports p join sites s on s.id=p.site"
                                               " join contracts k on k.tenant=p.tenant where s.metro=? and k.status='active'",
                                               (r["metro"],))]
        if not local:
            continue
        with db.conn() as c:
            c.executemany("insert into metal_servers(tenant,metro,plan,ts) values(?,?,?,?)",
                          [(rnd.choice(local), r["metro"], r["plan"], db.now()) for _ in range(missing)])
        n += missing
    return n


def upgrade() -> bool:
    """v0.3: create the CRM from the current world (idempotent)."""
    if (db.setting("world_version") or 1) >= 3:
        init()
        return False
    init()
    claim_orphan_metal()
    sync()
    db.set_setting("crm_started_ts", db.now())
    from . import bi
    bi.load()
    db.set_setting("world_version", 3)
    return True


# ---------------------------------------------------------------- pipeline


def _order_mrr(kind: str, p: dict) -> float:
    """List-price MRR estimate for an order, used as the opportunity amount."""
    from . import products, world
    P, Q = world.PRICES, products.PRICES
    if kind == "cabinets":
        return p.get("cabinets", 1) * (P["cabinet_month"] + p.get("kw_per_cabinet", 5) * P["kw_month"])
    if kind == "ai_cabinets":
        return p.get("cabinets", 1) * (P["cabinet_month"] + p.get("kw_per_cabinet", 60) * Q["ai_kw_month"])
    if kind == "port":
        return P["port_month"].get(int(p.get("speed_g", 10)), 800)
    if kind == "cross_connect":
        return P["cross_connect_month"]
    if kind == "vc":
        return p.get("mbps", 100) * P["vc_per_mbps_month"]
    if kind == "metal":
        return p.get("count", 1) * P["metal_month"].get(p.get("plan", "s1.small"), 350)
    if kind == "cloud_connection":
        return p.get("mbps", 100) * Q["cloud_connection_per_mbps"]
    if kind == "cloud_router":
        return Q["cloud_router"].get(p.get("package", "standard"), 800)
    if kind == "edge_device":
        from .products import EDGE_SIZES
        mult = 2 if p.get("ha") else 1
        return mult * (EDGE_SIZES.get(p.get("size", "medium"), 4) * Q["edge_per_vcpu"] + Q["edge_licence"].get(p.get("role", "router"), 250))
    if kind == "ix_port":
        return Q["ix_port"].get(int(p.get("speed_g", 10)), 1200)
    if kind == "internet_access":
        return p.get("commit_mbps", 1000) * Q["internet_per_mbps"] + Q["internet_port_fee"]
    return 0.0


def open_opportunity(tenant: int, kind: str, params: dict, close_in_days: int, typ: str = "New Business") -> str:
    from . import products
    init()
    today = products.today_d()
    n = (db.scalar("select coalesce(max(cast(substr(Id,4) as integer)),0) from crm_opportunity") or 0) + 1
    oid = sfid("Opportunity", n)
    mrr = round(_order_mrr(kind, params), 2)
    name = db.scalar("select name from tenants where id=?", (tenant,)) or f"Tenant {tenant}"
    db.execute("insert into crm_opportunity(Id,AccountId,Name,Type,StageName,Probability,Amount,MRR__c,CloseDate,Product__c,"
               "Params__c,CreatedDate,LastModifiedDate) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (oid, account_id(tenant), f"{name} - {kind.replace('_', ' ')}", typ, STAGES[0][0], STAGES[0][1],
                round(mrr * 12, 2), mrr, (today + __import__("datetime").timedelta(days=close_in_days)).isoformat(),
                kind, json.dumps(params), today.isoformat(), today.isoformat()))
    return oid


def advance_pipeline(rnd: random.Random) -> dict:
    """Move open new-business opportunities along; those at their close date are won (placing
    the order) or lost."""
    from . import products, world
    init()
    today = products.today()
    won = lost = moved = 0
    for o in db.rows("select * from crm_opportunity where IsClosed=0 and Type!='Renewal'"):
        if o["CloseDate"] <= today:
            if rnd.random() < WIN_RATE:
                tid = int(o["AccountId"][3:])
                order = world.place_order(tid, o["Product__c"], json.loads(o["Params__c"] or "{}"))
                db.execute("update crm_opportunity set StageName='Closed Won', Probability=100, IsClosed=1, IsWon=1,"
                           " Order__c=?, LastModifiedDate=? where Id=?", (order["order_id"], today, o["Id"]))
                won += 1
            else:
                db.execute("update crm_opportunity set StageName='Closed Lost', Probability=0, IsClosed=1, IsWon=0,"
                           " LastModifiedDate=? where Id=?", (today, o["Id"]))
                lost += 1
            continue
        # stage by elapsed share of the sales cycle
        start = o["CreatedDate"]
        span = max(1, (_d(o["CloseDate"]) - _d(start)).days)
        frac = (_d(today) - _d(start)).days / span
        stage = STAGES[0] if frac < 0.34 else STAGES[1] if frac < 0.67 else STAGES[2]
        if stage[0] != o["StageName"]:
            db.execute("update crm_opportunity set StageName=?, Probability=?, LastModifiedDate=? where Id=?",
                       (stage[0], stage[1], today, o["Id"]))
            moved += 1
    return {"won": won, "lost": lost, "advanced": moved}


def _d(s):
    import datetime as dt
    return dt.date.fromisoformat(s)


def open_renewals(horizon_days: int = 90) -> int:
    """Keeper opens a Renewal opportunity for every active contract ending within the horizon."""
    from . import products
    init()
    import datetime as dt
    horizon = (products.today_d() + dt.timedelta(days=horizon_days)).isoformat()
    n = 0
    for c in db.rows("select c.* from contracts c where c.status='active' and c.end_date<=?", (horizon,)):
        aid = account_id(c["tenant"])
        if db.one("select Id from crm_opportunity where AccountId=? and Type='Renewal' and IsClosed=0", (aid,)):
            continue
        p, _ = products.renewal_probability(c["tenant"], c["escalator_pct"])
        mrr = products.tenant_mrr(c["tenant"])
        n_id = (db.scalar("select coalesce(max(cast(substr(Id,4) as integer)),0) from crm_opportunity") or 0) + 1
        name = db.scalar("select name from tenants where id=?", (c["tenant"],))
        db.execute("insert into crm_opportunity(Id,AccountId,Name,Type,StageName,Probability,Amount,MRR__c,CloseDate,Product__c,"
                   "Params__c,CreatedDate,LastModifiedDate) values(?,?,?,'Renewal','Negotiation',?,?,?,?,'renewal','{}',?,?)",
                   (sfid("Opportunity", n_id), aid, f"{name} - renewal {c['end_date'][:4]}", round(p * 100, 1),
                    round(mrr * 12, 2), round(mrr, 2), c["end_date"], products.today(), products.today()))
        n += 1
    return n


def close_renewal(tenant: int, event: str, mrr_after: float):
    o = db.one("select Id from crm_opportunity where AccountId=? and Type='Renewal' and IsClosed=0", (account_id(tenant),))
    if not o:
        return
    from . import products
    won = event in ("renewed", "downsized")
    db.execute("update crm_opportunity set StageName=?, Probability=?, IsClosed=1, IsWon=?, MRR__c=?, Amount=?,"
               " LastModifiedDate=? where Id=?",
               ("Closed Won" if won else "Closed Lost", 100 if won else 0, int(won), round(mrr_after, 2),
                round(mrr_after * 12, 2), products.today(), o["Id"]))


# ---------------------------------------------------------------- REST + SOQL


def describe(obj: str) -> dict:
    if obj not in OBJECTS:
        raise ValueError(f"unknown object {obj}; one of {list(OBJECTS)}")
    init()
    cols = db.rows(f"pragma table_info({OBJECTS[obj]})")
    return {"name": obj, "label": LABELS[obj], "keyPrefix": PREFIX[obj], "queryable": True,
            "fields": [{"name": c["name"], "type": _sf_type(c["type"]), "custom": c["name"].endswith("__c")} for c in cols]}


def _sf_type(t):
    t = (t or "").lower()
    return "double" if "real" in t else "int" if "int" in t else "string"


def _record(obj, row):
    rec = {"attributes": {"type": obj, "url": f"/services/data/{API_VERSION}/sobjects/{obj}/{row.get('Id')}"}}
    for k, v in row.items():
        if k in ("IsClosed", "IsWon", "SLA_Met__c") and v is not None:
            v = bool(v)
        rec[k] = v
    return rec


def get(obj: str, rid: str) -> dict | None:
    if obj not in OBJECTS:
        raise ValueError(f"unknown object {obj}")
    init()
    r = db.one(f"select * from {OBJECTS[obj]} where Id=?", (rid,))
    return _record(obj, r) if r else None


SOQL = re.compile(r"^\s*select\s+(?P<fields>.+?)\s+from\s+(?P<obj>\w+)(?:\s+where\s+(?P<where>.+?))?"
                  r"(?:\s+order\s+by\s+(?P<order>\w+)(?:\s+(?P<dir>asc|desc))?)?(?:\s+limit\s+(?P<limit>\d+))?\s*$",
                  re.I | re.S)
COND = re.compile(r"^\s*(\w+)\s*(=|!=|<=|>=|<|>|like|in)\s*(.+?)\s*$", re.I)


def _literal(v: str):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return 1 if v.lower() == "true" else 0
    if v.lower() == "null":
        return None
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("\\'", "'")
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return v
        raise ValueError(f"bad literal {v}")


def query(soql: str, max_rows: int = 2000) -> dict:
    """A practical subset of SOQL: SELECT fields (or COUNT()) FROM Object [WHERE a op b AND ...]
    [ORDER BY field [ASC|DESC]] [LIMIT n]. Operators: = != < <= > >= LIKE IN ('a','b')."""
    m = SOQL.match(soql)
    if not m:
        raise ValueError("unsupported SOQL; use SELECT f1, f2 FROM Object [WHERE ...] [ORDER BY f] [LIMIT n]")
    obj = next((o for o in OBJECTS if o.lower() == m["obj"].lower()), None)
    if not obj:
        raise ValueError(f"unknown object {m['obj']}; one of {list(OBJECTS)}")
    init()
    table = OBJECTS[obj]
    valid = {c["name"].lower(): c["name"] for c in db.rows(f"pragma table_info({table})")}

    def col(f):
        f = f.strip()
        if f.lower() not in valid:
            raise ValueError(f"no field {f} on {obj}")
        return valid[f.lower()]
    fields_raw = m["fields"].strip()
    count = fields_raw.lower().replace(" ", "") == "count()"
    fields = ["Id"] if count else [col(f) for f in fields_raw.split(",")]
    if "Id" not in fields:
        fields = ["Id"] + fields
    where, args = [], []
    if m["where"]:
        for part in re.split(r"\s+and\s+", m["where"], flags=re.I):
            c = COND.match(part)
            if not c:
                raise ValueError(f"unsupported condition: {part}")
            f, op, val = col(c[1]), c[2].upper(), c[3]
            if op == "IN":
                items = [_literal(x) for x in val.strip().strip("()").split(",")]
                where.append(f"{f} in ({','.join('?' * len(items))})")
                args += items
            else:
                lit = _literal(val)
                if lit is None:
                    where.append(f"{f} is {'not ' if op == '!=' else ''}null")
                else:
                    where.append(f"{f} {op} ?")
                    args.append(lit)
    sql = f"select {', '.join(fields)} from {table}"
    if where:
        sql += " where " + " and ".join(where)
    if count:
        n = db.scalar(f"select count(*) from ({sql})", tuple(args))
        return {"totalSize": n, "done": True, "records": []}
    if m["order"]:
        sql += f" order by {col(m['order'])} {(m['dir'] or 'asc').upper()}"
    limit = min(int(m["limit"]) if m["limit"] else max_rows, max_rows)
    total = db.scalar(f"select count(*) from ({sql})", tuple(args))
    rows = db.rows(sql + f" limit {limit}", tuple(args))
    return {"totalSize": total, "done": len(rows) >= total, "records": [_record(obj, r) for r in rows]}


def summary() -> dict:
    init()
    return {
        "accounts": db.rows("select Type, count(*) n from crm_account group by Type"),
        "contacts": db.scalar("select count(*) from crm_contact"),
        "open_pipeline": db.rows("select StageName, Type, count(*) n, round(sum(MRR__c)) mrr from crm_opportunity"
                                 " where IsClosed=0 group by StageName, Type order by Type, StageName"),
        "closed_90d": db.rows("select StageName, count(*) n, round(sum(MRR__c)) mrr from crm_opportunity where IsClosed=1"
                              " and LastModifiedDate>=date(?, '-90 day') group by StageName", (_today(),)),
        "assets": db.rows("select Status, count(*) n from crm_asset group by Status"),
        "cases": db.scalar("select count(*) from crm_case"),
        "last_sync": db.setting("crm_synced"),
    }
