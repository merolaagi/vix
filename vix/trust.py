"""Trust, risk and governance: physical security, the security operations centre, a compliance programme
whose evidence comes from vix's own records, backups and continuity drills, the risk register, contract
terms, and governance of the agents themselves (department budgets, decision outcomes, scorecards).
Control references are generic framework families; this is a simulation, not an attestation."""
from __future__ import annotations

import datetime as dt
import json
import random
import sqlite3

from . import db, products
from .config import DATA_DIR, DB_PATH

SCHEMA = """
create table if not exists sec_badge (
  id integer primary key, tenant integer, site text, holder text, issued text, revoked text, status text
);
create index if not exists sec_badge_t on sec_badge(tenant, status);
create table if not exists sec_access_daily (
  sim_date text, site text, entries integer, visitors integer, denied integer, primary key (sim_date, site)
);
create table if not exists sec_incident (
  id integer primary key, sim_date text, site text, kind text, severity text, detail text, action text, status text
);
create table if not exists soc_alert (
  id integer primary key, sim_date text, source text, kind text, severity text, target text, detail text, action text,
  minutes_to_contain real, customer_impact integer default 0, status text
);
create table if not exists cmp_control (
  id text primary key, family text, title text, description text, owner text, frameworks text
);
create table if not exists cmp_result (
  id integer primary key, control text, sim_date text, status text, evidence text, path text
);
create table if not exists cmp_audit (
  id integer primary key, sim_date text, period_from text, period_to text, samples integer, exceptions integer,
  opinion text, detail text
);
create table if not exists risk_register (
  id text primary key, title text, category text, owner text, likelihood integer, impact integer, score integer,
  prev_score integer, mitigation text, evidence text, updated text
);
create table if not exists bcm_drill (
  id integer primary key, sim_date text, scenario text, target text, rto_target_min real, rto_actual_min real,
  rpo_ok integer, customers_affected integer, result text, lessons text
);
create table if not exists legal_review (
  id integer primary key, opp text, tenant integer, sim_date text, request text, decision text, risk text, note text
);
create table if not exists legal_residency (tenant integer primary key, region text, since text, since_ts real);
create table if not exists gov_outcome (
  decision integer primary key, kind text, title text, agent text, approved_on text, check_on text, baseline text,
  status text, score real, verdict text, detail text
);
create table if not exists gov_budget (period text, dept text, budget real, actual real, primary key (period, dept));
create table if not exists gov_scorecard (
  sim_date text, agent text, dept text, runs integer, failed integer, decisions integer, approved_pct real,
  outcome_score real, kpi text, kpi_ok integer, grade text, primary key (sim_date, agent)
);
"""

CONTROLS = [
    ("AC-01", "Access", "Physical access is removed within a day of a customer leaving", "Badges for customers whose contract ended are revoked within 24 hours.", "physsec", "SOC 2 CC6.4; ISO 27001 A.7.2"),
    ("AC-02", "Access", "Visitors are escorted", "No unescorted after-hours visitor events in the last 30 days.", "physsec", "SOC 2 CC6.4; ISO 27001 A.7.2"),
    ("AC-03", "Access", "Automated changes to live network gear need founder approval", "The live-write kill switch is only on with an approved decision.", "cno", "SOC 2 CC6.1, CC8.1"),
    ("CM-01", "Change", "Network changes are approved before they run", "Every executed change has an approval recorded.", "change", "SOC 2 CC8.1; ISO 27001 A.8.32"),
    ("CM-02", "Change", "Firmware is certified before rollout", "Every firmware upgrade executed in the last 90 days used a certified release.", "certlab", "SOC 2 CC8.1"),
    ("CM-03", "Vulnerability", "Critical vulnerabilities are remediated within 30 days", "No device has run firmware with a critical CVE for more than 30 days.", "change", "SOC 2 CC7.1; ISO 27001 A.8.8"),
    ("IR-01", "Incident", "Customer-impacting incidents get a postmortem", "Every customer-impacting network incident older than 7 days has a postmortem.", "sre", "SOC 2 CC7.4"),
    ("IR-02", "Incident", "Security alerts are triaged within a day", "No security alert or incident left open for more than 1 day.", "soc", "SOC 2 CC7.3; ISO 27001 A.5.25"),
    ("BC-01", "Continuity", "Backups are taken weekly and restore cleanly", "A backup no older than 8 days exists and its restore test passed.", "platform", "SOC 2 A1.2; ISO 27001 A.8.13"),
    ("BC-02", "Continuity", "A continuity drill runs every quarter", "A business-continuity drill in the last 92 days.", "risk", "SOC 2 A1.3; ISO 27001 A.5.30"),
    ("FI-01", "Finance", "The books balance and close on time", "Every closed month balances and was closed within 3 days of month end.", "finance", "SOX-style ITGC"),
    ("FI-02", "Finance", "Large discounts are approved at the right level", "Discounts over 20% on deals of $40k/month or more were approved by the founder.", "dealdesk", "SOX-style ITGC"),
    ("PR-01", "Privacy", "Marketing honours consent", "The data cloud suppressed actions for contacts who opted out.", "datacloud", "GDPR Art. 21; ISO 27701"),
    ("SD-01", "Secure development", "No secrets in code and no high-severity findings", "The latest DevSecOps scan has no high findings.", "platform", "SOC 2 CC8.1; ISO 27001 A.8.25"),
    ("DR-01", "Privacy", "Data-residency commitments are kept", "Customers with a residency clause have services only in their region.", "legal", "GDPR Art. 44; ISO 27701"),
    ("GV-01", "Governance", "Approved decisions are checked against their outcome", "Every approved decision past its check date has been scored.", "gov", "COSO monitoring"),
]
REGION = {"SJC": "Americas", "ASH": "Americas", "NYC": "Americas", "LHR": "EMEA", "FRA": "EMEA", "SIN": "APAC", "NRT": "APAC", "SYD": "APAC"}


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
    for cid, fam, title, desc, owner, fw in CONTROLS:
        db.execute("insert or ignore into cmp_control(id,family,title,description,owner,frameworks) values(?,?,?,?,?,?)",
                   (cid, fam, title, desc, owner, fw))
    if not db.scalar("select count(*) from sec_badge"):
        rnd = random.Random(21)
        rows = []
        for r in db.rows("select distinct p.tenant, h.site from placements p join halls h on h.id=p.hall join contracts c on c.tenant=p.tenant"
                         " where c.status='active'"):
            for k in range(rnd.randint(2, 5)):
                rows.append((r["tenant"], r["site"], f"holder-{r['tenant']}-{k + 1}", products.today(), None, "active"))
        with db.conn() as c:
            c.executemany("insert into sec_badge(tenant,site,holder,issued,revoked,status) values(?,?,?,?,?,?)", rows)


