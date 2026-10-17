"""Finance: a double-entry general ledger and everything that posts to it.

Order-to-cash: quotes with discount approvals (Deal Desk), monthly invoices in the customer's currency
(Billing), payments, disputes and credit notes, dunning, credit holds, collections agency and write-offs
(Collections). Record-to-report: operating costs, depreciation, maintenance capex, interest and tax are
posted at the period close; the income statement, balance sheet and cash-flow statement are derived from
the ledger, so the balance sheet always balances. Treasury: bonds, a revolving credit facility, FX.
REIT metrics: FFO, AFFO, AFFO per share, leverage, interest cover, dividends.
Every number is simulated."""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import random

from . import db, products, world

SCHEMA = """
create table if not exists fin_gl (
  id integer primary key, sim_date text, period text, account text, amount real, memo text, ref text
);
create index if not exists fin_gl_pa on fin_gl(period, account);
create table if not exists fin_customer (
  tenant integer primary key, rating text, terms_days integer, currency text, pay_mean real, pay_sd real,
  default_p real, distressed integer default 0, cure integer default 0, credit_hold integer default 0, hold_since text
);
create table if not exists fin_quote (
  id integer primary key, opp text unique, tenant integer, kind text, metro text, sim_date text, list_mrr real,
  discount_pct real, net_mrr real, gm_pct real, approval text, status text, decision integer, note text
);
create table if not exists fin_invoice (
  id integer primary key, number text, tenant integer, period text, issue_date text, due_date text, currency text,
  fx_rate real, amount_usd real, balance_usd real, lines text, status text, expected_pay text, paid_date text,
  disputed integer default 0, dunning text default '', written_off real default 0
);
create index if not exists fin_invoice_st on fin_invoice(status, tenant);
create table if not exists fin_payment (
  id integer primary key, invoice integer, tenant integer, sim_date text, amount_usd real, fx_gain real
);
create table if not exists fin_dispute (
  id integer primary key, invoice integer, tenant integer, opened text, resolve_by text, reason text,
  amount_usd real, status text, resolved text, credit_usd real
);
create table if not exists fin_dunning (
  id integer primary key, tenant integer, invoice integer, sim_date text, stage text, balance_usd real
);
create table if not exists fin_month (
  period text primary key, closed_on text, income text, balance text, cashflow text, metrics text
);
create table if not exists fin_budget (period text, line text, amount real, primary key (period, line));
create table if not exists fin_debt (
  id integer primary key, name text, kind text, principal real, rate real, issued text, maturity text, status text
);
create table if not exists fin_fx (ccy text primary key, rate real, base real, updated text);
create table if not exists fin_dividend (
  id integer primary key, quarter text, per_share real, total_usd real, declared text, paid text, decision integer
);
"""

# account -> (type, statement label); debits are positive in fin_gl
ACCOUNTS = {
    "cash": ("asset", "Cash"), "ar": ("asset", "Accounts receivable"), "ppe": ("asset", "Property, plant and equipment"),
    "cip": ("asset", "Construction in progress"), "accum_dep": ("asset", "Accumulated depreciation"),
    "jv_investment": ("asset", "Investment in hyperscale joint ventures"), "jv_income": ("income", "Share of hyperscale JV income"),
    "revolver": ("liability", "Revolving credit facility"), "bonds": ("liability", "Senior notes"),
    "equity": ("equity", "Paid-in capital"), "dividends": ("equity", "Dividends declared"),
    "rev_recurring": ("income", "Recurring revenue"), "rev_usage": ("income", "Usage and services revenue"),
    "credit_notes": ("income", "Credits and allowances"),
    "power": ("expense", "Power"), "site_opex": ("expense", "Site operations"), "labour": ("expense", "Remote-hands staff"),
    "net_support": ("expense", "Network support and spares"), "sga": ("expense", "Selling, general and admin"),
    "bad_debt": ("expense", "Bad debt"), "depreciation": ("expense", "Depreciation"), "interest": ("expense", "Interest"),
    "tax": ("expense", "Income tax (taxable REIT subsidiary)"), "fx": ("expense", "FX losses (gains)"),
}
CCY = {"SJC": "USD", "ASH": "USD", "NYC": "USD", "LHR": "GBP", "FRA": "EUR", "SIN": "SGD", "NRT": "JPY", "SYD": "AUD"}
FX_SEED = {"USD": 1.0, "EUR": 0.92, "GBP": 0.78, "SGD": 1.34, "JPY": 150.0, "AUD": 1.52}
RATINGS = {  # mean days paid after due, sd, chance an invoice goes unpaid (customer becomes distressed)
    "A": (-1, 3, 0.0004), "B": (5, 6, 0.0015), "C": (18, 12, 0.006), "D": (40, 22, 0.02)}
GROSS_MARGIN = {"cabinets": .38, "ai_cabinets": .32, "port": .82, "cross_connect": .9, "vc": .74, "cloud_connection": .7,
                "cloud_router": .76, "edge_device": .64, "ix_port": .86, "metal": .22, "internet_access": .55}
SHARES = 24_000_000
SHELL_CAPEX_PER_SITE = 20_000_000.0
DEVICE_CAPEX = 45_000.0
DEP_MONTHS = 240          # blended life: shells and halls 25-30 years, M&E 15, network gear 5
MAINT_CAPEX_PCT = 0.025   # recurring (maintenance) capex as a share of revenue
SGA_PCT, SGA_FIXED = 0.05, 800_000.0
TAX_RATE_ON_EBT = 0.015   # REITs pay little income tax; this is the taxable subsidiary
MIN_CASH, TARGET_CASH = 120_000_000.0, 160_000_000.0

POWER_COST_HOOK = None    # growth module: fn(metro, facility_kwh_month, grid_price) -> usd (applies PPAs)
CAPEX_COMMITMENTS_HOOK = None  # growth module: fn() -> [{"period", "usd"}] future construction spend
JV_INCOME_HOOK = None     # growth module: fn() -> usd of vix's share of hyperscale JV income for a month


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- ledger


def period_of(date: str) -> str:
    return date[:7]


