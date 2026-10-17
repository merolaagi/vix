"""Playbooks for the engineering roles in the role catalog (vix.roles): platform engineering, product
software, data cloud, ITSM, network automation, deployment and migration, inventory audits, AIOps,
certification, SRE, architecture, SDN for AI inference, and implementation projects."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import crm, db, netinfra, products, world
from ..config import DATA_DIR, ROOT
from ..netdrivers import NetworkControl
from .engine import decision_add, playbook
from .playbooks import table, usd

DOCS = DATA_DIR / "docs"

SCHEMA = """
create table if not exists net_cert (vendor text, version text, status text, tested text, results text,
  primary key (vendor, version));
create table if not exists net_telemetry (date text, device text, ifname text, rx_dbm real, errors integer,
  primary key (date, device, ifname));
create table if not exists net_postmortem (incident integer primary key, created text, doc text);
create table if not exists ps_project (id integer primary key, account text, opportunity text, name text, kind text,
  start text, due text, status text, progress real, health text, csat real);
create table if not exists snow_incident (number text primary key, short_description text, priority integer, state text,
  opened_at text, resolved_at text, cmdb_ci text, assignment_group text, caller_id text, u_customers integer,
  u_credits real, close_notes text, problem_id text);
create table if not exists snow_change_request (number text primary key, short_description text, type text, risk text,
  state text, start_date text, cmdb_ci text, assignment_group text, close_code text, justification text);
create table if not exists snow_problem (number text primary key, short_description text, state text, opened_at text,
  cmdb_ci_class text, known_error integer, cause_notes text, related_incidents integer);
create table if not exists snow_cmdb_ci (sys_id text primary key, name text, sys_class_name text, manufacturer text,
  model_id text, firmware_version text, location text, operational_status text, ip_address text);
