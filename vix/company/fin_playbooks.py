"""Office of the CFO (Quill): Deal Desk, Billing, Collections & Credit, Finance (accounting and close),
FP&A, Treasury and Investor Relations."""
from __future__ import annotations

import json
import random
import time

from .. import crm, db, fin, products, world
from . import engine
from .engine import decision_add, playbook
from .playbooks import table


def usd(v):
    v = round(v or 0)
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


BS_LABEL = {"cash": "Cash", "accounts_receivable": "Accounts receivable", "ppe_gross": "Property, plant and equipment (gross)",
            "accumulated_depreciation": "Accumulated depreciation", "construction_in_progress": "Construction in progress", "jv_investment": "Investment in hyperscale JVs",
            "total_assets": "**Total assets**", "revolver": "Revolving credit facility", "senior_notes": "Senior notes",
            "total_liabilities": "**Total liabilities**", "paid_in_capital": "Paid-in capital", "retained_earnings": "Retained earnings",
            "dividends": "Dividends declared", "total_equity": "**Total equity**"}

engine.add_department({"id": "cfo", "name": "Office of the CFO", "agent": "Quill", "tier": "super", "reports_to": "ceo",
                       "mission": "Own the money: pricing authority, order-to-cash, the books, cash and debt, the budget and what "
                                  "investors are told. Keep leverage and liquidity inside policy and escalate what is not.",
                       "kpis": ["AFFO per share", "net debt / EBITDA", "DSO"]}, before="exec")
for _d in ({"id": "dealdesk", "name": "Deal Desk", "agent": "Margin", "mission": "Price every opportunity inside the discount "
            "authority matrix; protect gross margin; escalate large discounts.", "kpis": ["average discount", "win rate by band"]},
           {"id": "billing", "name": "Billing", "agent": "Docket", "mission": "Invoice every customer monthly in their currency, "
            "apply service credits, resolve disputes.", "kpis": ["invoices", "disputes"]},
           {"id": "collections", "name": "Collections & Credit", "agent": "Reckon", "mission": "Credit ratings, dunning, credit "
            "holds, collections agency, write-offs and terminations for non-payment.", "kpis": ["DSO", "bad debt"]},
           {"id": "fpa", "name": "FP&A", "agent": "Compass", "mission": "Budget, budget-vs-actual, rolling forecast and cash runway.",
            "kpis": ["EBITDA variance", "runway"]},
           {"id": "treasury", "name": "Treasury", "agent": "Anchor", "mission": "Liquidity, the revolver, bond issues and "
            "refinancing, FX exposure, dividends.", "kpis": ["liquidity", "leverage"]},
           {"id": "ir", "name": "Investor Relations", "agent": "Herald", "mission": "Quarterly earnings release: results, REIT "
            "metrics, guidance and the dividend.", "kpis": ["releases"]}):
    _d["reports_to"] = "cfo"
    engine.add_department(_d, before="exec")
engine.DEPT["finance"]["reports_to"] = "cfo"
engine.DEPT["finance"]["mission"] = "Keep the general ledger: post costs, depreciation, interest and tax, close each month and " \
                                    "produce the income statement, balance sheet and cash-flow statement."


def _boot():
    fin.init()
    fin.open_books()


def _sub(ctx, name, params=None):
    """Delegate: a real child run (with its own report) normally; inline during a fast-forward."""
    if getattr(ctx, "is_inline", False):
        return {"summary": ctx.inline(name, params), "report": None, "status": "done"}
    return ctx.child(name, params)


# ---------------------------------------------------------------- daily: billing and collections

@playbook("fin_billing", "Billing run", "billing",
          "Docket: on the first day of a month, Ledger closes the previous month and Docket invoices every active customer "
          "(recurring in advance, remote hands in arrears, network SLA credits applied) in their own currency. Every day: "
          "FX moves, payments arrive, disputes are resolved into credit notes or rejected.")