def post(date: str, entries: list[tuple[str, float]], memo: str, ref: str = ""):
    entries = [(a, round(v, 2)) for a, v in entries if abs(v) >= 0.005]
    bal = round(sum(v for _, v in entries), 2)
    if abs(bal) > 0.05:
        raise ValueError(f"unbalanced journal {memo}: {bal}")
    if bal:
        entries[0] = (entries[0][0], round(entries[0][1] - bal, 2))
    for a, _ in entries:
        if a not in ACCOUNTS:
            raise ValueError(f"unknown account {a}")
    with db.conn() as c:
        c.executemany("insert into fin_gl(sim_date,period,account,amount,memo,ref) values(?,?,?,?,?,?)",
                      [(date, period_of(date), a, v, memo, ref) for a, v in entries])


def balances(through: str | None = None) -> dict:
    q, args = "select account, sum(amount) v from fin_gl", ()
    if through:
        q, args = q + " where period<=?", (through,)
    out = {a: 0.0 for a in ACCOUNTS}
    for r in db.rows(q + " group by account", args):
        out[r["account"]] = r["v"] or 0.0
    return out


def activity(period: str) -> dict:
    out = {a: 0.0 for a in ACCOUNTS}
    for r in db.rows("select account, sum(amount) v from fin_gl where period=? group by account", (period,)):
        out[r["account"]] = r["v"] or 0.0
    return out


def opened() -> bool:
    try:
        return bool(db.scalar("select 1 from fin_gl limit 1"))
    except Exception:  # noqa: BLE001  (tables not created yet)
        return False


def open_books():
    """Opening balance sheet for the existing company, built from the world it already has."""
    init()
    if opened():
        return {"opened": False}
    today = products.today()
    halls = db.scalar("select coalesce(sum(build_capex_usd),0) from halls") or 0
    sites = db.scalar("select count(*) from sites") or 0
    devices = (db.scalar("select count(*) from net_device") or 0) if db.one(
        "select name from sqlite_master where name='net_device'") else 0
    ppe = halls + sites * SHELL_CAPEX_PER_SITE + devices * DEVICE_CAPEX
    accum = 0.40 * ppe
    bonds_total = 0.46 * ppe
    cash = 0.11 * ppe
    d0 = products.today_d()
    for name, share, years, rate in (("Senior notes 3.90% due", .30, 1.5, .039), ("Senior notes 4.60% due", .40, 4, .046),
                                     ("Senior notes 5.10% due", .30, 8, .051)):
        mat = products.add_months(d0, years * 12)
        db.execute("insert into fin_debt(name,kind,principal,rate,issued,maturity,status) values(?,?,?,?,?,?,'outstanding')",
                   (f"{name} {mat.year}", "bond", round(bonds_total * share, -5), rate, today, mat.isoformat()))
    bonds_total = db.scalar("select sum(principal) from fin_debt where kind='bond'")
    db.execute("insert into fin_debt(name,kind,principal,rate,issued,maturity,status) values(?,?,?,?,?,?,'outstanding')",
               ("Revolving credit facility", "revolver", 0.0, 0.0, today, products.add_months(d0, 60).isoformat()))
    db.set_setting("revolver_limit", round(max(250e6, 0.2 * ppe), -6))
    db.set_setting("base_rate", 0.046)
    db.set_setting("shares_outstanding", SHARES)
    post(today, [("cash", cash), ("ppe", ppe), ("accum_dep", -accum), ("bonds", -bonds_total),
                 ("equity", -(cash + ppe - accum - bonds_total))], "opening balance sheet", "open")
    for c, r in FX_SEED.items():
        db.execute("insert or replace into fin_fx(ccy,rate,base,updated) values(?,?,?,?)", (c, r, r, today))
    ensure_customers(random.Random(7))
    return {"opened": True, "ppe": ppe, "cash": cash, "bonds": bonds_total}


# ---------------------------------------------------------------- customers, FX


def ensure_customers(rnd: random.Random):
    have = {r["tenant"] for r in db.rows("select tenant from fin_customer")}
    for t in db.rows("select id, kind, home_metro from tenants"):
        if t["id"] not in have:
            add_customer(t["id"], rnd, t)


def add_customer(tid, rnd, t=None):
    t = t or db.one("select id, kind, home_metro from tenants where id=?", (tid,))
    w = {"carrier": [5, 4, 1, .3], "cloud": [7, 3, .5, .1], "content": [3, 4, 2, .6], "enterprise": [4, 4, 2, .8],
         "financial": [6, 3, .6, .2]}.get(t["kind"], [3, 3, 2, 1])
    rating = rnd.choices("ABCD", w)[0]
    mean, sd, dp = RATINGS[rating]
    db.execute("insert or ignore into fin_customer(tenant,rating,terms_days,currency,pay_mean,pay_sd,default_p) values(?,?,?,?,?,?,?)",
               (tid, rating, 45 if t["kind"] == "enterprise" else 30, CCY.get(t["home_metro"], "USD"), mean, sd, dp))


def fx(ccy: str) -> float:
    return db.scalar("select rate from fin_fx where ccy=?", (ccy,)) or 1.0


def fx_step(rnd: random.Random):
    for r in db.rows("select * from fin_fx where ccy!='USD'"):
        drift = 0.03 * (r["base"] - r["rate"]) / r["base"]
        rate = r["rate"] * (1 + drift + rnd.gauss(0, 0.0035))
        db.execute("update fin_fx set rate=?, updated=? where ccy=?", (rate, products.today(), r["ccy"]))


# ---------------------------------------------------------------- run-rate costs (flash P&L and period close)