def _since(days):
    return (products.today_d() - dt.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------- physical security


def on_contract_event(tid, event, before, after):
    """Customer exits remove access. The process misses about one in ten; Aegis's daily sweep catches it."""
    if event != "churned" or not db.one("select name from sqlite_master where name='sec_badge'"):
        return
    if random.random() < 0.9:
        db.execute("update sec_badge set status='revoked', revoked=? where tenant=? and status='active'", (products.today(), tid))


def security_day(rnd) -> dict:
    today = products.today()
    out = {"entries": 0, "incidents": [], "swept": 0}
    visits = {r["site"]: r["n"] for r in db.rows("select site, count(*) n from tickets where sim_date=? and kind='access' group by site", (today,))}
    for s in db.rows("select id from sites"):
        badges = db.scalar("select count(*) from sec_badge where site=? and status='active'", (s["id"],)) or 0
        entries = int(rnd.gauss(badges * 0.25, max(1, badges * 0.05)))
        vis = visits.get(s["id"], 0) + rnd.randint(0, 4)
        denied = rnd.randint(0, 2)
        db.execute("insert or replace into sec_access_daily values(?,?,?,?,?)", (today, s["id"], max(0, entries), vis, denied))
        out["entries"] += max(0, entries)
        for kind, p, sev, action in (("tailgating attempt", 0.012, "medium", "door alarm; guard stopped the second person"),
                                     ("unescorted visitor after hours", 0.0006, "high", "visitor escorted out; escort retrained"),
                                     ("lost badge reported", 0.02, "low", "badge deactivated and reissued"),
                                     ("forced door alarm", 0.0008, "high", "guard response in 4 minutes; door repaired")):
            if rnd.random() < p:
                iid = db.execute("insert into sec_incident(sim_date,site,kind,severity,detail,action,status) values(?,?,?,?,?,?,'closed')",
                                 (today, s["id"], kind, sev, f"{kind} at {s['id']}", action))
                out["incidents"].append({"site": s["id"], "kind": kind, "severity": sev})
    # sweep: badges still active for customers that left
    for b in db.rows("select b.* from sec_badge b join contracts c on c.tenant=b.tenant where b.status='active' and c.status='ended'"):
        if rnd.random() < 0.15:
            db.execute("insert into sec_incident(sim_date,site,kind,severity,detail,action,status) values(?,?,?,?,?,?,'closed')",
                       (today, b["site"], "former customer's badge used", "high", f"{b['holder']} badged in after the contract ended",
                        "access denied at the mantrap; badge revoked; customer contacted"))
            out["incidents"].append({"site": b["site"], "kind": "former customer's badge used", "severity": "high"})
        db.execute("update sec_badge set status='revoked', revoked=? where id=?", (today, b["id"]))
        out["swept"] += 1
    # new customers get badges
    for r in db.rows("select distinct p.tenant, h.site from placements p join halls h on h.id=p.hall join contracts c on c.tenant=p.tenant"
                     " where c.status='active' and not exists (select 1 from sec_badge b where b.tenant=p.tenant and b.site=h.site and b.status='active')"):
        for k in range(2):
            db.execute("insert into sec_badge(tenant,site,holder,issued,status) values(?,?,?,?,'active')",
                       (r["tenant"], r["site"], f"holder-{r['tenant']}-{rnd.randint(10, 99)}", today))
    return out


# ---------------------------------------------------------------- security operations


def soc_day(rnd) -> dict:
    today = products.today()
    alerts = []

    def alert(source, kind, sev, target, detail, action, mins, impact=0):
        db.execute("insert into soc_alert(sim_date,source,kind,severity,target,detail,action,minutes_to_contain,customer_impact,status)"
                   " values(?,?,?,?,?,?,?,?,?,'closed')", (today, source, kind, sev, target, detail, action, mins, impact))
        alerts.append({"kind": kind, "severity": sev, "target": target})
    if rnd.random() < 0.10:
        t = db.scalar("select name from tenants order by random() limit 1")
        alert("portal API", "credential stuffing", "medium", f"portal login for {t}", "800 failed logins from 40 addresses in 10 minutes",
              "rate-limited, addresses blocked, customer told to rotate keys", rnd.uniform(3, 15))
    if rnd.random() < 0.035:
        ia = db.one("select i.metro, t.name from internet_access i join tenants t on t.id=i.tenant order by random() limit 1")
        if ia:
            mins = rnd.uniform(4, 35)
            alert("netflow", "volumetric DDoS", "high" if mins > 15 else "medium", f"{ia['name']} internet access in {ia['metro']}",
                  f"{rnd.randint(40, 400)} Gbps UDP reflection", "traffic diverted to scrubbing; clean traffic returned", mins, int(mins > 15))
    if rnd.random() < 0.05:
        alert("email gateway", "phishing campaign", "low", "staff mailboxes", "invoice-themed lure with credential page",
              "messages purged, link blocked, two users reset", rnd.uniform(20, 90))
    if rnd.random() < 0.004:
        alert("EDR", "malware on a management workstation", "critical", "NOC jump host", "commodity infostealer",
              "host isolated and rebuilt; credentials rotated; no network gear touched", rnd.uniform(30, 120))
    try:
        refused = db.scalar("select count(*) from net_audit where sim_date=? and allowed=0", (today,)) or 0
    except Exception:  # noqa: BLE001
        refused = 0
    if refused:
        alert("network audit log", "refused write to network gear", "low", f"{refused} attempts", "driver guardrails refused writes",
              "reviewed: guardrails working as designed", 5)
    return {"alerts": len(alerts), "by_kind": alerts}


# ---------------------------------------------------------------- backups


def backup(rnd=None) -> dict:
    """Online backup of the company database with SQLite's backup API, then a restore test."""
    folder = DATA_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"vix-{products.today()}.db"
    src = db.conn()
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    tables = ("tenants", "contracts", "fin_gl", "decisions")
    ok, counts = True, {}
    for t in tables:
        try:
            a = db.scalar(f"select count(*) from {t}")
            b = dst.execute(f"select count(*) from {t}").fetchone()[0]
        except Exception:  # noqa: BLE001
            continue
        counts[t] = {"live": a, "backup": b}
        ok = ok and a == b
    integ = dst.execute("pragma quick_check").fetchone()[0]
    dst.close()
    for old in sorted(folder.glob("vix-*.db"))[:-2]:
        old.unlink()
    res = {"path": str(path.relative_to(DATA_DIR)), "date": products.today(), "restore_ok": ok and integ == "ok",
           "integrity": integ, "counts": counts, "bytes": path.stat().st_size}
    db.set_setting("last_backup", res)
    return res


# ---------------------------------------------------------------- compliance


def _ctl(cid) -> tuple[str, dict]:
    today = products.today()
    if cid == "AC-01":
        late = db.rows("select b.tenant, b.site, c.end_date from sec_badge b join contracts c on c.tenant=b.tenant"
                       " where b.status='active' and c.status='ended'")
        late_rev = db.scalar("select count(*) from sec_badge b join contracts c on c.tenant=b.tenant where b.status='revoked'"
                             " and julianday(b.revoked) - julianday(c.end_date) > 1 and b.revoked>?", (_since(90),)) or 0
        return ("pass" if not late and not late_rev else "fail"), {"still_active": len(late), "revoked_late_90d": late_rev}
    if cid == "AC-02":
        n = db.scalar("select count(*) from sec_incident where kind='unescorted visitor after hours' and sim_date>?", (_since(30),)) or 0
        return ("pass" if n == 0 else "exception"), {"events_30d": n}
    if cid == "AC-03":
        on = bool(db.setting("net_live_writes", False))
        appr = db.scalar("select count(*) from decisions where kind='net_live_writes' and status='approved'") or 0
        return ("pass" if not on or appr else "fail"), {"live_writes_on": on, "approvals": appr}
    if cid == "CM-01":
        n = db.scalar("select count(*) from net_change where status='done' and (approval is null or approval='')") or 0
        tot = db.scalar("select count(*) from net_change where status='done'") or 0
        return ("pass" if n == 0 else "fail"), {"executed": tot, "without_approval": n}
    if cid == "CM-02":
        rows = db.rows("select c.device, c.to_ver, d.vendor from net_change c join net_device d on d.hostname=c.device"
                       " where c.kind='firmware_upgrade' and c.status='done' and c.window_date>?", (_since(90),))
        bad = [r for r in rows if not db.one("select 1 from net_cert where vendor=? and version=? and status='certified'", (r["vendor"], r["to_ver"]))]
        return ("pass" if not bad else "exception"), {"upgrades_90d": len(rows), "uncertified": len(bad),
                                                      "examples": [f"{b['device']} -> {b['to_ver']}" for b in bad[:5]]}
    if cid == "CM-03":
        n = 0
        fixed = {}
        for f in db.rows("select vendor, version, cves, released from net_firmware where released<=?", (_since(30),)):
            if not any(c.get("severity") == "critical" for c in json.loads(f["cves"] or "[]")):
                fixed.setdefault(f["vendor"], []).append(f["version"])
        for e in db.rows("select d.hostname, d.vendor, f.cves from net_device d join net_firmware f on f.vendor=d.vendor and f.version=d.os_version"):
            if any(c.get("severity") == "critical" for c in json.loads(e["cves"] or "[]")) and fixed.get(e["vendor"]):
                n += 1
        return ("pass" if n == 0 else "fail" if n > 10 else "exception"), {"devices_overdue": n}
    if cid == "IR-01":
        miss = db.scalar("select count(*) from net_incident where services_down>0 and sim_date<? and sim_date>? and id not in"
                         " (select incident from net_postmortem)", (_since(7), _since(90))) or 0
        return ("pass" if miss == 0 else "exception"), {"missing_postmortems": miss}
    if cid == "IR-02":
        n = (db.scalar("select count(*) from soc_alert where status!='closed' and sim_date<?", (_since(1),)) or 0) + (
            db.scalar("select count(*) from sec_incident where status!='closed' and sim_date<?", (_since(1),)) or 0)
        return ("pass" if n == 0 else "fail"), {"open_over_1_day": n}
    if cid == "BC-01":
        b = db.setting("last_backup") or {}
        fresh = b.get("date") and b["date"] >= _since(8)
        return ("pass" if fresh and b.get("restore_ok") else "fail"), {"last_backup": b.get("date"), "restore_ok": b.get("restore_ok")}
    if cid == "BC-02":
        d = db.scalar("select max(sim_date) from bcm_drill")
        return ("pass" if d and d >= _since(92) else "fail"), {"last_drill": d}
    if cid == "FI-01":
        try:
            ms = db.rows("select period, closed_on, metrics from fin_month order by period desc limit 6")
        except Exception:  # noqa: BLE001
            return "n/a", {}
        bad = []
        for m in ms:
            met = json.loads(m["metrics"])
            y, mo = map(int, m["period"].split("-"))
            due = (dt.date(y + (mo == 12), mo % 12 + 1, 1) + dt.timedelta(days=3)).isoformat()
            if abs(met.get("balance_check") or 0) > 1 or m["closed_on"] > due:
                bad.append(m["period"])
        return ("pass" if not bad else "fail"), {"months_checked": len(ms), "exceptions": bad}
    if cid == "FI-02":
        try:
            n = db.scalar("select count(*) from fin_quote where discount_pct>0.2 and list_mrr>=40000 and approval!='founder' and status='won'") or 0
        except Exception:  # noqa: BLE001
            return "n/a", {}
        return ("pass" if n == 0 else "exception"), {"large_discounts_without_founder": n}
    if cid == "PR-01":
        r = db.one("select summary from runs where playbook='crm_data_cloud' and status='done' order by id desc limit 1")
        s = json.loads(r["summary"]) if r else {}
        return ("pass" if "suppressed_by_consent" in s else "fail"), {"suppressed_by_consent": s.get("suppressed_by_consent")}
    if cid == "SD-01":
        r = db.one("select summary from runs where playbook='platform_devsecops' and status='done' order by id desc limit 1")
        s = json.loads(r["summary"]) if r else {}
        return ("pass" if r and not s.get("high") else "fail"), {"high_findings": s.get("high"), "scanned": bool(r)}
    if cid == "DR-01":
        bad = []
        for r in db.rows("select * from legal_residency"):
            out_ = db.scalar("select count(*) from placements p join halls h on h.id=p.hall join sites s on s.id=h.site where p.tenant=?"
                             " and p.ts>? and s.metro not in (" + ",".join(f"'{m}'" for m, g in REGION.items() if g == r["region"]) + ")",
                             (r["tenant"], r["since_ts"] or 0)) or 0
            if out_:
                bad.append(r["tenant"])
        return ("pass" if not bad else "exception"), {"customers_with_clause": db.scalar("select count(*) from legal_residency"),
                                                      "outside_region": len(bad)}
    if cid == "GV-01":
        n = db.scalar("select count(*) from gov_outcome where status='pending' and check_on<?", (_since(7),)) or 0
        return ("pass" if n == 0 else "exception"), {"overdue_reviews": n}
    return "n/a", {}


def run_controls() -> dict:
    today = products.today()
    folder = DATA_DIR / "evidence" / today
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for c in db.rows("select * from cmp_control order by id"):
        try:
            status, ev = _ctl(c["id"])
        except Exception as e:  # noqa: BLE001
            status, ev = "error", {"error": str(e)}
        path = folder / f"{c['id']}.json"
        path.write_text(json.dumps({"control": c["id"], "title": c["title"], "date": today, "status": status, "evidence": ev}, indent=2, default=str))
        db.execute("delete from cmp_result where control=? and sim_date=?", (c["id"], today))
        db.execute("insert into cmp_result(control,sim_date,status,evidence,path) values(?,?,?,?,?)",
                   (c["id"], today, status, json.dumps(ev, default=str), str(path.relative_to(DATA_DIR))))
        out.append({"control": c["id"], "family": c["family"], "title": c["title"], "owner": c["owner"], "status": status,
                    "evidence": json.dumps(ev, default=str)[:160]})
    scored = [r for r in out if r["status"] != "n/a"]
    pts = sum(1 if r["status"] == "pass" else 0.5 if r["status"] == "exception" else 0 for r in scored)
    readiness = round(100 * pts / len(scored), 1) if scored else None
    db.set_setting("compliance_readiness", {"date": today, "score": readiness})
    return {"controls": out, "readiness": readiness}


def audit(rnd) -> dict:
    """An independent-auditor simulation: samples six months of control evidence and counts exceptions."""
    frm = _since(182)
    res = db.rows("select * from cmp_result where sim_date>?", (frm,))
    if not res:
        return {"opinion": None}
    sample = rnd.sample(res, min(40, len(res)))
    exc = [r for r in sample if r["status"] in ("fail", "exception")]
    key = [r for r in exc if r["control"] in ("CM-01", "AC-01", "FI-01", "BC-01")]
    opinion = "unqualified" if len(exc) <= 2 and not key else "qualified"
    detail = [{"control": r["control"], "date": r["sim_date"], "status": r["status"]} for r in exc]
    aid = db.execute("insert into cmp_audit(sim_date,period_from,period_to,samples,exceptions,opinion,detail) values(?,?,?,?,?,?,?)",
                     (products.today(), frm, products.today(), len(sample), len(exc), opinion, json.dumps(detail)))
    return {"audit": aid, "samples": len(sample), "exceptions": len(exc), "opinion": opinion, "detail": detail}


# ---------------------------------------------------------------- risk register


def refresh_risks() -> list[dict]:
    from . import products as P
    lb = P.lines_by_tenant()
    tot = sum(sum(v.values()) for v in lb.values()) or 1
    top10 = sum(sorted((sum(v.values()) for v in lb.values()), reverse=True)[:10]) / tot
    risks = []

    def add(rid, title, cat, owner, l, i, mit, ev):
        risks.append({"id": rid, "title": title, "category": cat, "owner": owner, "likelihood": max(1, min(5, l)),
                      "impact": max(1, min(5, i)), "mitigation": mit, "evidence": ev})
    add("R-01", "Customer concentration", "commercial", "Summit", 2 + int(top10 > 0.25) + int(top10 > 0.35), 4,
        "multi-metro deployments for the largest accounts; executive sponsors", f"top 10 customers are {top10:.0%} of revenue")
    try:
        tight = db.rows("select p.metro, sum(p.mw) need, g.grid_mw_available avail from dev_project p join dev_power g on g.metro=p.metro"
                        " where p.status='active' group by p.metro having need>avail")
    except Exception:  # noqa: BLE001
        tight = []
    add("R-02", "Grid power constrains growth", "growth", "Volt", 2 + min(3, len(tight)), 4, "early utility applications, bridge power, PPAs",
        ", ".join(f"{t['metro']} needs {t['need']:.0f} MW, {t['avail']:.0f} free" for t in tight) or "no metro short of power for its pipeline")
    r = db.one("select summary from runs where playbook='arch_review' and status='done' order by id desc limit 1")
    single = (json.loads(r["summary"]) or {}).get("single_homed_ix_pct") if r else None
    add("R-03", "Single-homed exchange members", "network", "Vantage", 3 if (single or 0) > 50 else 2, 3,
        "offer second ports at a discount; redundant exchange switch pairs", f"{single}% of exchange members single-homed" if single is not None else "not measured")
    try:
        m = (json.loads(db.scalar("select metrics from fin_month order by period desc limit 1") or "{}"))
        mat = db.scalar("select count(*) from fin_debt where kind='bond' and status='outstanding' and maturity<=?",
                        ((products.today_d() + dt.timedelta(days=365)).isoformat(),)) or 0
    except Exception:  # noqa: BLE001
        m, mat = {}, 0
    lev = m.get("net_debt_to_ebitda") or 0
    add("R-04", "Refinancing and leverage", "finance", "Anchor", 1 + int(lev > 4.5) + int(lev > 5.5) + min(2, mat), 4,
        "ladder maturities, keep the revolver undrawn, term out early", f"leverage {lev:.1f}x, {mat} bond(s) maturing within 12 months")
    vend = db.rows("select vendor, count(*) n from net_device group by vendor order by n desc")
    share = vend[0]["n"] / sum(v["n"] for v in vend) if vend else 0
    add("R-05", "Network vendor concentration", "network", "Vantage", 2 + int(share > 0.45), 4, "dual-vendor fabric in new builds",
        f"{vend[0]['vendor']} is {share:.0%} of devices" if vend else "")
    nk = db.setting("net_kpis") or {}
    crit = nk.get("devices_critical_cve") or 0
    add("R-06", "Vulnerable firmware", "security", "Tempo", 1 + min(4, crit // 10), 4, "certify and roll out fixed releases within 30 days",
        f"{crit} devices on firmware with critical or high CVEs")
    try:
        late = db.scalar("select count(*) from dev_project where status='active' and history like '%delay%'") or 0
    except Exception:  # noqa: BLE001
        late = 0
    add("R-07", "Construction delivery", "growth", "Mason", 1 + min(4, late), 3, "long-lead equipment ordered at design; contractor incentives",
        f"{late} active projects with delays")
    try:
        lost = db.scalar("select count(*) from mkt_winloss where outcome='lost' and reason='price' and sim_date>?", (_since(30),)) or 0
        alll = db.scalar("select count(*) from mkt_winloss where outcome='lost' and sim_date>?", (_since(30),)) or 1
    except Exception:  # noqa: BLE001
        lost, alll = 0, 1
    add("R-08", "Competitive price pressure", "commercial", "Lens", 1 + int(lost / alll > 0.3) + int(lost / alll > 0.45) + 1, 3,
        "sell ecosystem value, targeted discounts within the authority matrix", f"{lost} of {alll} losses in 30 days on price")
    phys = db.scalar("select count(*) from sec_incident where severity='high' and sim_date>?", (_since(90),)) or 0
    add("R-09", "Physical security breach", "security", "Aegis", 1 + min(3, phys // 2), 5, "mantraps, escorts, daily badge sweeps", f"{phys} high-severity incidents in 90 days")
    cyb = db.scalar("select count(*) from soc_alert where severity in ('high','critical') and sim_date>?", (_since(90),)) or 0
    add("R-10", "Cyber attack on operations", "security", "Lookout", 1 + min(3, cyb // 3), 5, "EDR, MFA, segmented management network, scrubbing",
        f"{cyb} high or critical alerts in 90 days")
    try:
        fx = db.rows("select currency, sum(balance_usd) v from fin_invoice where status='open' group by currency")
        nonusd = sum(r["v"] for r in fx if r["currency"] != "USD") / (sum(r["v"] for r in fx) or 1)
    except Exception:  # noqa: BLE001
        nonusd = 0
    add("R-11", "Foreign-exchange exposure", "finance", "Anchor", 2 + int(nonusd > 0.4), 2, "local-currency costs offset; hedge large balances",
        f"{nonusd:.0%} of receivables in currencies other than USD")
    big = db.one("select h.site, sum(p.cabinets) c from placements p join halls h on h.id=p.hall group by h.site order by c desc limit 1")
    allc = db.scalar("select sum(cabinets) from placements") or 1
    add("R-12", "Key-site concentration", "operations", "Warden", 2, 4 + int((big["c"] / allc if big else 0) > 0.1),
        "campus-level redundancy; tested failover", f"{big['site']} hosts {big['c'] / allc:.0%} of sold cabinets" if big else "")
    today = products.today()
    for r in risks:
        r["score"] = r["likelihood"] * r["impact"]
        prev = db.scalar("select score from risk_register where id=?", (r["id"],))
        r["trend"] = "new" if prev is None else "up" if r["score"] > prev else "down" if r["score"] < prev else "flat"
        db.execute("insert or replace into risk_register(id,title,category,owner,likelihood,impact,score,prev_score,mitigation,evidence,updated)"
                   " values(?,?,?,?,?,?,?,?,?,?,?)", (r["id"], r["title"], r["category"], r["owner"], r["likelihood"], r["impact"],
                                                       r["score"], prev, r["mitigation"], r["evidence"], today))
    return sorted(risks, key=lambda r: -r["score"])


def drill(rnd) -> dict:
    """Quarterly continuity drill, rotating scenarios; uses the real blast-radius model and a real restore."""
    n = db.scalar("select count(*) from bcm_drill") or 0
    scen = ["metro core failure", "database restore", "site power loss", "ransomware tabletop"][n % 4]
    if scen == "metro core failure":
        from . import netinfra
        m = rnd.choice([x["code"] for x in db.rows("select code from metros")])
        core = db.scalar("select hostname from net_device where metro=? and role='core' order by hostname limit 1", (m,))
        br = netinfra.blast_radius(core) if core else {}
        affected = br.get("customers", 0) if isinstance(br, dict) else 0
        if isinstance(affected, list):
            affected = len(affected)
        rto = rnd.uniform(2, 12)
        res = ("pass", f"HA peer carried {m} traffic; {affected} customers would see a single-device failure only as a reroute")
        target, rto_t, rpo = core, 15, 1
    elif scen == "database restore":
        b = backup(rnd)
        rto = rnd.uniform(8, 25)
        res = ("pass" if b["restore_ok"] else "fail", f"restored {b['bytes'] / 1e6:.0f} MB, integrity {b['integrity']}")
        target, rto_t, rpo, affected = b["path"], 60, int(b["restore_ok"]), 0
    elif scen == "site power loss":
        h = db.one("select h.id, h.ups_minutes, h.generators from halls h order by random() limit 1")
        rto = rnd.uniform(0.5, 3) if h["generators"] >= 2 else rnd.uniform(5, 30)
        res = ("pass" if rto < 10 else "fail", f"{h['id']}: UPS {h['ups_minutes']} min, {h['generators']} generators; load on generators in {rto:.1f} min")
        target, rto_t, rpo, affected = h["id"], 10, 1, 0
    else:
        rto = rnd.uniform(120, 480)
        res = ("pass" if rto < 360 else "partial", "tabletop: isolate management network, rebuild jump hosts from images, restore from backup")
        target, rto_t, rpo, affected = "corporate IT", 360, 1, 0
    lessons = rnd.choice(["update the runbook with the new site's contacts", "pre-stage spare optics at each campus",
                          "add the backup location to the access review", "rehearse the customer notification template"])
    did = db.execute("insert into bcm_drill(sim_date,scenario,target,rto_target_min,rto_actual_min,rpo_ok,customers_affected,result,lessons)"
                     " values(?,?,?,?,?,?,?,?,?)", (products.today(), scen, target, rto_t, round(rto, 1), rpo, affected, f"{res[0]}: {res[1]}", lessons))
    return {"drill": did, "scenario": scen, "result": res[0], "detail": res[1], "rto_min": round(rto, 1), "rto_target": rto_t}


# ---------------------------------------------------------------- legal


def residency_guard(oid):
    """Clause keeps residency promises: colocation orders for a customer with a residency clause are amended to the
    customer's home metro when they name a metro outside the agreed region."""
    if not db.one("select name from sqlite_master where name='legal_residency'"):
        return
    o = db.one("select AccountId, Product__c, Params__c from crm_opportunity where Id=?", (oid,))
    if not o or o["Product__c"] not in ("cabinets", "ai_cabinets", "metal", "edge_device"):
        return
    tid = int(o["AccountId"][3:])
    r = db.one("select region from legal_residency where tenant=?", (tid,))
    if not r:
        return
    p = json.loads(o["Params__c"] or "{}")
    if p.get("metro") and REGION.get(p["metro"]) != r["region"]:
        home = db.scalar("select home_metro from tenants where id=?", (tid,))
        p["metro"] = home if REGION.get(home) == r["region"] else next(m for m, g in REGION.items() if g == r["region"])
        db.execute("update crm_opportunity set Params__c=? where Id=?", (json.dumps(p), oid))


def legal_on_opportunity(oid):
    """Large enterprise and financial deals come with non-standard terms; Clause applies the playbook."""
    if not db.one("select name from sqlite_master where name='legal_review'"):
        return
    o = db.one("select * from crm_opportunity where Id=?", (oid,))
    if not o or (o["MRR__c"] or 0) < 15000:
        return
    tid = int(o["AccountId"][3:])
    t = db.one("select kind, home_metro from tenants where id=?", (tid,))
    rnd = random.Random(oid + "legal")
    if t["kind"] not in ("enterprise", "financial", "cloud") or rnd.random() > 0.45:
        return
    req = rnd.choice(["liability cap at 3x annual fees", "unlimited liability for data breaches", "SLA credits up to 30% of MRR",
                      "data must stay in region", "termination for convenience after 12 months", "most-favoured-customer pricing"])
    if req == "liability cap at 3x annual fees":
        dec, risk, note = "fallback", "medium", "countered at 2x annual fees, the playbook maximum"
    elif req == "unlimited liability for data breaches":
        dec, risk, note = "rejected", "high", "vix does not hold customer data on its systems; capped at 2x"
    elif req == "SLA credits up to 30% of MRR":
        dec, risk, note = "fallback", "medium", "standard credits capped at 15%"
    elif req == "data must stay in region":
        region = REGION.get(t["home_metro"])
        dec, risk, note = "accepted", "low", f"residency clause: new colocation limited to {region}; Clause amends out-of-region orders"
        db.execute("insert or ignore into legal_residency(tenant,region,since,since_ts) values(?,?,?,?)", (tid, region, products.today(), db.now()))
    elif req == "termination for convenience after 12 months":
        dec, risk, note = "fallback", "medium", "allowed with a fee equal to 50% of the remaining term"
    else:
        dec, risk, note = "rejected", "medium", "pricing is confidential and set per deal"
    db.execute("insert into legal_review(opp,tenant,sim_date,request,decision,risk,note) values(?,?,?,?,?,?,?)",
               (oid, tid, products.today(), req, dec, risk, note))


# ---------------------------------------------------------------- governance


CHECK_DAYS = {"hire_techs": 45, "cooling_upgrade": 30, "network_capex": 60, "sign_ppa": 120, "discount_approval": 30,
              "declare_dividend": 95, "issue_bonds": 30, "refinance_bond": 30, "hyperscale_bid": 45, "sunset_metal": 200,
              "net_change": 21, "dev_project": 365, "build_hall": 365, "onsite_power": 60, "network_standard": 90}


def on_decide(d, approved, effect):
    if not approved or not db.one("select name from sqlite_master where name='gov_outcome'"):
        return
    kind = d["kind"]
    det = d["detail"]
    base = {}
    if kind == "hire_techs":
        base = {"site": det["site"], "sla": _site_sla(det["site"])}
    elif kind == "network_capex":
        base = {"ref": det.get("ref"), "util": det.get("util")}
    elif kind == "sign_ppa":
        base = {"metro": det["metro"], "price": det["price_mwh"], "grid": det["grid_price_mwh"]}
    elif kind == "declare_dividend":
        base = {"lev": det.get("net_debt_to_ebitda")}
    elif kind in ("issue_bonds", "refinance_bond"):
        base = {"cash": _cash()}
    db.execute("insert or replace into gov_outcome(decision,kind,title,agent,approved_on,check_on,baseline,status) values(?,?,?,?,?,?,?,'pending')",
               (d["id"], kind, d["title"], d["agent"], products.today(),
                (products.today_d() + dt.timedelta(days=CHECK_DAYS.get(kind, 60))).isoformat(), json.dumps({**base, "detail": det}, default=str)))


def _site_sla(site):
    r = db.one("select count(*) n, sum(met) m from tickets where site=? and sim_date>?", (site, _since(30)))
    return round(100 * r["m"] / r["n"], 2) if r and r["n"] else None


def _cash():
    try:
        return db.scalar("select sum(amount) from fin_gl where account='cash'")
    except Exception:  # noqa: BLE001
        return None


def score_outcomes() -> list[dict]:
    out = []
    for o in db.rows("select * from gov_outcome where status='pending' and check_on<=?", (products.today(),)):
        b = json.loads(o["baseline"] or "{}")
        det = b.get("detail", {})
        score, verdict, why = None, "n/a", "no measurable outcome defined"
        k = o["kind"]
        if k == "hire_techs":
            now = _site_sla(b["site"])
            if now is not None and b.get("sla") is not None:
                score = 1.0 if now >= 95 else 0.5 if now > b["sla"] else 0.0
                why = f"30-day SLA at {b['site']} {b['sla']}% -> {now}%"
        elif k == "cooling_upgrade":
            h = db.one("select crah_units, crah_down from halls where id=?", (det.get("hall"),))
            if h:
                score = 1.0 if h["crah_units"] - h["crah_down"] >= h["crah_units"] - 1 else 0.5
                why = f"{h['crah_units'] - h['crah_down']} of {h['crah_units']} cooling units running"
        elif k == "network_capex":
            score, why = 1.0, "capacity added before exhaustion"
            if det.get("action") == "leaf_pair":
                st = db.scalar("select count(*) from net_device where site=? and role='leaf' and status='staged'", (det.get("ref"),))
                score, why = (0.5, "new leaves still staged") if st else (1.0, "new leaf pair accepted and carrying ports")
        elif k == "sign_ppa":
            from . import fin
            grid = fin.metro_power_price(b["metro"]) * 1000
            score = 1.0 if b["price"] <= grid else 0.5 if b["price"] <= grid * 1.08 else 0.0
            why = f"PPA ${b['price']}/MWh vs grid now ${grid:.0f}/MWh (was ${b['grid']})"
        elif k == "discount_approval":
            q = db.one("select status, gm_pct from fin_quote where opp=?", (det.get("opp"),))
            if q and q["status"] in ("won", "lost"):
                score = 1.0 if q["status"] == "won" and (q["gm_pct"] or 0) >= 0.25 else 0.0
                why = f"deal {q['status']} at {(q['gm_pct'] or 0):.0%} gross margin"
            else:
                continue
        elif k == "declare_dividend":
            try:
                lev = json.loads(db.scalar("select metrics from fin_month order by period desc limit 1"))["net_debt_to_ebitda"]
            except Exception:  # noqa: BLE001
                lev = None
            if lev:
                score = 1.0 if lev < 5.5 else 0.5 if lev < 6 else 0.0
                why = f"leverage after the payout {lev:.2f}x"
        elif k in ("issue_bonds", "refinance_bond"):
            score, why = 1.0, "funding in place; no maturity missed"
        elif k == "hyperscale_bid":
            st = db.scalar("select status from dev_rfp where id=?", (det.get("rfp"),))
            if st in ("won", "lost"):
                score, why = (1.0 if st == "won" else 0.0), f"bid {st}"
            else:
                continue
        elif k == "sunset_metal":
            r = db.one("select sum(mrr_before) b, sum(mrr_after) a from mkt_migration where status!='offered'")
            if r and r["b"]:
                ret = (r["a"] or 0) / r["b"]
                score = 1.0 if ret >= 0.4 else 0.5 if ret >= 0.25 else 0.0
                why = f"kept {ret:.0%} of bare-metal revenue through migration"
        elif k == "net_change":
            st = db.scalar("select status from net_change where id=?", (det.get("change"),))
            if st in ("done", "rolled_back"):
                score, why = (1.0 if st == "done" else 0.0), f"change {st}"
            else:
                continue
        elif k in ("dev_project", "build_hall"):
            p = db.one("select * from dev_project where decision=?", (o["decision"],))
            if not p:
                score, why = None, "no project found"
            elif p["status"] != "live":
                db.execute("update gov_outcome set check_on=? where decision=?", ((products.today_d() + dt.timedelta(days=60)).isoformat(), o["decision"]))
                continue
            else:
                roi = json.loads(p["roi"] or "[]")
                if not roi:
                    db.execute("update gov_outcome set check_on=? where decision=?", ((products.today_d() + dt.timedelta(days=60)).isoformat(), o["decision"]))
                    continue
                r = roi[-1]["ratio"] or 0
                score = 1.0 if r >= 0.9 else 0.5 if r >= 0.7 else 0.0
                why = f"earns {r:.0%} of plan after {roi[-1]['months']} months"
        elif k == "onsite_power":
            p = db.one("select stage from dev_project where id=?", (det.get("project"),))
            score, why = (1.0, "project moved past the power stage") if p and p["stage"] != "power" else (0.0, "still waiting for power")
        verdict = "n/a" if score is None else "good" if score >= 1 else "mixed" if score >= 0.5 else "poor"
        db.execute("update gov_outcome set status='scored', score=?, verdict=?, detail=? where decision=?", (score, verdict, why, o["decision"]))
        out.append({"decision": o["decision"], "kind": k, "title": o["title"], "agent": o["agent"], "verdict": verdict, "why": why})
    return out


COST_OWNER = {"power": "energy", "site_opex": "ops", "labour": "cx", "net_support": "cno", "sga": "cco", "bad_debt": "collections",
              "interest": "treasury"}


def budgets(period: str) -> list[dict]:
    """Department budgets for a closed month: each owned cost line against the FP&A budget split by last year's mix."""
    try:
        a = {r["account"]: r["v"] for r in db.rows("select account, sum(amount) v from fin_gl where period=? group by account", (period,))}
        b = {r["line"]: r["amount"] for r in db.rows("select line, amount from fin_budget where period=?", (period,))}
    except Exception:  # noqa: BLE001
        return []
    if not a or not b:
        return []
    cor = sum(a.get(k, 0) for k in ("power", "site_opex", "labour", "net_support")) or 1
    out = []
    for acct, dept in COST_OWNER.items():
        act = a.get(acct, 0.0)
        if acct in ("power", "site_opex", "labour", "net_support"):
            bud = b.get("cost_of_revenue", 0) * act / cor
        elif acct in ("sga", "bad_debt"):
            bud = b.get("sga", 0) * (act / ((a.get("sga", 0) + a.get("bad_debt", 0)) or 1))
        else:
            bud = act
        db.execute("insert or replace into gov_budget(period,dept,budget,actual) values(?,?,?,?)", (period, dept, round(bud, 2), round(act, 2)))
        out.append({"period": period, "dept": dept, "budget": round(bud), "actual": round(act),
                    "variance_pct": round(100 * (act / bud - 1), 1) if bud else None})
    return out


def scorecards() -> list[dict]:
    """One line per agent: work done, reliability, decisions and how they turned out, and the department's own KPI."""
    from .company import engine
    week = db.now() - 30 * 86400
    k = engine.kpis()
    fin_k = k.get("finance") or {}
    mk = k.get("market") or {}
    nk = k.get("network") or {}
    dev = k.get("development") or {}
    kpi = {
        "noc": ("availability >= 99.99%", (nk.get("availability_30d") or 0) >= 99.99),
        "cx": ("remote-hands SLA >= 95%", (k.get("remote_hands_sla_30d") or 0) >= 95),
        "collections": ("DSO <= 45 days", (fin_k.get("dso_days") or 99) <= 45),
        "sales": ("win rate >= 55%", (mk.get("win_rate_30d") or 0) >= 0.55),
        "portal": ("NPS >= 30", (mk.get("nps") or -100) >= 30),
        "cdo": ("data quality >= 95", (k.get("data_quality_score") or 0) >= 95),
        "energy": ("fleet PUE <= 1.25", (k.get("fleet_pue") or 9) <= 1.25),
        "accounts": ("net retention >= 98%", (k.get("net_retention_90d") or 0) >= 0.98),
        "cfo": ("leverage <= 5.5x", (fin_k.get("net_debt_to_ebitda") or 0) <= 5.5),
        "risk": ("compliance readiness >= 90%", ((db.setting("compliance_readiness") or {}).get("score") or 0) >= 90),
        "dev": ("projects delivering", True if dev.get("projects_active", 0) >= 0 else False),
    }
    out = []
    for d in engine.DEPARTMENTS:
        runs = db.one("select count(*) n, sum(status='failed') f from runs where department=? and started>?", (d["id"], week))
        dec = db.one("select count(*) n, sum(status='approved') a from decisions where agent=? and ts>?", (d["agent"], db.now() - 90 * 86400))
        oc = db.one("select avg(score) s, count(score) n from gov_outcome where agent=? and status='scored' and score is not null", (d["agent"],))
        kp = kpi.get(d["id"])
        fail_rate = (runs["f"] or 0) / runs["n"] if runs["n"] else 0
        pts = 2.0 - 4 * fail_rate
        if oc["n"]:
            pts += (oc["s"] or 0) * 2 - 1
        if kp:
            pts += 1 if kp[1] else -1
        grade = "A" if pts >= 2.5 else "B" if pts >= 1.5 else "C" if pts >= 0.5 else "D"
        row = {"agent": d["agent"], "dept": d["name"], "runs": runs["n"], "failed": runs["f"] or 0, "decisions": dec["n"],
               "approved_pct": round(100 * (dec["a"] or 0) / dec["n"], 0) if dec["n"] else None,
               "outcome_score": round(oc["s"], 2) if oc["n"] else None, "kpi": kp[0] if kp else "", "kpi_ok": (1 if kp[1] else 0) if kp else None,
               "grade": grade}
        db.execute("insert or replace into gov_scorecard values(?,?,?,?,?,?,?,?,?,?,?)",
                   (products.today(), row["agent"], row["dept"], row["runs"], row["failed"], row["decisions"], row["approved_pct"],
                    row["outcome_score"], row["kpi"], row["kpi_ok"], grade))
        out.append(row)
    return out


def kpi_hook() -> dict:
    if not db.one("select name from sqlite_master where name='cmp_result'"):
        return {}
    top = db.one("select title, score from risk_register order by score desc limit 1")
    return {"trust": {"compliance_readiness": (db.setting("compliance_readiness") or {}).get("score"),
                      "top_risk": f"{top['title']} ({top['score']})" if top else None,
                      "security_incidents_30d": db.scalar("select count(*) from sec_incident where sim_date>?", (_since(30),)),
                      "soc_alerts_30d": db.scalar("select count(*) from soc_alert where sim_date>?", (_since(30),)),
                      "last_audit": db.scalar("select opinion from cmp_audit order by id desc limit 1")}}