def fin_billing(ctx):
    _boot()
    rnd = random.Random(f"bill-{products.today()}")
    fin.fx_step(rnd)
    period = products.today()[:7]
    last = db.setting("fin_billed_period")
    closed, inv = None, {"invoices": 0}
    if last != period:
        if last and not db.one("select 1 from fin_month where period=?", (last,)):
            closed = _sub(ctx, "fin_period_close", {"period": last})
        inv = fin.issue_invoices(period, rnd)
        db.set_setting("fin_billed_period", period)
    pay = fin.process_payments(rnd)
    disp = fin.resolve_disputes(rnd)
    tr = fin.treasury_daily(rnd)
    ctx.log(f"{inv.get('invoices', 0)} invoices, {pay['payments']} payments, {disp['credited']} credits")
    md = (f"Simulated day {products.today()}. "
          + (f"Ledger closed **{last}** first. " if closed else "")
          + (f"Issued **{inv['invoices']} invoices** for {period} worth {usd(inv['billed_usd'])}; {inv['disputes']} disputed on arrival. "
             if inv.get("invoices") else "")
          + f"{pay['payments']} payments received ({usd(pay['received_usd'])}); disputes: {disp['credited']} credited, "
            f"{disp['rejected']} rejected. Treasury: drew {usd(tr['drawn'])}, repaid {usd(tr['repaid'])}"
          + (f", repaid at maturity: {', '.join(tr['matured'])}" if tr["matured"] else "") + ".\n")
    return {"invoices": inv.get("invoices", 0), "payments": pay["payments"], "received_usd": pay["received_usd"],
            "credited": disp["credited"], "closed": last if closed else None}, md


@playbook("fin_collections", "Collections", "collections",
          "Reckon: dunning ladder for overdue invoices (reminder at 7 days, final notice at 30, credit hold at 45, collections "
          "agency at 90, write-off and termination for non-payment at 120). Lifts holds when accounts are current.")
def fin_collections(ctx):
    _boot()
    rnd = random.Random(f"coll-{products.today()}")
    r = fin.collections(rnd)
    ag = fin.ar_aging()
    ctx.log(f"AR {usd(ag['total'])}, DSO {ag['dso_days']}")
    md = (f"Receivables **{usd(ag['total'])}**, DSO {ag['dso_days']} days, {ag['on_hold']} customers on credit hold, "
          f"{ag['distressed']} in distress.\n\n" + table([{"bucket": k, "usd": usd(v)} for k, v in ag["buckets"].items()], ["bucket", "usd"]) +
          f"\nToday: {r['reminders']} reminders, {r['holds']} new holds, {r['holds_lifted']} holds lifted, {r['agency']} sent to the agency "
          f"({usd(r['recovered_usd'])} recovered), {usd(r['written_off_usd'])} written off"
          + (f"; terminated for non-payment: {', '.join(r['terminated'])}" if r["terminated"] else "") + ".\n")
    return {**{k: v for k, v in r.items() if k != "terminated"}, "terminated": len(r["terminated"]), "ar": ag["total"],
            "dso_days": ag["dso_days"]}, md


# ---------------------------------------------------------------- monthly close

def _stmt_md(st, bva=None):
    i, b, c, m = st["income"], st["balance"], st["cashflow"], st["metrics"]
    pl = [("Recurring revenue", i["recurring_revenue"]), ("Usage and services", i["usage_revenue"]), ("Credits and allowances", i["credits"]),
          ("**Revenue**", i["revenue"]), ("Power", -i["power"]), ("Site operations", -i["site_operations"]),
          ("Remote-hands staff", -i["remote_hands_staff"]), ("Network support", -i["network_support"]), ("**Gross profit**", i["gross_profit"]),
          ("Selling, general and admin", -i["sga"]), ("Bad debt", -i["bad_debt"]), ("Hyperscale JV income", i.get("jv_income", 0)),
          ("**Adjusted EBITDA**", i["adj_ebitda"]),
          ("Depreciation", -i["depreciation"]), ("Operating income", i["operating_income"]), ("Interest", -i["interest"]),
          ("FX gains (losses)", -i["fx_loss"]), ("Income tax", -i["tax"]), ("**Net income**", i["net_income"])]
    bs = [(BS_LABEL.get(k, k), v) for k, v in {**b["assets"], **b["liabilities"], **b["equity"]}.items()]
    cf = [(k.replace("_", " ").capitalize(), v) for k, v in c.items() if k != "check"]
    rows = lambda xs: [{"line": a, "usd": usd(v)} for a, v in xs]
    md = ("## Income statement\n\n" + table(rows(pl), ["line", "usd"]) + "\n## Balance sheet (period end)\n\n" + table(rows(bs), ["line", "usd"]) +
          f"\n_Assets minus liabilities minus equity: {usd(m['balance_check'])}._\n\n## Cash flow\n\n" + table(rows(cf), ["line", "usd"]) +
          "\n## REIT and credit metrics\n\n" + table([
              {"metric": "FFO", "value": usd(m["ffo"])}, {"metric": "AFFO", "value": usd(m["affo"])},
              {"metric": "AFFO per share", "value": f"${m['affo_per_share']:.3f}"},
              {"metric": "Adjusted EBITDA margin", "value": f"{(m['adj_ebitda_margin'] or 0):.1%}"},
              {"metric": "Net debt", "value": usd(m["net_debt"])},
              {"metric": "Net debt / annualised EBITDA", "value": f"{m['net_debt_to_ebitda']:.2f}x" if m["net_debt_to_ebitda"] else "n/a"},
              {"metric": "Interest cover", "value": f"{m['interest_cover']:.1f}x" if m["interest_cover"] else "n/a"},
              {"metric": "DSO", "value": f"{m['dso_days']:.1f} days" if m["dso_days"] else "n/a"},
              {"metric": "Liquidity (cash + undrawn revolver)", "value": usd(m["liquidity"])}], ["metric", "value"]))
    if bva:
        md += "\n## Budget vs actual\n\n" + table([{**r, "budget": usd(r["budget"]), "actual": usd(r["actual"]), "variance": usd(r["variance"])}
                                                  for r in bva], ["line", "budget", "actual", "variance", "variance_pct"])
    return md