"""

GOLDEN = ["ntp server 192.0.2.123", "logging host 192.0.2.50", "snmp-server group vix-ro v3 priv",
          "aaa authentication login default group tacacs+ local", "banner login ^authorized use only^"]
FORBIDDEN = ["snmp-server community"]


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("pragma table_info(crm_contact)")}
        if have and "HasOptedOutOfEmail" not in have:
            c.execute("alter table crm_contact add column HasOptedOutOfEmail integer default 0")
            c.execute("alter table crm_contact add column ConsentDate__c text")
    DOCS.mkdir(parents=True, exist_ok=True)


def _rnd(tag):
    return random.Random(f"{tag}-{products.today()}-{time.time()}")


# ================================================================ CTO: platform engineering


HOT_QUERIES = [
    ("tickets by date", "select count(*) from tickets where sim_date=?", ("2026-01-01",), "create index if not exists ix_tickets_date on tickets(sim_date)"),
    ("tickets by tenant", "select count(*) from tickets where tenant=? and met=0", (1,), "create index if not exists ix_tickets_tenant on tickets(tenant, met)"),
    ("impact by tenant", "select count(*) from net_impact where tenant=?", (1,), "create index if not exists ix_impact_tenant on net_impact(tenant)"),
    ("incidents by date", "select count(*) from net_incident where sim_date>?", ("2026-01-01",), "create index if not exists ix_incident_date on net_incident(sim_date)"),
    ("orders by status", "select count(*) from orders where status=? and kind=?", ("pending", "port"), "create index if not exists ix_orders_status on orders(status, kind)"),
    ("ports by tenant", "select count(*) from ports where tenant=? and site=?", (1, "ASH1"), "create index if not exists ix_ports_tenant on ports(tenant, site)"),
    ("runs by playbook", "select count(*) from runs where playbook=?", ("x",), "create index if not exists ix_runs_playbook on runs(playbook, id)"),
]


@playbook("platform_api_review", "API and data-store review", "platform",
          "Anvil times the core service APIs, checks database integrity and size, finds hot queries that scan whole "
          "tables and adds the missing indexes (a safe, reversible fix), and runs the API contract check.")
def platform_api_review(ctx):
    from .. import armory, bi
    timings = []
    for name, fn in [("CRM SOQL query", lambda: crm.query("SELECT Name, MRR__c FROM Account ORDER BY MRR__c DESC LIMIT 20")),
                     ("BI metric mrr", lambda: bi.metric("mrr")), ("BI dashboard executive", lambda: bi.dashboard("executive")),
                     ("revenue breakdown", products.revenue_breakdown), ("network impact model", lambda: netinfra.services_affected(set(), set()))]:
        t0 = time.time()
        fn()
        timings.append({"api": name, "ms": round((time.time() - t0) * 1000, 1)})
    fixed = []
    for label, q, args, ddl in HOT_QUERIES:
        try:
            plan = " ".join(str(r[3]) for r in db.conn().execute("explain query plan " + q, args).fetchall())
        except Exception as e:  # noqa: BLE001
            plan = f"n/a ({e})"
        scan = plan.startswith("SCAN") or " SCAN " in f" {plan} " and "USING" not in plan
        if scan:
            db.execute(ddl)
            fixed.append({"query": label, "before": plan[:60], "fix": ddl.split(" on ")[1]})
    integ = db.scalar("pragma integrity_check")
    size_mb = round(sum(p.stat().st_size for p in DATA_DIR.glob("*.db*")) / 1e6, 1)
    contract = armory.api_contract_check({})
    ok_contract = contract.get("ok") if "checks" in contract else None
    p95 = sorted(t["ms"] for t in timings)[-1]
    db.set_setting("platform_api", {"p95_ms": p95, "integrity": integ, "date": products.today(), "contract_ok": ok_contract})
    ctx.log(f"slowest {p95} ms, {len(fixed)} indexes added, integrity {integ}")
    md = ("## Service API timings (in-process)\n\n" + table(timings, ["api", "ms"]) +
          f"\nDatabase integrity: **{integ}**; data files {size_mb} MB.\n\n## Hot queries fixed\n\n" +
          table(fixed, ["query", "before", "fix"]) +
          "\n## External API contract\n\n" + (f"{contract['passed']}/{contract['total']} endpoint checks pass."
                                               if "checks" in contract else f"Not run: {contract.get('error', '')[:120]}") + "\n")
    return {"slowest_ms": p95, "indexes_added": len(fixed), "integrity": integ, "contract_ok": ok_contract}, md


@playbook("platform_reliability", "Workflow reliability", "platform",
          "Anvil treats every playbook run as a durable workflow: closes runs stuck for hours, retries recent "
          "failures once, and reports success rate and duration per workflow.")
def platform_reliability(ctx):
    from . import engine
    now = time.time()
    stuck = db.rows("select id, playbook from runs where status='running' and started<?", (now - 2 * 3600,))
    for r in stuck:
        db.execute("update runs set status='failed', finished=?, summary=? where id=?",
                   (now, json.dumps({"error": "abandoned: no progress for 2 hours"}), r["id"]))
    retried = []
    for r in db.rows("select id, playbook, params, trigger from runs where status='failed' and started>? and parent is null"
                     " and trigger not like 'retry%'", (now - 86400,)):
        pb = engine.PLAYBOOKS.get(r["playbook"])
        # never auto-retry anything that moves the simulated calendar or a chief's review: a human decides those
        if not pb or r["playbook"] in ("exec_operating_cycle",) or engine.DEPT[pb.department]["tier"] in ("super", "chief"):
            continue
        if not db.one("select id from runs where trigger=?", (f"retry of {r['id']}",)):
            rid = engine.start(r["playbook"], json.loads(r["params"] or "{}"), trigger=f"retry of {r['id']}")
            retried.append({"failed_run": r["id"], "playbook": r["playbook"], "retry_run": rid})
    stats = db.rows("select playbook, count(*) runs, round(100.0*sum(status='done')/count(*),1) success_pct,"
                    " round(avg(finished-started),2) avg_s, round(max(finished-started),2) max_s from runs where started>?"
                    " group by playbook order by success_pct, runs desc", (now - 7 * 86400,))
    worst = [s for s in stats if s["success_pct"] < 99]
    ctx.log(f"{len(stuck)} stuck closed, {len(retried)} retried")
    md = (f"{len(stuck)} stuck runs closed, {len(retried)} failed runs retried.\n\n## Retries\n\n" +
          table(retried, ["failed_run", "playbook", "retry_run"]) +
          "\n## Workflow SLOs (last 7 days)\n\n" + table(stats[:25], ["playbook", "runs", "success_pct", "avg_s", "max_s"]))
    return {"stuck": len(stuck), "retried": len(retried), "workflows": len(stats), "below_99": len(worst)}, md


def _e2e(kinds=None) -> dict:
    """Run vix.e2e in a subprocess against a throwaway copy of the database."""
    args = [sys.executable, "-m", "vix.e2e"] + (["--kinds", ",".join(kinds)] if kinds else [])
    code = str(Path(__file__).resolve().parents[2])
    p = subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=code,
                       env={**os.environ, "PYTHONPATH": code})
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (p.stderr or p.stdout)[-600:], "tests": []}


@playbook("platform_e2e_tests", "End-to-end tests", "platform",
          "Anvil copies the live database to a sandbox and runs the interconnection platform end to end: order every "
          "product, fulfil it, sync the CRM, check billing and the network map. Nothing touches production data.")
def platform_e2e_tests(ctx):
    r = _e2e()
    tests = r.get("tests", [])
    passed = sum(t["ok"] for t in tests)
    ctx.log(f"e2e {passed}/{len(tests)}")
    md = (f"Sandbox run: **{passed}/{len(tests)} tests pass**" + (f". Error: {r.get('error')}" if r.get("error") else "") +
          ".\n\n" + table(tests, ["test", "ok", "detail"]))
    return {"passed": passed, "total": len(tests), "ok": r.get("ok", False)}, md


SECRET_PATTERNS = [(r"sk-[A-Za-z0-9]{20,}", "API key"), (r"ghp_[A-Za-z0-9]{30,}", "GitHub token"),
                   (r"AKIA[0-9A-Z]{16}", "AWS key"), (r"(?i)password\s*=\s*['\"][^'\"]{4,}", "hard-coded password")]


@playbook("platform_devsecops", "DevSecOps scan", "platform",
          "Anvil scans the codebase for secrets, checks .env permissions and MCP exposure settings, inventories "
          "dependencies and runs pip-audit when it is installed.")
def platform_devsecops(ctx):
    findings = []
    for p in ROOT.rglob("*"):
        if any(part in (".venv", "data", ".git", "logs", "__pycache__", "node_modules") for part in p.parts) or not p.is_file():
            continue
        if p.suffix not in (".py", ".sh", ".html", ".md", ".json", ".txt", ".example", ".yml", ".yaml"):
            continue
        text = p.read_text(errors="ignore")
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, text):
                findings.append({"severity": "high", "where": str(p.relative_to(ROOT)), "issue": label})
    env = ROOT / ".env"
    if env.exists():
        mode = oct(env.stat().st_mode & 0o777)
        if env.stat().st_mode & 0o077:
            os.chmod(env, 0o600)
            findings.append({"severity": "medium", "where": ".env", "issue": f"was {mode}, readable by others: fixed to 0600"})
        envtxt = env.read_text()
        if re.search(r"^VIX_MCP_PUBLIC_HOST=\S+", envtxt, re.M) and not re.search(r"^VIX_MCP_TOKEN=\S{16,}", envtxt, re.M):
            findings.append({"severity": "high", "where": ".env", "issue": "public MCP host without a strong token (server refuses to start)"})
    from importlib import metadata
    deps = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                  if d.metadata["Name"] and d.metadata["Name"].lower() in ("fastapi", "uvicorn", "pydantic", "numpy", "httpx", "mcp",
                                                                          "napalm", "tableauhyperapi", "neo4j", "starlette"))
    audit = "pip-audit not installed (pip install pip-audit to scan dependencies for known CVEs)"
    pa = Path(sys.executable).with_name("pip-audit")
    if pa.exists():
        r = subprocess.run([str(pa), "-f", "json", "--progress-spinner", "off"], capture_output=True, text=True, timeout=240)
        try:
            data = json.loads(r.stdout)
            vulns = [(d["name"], v["id"]) for d in data.get("dependencies", []) for v in d.get("vulns", [])]
            audit = f"pip-audit: {len(vulns)} known vulnerabilities" + (f": {vulns[:8]}" if vulns else "")
            findings += [{"severity": "high", "where": n, "issue": v} for n, v in vulns]
        except ValueError:
            audit = "pip-audit could not complete (offline?)"
    ctx.log(f"{len(findings)} findings")
    md = (f"**{len(findings)} findings.** {audit}.\n\n" + table(findings, ["severity", "where", "issue"]) +
          "\n## Dependency inventory\n\n" + "".join(f"- {d}\n" for d in deps))
    return {"findings": len(findings), "high": sum(f["severity"] == "high" for f in findings)}, md


# ================================================================ CTO: product software


@playbook("product_regression", "Product regression", "productsw",
          "Keel runs the product test suite in a sandbox for every product line, including Internet Access, and reports "
          "provisioning lead times from real orders.")
def product_regression(ctx):
    r = _e2e()
    tests = r.get("tests", [])
    lead = db.rows("select kind, count(*) n, round(100.0*sum(status='done')/count(*),1) done_pct,"
                   " sum(status='waitlisted') waitlisted from orders group by kind order by kind")
    ia = db.one("select count(*) n, coalesce(sum(commit_mbps),0) mbps, coalesce(sum(bgp),0) bgp from internet_access")
    passed = sum(t["ok"] for t in tests)
    ctx.log(f"regression {passed}/{len(tests)}")
    md = (f"Regression: **{passed}/{len(tests)}** product tests pass in the sandbox.\n\n" + table(tests, ["test", "ok", "detail"]) +
          f"\n## Internet Access in production\n\n{ia['n']} services, {ia['mbps']:,} Mbps committed, {ia['bgp']} with BGP.\n\n"
          "## Order fulfilment by product\n\n" + table(lead, ["kind", "n", "done_pct", "waitlisted"]))
    return {"passed": passed, "total": len(tests), "internet_services": ia["n"]}, md


# ================================================================ CTO: AI engineering


@playbook("ai_assistant_eval", "Assistant evaluation", "aieng",
          "Echo tests the Ask vix assistant against golden questions whose answers are computed live from the "
          "systems of record, checking that it picks the right tool and states the right number.")
def ai_assistant_eval(ctx):
    from .. import chat
    rows = chat.evaluate()
    ok = sum(r["ok"] for r in rows)
    ctx.log(f"eval {ok}/{len(rows)}")
    md = (f"**{ok}/{len(rows)}** golden questions answered correctly by the built-in router (the always-available fallback). "
          f"Live engine in HQ: {chat.engine_name()}. Expected answers are computed from the systems of record at test time.\n\n" +
          table(rows, ["question", "tool", "expected", "ok"]))
    return {"passed": ok, "total": len(rows), "engine": chat.engine_name()}, md


# ================================================================ CTO: ITSM (ServiceNow-shaped)


def _num(prefix, n):
    return f"{prefix}{int(n):07d}"


@playbook("itsm_sync", "ITSM sync", "itsm",
          "Tally keeps ServiceNow-shaped records: incidents and change requests from the NOC and Tempo, a CMDB of every "
          "device, and problem records when the same fault keeps coming back on one product family.")
def itsm_sync(ctx):
    init()
    with db.conn() as c:
        c.execute("delete from snow_cmdb_ci")
        c.executemany("insert into snow_cmdb_ci values(?,?,?,?,?,?,?,?,?)",
                      [(f"ci-{d['hostname']}", d["hostname"], f"cmdb_ci_ip_{'router' if d['role'] == 'core' else 'switch' if d['role'] in ('spine', 'leaf', 'ix') else d['role']}",
                        d["vendor"], d["model"], d["os_version"], d["site"], "operational" if d["status"] == "up" else "non-operational",
                        d["mgmt_ip"]) for d in db.rows("select * from net_device")])
        have = {r[0] for r in c.execute("select number from snow_incident")}
        new_inc = 0
        for i in db.rows("select * from net_incident"):
            num = _num("INC", i["id"])
            if num in have:
                continue
            pr = 1 if i["severity"] == "sev1" else 2 if i["severity"] == "sev2" else 4
            c.execute("insert into snow_incident values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (num, (i["summary"] or "")[:160], pr, "Closed", f"{i['sim_date']} {int(i['start_min']) // 60:02d}:{int(i['start_min']) % 60:02d}",
                       i["sim_date"], i["target"].split(":")[0], "Network Operations Center", None, i["customers"], i["credits_usd"],
                       f"{i['diagnosis']}; {i['action']}", None))
            new_inc += 1
        haveC = {r[0] for r in c.execute("select number from snow_change_request")}
        for ch in db.rows("select * from net_change"):
            num = _num("CHG", ch["id"])
            state = {"done": "Closed", "rolled_back": "Closed", "approved": "Scheduled", "awaiting_approval": "Assess",
                     "needs_engineer": "Implement"}.get(ch["status"], "New")
            code = {"done": "successful", "rolled_back": "unsuccessful"}.get(ch["status"])
            c.execute("insert or replace into snow_change_request values(?,?,?,?,?,?,?,?,?,?)",
                      (num, f"{ch['kind']} on {ch['device']}: {ch['from_ver'] or ''} -> {ch['to_ver'] or ''}".strip(),
                       "normal" if ch["approval"] == "founder" else "standard", ch["risk"], state, ch["window_date"], ch["device"],
                       "Change Management", code, ch["notes"]))
        new_chg = len({_num("CHG", r["id"]) for r in db.rows("select id from net_change")} - haveC)
    since = (products.today_d() - dt.timedelta(days=30)).isoformat()
    problems = []
    for g in db.rows("select d.vendor, d.model, n.cause, count(*) k, group_concat(n.id) ids from net_incident n"
                     " join net_device d on d.hostname = substr(n.target, 1, instr(n.target || ':', ':') - 1)"
                     " where n.sim_date>? group by d.vendor, d.model, n.cause having k>=3", (since,)):
        fw = db.rows("select d.os_version, count(*) k from net_incident n join net_device d on d.hostname=n.target"
                     " where n.id in (" + g["ids"] + ") group by d.os_version order by k desc")
        num = f"PRB{abs(hash((g['vendor'], g['model'], g['cause']))) % 10**7:07d}"
        note = f"{g['k']} {g['cause']} incidents on {g['vendor']} {g['model']} in 30 days" + \
               (f"; most on firmware {fw[0]['os_version']}" if fw else "")
        db.execute("insert or replace into snow_problem values(?,?,?,?,?,?,?,?)",
                   (num, f"Recurring {g['cause']} on {g['model']}", "Root Cause Analysis", products.today(), g["model"],
                    int(bool(fw)), note, g["k"]))
        for iid in g["ids"].split(","):
            db.execute("update snow_incident set problem_id=? where number=?", (num, _num("INC", iid)))
        problems.append({"problem": num, "note": note})
    ctx.log(f"{new_inc} incidents, {new_chg} changes, {len(problems)} problems")
    md = (f"Synced **{new_inc} new incidents**, {new_chg} new change requests, "
          f"{db.scalar('select count(*) from snow_cmdb_ci')} CMDB items.\n\n## Problems (recurring faults)\n\n" +
          table(problems, ["problem", "note"]) +
          "\nServiceNow-shaped Table API: `GET /api/now/table/incident?sysparm_query=priority=1^state=Closed&sysparm_limit=10`\n")
    return {"incidents_new": new_inc, "changes_new": new_chg, "problems": len(problems)}, md


SNOW_TABLES = {"incident": "snow_incident", "change_request": "snow_change_request", "problem": "snow_problem",
               "cmdb_ci": "snow_cmdb_ci"}


def snow_table(name: str, query: str = "", limit: int = 50) -> dict:
    """ServiceNow Table API subset: sysparm_query with field=value / field!=value / fieldLIKEtext joined by ^."""
    init()
    t = SNOW_TABLES.get(name)
    if not t:
        raise ValueError(f"table must be one of {list(SNOW_TABLES)}")
    cols = {r["name"] for r in db.rows(f"pragma table_info({t})")}
    where, args = [], []
    for part in [p for p in (query or "").split("^") if p]:
        m = re.match(r"^(\w+)(!=|=|LIKE)(.*)$", part)
        if not m or m[1] not in cols:
            raise ValueError(f"bad sysparm_query term: {part}")
        f, op, v = m[1], m[2], m[3]
        if op == "LIKE":
            where.append(f"{f} like ?")
            args.append(f"%{v}%")
        else:
            where.append(f"{f} {op} ?")
            args.append(v)
    sql = f"select * from {t}" + (" where " + " and ".join(where) if where else "") + " order by rowid desc limit ?"
    return {"result": db.rows(sql, tuple(args) + (min(limit, 500),))}


# ================================================================ CDO: data cloud & marketing technology


NBA_RULES = [
    ("cloud_router", "Uses 2+ cloud connections without a cloud router",
     "select c.tenant, min(c.metro) metro from cloud_connections c where not exists (select 1 from cloud_routers r where r.tenant=c.tenant)"
     " group by c.tenant having count(*)>=2", lambda m: {"metro": m, "package": "standard"}),
    ("ix_port", "Single exchange port: redundancy upsell",
     "select tenant, metro from ix_ports group by tenant, metro having count(*)=1", lambda m: {"metro": m, "speed_g": 100}),
    ("internet_access", "Colocated with no Internet Access",
     "select p.tenant, min(s.metro) metro from placements p join halls h on h.id=p.hall join sites s on s.id=h.site"
     " where not exists (select 1 from internet_access i where i.tenant=p.tenant) group by p.tenant",
     lambda m: {"metro": m, "commit_mbps": 1000}),
    ("cloud_connection", "AI cabinets without a cloud on-ramp",
     "select p.tenant, min(s.metro) metro from placements p join halls h on h.id=p.hall join sites s on s.id=h.site"
     " where h.cooling_type='liquid' and not exists (select 1 from cloud_connections c where c.tenant=p.tenant) group by p.tenant",
     lambda m: {"metro": m, "cloud": "STRATUS", "mbps": 1000}),
]


@playbook("crm_data_cloud", "Data cloud & next best actions", "datacloud",
          "Signal unifies customer profiles (duplicate accounts), enforces marketing consent, builds segments, and turns "
          "next-best-action rules into Expansion opportunities for consented accounts only.",
          {"max_actions": {"type": "int", "default": 6}})
def crm_data_cloud(ctx):
    init()
    rnd = _rnd("dc")
    with db.conn() as c:
        for (cid,) in c.execute("select Id from crm_contact where ConsentDate__c is null").fetchall():
            c.execute("update crm_contact set HasOptedOutOfEmail=?, ConsentDate__c=? where Id=?",
                      (int(rnd.random() < 0.08), products.today(), cid))
    names = {}
    for a in db.rows("select Id, Name, Tenant__c from crm_account"):
        key = re.sub(r"[^a-z]", "", re.sub(r"\s+\d+$", "", a["Name"].lower()))
        names.setdefault(key, []).append(a)
    dups = [{"profile": v[0]["Name"], "accounts": len(v), "ids": ", ".join(x["Id"] for x in v)} for v in names.values() if len(v) > 1]
    seg = db.rows("select Industry, case when MRR__c>=200000 then 'strategic' when MRR__c>=50000 then 'growth' else 'core' end tier,"
                  " count(*) n, round(sum(MRR__c)) mrr from crm_account where Type='Customer' group by Industry, tier order by mrr desc")
    opted_out = {r["AccountId"] for r in db.rows("select AccountId from crm_contact group by AccountId having min(HasOptedOutOfEmail)=1")}
    open_exp = {(r["AccountId"], r["Product__c"]) for r in db.rows("select AccountId, Product__c from crm_opportunity where IsClosed=0")}
    actions, blocked = [], 0
    limit = ctx.p("max_actions", 6, int)
    for kind, why, sql, params in NBA_RULES:
        for r in db.rows(sql):
            aid = crm.account_id(r["tenant"])
            if not db.one("select 1 from contracts where tenant=? and status='active'", (r["tenant"],)):
                continue
            if aid in opted_out:
                blocked += 1
                continue
            if (aid, kind) in open_exp or len(actions) >= limit:
                continue
            oid = crm.open_opportunity(r["tenant"], kind, params(r["metro"]), rnd.randint(7, 28), "Expansion")
            actions.append({"account": db.scalar("select Name from crm_account where Id=?", (aid,)), "action": kind, "why": why,
                            "opportunity": oid})
    consent = db.one("select count(*) n, sum(HasOptedOutOfEmail) out from crm_contact")
    ctx.log(f"{len(actions)} next best actions, {blocked} blocked by consent, {len(dups)} duplicate profiles")
    md = (f"Consent: {consent['out']} of {consent['n']} contacts opted out of marketing; **{blocked} actions suppressed** for them.\n\n"
          "## Next best actions created (Expansion opportunities)\n\n" + table(actions, ["account", "action", "why", "opportunity"]) +
          "\n## Segments\n\n" + table(seg, ["Industry", "tier", "n", "mrr"]) +
          "\n## Possible duplicate customer profiles\n\n" + table(dups[:15], ["profile", "accounts", "ids"]))
    return {"actions": len(actions), "suppressed_by_consent": blocked, "duplicates": len(dups)}, md


# ================================================================ CNO: network automation (compliance)


def config_compliance(cfg: str) -> dict:
    missing = [g for g in GOLDEN if g not in cfg]
    bad = [line for line in cfg.splitlines() if any(line.startswith(f) for f in FORBIDDEN)]
    return {"missing": missing, "forbidden": bad, "ok": not missing and not bad}


@playbook("netauto_compliance", "Config compliance", "netauto",
          "Cadence pulls every device's running config through the driver, checks it against the golden standard "
          "(NTP, logging, SNMPv3 only, TACACS+, banner), and queues remediation changes for Tempo's next window.")
def netauto_compliance(ctx):
    init()
    ctl = NetworkControl("Cadence")
    rows, queued, unreachable = [], 0, 0
    today = products.today_d()
    pending = {r["device"] for r in db.rows("select device from net_change where kind='config' and status in ('approved','awaiting_approval')")}
    for d in db.rows("select hostname, mode from net_device where status!='staged'"):
        try:
            cfg = ctl.poll(d["hostname"], ["get_config"])["get_config"].get("running", "")
        except Exception:  # noqa: BLE001
            unreachable += 1
            continue
        r = config_compliance(cfg)
        if r["ok"]:
            continue
        rows.append({"device": d["hostname"], "missing": len(r["missing"]), "forbidden": len(r["forbidden"])})
        if d["hostname"] in pending:
            continue
        snippet = "\n".join(r["missing"] + [f"no {b}" for b in r["forbidden"]])
        from .net_playbooks import _next_window
        w = _next_window(today + dt.timedelta(days=1)).isoformat()
        status = "approved" if d["mode"] == "sim" else "awaiting_approval"
        cid = db.execute("insert into net_change(created,device,kind,from_ver,to_ver,window_date,risk,blast,approval,status,notes,notified)"
                         " values(?,?,?,?,?,?,?,?,?,?,?,0)", (today.isoformat(), d["hostname"], "config", None, None, w, "low", 0,
                                                              "auto" if status == "approved" else "founder", status, snippet))
        if status != "approved":
            decision_add(ctx.agent, "net_change", f"Approve config remediation on {d['hostname']}",
                         {"change": cid, "device": d["hostname"], "window": w, "risk": "low", "reason": "golden-config drift"})
        queued += 1
    total = db.scalar("select count(*) from net_device where status!='staged'") - unreachable
    pct = round(100 * (total - len(rows)) / total, 1) if total else 100.0
    db.set_setting("config_compliance", {"pct": pct, "date": products.today()})
    ctx.log(f"compliance {pct}%, {queued} remediations queued")
    md = (f"Fleet config compliance **{pct}%** ({len(rows)} of {total} devices drift from the golden standard; {unreachable} unreachable). "
          f"Queued **{queued}** remediation changes for the next window.\n\n## Golden standard\n\n" +
          "".join(f"- `{g}`\n" for g in GOLDEN) + "".join(f"- never `{f}` (SNMPv2 community strings)\n" for f in FORBIDDEN) +
          "\n## Non-compliant devices\n\n" + table(rows[:40], ["device", "missing", "forbidden"]))
    return {"compliance_pct": pct, "non_compliant": len(rows), "queued": queued}, md


# ================================================================ CNO: network engineering (deploy, migrate, audit)


@playbook("net_deploy_migrate", "Deploy & migrate", "net",
          "Pathfinder acceptance-tests newly installed gear before it carries customers, migrates customer ports off "
          "crowded switches, and validates freshly provisioned services through the driver.")
def net_deploy_migrate(ctx):
    init()
    ctl = NetworkControl("Pathfinder")
    accepted, rejected = [], []
    std = {r["vendor"]: r["version"] for r in db.rows("select vendor, version from net_cert where status='certified' order by tested")}
    for d in db.rows("select * from net_device where status='staged'"):
        try:
            data = ctl.poll(d["hostname"], ["get_facts", "get_interfaces", "get_environment", "get_config"])
        except Exception as e:  # noqa: BLE001
            rejected.append({"device": d["hostname"], "reason": str(e)[:80]})
            continue
        env = data["get_environment"]
        hw_ok = all(v.get("status", True) for v in env.get("power", {}).values()) and \
            not any(v.get("is_critical") for v in env.get("temperature", {}).values())
        cfg = data["get_config"].get("running", "")
        if not config_compliance(cfg)["ok"]:
            missing = config_compliance(cfg)["missing"]
            db.execute("update net_device set config=? where hostname=?", (cfg + "\n".join(missing) + "\n", d["hostname"]))
        fw = d["os_version"]
        if d["vendor"] in std and std[d["vendor"]] != fw:
            db.execute("update net_device set os_version=? where hostname=?", (std[d["vendor"]], d["hostname"]))
            fw = std[d["vendor"]]
        if hw_ok:
            db.execute("update net_device set status='up' where hostname=?", (d["hostname"],))
            accepted.append({"device": d["hostname"], "firmware": fw, "checks": "hardware, golden config, certified firmware"})
        else:
            rejected.append({"device": d["hostname"], "reason": "hardware alarm at acceptance: returned to vendor"})
    placed = netinfra.assign_ports()
    migrations = []
    for site in [r["site"] for r in db.rows("select distinct site from net_device where role='leaf'")]:
        loads = {r["hostname"]: db.scalar("select count(*) from ports where leaf=?", (r["hostname"],))
                 for r in db.rows("select hostname from net_device where site=? and role='leaf' and status='up'", (site,))}
        if len(loads) < 2:
            continue
        hot = max(loads, key=loads.get)
        cold = min(loads, key=loads.get)
        if loads[hot] >= 0.85 * netinfra.LEAF_CUSTOMER_PORTS and loads[cold] <= 0.5 * netinfra.LEAF_CUSTOMER_PORTS:
            move = (loads[hot] - loads[cold]) // 2
            for p in db.rows("select id, tenant from ports where leaf=? order by id desc limit ?", (hot, move)):
                used = {r["leaf_if"] for r in db.rows("select leaf_if from ports where leaf=?", (cold,))}
                ifn = next(f"Ethernet{i}" for i in range(1, 49) if f"Ethernet{i}" not in used)
                old = db.one("select leaf_if from ports where id=?", (p["id"],))
                db.execute("update net_interface set description='' where device=? and name=?", (hot, old["leaf_if"]))
                db.execute("update ports set leaf=?, leaf_if=? where id=?", (cold, ifn, p["id"]))
                db.execute("update net_interface set description=? where device=? and name=?", (f"cust:{p['tenant']} port:{p['id']}", cold, ifn))
            migrations.append({"site": site, "from": hot, "to": cold, "ports": move})
    since = db.scalar("select max(id) from ports") or 0
    recent = db.rows("select id, leaf, leaf_if from ports where leaf is not null order by id desc limit 25")
    bad = []
    for p in recent:
        try:
            ifs = ctl.poll(p["leaf"], ["get_interfaces"])["get_interfaces"]
            if not ifs.get(p["leaf_if"], {}).get("is_up", False):
                bad.append({"port": p["id"], "switch": p["leaf"], "interface": p["leaf_if"]})
        except Exception:  # noqa: BLE001
            bad.append({"port": p["id"], "switch": p["leaf"], "interface": "switch unreachable"})
    del since
    ctx.log(f"{len(accepted)} accepted, {len(migrations)} migrations, {len(bad)} validation failures")
    md = (f"Acceptance: **{len(accepted)} in service**, {len(rejected)} rejected. Ports placed: {placed['placed']}. "
          f"Migrations: {len(migrations)}. Service validation: {len(recent) - len(bad)}/{len(recent)} newest ports up.\n\n"
          "## Accepted into service\n\n" + table(accepted, ["device", "firmware", "checks"]) +
          "\n## Rejected\n\n" + table(rejected, ["device", "reason"]) +
          "\n## Customer port migrations\n\n" + table(migrations, ["site", "from", "to", "ports"]) +
          "\n## Validation failures (handed to the NOC)\n\n" + table(bad, ["port", "switch", "interface"]))
    return {"accepted": len(accepted), "rejected": len(rejected), "migrations": len(migrations), "validation_failures": len(bad)}, md


@playbook("net_inventory_audit", "Inventory audit & docs", "net",
          "Pathfinder audits inventory against what devices report, checks every customer port's interface label, writes "
          "a network runbook per site, and lists gear due for retirement or upgrade.")
def net_inventory_audit(ctx):
    init()
    ctl = NetworkControl("Pathfinder")
    drift, label_bad = [], []
    for d in db.rows("select * from net_device where status='up'"):
        try:
            f = ctl.poll(d["hostname"], ["get_facts"])["get_facts"]
        except Exception:  # noqa: BLE001
            continue
        for k_inv, k_dev in (("os_version", "os_version"), ("model", "model")):
            if str(f.get(k_dev)) != str(d[k_inv]) and d["mode"] == "sim":
                drift.append({"device": d["hostname"], "field": k_inv, "inventory": d[k_inv], "device_says": f.get(k_dev)})
    for p in db.rows("select p.id, p.tenant, p.leaf, p.leaf_if, i.description from ports p join net_interface i"
                     " on i.device=p.leaf and i.name=p.leaf_if"):
        if p["description"] != f"cust:{p['tenant']} port:{p['id']}":
            label_bad.append({"port": p["id"], "switch": p["leaf"], "label": p["description"]})
            db.execute("update net_interface set description=? where device=? and name=?",
                       (f"cust:{p['tenant']} port:{p['id']}", p["leaf"], p["leaf_if"]))
    cutoff = (products.today_d() - dt.timedelta(days=540)).isoformat()
    retire = db.rows("select d.hostname, d.vendor, d.os_version, f.released from net_device d join net_firmware f on"
                     " f.vendor=d.vendor and f.version=d.os_version where f.released<? order by f.released", (cutoff,))
    docs = []
    (DOCS / "sites").mkdir(parents=True, exist_ok=True)
    for s in db.rows("select id, metro, techs from sites order by id"):
        devs = db.rows("select hostname, role, model, os_version, status, mgmt_ip from net_device where site=? order by role, hostname", (s["id"],))
        cust = db.scalar("select count(distinct tenant) from ports where site=?", (s["id"],))
        text = (f"# Network runbook: {s['id']} ({s['metro']})\n\nGenerated {products.today()} by Pathfinder. Simulated.\n\n"
                f"Customers with ports here: {cust}. Remote-hands technicians on shift: {s['techs']}.\n\n## Devices\n\n" +
                table(devs, ["hostname", "role", "model", "os_version", "status", "mgmt_ip"]) +
                "\n## Escalation\n\n1. NOC (Sentinel) opens the incident and polls the device.\n2. Hardware: RMA from site spares.\n"
                "3. Fiber: carrier ticket; traffic reroutes over the diverse path.\n4. Customer comms: Account Management.\n")
        (DOCS / "sites" / f"{s['id']}.md").write_text(text)
        docs.append(s["id"])
    ctx.log(f"{len(drift)} drift, {len(label_bad)} labels fixed, {len(retire)} retirement candidates, {len(docs)} runbooks")
    md = (f"Audit: {len(drift)} inventory drift items, **{len(label_bad)} interface labels corrected**, "
          f"{len(retire)} devices on firmware older than 18 months. Runbooks written for {len(docs)} sites in `data/docs/sites/`.\n\n"
          "## Drift\n\n" + table(drift[:20], ["device", "field", "inventory", "device_says"]) +
          "\n## Retirement / upgrade candidates\n\n" + table(retire[:25], ["hostname", "vendor", "os_version", "released"]))
    return {"drift": len(drift), "labels_fixed": len(label_bad), "retire_candidates": len(retire), "runbooks": len(docs)}, md


# ================================================================ CNO: AIOps


@playbook("aiops_predict", "AIOps prediction", "noc",
          "Sentinel records optics telemetry, predicts which links will go dark from light-level trends and replaces those "
          "optics before failure, flags error-counter anomalies, and runs a resilience test for single points of failure.")
def aiops_predict(ctx):
    init()
    ctl = NetworkControl("Sentinel")
    rnd = _rnd("aiops")
    day = products.today()
    rows = []
    for d in db.rows("select hostname from net_device where status='up' and mode='sim'"):
        for i in db.rows("select name, rx_dbm, errors from net_interface where device=? and description!='' and (rx_dbm<-5 or errors>0)",
                         (d["hostname"],)):
            rows.append((day, d["hostname"], i["name"], i["rx_dbm"], i["errors"]))
    with db.conn() as c:
        c.executemany("insert or replace into net_telemetry values(?,?,?,?,?)", rows)
    preds = []
    for k in db.rows("select device, ifname, count(*) n from net_telemetry group by device, ifname having n>=3"):
        h = db.rows("select date, rx_dbm from net_telemetry where device=? and ifname=? order by date desc limit 10", (k["device"], k["ifname"]))[::-1]
        xs = [(dt.date.fromisoformat(x["date"]) - dt.date.fromisoformat(h[0]["date"])).days for x in h]
        ys = [x["rx_dbm"] for x in h]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs) or 1
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        if slope < -0.05:
            days_left = (ys[-1] + 14) / -slope
            if days_left <= 5:
                ctl.field_work(f"{k['device']}:{k['ifname']}", "replace_optic", rnd)
                preds.append({"interface": f"{k['device']}:{k['ifname']}", "rx_now": ys[-1], "slope_db_per_day": round(slope, 2),
                              "days_to_dark": round(days_left, 1), "action": "replaced proactively"})
    errs = [r["errors"] for r in db.rows("select errors from net_interface where description!='' and errors>0")]
    mean = sum(errs) / len(errs) if errs else 0
    sd = math.sqrt(sum((e - mean) ** 2 for e in errs) / len(errs)) if errs else 0
    anomalies = db.rows("select device, name, errors, rx_dbm from net_interface where description!='' and errors>? order by errors desc limit 10",
                        (mean + 3 * sd,)) if errs else []
    spof = []
    for d in db.rows("select hostname, role from net_device where role in ('core','spine','ix') and status='up' and mode='sim'"):
        br = netinfra.blast_radius(d["hostname"])
        if br["services"]:
            spof.append({"device": d["hostname"], "role": d["role"], "single_homed_services": br["services"],
                         "customers": br["customers"], "mrr_at_risk": round(br["mrr"])})
    spof.sort(key=lambda x: -x["mrr_at_risk"])
    ctx.log(f"{len(preds)} predicted failures prevented, {len(anomalies)} anomalies, {len(spof)} SPOFs")
    md = (f"Recorded {len(rows)} telemetry points. **{len(preds)} optics replaced before failure** (predicted from light-level trend).\n\n"
          + table(preds, ["interface", "rx_now", "slope_db_per_day", "days_to_dark", "action"]) +
          "\n## Error-counter anomalies (> 3 sigma)\n\n" + table(anomalies, ["device", "name", "errors", "rx_dbm"]) +
          "\n## Resilience test: single device failures that take customers down\n\n" +
          table(spof[:15], ["device", "role", "single_homed_services", "customers", "mrr_at_risk"]) +
          "\nThese are customers with only one exchange port or one switch connection; Signal can offer them redundancy.\n")
    return {"telemetry_points": len(rows), "prevented": len(preds), "anomalies": len(anomalies), "spof_devices": len(spof)}, md


# ================================================================ CNO: certification lab


@playbook("nos_certify", "Firmware certification", "certlab",
          "Proof certifies every released network OS version before Tempo may deploy it: regression, multi-vendor "
          "interoperability, BGP scale on the lab exchange, and a rollback drill. Rejected releases never reach production.")
def nos_certify(ctx):
    init()
    from .. import netsim
    rnd = _rnd("cert")
    done = []
    for f in db.rows("select * from net_firmware where released<=? order by released", (products.today(),)):
        if db.one("select 1 from net_cert where vendor=? and version=?", (f["vendor"], f["version"])):
            continue
        bug = f["bug_factor"]
        regression_fail = sum(1 for _ in range(40) if rnd.random() < max(0.0, (bug - 0.75)) * 0.06)
        others = [v["vendor"] for v in db.rows("select distinct vendor from net_device where vendor!=?", (f["vendor"],))]
        interop_fail = sum(1 for _ in others if rnd.random() < max(0.0, bug - 0.9) * 0.3)
        scale = netsim.study("ASH", "baseline", max_as=64, seed=rnd.randint(1, 999))
        scale_ok = scale["baseline"]["reachability"] >= 0.999 and rnd.random() > max(0.0, bug - 1.1)
        rollback_ok = rnd.random() > 0.02 * bug
        crit = any(c["severity"] in ("critical", "high") for c in json.loads(f["cves"] or "[]"))
        ok = regression_fail <= 1 and interop_fail == 0 and scale_ok and rollback_ok and not crit
        res = {"regression_failed": regression_fail, "regression_total": 40, "interop_failed": interop_fail,
               "interop_total": len(others), "scale_converged_s": scale["baseline"]["converged_s"], "scale_ok": scale_ok,
               "rollback_ok": rollback_ok, "open_critical_cve": crit}
        db.execute("insert into net_cert values(?,?,?,?,?)", (f["vendor"], f["version"], "certified" if ok else "rejected",
                                                              products.today(), json.dumps(res)))
        done.append({"vendor": f["vendor"], "version": f["version"], "verdict": "certified" if ok else "rejected",
                     "regression": f"{40 - regression_fail}/40", "interop": f"{len(others) - interop_fail}/{len(others)}",
                     "scale": "ok" if scale_ok else "fail", "rollback": "ok" if rollback_ok else "fail",
                     "cve": "open critical/high" if crit else "none"})
    allc = db.rows("select vendor, version, status, tested from net_cert order by vendor, version")
    ctx.log(f"certified {sum(d['verdict'] == 'certified' for d in done)}, rejected {sum(d['verdict'] == 'rejected' for d in done)}")
    md = (f"Tested **{len(done)}** new releases.\n\n" + table(done, ["vendor", "version", "verdict", "regression", "interop", "scale",
                                                                     "rollback", "cve"]) +
          "\n## Certification register\n\n" + table(allc, ["vendor", "version", "status", "tested"]) +
          "\nTempo deploys only certified versions.\n")
    return {"tested": len(done), "certified": sum(d["verdict"] == "certified" for d in done),
            "rejected": sum(d["verdict"] == "rejected" for d in done)}, md


# ================================================================ CNO: SRE


SLOS = [("network_availability", 99.99, "%"), ("remote_hands_sla", 95.0, "%"), ("change_success", 97.0, "%"),
        ("data_quality", 95.0, "score"), ("api_p95_ms", 300.0, "ms (max)"), ("config_compliance", 98.0, "%")]


@playbook("sre_slo_review", "SLOs & error budgets", "sre",
          "Beacon tracks service-level objectives and the network error budget, freezes non-security changes when the "
          "budget is spent, and writes postmortems for major incidents.")
def sre_slo_review(ctx):
    init()
    from .net_playbooks import network_kpis
    k = network_kpis(30)
    services = (db.scalar("select count(*) from ports") + db.scalar("select count(*) from virtual_circuits") +
                db.scalar("select count(*) from cloud_connections") + db.scalar("select count(*) from ix_ports"))
    allowed = services * 30 * 1440 * (1 - 0.9999)
    since = (products.today_d() - dt.timedelta(days=30)).isoformat()
    used = db.scalar("select coalesce(sum(i.minutes),0) from net_impact i join net_incident n on n.id=i.incident where n.sim_date>?", (since,))
    burn = round(100 * used / allowed, 1) if allowed else 0
    chd = k["changes_done"] + k["changes_rolled_back"]
    vals = {"network_availability": k["availability_pct"], "remote_hands_sla": products.sla_window(30)["sla_pct"] or 100.0,
            "change_success": round(100 * k["changes_done"] / chd, 2) if chd else 100.0,
            "data_quality": (db.setting("data_quality") or {}).get("score") or 100.0,
            "api_p95_ms": (db.setting("platform_api") or {}).get("p95_ms") or 0,
            "config_compliance": (db.setting("config_compliance") or {}).get("pct") or 100.0}
    rows = []
    for name, target, unit in SLOS:
        v = vals[name]
        ok = v <= target if name == "api_p95_ms" else v >= target
        rows.append({"slo": name, "target": f"{target} {unit}", "actual": v, "met": "yes" if ok else "NO"})
    freeze = burn >= 100
    db.set_setting("change_freeze", freeze)
    pms = []
    (DOCS / "postmortems").mkdir(parents=True, exist_ok=True)
    for i in db.rows("select * from net_incident where severity in ('sev1','sev2') and services_down>0 and id not in"
                     " (select incident from net_postmortem) order by id"):
        imp = db.rows("select service_kind, count(*) n from net_impact where incident=? group by service_kind", (i["id"],))
        doc = (f"# Postmortem: incident {i['id']} ({i['severity']})\n\n_{i['sim_date']}, simulated. Written by Beacon (SRE)._\n\n"
               f"## Summary\n\n{i['summary']}.\n\n## Timeline (minutes after midnight)\n\n- {i['start_min']:.0f}: fault starts\n"
               f"- {i['detected_min']:.0f}: NOC detects ({i['detected_min'] - i['start_min']:.0f} min)\n"
               f"- {i['restored_min']:.0f}: service restored ({i['restored_min'] - i['start_min']:.0f} min total)\n\n"
               f"## Impact\n\n{i['customers']} customers, {i['services_down']} services, SLA credits {usd(i['credits_usd'])}.\n\n" +
               table(imp, ["service_kind", "n"]) +
               f"\n## Root cause\n\n{i['cause']}: {i['diagnosis']}.\n\n## What went well / what to improve\n\n"
               f"- Remediation: {i['action']}.\n- Single-homed customers took the outage; offer redundant connections.\n"
               f"- If repeated on this model, Tally opens a problem record for root-cause analysis.\n")
        p = DOCS / "postmortems" / f"incident-{i['id']}.md"
        p.write_text(doc)
        db.execute("insert into net_postmortem values(?,?,?)", (i["id"], products.today(), str(p.relative_to(DATA_DIR))))
        pms.append({"incident": i["id"], "severity": i["severity"], "customers": i["customers"], "doc": p.name})
    ctx.log(f"budget burn {burn}%, freeze={freeze}, {len(pms)} postmortems")
    md = (f"Network error budget (99.99%, 30 days): **{burn}% used** ({used:,.0f} of {allowed:,.0f} service-minutes). "
          + ("**Change freeze ON**: Tempo pauses non-security changes until the budget recovers.\n\n" if freeze else
             "No freeze.\n\n") + "## SLOs\n\n" + table(rows, ["slo", "target", "actual", "met"]) +
          "\n## Postmortems written\n\n" + table(pms, ["incident", "severity", "customers", "doc"]))
    return {"budget_burn_pct": burn, "freeze": freeze, "slos_missed": sum(r["met"] == "NO" for r in rows), "postmortems": len(pms)}, md


# ================================================================ CNO: architecture


@playbook("arch_review", "Architecture review", "arch",
          "Vantage measures each vendor family from the company's own incident and certification history (failures per "
          "device-month, repair time, release quality, cost) and writes architecture decision records.")
def arch_review(ctx):
    init()
    since = (products.today_d() - dt.timedelta(days=90)).isoformat()
    rows = []
    for v in db.rows("select vendor, model, role, count(*) n from net_device group by vendor, model"):
        inc = db.one("select count(*) k, avg(n.restored_min-n.start_min) mttr, coalesce(sum(n.credits_usd),0) cr from net_incident n"
                     " join net_device d on d.hostname = substr(n.target, 1, instr(n.target || ':', ':') - 1)"
                     " where d.model=? and n.sim_date>?", (v["model"], since))
        cert = db.one("select count(*) t, sum(status='certified') ok from net_cert where vendor=?", (v["vendor"],))
        rate = inc["k"] / (v["n"] * 3)
        rows.append({"vendor": v["vendor"], "model": v["model"], "role": v["role"], "devices": v["n"],
                     "failures_per_100_dev_months": round(100 * rate, 2), "mttr_min": round(inc["mttr"] or 0),
                     "credits_90d": round(inc["cr"]), "release_pass_rate": f"{(cert['ok'] or 0)}/{cert['t'] or 0}",
                     "support_cost_yr": v["n"] * 900 * 12})
    single = db.scalar("select count(*) from (select tenant, metro from ix_ports group by tenant, metro having count(*)=1)")
    total_ix = db.scalar("select count(*) from (select tenant, metro from ix_ports group by tenant, metro)") or 1
    worst = max(rows, key=lambda r: r["failures_per_100_dev_months"]) if rows else None
    adrs = [
        ("ADR-1", "EVPN/VXLAN data-center fabric", "Keep the leaf-spine design; customer ports dual-attach to a leaf pair. "
                                                     "Leaf failures account for most customer-impacting incidents."),
        ("ADR-2", "Exchange redundancy", f"{single} of {total_ix} exchange memberships ({100 * single / total_ix:.0f}%) are single-port; "
                                          "price a redundant second port and have Signal offer it."),
        ("ADR-3", "Release policy", "Only certified releases; security fixes after 7 days of soak, others after 30; HA peers in "
                                    "separate windows."),
    ]
    if worst and worst["failures_per_100_dev_months"] > 0:
        adrs.append(("ADR-4", f"Watch {worst['vendor']} {worst['model']}",
                     f"Highest failure rate ({worst['failures_per_100_dev_months']} per 100 device-months). Ask the "
                     "certification lab for a proof of concept of an alternative before the next refresh."))
    (DOCS / "architecture").mkdir(parents=True, exist_ok=True)
    for a in adrs:
        (DOCS / "architecture" / f"{a[0]}.md").write_text(f"# {a[0]}: {a[1]}\n\n_{products.today()}, Vantage_\n\n{a[2]}\n")
    ctx.log(f"{len(rows)} families scored, {len(adrs)} ADRs")
    md = ("## Vendor families, measured (90 days)\n\n" + table(rows, ["vendor", "model", "role", "devices",
                                                                       "failures_per_100_dev_months", "mttr_min", "credits_90d",
                                                                       "release_pass_rate", "support_cost_yr"]) +
          "\n## Architecture decision records\n\n" + "".join(f"- **{a[0]} {a[1]}.** {a[2]}\n" for a in adrs))
    return {"families": len(rows), "adrs": len(adrs), "single_homed_ix_pct": round(100 * single / total_ix, 1)}, md


# ================================================================ R&D: SDN for AI inference


@playbook("sdn_inference_study", "AI inference routing study", "sdn",
          "Mesh simulates AI inference requests from every metro to the liquid-cooled AI halls through an L7 proxy layer, "
          "comparing nearest-hall, least-loaded and latency-aware routing with mTLS session resumption, and recommends a policy.")
def sdn_inference_study(ctx):
    rnd = _rnd("sdn")
    halls = db.rows("select h.id, s.metro, h.it_load_kw, h.utility_kw from halls h join sites s on s.id=h.site where h.cooling_type='liquid'")
    if not halls:
        return {"error": "no AI halls"}, "No AI halls to route to.\n"
    metros = [m[0] for m in world.METROS]
    cap = {h["id"]: max(50.0, h["it_load_kw"] / 1000 * 400) for h in halls}     # requests/s per hall ~ 400 per MW
    demand = {m: rnd.uniform(40, 140) for m in metros}
    total = sum(demand.values())

    def latency(policy):
        load = {h["id"]: 0.0 for h in halls}
        assign = {}
        for m in sorted(metros, key=lambda x: -demand[x]):
            if policy == "nearest":
                h = min(halls, key=lambda h: world.rtt_ms(m, h["metro"]))
            elif policy == "least_loaded":
                h = min(halls, key=lambda h: load[h["id"]] / cap[h["id"]])
            else:
                h = min(halls, key=lambda h: world.rtt_ms(m, h["metro"]) + 1000 / max(1e-3, cap[h["id"]] - load[h["id"]] - demand[m]) * 20
                        if cap[h["id"]] > load[h["id"]] + demand[m] else 1e9)
            load[h["id"]] += demand[m]
            assign[m] = h
        lat = []
        for m in metros:
            h = assign[m]
            rho = min(0.97, load[h["id"]] / cap[h["id"]])
            queue = 35 * rho / (1 - rho)                     # M/M/1-style queueing at the GPU front end, ms
            net = world.rtt_ms(m, h["metro"])
            tls = net * 0.1                                  # 90% of requests resume mTLS sessions; 10% pay one extra RTT
            lat.append((demand[m], net + queue + tls + 45))  # 45 ms model compute
        lat.sort(key=lambda x: x[1])
        acc, p50, p95 = 0, None, None
        for w, l in lat:
            acc += w
            if p50 is None and acc >= total * 0.5:
                p50 = l
            if p95 is None and acc >= total * 0.95:
                p95 = l
        over = sum(1 for h in halls if load[h["id"]] > cap[h["id"]])
        return {"policy": policy, "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "overloaded_halls": over}
    res = [latency(p) for p in ("nearest", "least_loaded", "latency_aware")]
    best = min(res, key=lambda r: (r["overloaded_halls"], r["p95_ms"]))
    ctx.log(f"best {best['policy']} p95 {best['p95_ms']} ms")
    md = (f"{len(halls)} AI halls, {total:.0f} inference requests/s from {len(metros)} metros (simulated).\n\n" +
          table(res, ["policy", "p50_ms", "p95_ms", "overloaded_halls"]) +
          f"\n**Recommendation:** `{best['policy']}` routing at the L7 proxy tier, with outlier ejection on halls above 90% "
          "utilisation and mTLS session resumption (one extra round trip for 10% of requests).\n")
    return {"best": best["policy"], "results": res}, md


# ================================================================ Chief Operator: implementation projects


@playbook("ps_projects", "Implementation projects", "ps",
          "Bridge turns big wins (AI cabinets, Internet Access with BGP, and anything above $20k/month) into implementation "
          "projects with milestones, advances them daily, flags delays from capacity or network incidents, and records CSAT.")
def ps_projects(ctx):
    init()
    rnd = _rnd("ps")
    today = products.today_d()
    new = 0
    have = {r["opportunity"] for r in db.rows("select opportunity from ps_project")}
    for o in db.rows("select * from crm_opportunity where IsWon=1 and Type!='Renewal' and (MRR__c>=20000 or Product__c in"
                     " ('ai_cabinets','internet_access'))"):
        if o["Id"] in have:
            continue
        days = {"ai_cabinets": 30, "internet_access": 10}.get(o["Product__c"], 21)
        db.execute("insert into ps_project(account,opportunity,name,kind,start,due,status,progress,health) values(?,?,?,?,?,?,?,0,'green')",
                   (o["AccountId"], o["Id"], o["Name"], o["Product__c"], today.isoformat(), (today + dt.timedelta(days=days)).isoformat(),
                    "in_progress"))
        new += 1
    moved, done = 0, []
    for p in db.rows("select * from ps_project where status='in_progress'"):
        tenant = int(p["account"][3:])
        blocked = db.scalar("select count(*) from orders where tenant=? and status='waitlisted'", (tenant,))
        hit = db.scalar("select count(*) from net_impact i join net_incident n on n.id=i.incident where i.tenant=? and n.sim_date=?",
                        (tenant, today.isoformat()))
        span = max(1, (dt.date.fromisoformat(p["due"]) - dt.date.fromisoformat(p["start"])).days)
        step = 0 if blocked else rnd.uniform(0.6, 1.4) / span
        prog = min(1.0, p["progress"] + step)
        late = today.isoformat() > p["due"] and prog < 1
        health = "red" if late or blocked else "amber" if hit else "green"
        if prog >= 1:
            csat = round(max(1, min(5, rnd.gauss(4.5 if not late else 3.6, 0.4))), 1)
            db.execute("update ps_project set progress=1, status='done', health=?, csat=? where id=?", (health, csat, p["id"]))
            done.append({"project": p["name"], "csat": csat, "on_time": not late})
        else:
            db.execute("update ps_project set progress=?, health=? where id=?", (round(prog, 3), health, p["id"]))
        moved += 1
    board = db.rows("select name, kind, due, round(progress*100) pct, health from ps_project where status='in_progress'"
                    " order by health desc, due limit 20")
    csat = db.scalar("select round(avg(csat),2) from ps_project where status='done'")
    ctx.log(f"{new} new, {len(done)} delivered, CSAT {csat}")
    md = (f"{new} new projects, {len(done)} delivered today, {moved} in flight. Average CSAT **{csat or 'n/a'}**.\n\n"
          "## In flight\n\n" + table(board, ["name", "kind", "due", "pct", "health"]) +
          "\n## Delivered today\n\n" + table(done, ["project", "csat", "on_time"]))
    return {"new": new, "delivered": len(done), "in_flight": moved, "csat": csat}, md


# ================================================================ CTO


@playbook("cto_engineering_review", "Engineering review", "cto",
          "CTO: delegate the platform API review, reliability pass, DevSecOps scan, product regression, ITSM sync and "
          "assistant evaluation; report engineering health to the CEO.")
def cto_engineering_review(ctx):
    names = ["platform_api_review", "platform_reliability", "platform_devsecops", "product_regression", "itsm_sync", "ai_assistant_eval"]
    rows, esc = [], []
    for n in names:
        c = ctx.child(n)
        s = c["summary"] or {}
        rows.append({"step": n, "status": c["status"],
                     "result": ", ".join(f"{k}={v}" for k, v in s.items() if not isinstance(v, (list, dict)))[:150],
                     "report": c["report"]})
        if c["status"] != "done":
            esc.append(f"{n} failed")
    sec = db.one("select summary from runs where playbook='platform_devsecops' order by id desc limit 1")
    secs = json.loads(sec["summary"]) if sec and sec["summary"] else {}
    if secs.get("high"):
        esc.append(f"{secs['high']} high-severity security findings")
    md = ("## Delegated\n\n" + table(rows, ["step", "status", "result", "report"]) +
          "\n## Escalated to the CEO\n\n" + ("".join(f"- {e}\n" for e in esc) or "- Nothing.\n"))
    return {"steps": len(rows), "failed": sum(r["status"] != "done" for r in rows), "escalations": esc}, md