def run_rate() -> dict:
    """Monthly run-rate of revenue and operating cost lines from the current state of the world."""
    rev = world.revenue_breakdown()
    tele = {t["hall"]: t for t in db.rows(
        "select t.* from telemetry t join (select hall, max(id) id from telemetry group by hall) l on l.id=t.id")}
    metros = {m["code"]: m for m in db.rows("select * from metros")}
    power, per_metro = 0.0, {}
    kwh_by_metro = {}
    for h in world.halls():
        t = tele.get(h["id"])
        fac_kw = t["facility_kw"] if t else h["it_load_kw"] * 1.45
        kwh_by_metro[h["metro"]] = kwh_by_metro.get(h["metro"], 0.0) + fac_kw * 730
        pm = per_metro.setdefault(h["metro"], {"metro": h["metro"], "power_cost": 0.0, "cabinets_sold": 0, "cabinets_total": 0})
        pm["cabinets_sold"] += h["cabinets_sold"]
        pm["cabinets_total"] += h["cabinets_total"]
    for m, kwh in kwh_by_metro.items():
        price = metro_power_price(m, metros)
        cost = POWER_COST_HOOK(m, kwh, price) if POWER_COST_HOOK else kwh * price
        per_metro[m]["power_cost"] = cost
        power += cost
    sites = db.scalar("select count(*) from sites")
    opex = sites * world.SITE_OPEX_MONTH
    techs = db.scalar("select coalesce(sum(techs),0) from sites") or 0
    labour = techs * products.TECH_COST_MONTH
    since30 = (products.today_d() - dt.timedelta(days=30)).isoformat()
    try:
        credits = db.scalar("select coalesce(sum(credits_usd),0) from net_incident where sim_date>?", (since30,)) or 0
        n_dev = db.scalar("select count(*) from net_device") or 0
    except Exception:  # noqa: BLE001
        credits, n_dev = 0, 0
    net_support = n_dev * 900.0
    sga = SGA_PCT * rev["total"] + SGA_FIXED
    return {"revenue": rev, "power_cost": power, "per_metro": per_metro, "site_opex": opex, "techs": techs,
            "tech_labour": labour, "sla_credits": credits, "net_support": net_support, "devices": n_dev, "sga": sga,
            "kwh_by_metro": kwh_by_metro}


def metro_power_price(code, metros=None):
    """Grid price per kWh: the metro's base price moved by the market index Steward/Volt track."""
    base = (metros or {}).get(code, {}).get("power_usd_kwh") if metros else db.scalar(
        "select power_usd_kwh from metros where code=?", (code,))
    idx = (db.setting("power_index") or {}).get(code, 1.0)
    return (base or 0.2) * idx


# ---------------------------------------------------------------- deal desk (quotes)


def gm_after(kind, discount):
    gm = GROSS_MARGIN.get(kind, .5)
    return (gm - discount) / (1 - discount) if discount < 1 else -1


PRESSURE_HOOK = None   # market module: fn(tenant_row, kind, metro) -> (extra discount, competitor name)