@playbook("fin_period_close", "Month-end close", "finance",
          "Ledger: post the month's operating costs, depreciation, maintenance capex, interest and tax; derive the income "
          "statement, balance sheet and cash-flow statement from the general ledger; compare to budget.",
          {"period": {"type": "str", "default": ""}})
def fin_period_close(ctx):
    _boot()
    period = ctx.p("period", "") or fin._prev_period(products.today()[:7])
    try:
        st = fin.close_period(period)
    except ValueError as e:
        ctx.log(str(e))
        return {"period": period, "closed": False, "reason": str(e)}, f"Nothing to close: {e}.\n"
    bva = fin.budget_vs_actual(period)
    m = st["metrics"]
    ctx.log(f"closed {period}: revenue {usd(st['income']['revenue'])}, net income {usd(st['income']['net_income'])}")
    md = f"Closed **{period}** on {products.today()} (simulated). Every figure below is derived from the general ledger.\n\n" + _stmt_md(st, bva)
    return {"period": period, "revenue": round(st["income"]["revenue"]), "adj_ebitda": round(st["income"]["adj_ebitda"]),
            "net_income": round(st["income"]["net_income"]), "affo_per_share": round(m["affo_per_share"], 3),
            "balance_check": m["balance_check"], "cash_check": st["cashflow"]["check"]}, md


@playbook("fin_monthly_close", "Run-rate P&L (flash)", "finance",
          "Ledger: a flash P&L at today's run-rate (revenue by product and metro, power, opex, staff, credits, EBITDA). "
          "The month-end close (fin_period_close) is the book of record.")
