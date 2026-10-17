"""Office of the Chief Trust & Risk Officer (Rampart): Physical Security (Aegis), Security Operations Center
(Lookout), Compliance (Canon), Risk & Resilience (Brace), Legal & Contracts (Clause). Governance (Arbiter)
reports to the CEO and grades the agents themselves."""
from __future__ import annotations

import random

from .. import crm, db, fin, products, trust
from . import engine
from .engine import playbook
from .fin_playbooks import _sub, usd
from .playbooks import table

engine.add_department({"id": "cro", "name": "Office of the Chief Trust & Risk Officer", "agent": "Rampart", "tier": "super",
                       "reports_to": "ceo", "mission": "Keep vix trustworthy: physical and cyber security, compliance with evidence, "
                       "continuity, the risk register and contracts. Escalate what threatens customers or the licence to operate.",
                       "kpis": ["compliance readiness", "high-severity incidents", "top risk score"]}, before="exec")
for _d in ({"id": "physsec", "name": "Physical Security", "agent": "Aegis", "mission": "Badges, visitors, mantraps and guards at "
            "every site; daily access reviews; remove access when customers leave.", "kpis": ["incidents", "badges swept"]},
           {"id": "soc", "name": "Security Operations Center", "agent": "Lookout", "mission": "Detect and contain attacks on the "
            "portal, the network edge, staff and the management network.", "kpis": ["alerts", "time to contain"]},
           {"id": "compliance", "name": "Compliance", "agent": "Canon", "mission": "Test every control weekly against vix's own "
            "records, keep the evidence, prepare for audit.", "kpis": ["readiness", "exceptions"]},
           {"id": "risk", "name": "Risk & Resilience", "agent": "Brace", "mission": "The risk register, backups and restore tests, "
            "and a continuity drill every quarter.", "kpis": ["top risk", "drills passed"]},
           {"id": "legal", "name": "Legal & Contracts", "agent": "Clause", "mission": "Negotiate non-standard terms on large deals "
            "inside the playbook; track data-residency commitments.", "kpis": ["reviews", "high-risk terms"]}):
    _d["reports_to"] = "cro"
    engine.add_department(_d, before="exec")
engine.add_department({"id": "gov", "name": "Governance", "agent": "Arbiter", "reports_to": "ceo",
                       "mission": "Hold the agents to account for the founder: score every approved decision against its outcome, "
                                  "track department budgets, and grade each agent.", "kpis": ["decisions scored", "agent grades"]},
                      before="exec")


def _boot():
    trust.init()


def _boot_all():
    _boot()
    engine.add_schedules([("cro_review", "weekly wed 06:30"), ("governance_review", "weekly mon 07:00")])


engine.BOOT.append(_boot_all)


@playbook("trust_daily", "Security day", "physsec",
          "Aegis and Lookout: badge activity and incidents at every site, the sweep that removes access for customers who left, "
          "badges for new customers, and the security operations centre's alerts and containment.")
def trust_daily(ctx):
    _boot()
    rnd = random.Random(f"trust-{products.today()}")
    s = trust.security_day(rnd)
    c = trust.soc_day(rnd)
    ctx.log(f"{len(s['incidents'])} physical incidents, {c['alerts']} SOC alerts")
    md = (f"{s['entries']:,} badge entries across all sites. {len(s['incidents'])} physical incidents; {s['swept']} stale badges removed.\n\n" +
          (table(s["incidents"], ["site", "kind", "severity"]) if s["incidents"] else "") +
          f"\nSecurity operations: {c['alerts']} alerts.\n\n" + (table(c["by_kind"], ["kind", "severity", "target"]) if c["by_kind"] else ""))
    return {"physical_incidents": len(s["incidents"]), "badges_swept": s["swept"], "soc_alerts": c["alerts"]}, md


@playbook("compliance_check", "Compliance controls", "compliance",
          "Canon: test every control against vix's own records and save the evidence (data/evidence/<date>/). Twice a year an "
          "independent-auditor simulation samples six months of evidence and gives an opinion.")
def compliance_check(ctx):
    _boot()
    r = trust.run_controls()
    aud = None
    last = db.scalar("select max(sim_date) from cmp_audit")
    if (not last or last < (products.today_d() - __import__("datetime").timedelta(days=182)).isoformat()) and db.scalar(
            "select count(distinct sim_date) from cmp_result") >= 12:
        aud = trust.audit(random.Random(f"audit-{products.today()}"))
    ctx.log(f"readiness {r['readiness']}%")
    fails = [c for c in r["controls"] if c["status"] in ("fail", "exception")]
    md = (f"Audit readiness **{r['readiness']}%** across {len(r['controls'])} controls; {len(fails)} need attention. Evidence saved for each "
          f"control under data/evidence/{products.today()}/.\n\n" + table(r["controls"], ["control", "family", "title", "owner", "status", "evidence"]) +
          (f"\n## Independent audit (simulated)\n\nSampled {aud['samples']} evidence records from six months: {aud['exceptions']} exceptions. "
           f"Opinion: **{aud['opinion']}**.\n\n" + table(aud["detail"], ["control", "date", "status"]) if aud and aud.get("opinion") else "") +
          "\n_A simulation of a controls programme. It is not an attestation and no auditor was involved._\n")
    return {"readiness": r["readiness"], "failing": [c["control"] for c in fails], "audit_opinion": aud.get("opinion") if aud else None}, md