def quote_for(opp_id: str):
    """Margin prices every new opportunity: the discount depends on deal size, how price-sensitive the
    customer is and how hard competitors are pushing in that metro. Approval follows the authority matrix:
    up to 10% Deal Desk, up to 20% the CFO (only if gross margin stays above 25%), above that the founder
    for deals of $40k/month or more."""
    init()
    o = db.one("select * from crm_opportunity where Id=?", (opp_id,))
    if not o or db.one("select id from fin_quote where opp=?", (opp_id,)):
        return None
    tid = int(o["AccountId"][3:])
    t = db.one("select * from tenants where id=?", (tid,))
    params = json.loads(o["Params__c"] or "{}")
    metro = params.get("metro") or params.get("a_metro") or (params.get("site") or t["home_metro"])[:3]
    mrr = o["MRR__c"] or 0
    h = int(hashlib.md5(opp_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    base = 0.01 + 0.03 * h if mrr < 2000 else 0.03 + 0.05 * h if mrr < 20000 else 0.06 + 0.08 * h
    comp = None
    if PRESSURE_HOOK:
        extra, comp = PRESSURE_HOOK(t, o["Product__c"], metro)
        base += extra
    from . import harness
    base *= float(harness.get("dealdesk.discount_scale"))
    disc = round(min(base, 0.35), 3)
    approval, status, decision, note = "deal desk", "approved", None, ""
    try:
        channel = db.scalar("select channel from mkt_opp_channel where opp=?", (opp_id,))
    except Exception:  # noqa: BLE001
        channel = None
    if channel in ("portal", "marketplace"):
        disc, approval = 0.0, "self-serve list price"
    if disc > 0.10:
        if disc > 0.20 and mrr >= 40000:
            from .company.engine import decision_add
            approval, status = "founder", "pending"
            decision = decision_add("Quill", "discount_approval", f"Approve {disc:.0%} discount for {t['name']}",
                                    {"opp": opp_id, "tenant": t["name"], "kind": o["Product__c"], "list_mrr": round(mrr),
                                     "discount_pct": disc, "net_mrr": round(mrr * (1 - disc)),
                                     "gross_margin_after": round(gm_after(o["Product__c"], disc), 3),
                                     "competitor": comp, "close_date": o["CloseDate"]})
        else:
            floor = float(harness.get("dealdesk.gm_floor"))
            cap = 0.20
            while cap > 0 and gm_after(o["Product__c"], min(disc, cap)) < floor:
                cap = round(cap - 0.01, 2)
            if disc > cap:
                note = f"CFO capped {disc:.0%} to {cap:.0%} to keep gross margin at or above {floor:.0%}"
                disc = cap
            approval = "cfo"
    net = round(mrr * (1 - disc), 2)
    db.execute("insert into fin_quote(opp,tenant,kind,metro,sim_date,list_mrr,discount_pct,net_mrr,gm_pct,approval,status,decision,note)"
               " values(?,?,?,?,?,?,?,?,?,?,?,?,?)", (opp_id, tid, o["Product__c"], metro, products.today(), mrr, disc, net,
                                                     round(gm_after(o["Product__c"], disc), 3), approval, status, decision, note))
    db.execute("update crm_opportunity set MRR__c=?, Amount=? where Id=?", (net, round(net * 12, 2), opp_id))
    return {"opp": opp_id, "discount": disc, "approval": approval}


def on_closed(o, won, info, order):
    q = db.one("select * from fin_quote where opp=?", (o["Id"],))
    if not q:
        return
    if q["status"] == "pending":  # founder never answered before the close date: CFO caps it
        disc = 0.20
        db.execute("update fin_quote set discount_pct=?, net_mrr=?, approval='cfo', note='founder did not decide by the "
                   "close date; CFO capped at 20%' where id=?", (disc, round(q["list_mrr"] * 0.8, 2), q["id"]))
        q = db.one("select * from fin_quote where id=?", (q["id"],))
    db.execute("update fin_quote set status=? where id=?", ("won" if won else "lost", q["id"]))
    if won and q["discount_pct"] > 0:
        tid = q["tenant"]
        cur = products.tenant_mrr(tid)
        idx = db.scalar("select price_index from tenants where id=?", (tid,)) or 1.0
        new = idx * (1 - q["discount_pct"] * q["list_mrr"] / max(1.0, cur + q["list_mrr"]))
        db.execute("update tenants set price_index=? where id=?", (round(new, 5), tid))


# ---------------------------------------------------------------- billing


def _prev_period(p: str) -> str:
    y, m = map(int, p.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def issue_invoices(period: str, rnd: random.Random) -> dict:
    """Bill every active customer for the period: recurring charges in advance, last month's remote hands
    in arrears, less network SLA credits earned last month. Invoices are in the customer's currency."""
    if db.scalar("select count(*) from fin_invoice where period=?", (period,)):
        return {"invoices": 0}
    ensure_customers(rnd)
    today = products.today()
    prev = _prev_period(period)
    lines_all = products.lines_by_tenant()
    usage = {r["tenant"]: r["b"] for r in db.rows(
        "select tenant, sum(billed_usd) b from tickets where substr(sim_date,1,7)=? group by tenant", (prev,))}
    credits, outages = {}, {}
    try:
        for r in db.rows("select i.tenant, sum(i.credit_usd) c, count(distinct i.incident) n from net_impact i join net_incident n"
                         " on n.id=i.incident where substr(n.sim_date,1,7)=? group by i.tenant", (prev,)):
            credits[r["tenant"]], outages[r["tenant"]] = r["c"] or 0, r["n"]
    except Exception:  # noqa: BLE001
        pass
    misses = {r["tenant"]: r["n"] for r in db.rows(
        "select tenant, count(*) n from tickets where met=0 and substr(sim_date,1,7)=? group by tenant", (prev,))}
    cust = {r["tenant"]: r for r in db.rows("select * from fin_customer")}
    seq = db.scalar("select count(*) from fin_invoice") or 0
    n, total, disputes = 0, 0.0, 0
    for c in db.rows("select tenant from contracts where status='active'"):
        tid = c["tenant"]
        ln = lines_all.get(tid, {})
        recurring = sum(v for k, v in ln.items() if k != "remote_hands")
        use = usage.get(tid, 0.0)
        cr = min(credits.get(tid, 0.0), recurring + use)
        amount = round(recurring + use - cr, 2)
        if amount <= 0:
            continue
        cu = cust.get(tid)
        if not cu:
            add_customer(tid, rnd)
            cu = db.one("select * from fin_customer where tenant=?", (tid,))
        seq += 1
        due = (products.today_d() + dt.timedelta(days=cu["terms_days"])).isoformat()
        if not cu["distressed"] and rnd.random() < cu["default_p"]:
            db.execute("update fin_customer set distressed=1, cure=? where tenant=?", (1 if rnd.random() < 0.5 else 0, tid))
            cu = dict(cu, distressed=1)
        exp = None if cu["distressed"] else (dt.date.fromisoformat(due) + dt.timedelta(
            days=round(rnd.gauss(cu["pay_mean"], cu["pay_sd"])))).isoformat()
        if exp and exp < today:
            exp = today
        rate = fx(cu["currency"])
        detail = {k: round(v, 2) for k, v in ln.items() if k != "remote_hands"}
        if use:
            detail["remote_hands (" + prev + ")"] = round(use, 2)
        if cr:
            detail["network SLA credits (" + prev + ")"] = -round(cr, 2)
        num = f"INV-{period.replace('-', '')}-{seq:06d}"
        iid = db.execute("insert into fin_invoice(number,tenant,period,issue_date,due_date,currency,fx_rate,amount_usd,balance_usd,"
                         "lines,status,expected_pay) values(?,?,?,?,?,?,?,?,?,?,'open',?)",
                         (num, tid, period, today, due, cu["currency"], rate, amount, amount, json.dumps(detail), exp))
        post(today, [("ar", amount), ("rev_recurring", -recurring), ("rev_usage", -use), ("credit_notes", cr)],
             f"invoice {num}", f"inv:{iid}")
        p_disp = 0.012 + 0.03 * min(3, outages.get(tid, 0)) + (0.02 if misses.get(tid, 0) > 2 else 0)
        if rnd.random() < p_disp:
            amt = round(amount * rnd.uniform(0.04, 0.2), 2)
            reason = ("service credits not applied for an outage" if outages.get(tid) else
                      "remote-hands charges for missed SLAs" if misses.get(tid) else "pricing does not match the order form")
            db.execute("insert into fin_dispute(invoice,tenant,opened,resolve_by,reason,amount_usd,status) values(?,?,?,?,?,?,'open')",
                       (iid, tid, today, (products.today_d() + dt.timedelta(days=rnd.randint(5, 20))).isoformat(), reason, amt))
            db.execute("update fin_invoice set disputed=1 where id=?", (iid,))
            disputes += 1
        n += 1
        total += amount
    return {"invoices": n, "billed_usd": round(total), "disputes": disputes}


def _pay(inv, amount_usd, today, memo):
    rate_now = fx(inv["currency"])
    received = round(amount_usd * inv["fx_rate"] / rate_now, 2)
    gain = round(received - amount_usd, 2)
    post(today, [("cash", received), ("ar", -amount_usd), ("fx", -gain)], memo, f"inv:{inv['id']}")
    db.execute("insert into fin_payment(invoice,tenant,sim_date,amount_usd,fx_gain) values(?,?,?,?,?)",
               (inv["id"], inv["tenant"], today, received, gain))
    bal = round(inv["balance_usd"] - amount_usd, 2)
    db.execute("update fin_invoice set balance_usd=?, status=?, paid_date=? where id=?",
               (bal, "paid" if bal <= 0.01 else "open", today if bal <= 0.01 else None, inv["id"]))
    return received


def process_payments(rnd: random.Random) -> dict:
    today = products.today()
    paid = n = 0
    for inv in db.rows("select * from fin_invoice where status='open' and expected_pay is not null and expected_pay<=?", (today,)):
        held = db.scalar("select coalesce(sum(amount_usd),0) from fin_dispute where invoice=? and status='open'", (inv["id"],)) or 0
        amt = round(inv["balance_usd"] - held, 2)
        if amt <= 0.01:
            continue
        paid += _pay(inv, amt, today, f"payment {inv['number']}")
        n += 1
        if held:
            db.execute("update fin_invoice set expected_pay=null where id=?", (inv["id"],))
    return {"payments": n, "received_usd": round(paid)}


def resolve_disputes(rnd: random.Random) -> dict:
    today = products.today()
    granted = rejected = 0
    for d in db.rows("select * from fin_dispute where status='open' and resolve_by<=?", (today,)):
        inv = db.one("select * from fin_invoice where id=?", (d["invoice"],))
        valid = 0.75 if "outage" in d["reason"] or "SLA" in d["reason"] else 0.45
        if rnd.random() < valid:
            amt = min(d["amount_usd"], inv["balance_usd"])
            post(today, [("credit_notes", amt), ("ar", -amt)], f"credit note on {inv['number']}: {d['reason']}", f"inv:{inv['id']}")
            bal = round(inv["balance_usd"] - amt, 2)
            db.execute("update fin_invoice set balance_usd=?, status=?, paid_date=? where id=?",
                       (bal, "paid" if bal <= 0.01 else "open", today if bal <= 0.01 else None, inv["id"]))
            db.execute("update fin_dispute set status='credited', resolved=?, credit_usd=? where id=?", (today, amt, d["id"]))
            granted += 1
        else:
            db.execute("update fin_dispute set status='rejected', resolved=?, credit_usd=0 where id=?", (today, d["id"]))
            rejected += 1
        cu = db.one("select distressed from fin_customer where tenant=?", (inv["tenant"],))
        if not (cu and cu["distressed"]):
            db.execute("update fin_invoice set expected_pay=? where id=? and status='open'",
                       ((products.today_d() + dt.timedelta(days=rnd.randint(3, 10))).isoformat(), inv["id"]))
    return {"credited": granted, "rejected": rejected}


# ---------------------------------------------------------------- collections

def stages():
    from . import harness
    hold, wo = int(harness.get("collections.hold_days")), int(harness.get("collections.writeoff_days"))
    return [(7, "reminder"), (30, "final_notice"), (hold, "credit_hold"), (max(hold + 15, wo - 30), "collections_agency"), (wo, "write_off")]


def collections(rnd: random.Random) -> dict:
    """Reckon's dunning ladder. Distressed customers either recover through the agency or are written off and
    their services terminated for non-payment."""
    today = products.today()
    td = products.today_d()
    out = {"reminders": 0, "holds": 0, "agency": 0, "recovered_usd": 0, "written_off_usd": 0, "terminated": []}
    for inv in db.rows("select * from fin_invoice where status='open' and due_date<?", (today,)):
        late = (td - dt.date.fromisoformat(inv["due_date"])).days
        stage = None
        for d, s in stages():
            if late >= d:
                stage = s
        if not stage or stage in (inv["dunning"] or "").split(","):
            continue
        db.execute("update fin_invoice set dunning=? where id=?", (((inv["dunning"] or "") + "," + stage).strip(","), inv["id"]))
        db.execute("insert into fin_dunning(tenant,invoice,sim_date,stage,balance_usd) values(?,?,?,?,?)",
                   (inv["tenant"], inv["id"], today, stage, inv["balance_usd"]))
        cu = db.one("select * from fin_customer where tenant=?", (inv["tenant"],))
        if stage in ("reminder", "final_notice"):
            out["reminders"] += 1
        elif stage == "credit_hold" and not cu["credit_hold"]:
            db.execute("update fin_customer set credit_hold=1, hold_since=? where tenant=?", (today, inv["tenant"]))
            out["holds"] += 1
        elif stage == "collections_agency":
            out["agency"] += 1
            if cu["distressed"] and cu["cure"]:
                rec = 0.0
                for o in db.rows("select * from fin_invoice where tenant=? and status='open'", (inv["tenant"],)):
                    rec += _pay(o, o["balance_usd"], today, f"agency recovery {o['number']}")
                db.execute("update fin_customer set distressed=0, cure=0 where tenant=?", (inv["tenant"],))
                out["recovered_usd"] += round(rec)
        elif stage == "write_off":
            tid = inv["tenant"]
            wo = 0.0
            for o in db.rows("select * from fin_invoice where tenant=? and status='open'", (tid,)):
                post(today, [("bad_debt", o["balance_usd"]), ("ar", -o["balance_usd"])], f"write-off {o['number']}", f"inv:{o['id']}")
                db.execute("update fin_invoice set status='written_off', written_off=balance_usd, balance_usd=0 where id=?", (o["id"],))
                wo += o["balance_usd"]
            out["written_off_usd"] += round(wo)
            if db.scalar("select status from contracts where tenant=?", (tid,)) == "active":
                before = products.tenant_mrr(tid)
                products.release_tenant(tid, 1.0)
                db.execute("update contracts set status='ended' where tenant=?", (tid,))
                db.execute("insert into contract_events(ts,sim_date,tenant,event,mrr_before,mrr_after,note) values(?,?,?,?,?,?,?)",
                           (db.now(), today, tid, "churned", round(before, 2), 0.0, "terminated for non-payment"))
                for hook in products.ON_CONTRACT_EVENT:
                    hook(tid, "churned", before, 0.0)
                out["terminated"].append(db.scalar("select name from tenants where id=?", (tid,)))
            db.execute("update fin_customer set distressed=0, credit_hold=0 where tenant=?", (tid,))
    # lift holds once nothing is more than 30 days late
    lifted = 0
    for cu in db.rows("select tenant from fin_customer where credit_hold=1"):
        worst = db.scalar("select min(due_date) from fin_invoice where tenant=? and status='open'", (cu["tenant"],))
        if not worst or (td - dt.date.fromisoformat(worst)).days <= 30:
            db.execute("update fin_customer set credit_hold=0, hold_since=null where tenant=?", (cu["tenant"],))
            lifted += 1
    out["holds_lifted"] = lifted
    return out


def ar_aging() -> dict:
    td = products.today_d()
    buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    for inv in db.rows("select due_date, balance_usd from fin_invoice where status='open'"):
        late = (td - dt.date.fromisoformat(inv["due_date"])).days
        k = "current" if late <= 0 else "1-30" if late <= 30 else "31-60" if late <= 60 else "61-90" if late <= 90 else "90+"
        buckets[k] += inv["balance_usd"]
    total = sum(buckets.values())
    rev90 = -sum((r["v"] or 0) for r in db.rows(
        "select sum(amount) v from fin_gl where account in ('rev_recurring','rev_usage','credit_notes') and sim_date>?",
        ((td - dt.timedelta(days=90)).isoformat(),)))
    return {"buckets": {k: round(v) for k, v in buckets.items()}, "total": round(total),
            "dso_days": round(total / rev90 * 90, 1) if rev90 > 0 else None,
            "on_hold": db.scalar("select count(*) from fin_customer where credit_hold=1"),
            "distressed": db.scalar("select count(*) from fin_customer where distressed=1")}


# ---------------------------------------------------------------- treasury


def debt_summary() -> dict:
    rows = db.rows("select * from fin_debt where status='outstanding' order by maturity")
    rev = next((r for r in rows if r["kind"] == "revolver"), None)
    limit = db.setting("revolver_limit") or 0
    return {"instruments": rows, "bonds": round(sum(r["principal"] for r in rows if r["kind"] == "bond")),
            "revolver_drawn": round(rev["principal"]) if rev else 0, "revolver_limit": limit,
            "revolver_available": round(limit - (rev["principal"] if rev else 0)), "base_rate": db.setting("base_rate") or 0.046}


def revolver_rate():
    return (db.setting("base_rate") or 0.046) + 0.012


def draw_revolver(amount, date, memo="revolver draw"):
    rev = db.one("select * from fin_debt where kind='revolver' and status='outstanding'")
    limit = db.setting("revolver_limit") or 0
    amount = min(amount, limit - rev["principal"])
    if amount <= 0:
        return 0.0
    db.execute("update fin_debt set principal=principal+? where id=?", (amount, rev["id"]))
    post(date, [("cash", amount), ("revolver", -amount)], memo, "revolver")
    return amount


def repay_revolver(amount, date):
    rev = db.one("select * from fin_debt where kind='revolver' and status='outstanding'")
    amount = min(amount, rev["principal"])
    if amount <= 0:
        return 0.0
    db.execute("update fin_debt set principal=principal-? where id=?", (amount, rev["id"]))
    post(date, [("revolver", amount), ("cash", -amount)], "revolver repayment", "revolver")
    return amount


def issue_bond(amount, rate, years, date, refinance_id=None) -> dict:
    mat = products.add_months(dt.date.fromisoformat(date), years * 12)
    fees = round(amount * 0.008, 2)
    bid = db.execute("insert into fin_debt(name,kind,principal,rate,issued,maturity,status) values(?,?,?,?,?,?,'outstanding')",
                     (f"Senior notes {rate:.2%} due {mat.year}", "bond", amount, rate, date, mat.isoformat()))
    post(date, [("cash", amount - fees), ("interest", fees), ("bonds", -amount)], f"bond issue {amount:,.0f} at {rate:.2%}", f"debt:{bid}")
    if refinance_id:
        old = db.one("select * from fin_debt where id=?", (refinance_id,))
        post(date, [("bonds", old["principal"]), ("cash", -old["principal"])], f"redeem {old['name']}", f"debt:{old['id']}")
        db.execute("update fin_debt set status='redeemed' where id=?", (refinance_id,))
    return {"bond": bid, "amount": amount, "rate": rate, "maturity": mat.isoformat()}


def min_cash():
    from . import harness
    return float(harness.get("treasury.min_cash_musd")) * 1e6


def treasury_daily(rnd: random.Random) -> dict:
    """Keep cash between the floor and the target with the revolver; repay maturing notes."""
    today = products.today()
    if rnd.random() < 0.05:
        r = (db.setting("base_rate") or 0.046) + rnd.gauss(0, 0.0008)
        db.set_setting("base_rate", round(min(0.075, max(0.025, r)), 5))
    out = {"drawn": 0.0, "repaid": 0.0, "matured": []}
    for b in db.rows("select * from fin_debt where kind='bond' and status='outstanding' and maturity<=?", (today,)):
        cash = balances()["cash"]
        if cash < b["principal"] + min_cash():
            out["drawn"] += draw_revolver(b["principal"] + min_cash() - cash, today, f"revolver draw to repay {b['name']}")
        post(today, [("bonds", b["principal"]), ("cash", -b["principal"])], f"repay {b['name']} at maturity", f"debt:{b['id']}")
        db.execute("update fin_debt set status='repaid' where id=?", (b["id"],))
        out["matured"].append(b["name"])
    cash = balances()["cash"]
    target = min_cash() * 4 / 3
    if cash < min_cash():
        out["drawn"] += draw_revolver(target - cash, today)
    elif cash > target * 1.6:
        out["repaid"] += repay_revolver(cash - target * 1.2, today)
    return out


# ---------------------------------------------------------------- period close and statements


def last_day(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{period}-{calendar.monthrange(y, m)[1]:02d}"


def close_period(period: str) -> dict:
    """Post the month's operating costs, depreciation, maintenance capex, interest and tax, then derive the
    statements from the ledger."""
    if db.one("select period from fin_month where period=?", (period,)):
        return statements(period)
    first = db.scalar("select min(period) from fin_gl")
    if not first or period < first:
        raise ValueError(f"the books opened in {first}; there is nothing to close for {period}")
    if period >= products.today()[:7]:
        raise ValueError(f"{period} is not over yet")
    date = last_day(period)
    rr = run_rate()
    act = activity(period)
    revenue = -(act["rev_recurring"] + act["rev_usage"] + act["credit_notes"])
    sga = SGA_PCT * revenue + SGA_FIXED
    post(date, [("power", rr["power_cost"]), ("site_opex", rr["site_opex"]), ("labour", rr["tech_labour"]),
                ("net_support", rr["net_support"]), ("sga", sga),
                ("cash", -(rr["power_cost"] + rr["site_opex"] + rr["tech_labour"] + rr["net_support"] + sga))],
         f"operating costs {period}", "opex")
    if JV_INCOME_HOOK:
        jv = JV_INCOME_HOOK()
        if jv:
            post(date, [("cash", jv), ("jv_income", -jv)], f"hyperscale JV distributions {period}", "jv")
    bal = balances(period)
    dep = bal["ppe"] / DEP_MONTHS
    post(date, [("depreciation", dep), ("accum_dep", -dep)], f"depreciation {period}", "dep")
    maint = MAINT_CAPEX_PCT * revenue
    post(date, [("ppe", maint), ("cash", -maint)], f"maintenance capex {period}", "maintenance")
    ints = sum(d["principal"] * d["rate"] / 12 for d in db.rows("select * from fin_debt where status='outstanding' and kind='bond'"))
    rv = db.one("select principal from fin_debt where kind='revolver' and status='outstanding'")
    ints += (rv["principal"] if rv else 0) * revolver_rate() / 12
    post(date, [("interest", ints), ("cash", -ints)], f"interest {period}", "interest")
    act = activity(period)
    ebt = -sum(act[a] for a, (t, _) in ACCOUNTS.items() if t in ("income", "expense"))
    tax = max(0.0, ebt) * TAX_RATE_ON_EBT
    post(date, [("tax", tax), ("cash", -tax)], f"income tax {period}", "tax")
    st = statements(period)
    db.execute("insert or replace into fin_month(period,closed_on,income,balance,cashflow,metrics) values(?,?,?,?,?,?)",
               (period, products.today(), json.dumps(st["income"]), json.dumps(st["balance"]), json.dumps(st["cashflow"]),
                json.dumps(st["metrics"])))
    return st


def statements(period: str) -> dict:
    a = activity(period)
    end = balances(period)
    start = balances(_prev_period(period))
    for r in db.rows("select account, sum(amount) v from fin_gl where period=? and ref='open' group by account", (period,)):
        start[r["account"]] += r["v"]  # the opening balance sheet is not a cash flow
    rev_rec, rev_use, credits, jv = -a["rev_recurring"], -a["rev_usage"], -a["credit_notes"], -a["jv_income"]
    revenue = rev_rec + rev_use + credits
    cor = a["power"] + a["site_opex"] + a["labour"] + a["net_support"]
    gross = revenue - cor
    ebitda = gross - a["sga"] - a["bad_debt"] + jv
    op_inc = ebitda - a["depreciation"]
    ebt = op_inc - a["interest"] - a["fx"]
    ni = ebt - a["tax"]
    income = {"recurring_revenue": rev_rec, "usage_revenue": rev_use, "credits": credits, "revenue": revenue,
              "power": a["power"], "site_operations": a["site_opex"], "remote_hands_staff": a["labour"],
              "network_support": a["net_support"], "cost_of_revenue": cor, "gross_profit": gross, "sga": a["sga"],
              "bad_debt": a["bad_debt"], "jv_income": jv, "adj_ebitda": ebitda, "depreciation": a["depreciation"], "operating_income": op_inc,
              "interest": a["interest"], "fx_loss": a["fx"], "pretax_income": ebt, "tax": a["tax"], "net_income": ni}
    cum_ni = -sum(end[k] for k, (t, _) in ACCOUNTS.items() if t in ("income", "expense"))
    assets = {"cash": end["cash"], "accounts_receivable": end["ar"], "ppe_gross": end["ppe"],
              "accumulated_depreciation": end["accum_dep"], "construction_in_progress": end["cip"],
              "jv_investment": end["jv_investment"]}
    assets["total_assets"] = sum(assets.values())
    liabilities = {"revolver": -end["revolver"], "senior_notes": -end["bonds"]}
    liabilities["total_liabilities"] = sum(liabilities.values())
    equity = {"paid_in_capital": -end["equity"], "retained_earnings": cum_ni, "dividends": -end["dividends"]}
    equity["total_equity"] = sum(equity.values())
    d = {k: end[k] - start[k] for k in ACCOUNTS}
    maint = -(db.scalar("select coalesce(sum(amount),0) from fin_gl where period=? and account='cash' and ref='maintenance'",
                        (period,)) or 0)
    cfo = ni + a["depreciation"] - d["ar"]
    capex = d["ppe"] + d["cip"] + d["jv_investment"]
    cff = -(d["revolver"] + d["bonds"] + d["equity"]) - d["dividends"]
    cash_flow = {"net_income": ni, "depreciation": a["depreciation"], "change_in_receivables": -d["ar"],
                 "operating_cash_flow": cfo, "maintenance_capex": -maint, "growth_capex": -(capex - maint),
                 "investing_cash_flow": -capex, "debt_drawn_repaid": -(d["revolver"] + d["bonds"]),
                 "dividends_paid": -d["dividends"], "equity_issued": -d["equity"], "financing_cash_flow": cff,
                 "net_change_in_cash": d["cash"], "check": round(cfo - capex + cff - d["cash"], 2)}
    shares = db.setting("shares_outstanding") or SHARES
    ffo = ni + a["depreciation"]
    affo = ffo - maint
    debt = liabilities["total_liabilities"]
    ar_end = end["ar"]
    metrics = {"ffo": ffo, "affo": affo, "affo_per_share": affo / shares, "adj_ebitda_margin": ebitda / revenue if revenue else None,
               "net_debt": debt - end["cash"], "net_debt_to_ebitda": (debt - end["cash"]) / (ebitda * 12) if ebitda > 0 else None,
               "interest_cover": ebitda / a["interest"] if a["interest"] else None, "dso_days": ar_end / revenue * 30 if revenue else None,
               "liquidity": end["cash"] + (db.setting("revolver_limit") or 0) + end["revolver"], "shares": shares,
               "balance_check": round(assets["total_assets"] - liabilities["total_liabilities"] - equity["total_equity"], 2)}
    r = lambda dct: {k: (round(v, 4) if isinstance(v, float) and abs(v) < 10 else round(v, 2) if isinstance(v, float) else v)
                     for k, v in dct.items()}
    return {"period": period, "income": r(income), "balance": {"assets": r(assets), "liabilities": r(liabilities), "equity": r(equity)},
            "cashflow": r(cash_flow), "metrics": r(metrics)}


def closed_periods(limit=24) -> list[dict]:
    out = []
    for m in db.rows("select * from fin_month order by period desc limit ?", (limit,)):
        out.append({"period": m["period"], "closed_on": m["closed_on"], "income": json.loads(m["income"]),
                    "balance": json.loads(m["balance"]), "cashflow": json.loads(m["cashflow"]), "metrics": json.loads(m["metrics"])})
    return out


def quarter_of(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{y}-Q{(m - 1) // 3 + 1}"


def quarter_periods(q: str) -> list[str]:
    y, n = q.split("-Q")
    return [f"{y}-{m:02d}" for m in range(3 * int(n) - 2, 3 * int(n) + 1)]


def quarter(q: str) -> dict | None:
    ps = quarter_periods(q)
    ms = {m["period"]: m for m in closed_periods(60)}
    if not all(p in ms for p in ps):
        return None
    inc = {k: sum(ms[p]["income"][k] for p in ps) for k in ms[ps[0]]["income"]}
    met = {k: sum((ms[p]["metrics"].get(k) or 0) for p in ps) for k in ("ffo", "affo", "affo_per_share")}
    last = ms[ps[-1]]
    return {"quarter": q, "income": inc, "metrics": {**met, "adj_ebitda_margin": inc["adj_ebitda"] / inc["revenue"] if inc["revenue"] else None,
                                                       "net_debt_to_ebitda": last["metrics"]["net_debt_to_ebitda"],
                                                       "dso_days": last["metrics"]["dso_days"]},
            "balance": last["balance"], "cashflow": {k: sum(ms[p]["cashflow"][k] for p in ps) for k in ms[ps[0]]["cashflow"]}}


# ---------------------------------------------------------------- budget and forecast


def build_budget(start: str, months: int = 12) -> int:
    """Compass sets a 12-month budget from the latest closed month (or the run-rate)."""
    last = closed_periods(1)
    if last:
        inc = last[0]["income"]
        rev, cor, sga = inc["revenue"], inc["cost_of_revenue"], inc["sga"] + inc["bad_debt"]
    else:
        rr = run_rate()
        rev = rr["revenue"]["total"]
        cor = rr["power_cost"] + rr["site_opex"] + rr["tech_labour"] + rr["net_support"]
        sga = rr["sga"]
    n, p = 0, start
    for k in range(months):
        if not db.one("select 1 from fin_budget where period=?", (p,)):
            r, c, s = rev * 1.009 ** (k + 1), cor * 1.005 ** (k + 1), sga * 1.006 ** (k + 1)
            for line, v in (("revenue", r), ("cost_of_revenue", c), ("sga", s), ("adj_ebitda", r - c - s)):
                db.execute("insert or replace into fin_budget(period,line,amount) values(?,?,?)", (p, line, round(v, 2)))
            n += 1
        y, m = map(int, p.split("-"))
        p = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
    return n


def budget_vs_actual(period: str) -> list[dict]:
    b = {r["line"]: r["amount"] for r in db.rows("select line, amount from fin_budget where period=?", (period,))}
    m = next((x for x in closed_periods(60) if x["period"] == period), None)
    if not b or not m:
        return []
    inc = m["income"]
    act = {"revenue": inc["revenue"], "cost_of_revenue": inc["cost_of_revenue"], "sga": inc["sga"] + inc["bad_debt"],
           "adj_ebitda": inc["adj_ebitda"]}
    return [{"line": k, "budget": round(b[k]), "actual": round(act[k]), "variance": round(act[k] - b[k]),
             "variance_pct": round(100 * (act[k] / b[k] - 1), 1) if b[k] else None} for k in ("revenue", "cost_of_revenue", "sga", "adj_ebitda")]


def forecast(months: int = 6) -> list[dict]:
    ms = list(reversed(closed_periods(4)))
    rr = run_rate()
    rev0 = rr["revenue"]["total"]
    ms = [m for m in ms if m["income"]["revenue"] > 0]
    if len(ms) >= 2:
        g = (ms[-1]["income"]["revenue"] / ms[0]["income"]["revenue"]) ** (1 / (len(ms) - 1)) - 1
    else:
        g = 0.008
    g = max(-0.03, min(0.04, g))
    cost0 = rr["power_cost"] + rr["site_opex"] + rr["tech_labour"] + rr["net_support"] + rr["sga"]
    commits = {c["period"]: c["usd"] for c in (CAPEX_COMMITMENTS_HOOK() if CAPEX_COMMITMENTS_HOOK else [])}
    cash = balances()["cash"]
    ints = sum(d["principal"] * d["rate"] / 12 for d in db.rows("select * from fin_debt where status='outstanding'"))
    out, p = [], products.today()[:7]
    for k in range(1, months + 1):
        y, m = map(int, p.split("-"))
        p = f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
        rev = rev0 * (1 + g) ** k
        cost = cost0 * 1.004 ** k
        ebitda = rev - cost
        capex = commits.get(p, 0.0) + MAINT_CAPEX_PCT * rev
        cash += ebitda - ints - capex
        out.append({"period": p, "revenue": round(rev), "adj_ebitda": round(ebitda), "capex": round(capex), "cash": round(cash)})
    return out


def pending_quarter() -> str | None:
    """The latest fully closed quarter that Investor Relations has not published yet."""
    done = set(db.setting("ir_published") or [])
    for m in closed_periods(12):
        q = quarter_of(m["period"])
        if q not in done and quarter(q):
            return q
    return None


def kpi_hook() -> dict:
    if not db.one("select name from sqlite_master where name='fin_gl'") or not opened():
        return {}
    b = balances()
    last = closed_periods(1)
    ag = ar_aging()
    return {"finance": {"cash": round(b["cash"]), "ar": ag["total"], "dso_days": ag["dso_days"],
                        "debt": round(-(b["revolver"] + b["bonds"])),
                        "affo_per_share_last_month": round(last[0]["metrics"]["affo_per_share"], 3) if last else None,
                        "net_debt_to_ebitda": round(last[0]["metrics"]["net_debt_to_ebitda"], 2) if last and last[0]["metrics"]["net_debt_to_ebitda"] else None,
                        "last_close": last[0]["period"] if last else None, "credit_holds": ag["on_hold"]}}