def fin_monthly_close(ctx):
    rr = fin.run_rate()
    rev = rr["revenue"]
    per_metro = rr["per_metro"]
    lb = products.lines_by_tenant()
    for pm in per_metro.values():
        pm["revenue"] = sum(sum(lb.get(r["tenant"], {}).values()) * r["share"] for r in db.rows(
            "select p.tenant, cast(sum(p.cabinets) as real) / (select sum(cabinets) from placements where tenant=p.tenant) share"
            " from placements p join halls h on h.id=p.hall join sites s on s.id=h.site where s.metro=? group by p.tenant",
            (pm["metro"],)))
        pm["util"] = round(pm["cabinets_sold"] / pm["cabinets_total"], 3)
        pm["revenue"], pm["power_cost"] = round(pm["revenue"]), round(pm["power_cost"])
    credits, power, opex, labour, net_support = rr["sla_credits"], rr["power_cost"], rr["site_opex"], rr["tech_labour"], rr["net_support"]
    ebitda = rev["total"] - credits - power - opex - labour - net_support
    margin = round(ebitda / rev["total"], 3) if rev["total"] else None
    dep = (fin.balances()["ppe"] / fin.DEP_MONTHS) if fin.opened() else (db.scalar("select sum(build_capex_usd) from halls") or 0) / 240
    snap = {"sim_date": products.today(), "mrr": rev["total"], "power_cost": round(power), "opex": opex, "tech_labour": round(labour),
            "sla_credits": round(credits), "net_support": round(net_support), "ebitda": round(ebitda), "ebitda_margin": margin,
            "depreciation": round(dep), "ebit": round(ebitda - dep), "ts": time.time()}
    db.set_setting("fin_last", snap)
    ctx.log(f"MRR {usd(rev['total'])}, EBITDA margin {margin:.1%}")
    md = ("Run-rate view (before SG&A, bad debt, interest and tax). The book of record is the month-end close.\n\n## P&L (one month at run-rate)\n\n" + table([
        {"line": "Revenue", "usd": usd(rev["total"])},
        {"line": "Network SLA credits (last 30 days)", "usd": "-" + usd(credits)},
        {"line": f"Network support and spares ({rr['devices']} devices)", "usd": "-" + usd(net_support)},
        {"line": "Power (after power purchase agreements)", "usd": "-" + usd(power)},
        {"line": "Site opex", "usd": "-" + usd(opex)},
        {"line": f"Remote-hands technicians ({rr['techs']} posts, 24x7)", "usd": "-" + usd(labour)},
        {"line": "**EBITDA (site level)**", "usd": f"**{usd(ebitda)}** ({margin:.1%})"},
        {"line": "Depreciation", "usd": "-" + usd(dep)},
        {"line": "EBIT", "usd": usd(ebitda - dep)}], ["line", "usd"]) +
        "\n## Revenue by product\n\n" + table([{"product": k, "usd": usd(v)} for k, v in rev.items() if k != "total"], ["product", "usd"]) +
        "\n## By metro\n\n" + table(sorted(per_metro.values(), key=lambda r: -r["revenue"]), ["metro", "revenue", "power_cost", "util"]))
    return snap, md


# ---------------------------------------------------------------- weekly finance team

@playbook("deal_desk_review", "Deal desk review", "dealdesk",
          "Margin: discounts given in the last 30 days by approval level, win rate by discount band, margin given away.")
def deal_desk_review(ctx):
    _boot()
    since = products.add_months(products.today_d(), -1).isoformat()
    qs = db.rows("select * from fin_quote where sim_date>?", (since,))
    bands = []
    for lo, hi, lab in ((0, .05, "0-5%"), (.05, .10, "5-10%"), (.10, .20, "10-20%"), (.20, 1, "20%+")):
        sel = [q for q in qs if lo <= q["discount_pct"] < hi or (hi == 1 and q["discount_pct"] >= lo)]
        closed = [q for q in sel if q["status"] in ("won", "lost")]
        won = [q for q in closed if q["status"] == "won"]
        bands.append({"band": lab, "quotes": len(sel), "closed": len(closed), "win_rate": f"{len(won) / len(closed):.0%}" if closed else "n/a",
                      "list_mrr": usd(sum(q["list_mrr"] for q in sel)), "discount_given": usd(sum(q["list_mrr"] - q["net_mrr"] for q in won))})
    by_appr = db.rows("select approval, count(*) n, round(avg(discount_pct)*100,1) avg_pct from fin_quote where sim_date>? group by approval", (since,))
    avg = round(100 * sum(q["discount_pct"] for q in qs) / len(qs), 1) if qs else 0
    capped = sum(1 for q in qs if q["note"])
    ctx.log(f"{len(qs)} quotes, average discount {avg}%")
    md = (f"**{len(qs)} quotes** in the last month, average discount **{avg}%**; {capped} capped by the CFO to protect margin.\n\n"
          "## Win rate by discount band\n\n" + table(bands, ["band", "quotes", "closed", "win_rate", "list_mrr", "discount_given"]) +
          "\n## By approval level\n\n" + table(by_appr, ["approval", "n", "avg_pct"]))
    return {"quotes": len(qs), "avg_discount_pct": avg, "capped": capped}, md


@playbook("fpa_forecast", "Budget & forecast", "fpa",
          "Compass: keep a rolling 12-month budget, report budget vs actual for the last closed month, forecast the next six "
          "months of revenue, EBITDA, capex and cash.")