@playbook("risk_review", "Risk register", "risk",
          "Brace: refresh the risk register from live data (concentration, power, network, leverage, vendors, CVEs, construction, "
          "price pressure, security, FX, key sites), take and test a backup, and run the quarterly continuity drill.")
def risk_review(ctx):
    _boot()
    rnd = random.Random(f"risk-{products.today()}")
    risks = trust.refresh_risks()
    b = trust.backup(rnd)
    dr = None
    last = db.scalar("select max(sim_date) from bcm_drill")
    if not last or last < (products.today_d() - __import__("datetime").timedelta(days=90)).isoformat():
        dr = trust.drill(rnd)
    ctx.log(f"top risk {risks[0]['title']} ({risks[0]['score']})")
    md = ("## Risk register\n\n" + table(risks, ["id", "title", "owner", "likelihood", "impact", "score", "trend", "evidence", "mitigation"]) +
          f"\n## Backup\n\n{b['path']} ({b['bytes'] / 1e6:.0f} MB), integrity {b['integrity']}, restore test "
          f"{'passed' if b['restore_ok'] else 'FAILED'}.\n" +
          (f"\n## Continuity drill: {dr['scenario']}\n\nResult **{dr['result']}**: {dr['detail']}. Recovery {dr['rto_min']} min against a "
           f"{dr['rto_target']}-minute target.\n" if dr else ""))
    return {"top_risk": risks[0]["title"], "top_score": risks[0]["score"], "high_risks": sum(1 for r in risks if r["score"] >= 15),
            "backup_ok": b["restore_ok"], "drill": dr["result"] if dr else None}, md


@playbook("security_report", "Security report", "soc",
          "Lookout and Aegis: physical incidents and security alerts over 30 days, time to contain, customer impact.")
def security_report(ctx):
    _boot()
    since = (products.today_d() - __import__("datetime").timedelta(days=30)).isoformat()
    phys = db.rows("select kind, severity, count(*) n from sec_incident where sim_date>? group by kind, severity order by n desc", (since,))
    soc = db.rows("select kind, severity, count(*) n, round(avg(minutes_to_contain),1) avg_min, sum(customer_impact) impact from soc_alert"
                  " where sim_date>? group by kind, severity order by n desc", (since,))
    ctx.log("security report")
    md = ("## Physical security (30 days)\n\n" + table(phys, ["kind", "severity", "n"]) + "\n## Security operations (30 days)\n\n" +
          table(soc, ["kind", "severity", "n", "avg_min", "impact"]))
    return {"physical": sum(r["n"] for r in phys), "soc": sum(r["n"] for r in soc),
            "high": sum(r["n"] for r in phys + soc if r["severity"] in ("high", "critical"))}, md


@playbook("legal_report", "Contract terms", "legal",
          "Clause: non-standard terms requested on large deals in the last 90 days, how each was handled, and the data-residency "
          "commitments now in force.")
def legal_report(ctx):
    _boot()
    since = (products.today_d() - __import__("datetime").timedelta(days=90)).isoformat()
    rows = db.rows("select l.sim_date, t.name, l.request, l.decision, l.risk, l.note from legal_review l join tenants t on t.id=l.tenant"
                   " where l.sim_date>? order by l.id desc limit 40", (since,))
    res = db.rows("select t.name, r.region, r.since from legal_residency r join tenants t on t.id=r.tenant")
    ctx.log(f"{len(rows)} reviews")
    md = (f"{len(rows)} contract reviews in 90 days.\n\n" + table(rows, ["sim_date", "name", "request", "decision", "risk", "note"]) +
          "\n## Data-residency commitments\n\n" + table(res, ["name", "region", "since"]))
    return {"reviews": len(rows), "high_risk": sum(1 for r in rows if r["risk"] == "high"), "residency": len(res)}, md


@playbook("cro_review", "Trust & risk review", "cro",
          "Rampart: weekly trust review. Delegates to Compliance, Risk & Resilience, Security and Legal; escalates failing key "
          "controls, high risks, failed restores or drills, and high-severity security events.")