def fpa_forecast(ctx):
    _boot()
    built = fin.build_budget(products.today()[:7])
    last = fin.closed_periods(1)
    bva = fin.budget_vs_actual(last[0]["period"]) if last else []
    fc = fin.forecast(6)
    low = min(fc, key=lambda r: r["cash"]) if fc else None
    ebitda_var = next((r["variance_pct"] for r in bva if r["line"] == "adj_ebitda"), None)
    ctx.log(f"budget months added {built}; EBITDA variance {ebitda_var}")
    md = ((f"Budget extended by {built} month(s). " if built else "") +
          (f"Last close **{last[0]['period']}**: EBITDA {ebitda_var:+.1f}% vs budget.\n\n" if ebitda_var is not None else "No closed month yet.\n\n") +
          ("## Budget vs actual\n\n" + table([{**r, "budget": usd(r["budget"]), "actual": usd(r["actual"]), "variance": usd(r["variance"])}
                                            for r in bva], ["line", "budget", "actual", "variance", "variance_pct"]) if bva else "") +
          "\n## Six-month forecast\n\n" + table([{k: (usd(v) if k != "period" else v) for k, v in r.items()} for r in fc],
                                               ["period", "revenue", "adj_ebitda", "capex", "cash"]) +
          (f"\nLowest projected cash: {usd(low['cash'])} in {low['period']} (before revolver draws).\n" if low else ""))
    return {"budget_months_added": built, "ebitda_variance_pct": ebitda_var, "min_cash": low["cash"] if low else None,
            "min_cash_period": low["period"] if low else None}, md


@playbook("treasury_review", "Treasury review", "treasury",
          "Anchor: liquidity, revolver use, maturities in the next 12 months, rates and FX exposure. Files bond issues and "
          "refinancing for the founder; recommends the quarterly dividend once a quarter is closed.")
def treasury_review(ctx):
    _boot()
    ds = fin.debt_summary()
    b = fin.balances()
    rate = ds["base_rate"]
    filed = []
    util = ds["revolver_drawn"] / ds["revolver_limit"] if ds["revolver_limit"] else 0
    if util > 0.6:
        amt = round(max(ds["revolver_drawn"], 400e6), -7)
        did = decision_add(ctx.agent, "issue_bonds", f"Issue {usd(amt)} of senior notes to term out the revolver",
                           {"amount_usd": amt, "rate": round(rate + 0.0125, 4), "years": 7,
                            "reason": f"revolver {util:.0%} drawn ({usd(ds['revolver_drawn'])} of {usd(ds['revolver_limit'])})"})
        filed.append(did)
    horizon = products.add_months(products.today_d(), 12).isoformat()
    for bnd in db.rows("select * from fin_debt where kind='bond' and status='outstanding' and maturity<=?", (horizon,)):
        did = decision_add(ctx.agent, "refinance_bond", f"Refinance {bnd['name']} ({usd(bnd['principal'])})",
                           {"debt_id": bnd["id"], "name": bnd["name"], "amount_usd": bnd["principal"], "old_rate": bnd["rate"],
                            "rate": round(rate + 0.0125, 4), "years": 8, "maturity": bnd["maturity"]})
        filed.append(did)
    q = fin.pending_quarter()
    if q and not db.one("select id from fin_dividend where quarter=?", (q,)):
        qd = fin.quarter(q)
        per = round(0.7 * qd["metrics"]["affo_per_share"], 3)
        if per > 0:
            shares = db.setting("shares_outstanding") or fin.SHARES
            did = decision_add(ctx.agent, "declare_dividend", f"Declare a {q} dividend of ${per:.3f} per share",
                               {"quarter": q, "per_share": per, "total_usd": round(per * shares), "affo_per_share": round(qd["metrics"]["affo_per_share"], 3),
                                "payout": 0.7, "net_debt_to_ebitda": qd["metrics"]["net_debt_to_ebitda"]})
            filed.append(did)
    exposure = db.rows("select currency, round(sum(balance_usd)) usd from fin_invoice where status='open' group by currency order by usd desc")
    fxr = db.rows("select ccy, round(rate,4) rate, round(100*(rate/base-1),2) move_pct from fin_fx where ccy!='USD'")
    ctx.log(f"cash {usd(b['cash'])}, revolver {util:.0%}, {len(filed)} decisions")
    md = (f"Cash **{usd(b['cash'])}**, revolver {usd(ds['revolver_drawn'])} drawn of {usd(ds['revolver_limit'])} ({util:.0%}), "
          f"base rate {rate:.2%}.\n\n## Debt\n\n" + table([{**d, "principal": usd(d["principal"]), "rate": f"{d['rate']:.2%}" if d["rate"] else "floating"}
                                                         for d in ds["instruments"]], ["name", "principal", "rate", "maturity"]) +
          "\n## FX\n\nOpen receivables by currency:\n\n" + table(exposure, ["currency", "usd"]) + "\n" + table(fxr, ["ccy", "rate", "move_pct"]) +
          ("\n## Filed for the founder\n\n" + "".join(f"- decision #{x}\n" for x in filed) if filed else "\nNothing needs the founder this week.\n"))
    return {"cash": round(b["cash"]), "revolver_util": round(util, 3), "decisions": filed}, md


@playbook("ir_earnings_pack", "Earnings release", "ir",
          "Herald: the quarterly earnings release for a closed quarter: revenue, growth, adjusted EBITDA, AFFO per share, "
          "leverage, the dividend and next-quarter guidance from the FP&A forecast.",
          {"quarter": {"type": "str", "default": ""}})
def ir_earnings_pack(ctx):
    _boot()
    q = ctx.p("quarter", "") or fin.pending_quarter()
    qd = fin.quarter(q) if q else None
    if not qd:
        return {"published": None}, "No fully closed quarter to report yet.\n"
    y, n = q.split("-Q")
    prev_q = f"{y}-Q{int(n) - 1}" if n != "1" else f"{int(y) - 1}-Q4"
    pq = fin.quarter(prev_q)
    i, m = qd["income"], qd["metrics"]
    growth = f"{100 * (i['revenue'] / pq['income']['revenue'] - 1):+.1f}% quarter on quarter" if pq else "first reported quarter"
    fc = fin.forecast(3)
    guide = sum(r["revenue"] for r in fc)
    div = db.one("select * from fin_dividend where quarter=?", (q,))
    pub = set(db.setting("ir_published") or [])
    pub.add(q)
    db.set_setting("ir_published", sorted(pub))
    ctx.log(f"published {q}")
    md = (f"# vix reports {q} results (simulated)\n\n"
          f"Revenue **{usd(i['revenue'])}** ({growth}). Adjusted EBITDA **{usd(i['adj_ebitda'])}**, a {m['adj_ebitda_margin']:.1%} margin. "
          f"AFFO **{usd(m['affo'])}**, **${m['affo_per_share']:.2f} per share**. Net income {usd(i['net_income'])}. "
          f"Net debt to annualised EBITDA {m['net_debt_to_ebitda']:.2f}x. "
          + (f"The board declared a dividend of ${div['per_share']:.3f} per share. " if div else "The dividend is with the board. ")
          + f"\n\n## Guidance\n\nNext quarter revenue {usd(guide * 0.985)} to {usd(guide * 1.015)}, from Compass's forecast.\n\n"
          "## Quarter in numbers\n\n" + table([{"line": k.replace("_", " "), "usd": usd(v)} for k, v in i.items()], ["line", "usd"]) +
          "\n_All figures simulated; vix is not a real company._\n")
    return {"published": q, "revenue": round(i["revenue"]), "affo_per_share": round(m["affo_per_share"], 3)}, md


@playbook("cfo_finance_review", "CFO review", "cfo",
          "Quill: weekly finance review. Delegates to Deal Desk, FP&A and Treasury, publishes the earnings release when a quarter "
          "closes, checks policy (leverage under 6x, liquidity above six months of costs, DSO under 45 days, EBITDA within 5% of "
          "budget) and escalates breaches to the CEO.")