def cro_review(ctx):
    _boot()
    cc = _sub(ctx, "compliance_check")
    rr = _sub(ctx, "risk_review")
    sr = _sub(ctx, "security_report")
    lr = _sub(ctx, "legal_report")
    a, b, c, d = (cc["summary"] or {}), (rr["summary"] or {}), (sr["summary"] or {}), (lr["summary"] or {})
    esc = []
    key = [x for x in a.get("failing", []) if x in ("AC-01", "CM-01", "FI-01", "BC-01", "IR-02")]
    if key:
        esc.append(f"key controls failing: {', '.join(key)}")
    if b.get("high_risks"):
        esc.append(f"{b['high_risks']} risks scored 15 or more (top: {b['top_risk']})")
    if b.get("backup_ok") is False:
        esc.append("backup restore test failed")
    if b.get("drill") == "fail":
        esc.append("continuity drill failed")
    if (c.get("high") or 0) >= 3:
        esc.append(f"{c['high']} high-severity security events in 30 days")
    if a.get("audit_opinion") == "qualified":
        esc.append("the simulated audit gave a qualified opinion")
    ctx.log(f"{len(esc)} escalations")
    md = ("Weekly trust and risk review by Rampart.\n\n"
          f"- Canon: readiness {a.get('readiness')}%" + (f", failing {', '.join(a['failing'])}" if a.get("failing") else "") +
          (f"; audit opinion {a['audit_opinion']}" if a.get("audit_opinion") else "") + "\n"
          f"- Brace: top risk {b.get('top_risk')} ({b.get('top_score')}), backup {'ok' if b.get('backup_ok') else 'FAILED'}"
          + (f", drill {b['drill']}" if b.get("drill") else "") + "\n"
          f"- Lookout and Aegis: {c.get('physical')} physical incidents, {c.get('soc')} security alerts in 30 days, {c.get('high')} high or critical\n"
          f"- Clause: {d.get('reviews')} contract reviews, {d.get('high_risk')} high-risk requests, {d.get('residency')} residency commitments\n"
          "\n## Escalations\n\n" + ("".join(f"- {e}\n" for e in esc) or "- none\n"))
    return {"escalations": esc, "readiness": a.get("readiness"), "top_risk": b.get("top_risk")}, md


@playbook("governance_review", "Governance review", "gov",
          "Arbiter: score every approved decision whose check date has come (did the hire fix the SLA, did the hall earn its case, "
          "did the PPA save money), compare department costs with budget, and grade every agent.")
def governance_review(ctx):
    _boot()
    scored = trust.score_outcomes()
    last = fin.closed_periods(1) if fin.opened() else []
    bud = trust.budgets(last[0]["period"]) if last else []
    cards = trust.scorecards()
    agg = db.rows("select kind, count(*) n, round(avg(score),2) avg_score from gov_outcome where status='scored' and score is not null group by kind")
    pending = db.scalar("select count(*) from gov_outcome where status='pending'")
    low = [c for c in cards if c["grade"] == "D"]
    ctx.log(f"{len(scored)} outcomes scored, {len(low)} agents graded D")
    md = (f"Scored **{len(scored)}** decisions this week; {pending} approved decisions are waiting for their check date.\n\n" +
          ("## Outcomes scored this week\n\n" + table(scored, ["decision", "kind", "title", "agent", "verdict", "why"]) if scored else "") +
          "\n## Track record by decision kind\n\n" + table(agg, ["kind", "n", "avg_score"]) +
          ("\n## Department costs vs budget (" + last[0]["period"] + ")\n\n" + table([{**r, "budget": usd(r["budget"]), "actual": usd(r["actual"])} for r in bud],
                                                                                  ["dept", "budget", "actual", "variance_pct"]) if bud else "") +
          "\n## Agent scorecards (last 30 days)\n\n" + table(sorted(cards, key=lambda c: c["grade"]),
                                                            ["agent", "dept", "grade", "runs", "failed", "decisions", "approved_pct", "outcome_score", "kpi", "kpi_ok"]))
    return {"scored": len(scored), "pending": pending, "grade_d": [c["agent"] for c in low],
            "grades": {g: sum(1 for c in cards if c["grade"] == g) for g in "ABCD"}}, md


engine.DAILY_STEPS.append("trust_daily")
engine.WEEKLY_STEPS.extend(["cro_review", "governance_review"])
engine.KPI_HOOKS.append(trust.kpi_hook)
engine.ON_DECIDE.append(trust.on_decide)
products.ON_CONTRACT_EVENT.append(trust.on_contract_event)
crm.ON_OPPORTUNITY.append(trust.legal_on_opportunity)
crm.ON_OPPORTUNITY.append(trust.residency_guard)


def _board_cro(r):
    s = r["summary"] or {}
    return (f"- **Rampart (trust & risk):** compliance readiness {s.get('readiness')}%, top risk {s.get('top_risk')}. Report: {r['report']}\n",
            [f"Trust escalation from Rampart: {e}" for e in s.get("escalations", [])])


def _board_gov(r):
    s = r["summary"] or {}
    g = s.get("grades") or {}
    return (f"- **Arbiter (governance):** {s.get('scored')} decision outcomes scored, {s.get('pending')} pending; agent grades "
            f"A {g.get('A', 0)} / B {g.get('B', 0)} / C {g.get('C', 0)} / D {g.get('D', 0)}"
            + (f" (D: {', '.join(s['grade_d'])})" if s.get("grade_d") else "") + f". Report: {r['report']}\n", [])


engine.BOARD_CHIEFS.append(("cro_review", _board_cro))
engine.BOARD_CHIEFS.append(("governance_review", _board_gov))