def cfo_finance_review(ctx):
    _boot()
    dd = _sub(ctx, "deal_desk_review")
    fp = _sub(ctx, "fpa_forecast")
    tr = _sub(ctx, "treasury_review")
    ir = _sub(ctx, "ir_earnings_pack") if fin.pending_quarter() else None
    last = fin.closed_periods(1)
    ag = fin.ar_aging()
    esc = []
    if last:
        m, i = last[0]["metrics"], last[0]["income"]
        if m["net_debt_to_ebitda"] and m["net_debt_to_ebitda"] > 6:
            esc.append(f"leverage {m['net_debt_to_ebitda']:.1f}x is above the 6x policy")
        monthly_cost = i["cost_of_revenue"] + i["sga"] + i["interest"]
        if monthly_cost and m["liquidity"] < 6 * monthly_cost:
            esc.append(f"liquidity {usd(m['liquidity'])} is under six months of costs")
        if m["balance_check"] and abs(m["balance_check"]) > 1:
            esc.append(f"balance sheet out by {usd(m['balance_check'])}")
    if last and last[0]["metrics"]["dso_days"] and last[0]["metrics"]["dso_days"] > 45:
        esc.append(f"month-end DSO {last[0]['metrics']['dso_days']:.0f} days is above 45")
    var = (fp["summary"] or {}).get("ebitda_variance_pct")
    if var is not None and var < -5:
        esc.append(f"EBITDA {var:+.1f}% against budget")
    if (fp["summary"] or {}).get("min_cash") is not None and fp["summary"]["min_cash"] < fin.MIN_CASH:
        esc.append(f"forecast cash falls to {usd(fp['summary']['min_cash'])} in {fp['summary']['min_cash_period']} before revolver draws")
    ctx.log(f"{len(esc)} escalations")
    md = ("Weekly finance review by Quill.\n\n" +
          (f"Last close **{last[0]['period']}**: revenue {usd(last[0]['income']['revenue'])}, adjusted EBITDA "
           f"{usd(last[0]['income']['adj_ebitda'])}, AFFO/share ${last[0]['metrics']['affo_per_share']:.3f}, leverage "
           f"{(last[0]['metrics']['net_debt_to_ebitda'] or 0):.2f}x.\n\n" if last else "No month closed yet.\n\n") +
          f"- Deal Desk: {(dd['summary'] or {}).get('quotes')} quotes, average discount {(dd['summary'] or {}).get('avg_discount_pct')}%\n"
          f"- FP&A: EBITDA variance {var}%; lowest forecast cash {usd((fp['summary'] or {}).get('min_cash') or 0)}\n"
          f"- Treasury: cash {usd((tr['summary'] or {}).get('cash') or 0)}, revolver {((tr['summary'] or {}).get('revolver_util') or 0):.0%} drawn\n"
          f"- Receivables: {usd(ag['total'])}, DSO {ag['dso_days']}, {ag['on_hold']} on credit hold\n"
          + (f"- Investor Relations published {(ir['summary'] or {}).get('published')}\n" if ir else "") +
          "\n## Escalations\n\n" + ("".join(f"- {e}\n" for e in esc) or "- none\n"))
    return {"escalations": esc, "last_close": last[0]["period"] if last else None,
            "affo_per_share": round(last[0]["metrics"]["affo_per_share"], 3) if last else None,
            "leverage": round(last[0]["metrics"]["net_debt_to_ebitda"], 2) if last and last[0]["metrics"]["net_debt_to_ebitda"] else None}, md


# ---------------------------------------------------------------- decisions

def _eff_discount(det, d):
    q = db.one("select * from fin_quote where opp=?", (det["opp"],))
    if not q:
        return {"note": "quote not found"}
    db.execute("update fin_quote set status='approved', approval='founder' where id=? and status='pending'", (q["id"],))
    return {"opp": det["opp"], "discount_pct": q["discount_pct"]}


def _eff_issue(det, d):
    return fin.issue_bond(float(det["amount_usd"]), float(det["rate"]), int(det.get("years", 7)), products.today())


def _eff_refi(det, d):
    old = db.one("select * from fin_debt where id=?", (det["debt_id"],))
    if not old or old["status"] != "outstanding":
        return {"note": "already repaid"}
    return fin.issue_bond(old["principal"], float(det["rate"]), int(det.get("years", 8)), products.today(), refinance_id=old["id"])


def _eff_dividend(det, d):
    if db.one("select id from fin_dividend where quarter=?", (det["quarter"],)):
        return {"note": "already declared"}
    total = float(det["total_usd"])
    today = products.today()
    fin.post(today, [("dividends", total), ("cash", -total)], f"dividend {det['quarter']}", "dividend")
    db.execute("insert into fin_dividend(quarter,per_share,total_usd,declared,paid,decision) values(?,?,?,?,?,?)",
               (det["quarter"], det["per_share"], total, today, today, d["id"]))
    return {"quarter": det["quarter"], "paid_usd": total}


engine.DECISION_EFFECTS.update({"discount_approval": _eff_discount, "issue_bonds": _eff_issue, "refinance_bond": _eff_refi,
                                "declare_dividend": _eff_dividend})


def _capex_on_decide(d, approved, effect):
    """Network and cooling capex are paid in cash when approved."""
    if not approved or not fin.opened():
        return
    amt = 0.0
    if d["kind"] == "network_capex":
        amt = float(d["detail"].get("capex_usd") or 0)
    elif d["kind"] == "cooling_upgrade":
        amt = 250_000.0 * int(d["detail"].get("units", 1))
    if amt:
        fin.post(products.today(), [("ppe", amt), ("cash", -amt)], f"capex: {d['title']}", f"decision:{d['id']}")


engine.ON_DECIDE.append(_capex_on_decide)


def _rec_discount(d):
    det = d["detail"]
    gm = det.get("gross_margin_after") or 0
    if gm >= 0.25:
        return "approve", f"{det['discount_pct']:.0%} off {usd(det['list_mrr'])}/mo still leaves {gm:.0%} gross margin" + (
            f"; {det['competitor']} is bidding" if det.get("competitor") else "")
    return "reject", f"gross margin would fall to {gm:.0%}; counter at 20%"


def _rec_issue(d):
    det = d["detail"]
    return "approve", f"{det['reason']}; terming out at {det['rate']:.2%} for {det['years']} years removes refinancing risk"


def _rec_refi(d):
    det = d["detail"]
    return "approve", f"{det['name']} matures {det['maturity']}; refinance now at {det['rate']:.2%} (was {det['old_rate']:.2%})"


def _rec_div(d):
    det = d["detail"]
    lev = det.get("net_debt_to_ebitda")
    if lev and lev > 6:
        return "hold", f"leverage {lev:.1f}x is above policy; cut the payout until it is below 6x"
    if lev:
        return "approve", f"70% of AFFO (${det['affo_per_share']:.3f}/share); leverage {lev:.2f}x within policy"
    return "approve", "70% of AFFO"


engine.RECOMMENDERS.update({"discount_approval": _rec_discount, "issue_bonds": _rec_issue, "refinance_bond": _rec_refi,
                            "declare_dividend": _rec_div})
engine.DEDUPE_KEY.update({"discount_approval": "opp", "issue_bonds": None, "refinance_bond": "debt_id", "declare_dividend": "quarter"})

def _boot_all():
    _boot()
    engine.add_schedules([("cfo_finance_review", "weekly mon 06:20")])


engine.BOOT.append(_boot_all)

# daily and weekly steps, hooks, board pack
engine.DAILY_STEPS.extend(["fin_billing", "fin_collections"])
engine.WEEKLY_STEPS.append("cfo_finance_review")
engine.KPI_HOOKS.append(fin.kpi_hook)


def _new_tenant(tid, rnd):
    if db.one("select name from sqlite_master where name='fin_customer'"):
        fin.add_customer(tid, rnd)


world.ON_NEW_TENANT.append(_new_tenant)
crm.ON_OPPORTUNITY.append(lambda oid: fin.quote_for(oid) if fin.opened() else None)
crm.ON_CLOSED.append(lambda o, won, info, order: fin.on_closed(o, won, info, order) if fin.opened() else None)


def _hold_filter(t, weights):
    if db.one("select name from sqlite_master where name='fin_customer'") and db.scalar(
            "select credit_hold from fin_customer where tenant=?", (t["id"],)):
        return None
    return weights


engine.DEMAND_HOOKS.append(_hold_filter)


def _board(r):
    s = r["summary"] or {}
    line = (f"- **Quill (finance):** last close {s.get('last_close') or 'none yet'}, AFFO/share "
            f"{('$%.3f' % s['affo_per_share']) if s.get('affo_per_share') is not None else 'n/a'}, leverage "
            f"{s.get('leverage') or 'n/a'}x. Report: {r['report']}\n")
    return line, [f"Finance escalation from Quill: {e}" for e in s.get("escalations", [])]


engine.BOARD_CHIEFS.append(("cfo_finance_review", _board))
