"""Company knowledge graph: who reports to whom, which agents run which playbooks for which roles,
which customers buy which products over which gear, where the money comes from and goes, and which
incidents and decisions touch what.

Two backends:
  * embedded: SQLite node/edge tables, always on; powers HQ's Graph tab and the assistant
  * Neo4j: synced when reachable (NEO4J_URI, default bolt://localhost:7687; NEO4J_USER/NEO4J_PASSWORD,
    empty password = no auth). Every vix node carries the label VixNode plus a Vix-prefixed type label and
    vix=true, and sync only ever deletes vix nodes, so other data in the same database (e.g. a PrimeKG
    graph) is never touched. The Cypher console is read-only."""
from __future__ import annotations

import json
import os
import re
from collections import deque

from . import db

SCHEMA = """
create table if not exists graph_node (id text primary key, label text, name text, props text);
create table if not exists graph_edge (src text, dst text, type text, props text);
create index if not exists graph_edge_src on graph_edge(src);
create index if not exists graph_edge_dst on graph_edge(dst);
"""

# who operates which revenue line, and who is accountable for which cost line
PRODUCT_OWNER = {"colocation_space": "ops", "colocation_power": "ops", "ai_colocation_power": "ops", "ports": "ix",
                 "cross_connects": "ix", "fabric_virtual_circuits": "ix", "cloud_connections": "ix", "internet_exchange": "ix",
                 "internet_access": "productsw", "cloud_routers": "digital", "network_edge": "digital", "metal": "metal",
                 "remote_hands": "cx"}
COST_OWNER = {"power": "energy", "site_opex": "ops", "technicians": "cx", "network_support": "cno", "sla_credits": "noc"}


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)


def ready():
    """Tables exist and the graph has been built at least once (first use builds it)."""
    init()
    if not db.scalar("select count(*) from graph_node"):
        store(*build())


def build() -> tuple[list[dict], list[dict]]:
    from . import products, roles
    from .company import engine
    nodes, edges = {}, []

    def node(i, label, name, **props):
        nodes[i] = {"id": i, "label": label, "name": name, "props": {k: v for k, v in props.items() if v is not None}}

    def edge(a, b, t, **props):
        if a in nodes and b in nodes:
            edges.append({"src": a, "dst": b, "type": t, "props": props})
    node("founder", "Founder", "Founder")
    for d in engine.DEPARTMENTS:
        node(f"dept:{d['id']}", "Department", d["name"], tier=d["tier"])
        node(f"agent:{d['agent']}", "Agent", d["agent"], tier=d["tier"])
    for d in engine.DEPARTMENTS:
        edge(f"agent:{d['agent']}", f"dept:{d['id']}", "WORKS_IN")
        edge(f"dept:{d['id']}", "founder" if d["reports_to"] == "founder" else f"dept:{d['reports_to']}", "REPORTS_TO")
    for p in engine.PLAYBOOKS.values():
        last = db.one("select status, started from runs where playbook=? order by id desc limit 1", (p.name,))
        node(f"pb:{p.name}", "Playbook", p.title, playbook=p.name, last_status=last["status"] if last else None)
        edge(f"dept:{p.department}", f"pb:{p.name}", "OWNS")
    for r in roles.ROLES:
        node(f"role:{r['ref']}", "Role", r["title"], requisition=r["req"], kind=r["kind"])
        d = engine.DEPT.get(r["dept"])
        if d:
            edge(f"role:{r['ref']}", f"agent:{d['agent']}", "STAFFED_BY")
        for pb in r["playbooks"]:
            edge(f"role:{r['ref']}", f"pb:{pb}", "DONE_BY")
    rev = products.revenue_breakdown()
    for line, usd in rev.items():
        if line == "total":
            continue
        node(f"prod:{line}", "Product", line.replace("_", " "), mrr=round(usd))
        node(f"pnl:rev:{line}", "PnLLine", f"Revenue: {line.replace('_', ' ')}", kind="revenue", monthly_usd=round(usd))
        edge(f"prod:{line}", f"pnl:rev:{line}", "EARNS", monthly_usd=round(usd))
        if PRODUCT_OWNER.get(line):
            edge(f"dept:{PRODUCT_OWNER[line]}", f"prod:{line}", "OPERATES")
    fin = db.setting("fin_last") or {}
    costs = {"power": fin.get("power_cost"), "site_opex": fin.get("opex"), "technicians": fin.get("tech_labour"),
             "network_support": fin.get("net_support"), "sla_credits": fin.get("sla_credits")}
    node("pnl:ebitda", "PnLLine", "EBITDA", kind="result", monthly_usd=fin.get("ebitda"), margin=fin.get("ebitda_margin"))
    for line in rev:
        if line != "total":
            edge(f"pnl:rev:{line}", "pnl:ebitda", "ADDS_TO")
    for k, v in costs.items():
        node(f"pnl:cost:{k}", "PnLLine", f"Cost: {k.replace('_', ' ')}", kind="cost", monthly_usd=round(v or 0))
        edge(f"pnl:cost:{k}", "pnl:ebitda", "SUBTRACTS_FROM")
        if COST_OWNER.get(k):
            edge(f"dept:{COST_OWNER[k]}", f"pnl:cost:{k}", "ACCOUNTABLE_FOR")
    for m in db.rows("select code, name from metros"):
        node(f"metro:{m['code']}", "Metro", m["name"])
    for s in db.rows("select id, metro from sites"):
        node(f"site:{s['id']}", "Site", s["id"])
        edge(f"site:{s['id']}", f"metro:{s['metro']}", "IN")
    for h in db.rows("select h.id, h.site, h.cooling_type, h.cabinets_sold, h.cabinets_total from halls h"):
        node(f"hall:{h['id']}", "Hall", h["id"], cooling=h["cooling_type"], cabinets=f"{h['cabinets_sold']}/{h['cabinets_total']}")
        edge(f"hall:{h['id']}", f"site:{h['site']}", "IN")
    if db.one("select name from sqlite_master where name='net_device'"):
        for v in db.rows("select id, name from net_vendor"):
            node(f"vendor:{v['id']}", "Vendor", v["name"])
        for d in db.rows("select hostname, site, role, vendor, model, os_version, status, mode from net_device"):
            node(f"dev:{d['hostname']}", "Device", d["hostname"], role=d["role"], model=d["model"], firmware=d["os_version"],
                 status=d["status"], mode=d["mode"])
            edge(f"dev:{d['hostname']}", f"site:{d['site']}", "IN")
            edge(f"dev:{d['hostname']}", f"vendor:{d['vendor']}", "MADE_BY")
        for l in db.rows("select * from net_link"):
            node(f"link:{l['id']}", "BackboneLink", l["id"], km=l["km"], capacity_g=l["capacity_g"], status=l["status"])
            edge(f"link:{l['id']}", f"metro:{l['a_metro']}", "CONNECTS")
            edge(f"link:{l['id']}", f"metro:{l['z_metro']}", "CONNECTS")
    lines = products.lines_by_tenant()
    for t in db.rows("select t.id, t.name, t.kind, coalesce(c.status,'active') st from tenants t left join contracts c on c.tenant=t.id"):
        mine = lines.get(t["id"], {})
        node(f"cust:{t['id']}", "Customer", t["name"], segment=t["kind"], status=t["st"], mrr=round(sum(mine.values())))
        for line, usd in mine.items():
            edge(f"cust:{t['id']}", f"prod:{line}", "BUYS", monthly_usd=round(usd))
    for r in db.rows("select distinct p.tenant, h.site from placements p join halls h on h.id=p.hall"):
        edge(f"cust:{r['tenant']}", f"site:{r['site']}", "COLOCATED_AT")
    if db.one("select name from sqlite_master where name='net_device'"):
        for r in db.rows("select tenant, leaf, count(*) n from ports where leaf is not null group by tenant, leaf"):
            edge(f"cust:{r['tenant']}", f"dev:{r['leaf']}", "CONNECTED_VIA", ports=r["n"])
        for r in db.rows("select tenant, ix_switch, count(*) n from ix_ports where ix_switch is not null group by tenant, ix_switch"):
            edge(f"cust:{r['tenant']}", f"dev:{r['ix_switch']}", "PEERS_VIA", ports=r["n"])
        since = db.scalar("select date(max(sim_date), '-90 day') from net_incident")
        for i in db.rows("select * from net_incident where sim_date>=?", (since or "0",)):
            node(f"inc:{i['id']}", "Incident", f"Incident {i['id']}: {i['cause']} on {i['target']}", date=i["sim_date"],
                 severity=i["severity"], minutes=round(i["restored_min"] - i["start_min"]), credits_usd=i["credits_usd"])
            tgt = i["target"].split(":")[0]
            edge(f"inc:{i['id']}", f"dev:{tgt}" if f"dev:{tgt}" in nodes else f"link:{tgt}", "ON")
            edge(f"inc:{i['id']}", "agent:Sentinel", "HANDLED_BY")
            if i["credits_usd"]:
                edge(f"inc:{i['id']}", "pnl:cost:sla_credits", "COST", usd=i["credits_usd"])
            for r in db.rows("select tenant, sum(minutes) m, sum(credit_usd) c from net_impact where incident=? group by tenant", (i["id"],)):
                edge(f"inc:{i['id']}", f"cust:{r['tenant']}", "IMPACTED", minutes=round(r["m"]), credit_usd=r["c"])
    for d in db.rows("select * from decisions order by id desc limit 200"):
        det = json.loads(d["detail"] or "{}")
        node(f"dec:{d['id']}", "Decision", d["title"], status=d["status"], kind=d["kind"])
        edge(f"dec:{d['id']}", f"agent:{d['agent']}", "FILED_BY")
        for key, prefix in (("site", "site:"), ("hall", "hall:"), ("device", "dev:"), ("hostname", "dev:"), ("ref", "")):
            v = det.get(key)
            if v:
                for cand in ([f"{prefix}{v}"] if prefix else [f"site:{v}", f"link:{v}", f"metro:{v}"]):
                    if cand in nodes:
                        edge(f"dec:{d['id']}", cand, "ABOUT")
    if db.one("select name from sqlite_master where name='crm_opportunity'"):
        for o in db.rows("select Id, AccountId, Name, Type, StageName, MRR__c from crm_opportunity where IsClosed=0"):
            node(f"opp:{o['Id']}", "Opportunity", o["Name"], stage=o["StageName"], type=o["Type"], mrr=o["MRR__c"])
            edge(f"opp:{o['Id']}", f"cust:{int(o['AccountId'][3:])}", "FOR")
            owner = {"Renewal": "Keeper", "Expansion": "Broker", "New Business": "Broker"}.get(o["Type"], "Broker")
            edge(f"opp:{o['Id']}", f"agent:{owner}", "OWNED_BY")
    _build_v9(nodes, edges, node, edge)
    return list(nodes.values()), edges


def _has(t):
    return bool(db.one("select name from sqlite_master where name=?", (t,)))


def _build_v9(nodes, edges, node, edge):
    """v0.6-0.9: the ledger below EBITDA, projects, PPAs, competitors, partners, risks and controls."""
    if _has("fin_month") and db.scalar("select count(*) from fin_month"):
        m = db.one("select period, income from fin_month order by period desc limit 1")
        inc = json.loads(m["income"])
        node("pnl:net_income", "PnLLine", "Net income", kind="result", monthly_usd=round(inc["net_income"]), period=m["period"])
        edge("pnl:ebitda", "pnl:net_income", "FLOWS_TO")
        for k, owner in (("sga", "cco"), ("bad_debt", "collections"), ("depreciation", "finance"), ("interest", "treasury"), ("tax", "finance")):
            node(f"pnl:cost:{k}", "PnLLine", f"Cost: {k.replace('_', ' ')}", kind="cost", monthly_usd=round(inc.get(k) or 0), period=m["period"])
            edge(f"pnl:cost:{k}", "pnl:ebitda" if k in ("sga", "bad_debt") else "pnl:net_income", "SUBTRACTS_FROM")
            edge(f"dept:{owner}", f"pnl:cost:{k}", "ACCOUNTABLE_FOR")
    if _has("dev_project"):
        for p in db.rows("select * from dev_project"):
            node(f"proj:{p['id']}", "Project", p["name"], kind=p["kind"], stage=p["stage"], status=p["status"], budget_usd=round(p["budget_usd"]),
                 spent_usd=round(p["spent_usd"] or 0), irr=p["irr"])
            edge(f"proj:{p['id']}", f"metro:{p['metro']}", "IN")
            edge(f"proj:{p['id']}", "dept:construction", "BUILT_BY")
            if p["decision"]:
                edge(f"dec:{p['decision']}", f"proj:{p['id']}", "STARTED")
            if p["hall"]:
                edge(f"proj:{p['id']}", f"hall:{p['hall']}", "DELIVERED")
        for a in db.rows("select * from dev_ppa"):
            node(f"ppa:{a['id']}", "PPA", f"{a['mw']} MW {a['kind']} PPA {a['metro']}", price_mwh=a["price_mwh"], start=a["start"])
            edge(f"ppa:{a['id']}", f"metro:{a['metro']}", "IN")
            edge(f"ppa:{a['id']}", "pnl:cost:power", "HEDGES")
            edge("dept:power", f"ppa:{a['id']}", "SIGNED")
    if _has("mkt_competitor"):
        for c in db.rows("select * from mkt_competitor"):
            node(f"comp:{c['id']}", "Competitor", c["name"], style=c["style"], price_index=c["price_index"], reputation=c["reputation"])
            for metro, pr in json.loads(c["presence"]).items():
                edge(f"comp:{c['id']}", f"metro:{metro}", "COMPETES_IN", capacity=pr["cap"], price=pr["price"])
            edge("dept:intel", f"comp:{c['id']}", "TRACKS")
        since = db.scalar("select date(max(sim_date), '-90 day') from mkt_winloss") or "0"
        cid = {c["name"]: c["id"] for c in db.rows("select id, name from mkt_competitor")}
        for r in db.rows("select competitor, tenant, count(*) n from mkt_winloss where outcome='lost' and competitor is not null and sim_date>?"
                         " group by competitor, tenant", (since,)):
            if r["competitor"] in cid:
                edge(f"comp:{cid[r['competitor']]}", f"cust:{r['tenant']}", "WON_DEALS_FROM", deals=r["n"])
        for pa in db.rows("select * from mkt_partner"):
            node(f"partner:{pa['id']}", "Partner", pa["name"], kind=pa["kind"], tier=pa["tier"], mrr_sourced=round(pa["mrr_sourced"] or 0))
            edge("dept:ecosystem", f"partner:{pa['id']}", "MANAGES")
        for r in db.rows("select distinct c.partner, o.AccountId from mkt_opp_channel c join crm_opportunity o on o.Id=c.opp"
                         " where c.partner is not null and o.IsWon=1"):
            edge(f"partner:{r['partner']}", f"cust:{int(r['AccountId'][3:])}", "SOURCED")
    if _has("risk_register"):
        agents = {d["agent"] for d in __import__("vix.company.engine", fromlist=["x"]).DEPARTMENTS}
        for r in db.rows("select * from risk_register"):
            node(f"risk:{r['id']}", "Risk", r["title"], score=r["score"], likelihood=r["likelihood"], impact=r["impact"], evidence=r["evidence"])
            if r["owner"] in agents:
                edge(f"risk:{r['id']}", f"agent:{r['owner']}", "OWNED_BY")
            edge("dept:risk", f"risk:{r['id']}", "TRACKS")
        latest = db.scalar("select max(sim_date) from cmp_result")
        for c in db.rows("select c.*, r.status from cmp_control c left join cmp_result r on r.control=c.id and r.sim_date=?", (latest,)):
            node(f"ctl:{c['id']}", "Control", f"{c['id']} {c['title']}", status=c["status"], frameworks=c["frameworks"])
            edge(f"ctl:{c['id']}", f"dept:{c['owner']}", "OWNED_BY")
            edge("dept:compliance", f"ctl:{c['id']}", "TESTS")


def store(nodes, edges):
    init()
    with db.conn() as c:
        c.execute("delete from graph_node")
        c.execute("delete from graph_edge")
        c.executemany("insert into graph_node values(?,?,?,?)", [(n["id"], n["label"], n["name"], json.dumps(n["props"])) for n in nodes])
        c.executemany("insert into graph_edge values(?,?,?,?)", [(e["src"], e["dst"], e["type"], json.dumps(e["props"])) for e in edges])


# ---------------------------------------------------------------- embedded queries


def _node(i):
    r = db.one("select * from graph_node where id=?", (i,))
    if r:
        r["props"] = json.loads(r["props"] or "{}")
    return r


FILLER = re.compile(r"\b(the|our|a|an|department|dept|team|agent|customer|product|line|device|site)\b", re.I)
RANK = {"Department": 0, "Agent": 1, "PnLLine": 2, "Customer": 3, "Product": 4, "Site": 5, "Metro": 6, "Competitor": 7, "Project": 8,
        "Risk": 9, "Control": 10, "Playbook": 11}


def search(text: str, limit: int = 15) -> list[dict]:
    """Exact name first, then substring, then the words with filler removed, then all words in any order."""
    ready()
    text = (text or "").strip()
    out, seen = [], set()

    def add(rows):
        for r in sorted(rows, key=lambda r: (RANK.get(r["label"], 9), len(r["name"]))):
            if r["id"] not in seen and len(out) < limit:
                seen.add(r["id"])
                out.append(r)
    add(db.rows("select id, label, name from graph_node where lower(name)=lower(?) or id=?", (text, text)))
    add(db.rows("select id, label, name from graph_node where name like ? or id like ? limit 200", (f"%{text}%", f"%{text}%")))
    core = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip()
    if core and core != text:
        add(db.rows("select id, label, name from graph_node where lower(name)=lower(?)", (core,)))
        add(db.rows("select id, label, name from graph_node where name like ? limit 200", (f"%{core}%",)))
    words = [w for w in re.findall(r"[A-Za-z0-9]+", core) if len(w) > 1]
    if len(words) > 1:
        add(db.rows("select id, label, name from graph_node where " + " and ".join("name like ?" for _ in words) + " limit 200",
                    tuple(f"%{w}%" for w in words)))
    return out


def neighborhood(center: str, depth: int = 1, limit: int = 120, types: list[str] | None = None) -> dict:
    ready()
    if not _node(center):
        hits = search(center, 1)
        if not hits:
            return {"nodes": [], "edges": [], "center": None}
        center = hits[0]["id"]
    seen, frontier, edges = {center}, [center], []
    for _ in range(max(1, min(depth, 3))):
        nxt = []
        for n in frontier:
            for e in db.rows("select * from graph_edge where src=? or dst=?", (n, n)):
                if types and e["type"] not in types:
                    continue
                edges.append(e)
                o = e["dst"] if e["src"] == n else e["src"]
                if o not in seen and len(seen) < limit:
                    seen.add(o)
                    nxt.append(o)
        frontier = nxt
    nodes = [n for n in (_node(i) for i in seen) if n]
    ids = {n["id"] for n in nodes}
    uniq = {(e["src"], e["dst"], e["type"]): {**e, "props": json.loads(e["props"] or "{}")} for e in edges
            if e["src"] in ids and e["dst"] in ids}
    return {"center": center, "nodes": nodes, "edges": list(uniq.values())}


def path(a: str, b: str, max_hops: int = 8) -> dict:
    ready()
    na, nb = _node(a) or (search(a, 1) or [None])[0], _node(b) or (search(b, 1) or [None])[0]
    if not na or not nb:
        return {"path": None, "note": "endpoint not found"}
    a, b = na["id"], nb["id"]
    prev, q = {a: None}, deque([(a, 0)])
    while q:
        u, d = q.popleft()
        if u == b or d >= max_hops:
            if u == b:
                break
            continue
        for e in db.rows("select src, dst, type from graph_edge where src=? or dst=?", (u, u)):
            v = e["dst"] if e["src"] == u else e["src"]
            if v not in prev:
                prev[v] = (u, e["type"], e["src"] == u)
                q.append((v, d + 1))
    if b not in prev:
        return {"path": None, "note": f"no path within {max_hops} hops"}
    steps, cur = [], b
    while prev[cur]:
        u, t, fwd = prev[cur]
        steps.append({"from": _node(u)["name"], "rel": t if fwd else f"<-{t}", "to": _node(cur)["name"]})
        cur = u
    return {"from": na["name"], "to": nb["name"], "hops": len(steps), "path": steps[::-1]}


def money_map() -> dict:
    """Departments joined to the money: revenue they operate, cost lines they own, credits their incidents caused."""
    ready()
    out = []
    for d in db.rows("select id, name from graph_node where label='Department'"):
        rev = db.scalar("select coalesce(sum(json_extract(n.props,'$.mrr')),0) from graph_edge e join graph_node n on n.id=e.dst"
                        " where e.src=? and e.type='OPERATES'", (d["id"],)) or 0
        cost = db.scalar("select coalesce(sum(json_extract(n.props,'$.monthly_usd')),0) from graph_edge e join graph_node n on n.id=e.dst"
                         " where e.src=? and e.type='ACCOUNTABLE_FOR'", (d["id"],)) or 0
        if rev or cost:
            out.append({"department": d["name"], "revenue_operated": round(rev), "costs_owned": round(cost)})
    out.sort(key=lambda r: -(r["revenue_operated"] - r["costs_owned"]))
    return {"departments": out, "ebitda": (db.setting("fin_last") or {}).get("ebitda")}


def stats() -> dict:
    init()
    return {"nodes": db.scalar("select count(*) from graph_node"), "edges": db.scalar("select count(*) from graph_edge"),
            "by_label": db.rows("select label, count(*) n from graph_node group by label order by n desc"),
            "by_type": db.rows("select type, count(*) n from graph_edge group by type order by n desc"),
            "neo4j": db.setting("neo4j_status")}


# ---------------------------------------------------------------- Neo4j


def _label(l):
    return "Vix" + re.sub(r"[^A-Za-z0-9]", "", l)


def _flat(p):
    return {k: (v if isinstance(v, (int, float, str, bool)) or v is None else json.dumps(v)) for k, v in p.items()}


def sync_cypher(run, nodes, edges, batch=500):
    """Write the graph through any run(query, params) callable that speaks Cypher. Only vix nodes are touched."""
    try:
        run("CREATE INDEX vix_node_id IF NOT EXISTS FOR (n:VixNode) ON (n.id)", {})
    except Exception:  # noqa: BLE001  (other Cypher engines spell index creation differently)
        pass
    run("MATCH (n:VixNode) WHERE n.vix = true DETACH DELETE n", {})
    by_label = {}
    for n in nodes:
        by_label.setdefault(n["label"], []).append({"id": n["id"], "props": {**_flat(n["props"]), "name": n["name"]}})
    for label, rows in by_label.items():
        q = f"UNWIND $rows AS r MERGE (n:VixNode:{_label(label)} {{id: r.id}}) SET n += r.props, n.vix = true"
        for i in range(0, len(rows), batch):
            run(q, {"rows": rows[i:i + batch]})
    by_type = {}
    for e in edges:
        by_type.setdefault(e["type"], []).append({"a": e["src"], "b": e["dst"], "props": _flat(e["props"])})
    for t, rows in by_type.items():
        q = f"UNWIND $rows AS r MATCH (a:VixNode {{id: r.a}}), (b:VixNode {{id: r.b}}) MERGE (a)-[e:{re.sub(r'[^A-Z_]', '', t)}]->(b) SET e += r.props, e.vix = true"
        for i in range(0, len(rows), batch):
            run(q, {"rows": rows[i:i + batch]})
    return {"nodes": len(nodes), "edges": len(edges)}


def _neo4j_driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return None, "neo4j driver not installed (pip install neo4j)"
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user, pw = os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw) if pw else None, connection_timeout=4)
        drv.verify_connectivity()
        return drv, uri
    except Exception as e:  # noqa: BLE001
        return None, f"Neo4j not reachable at {uri}: {type(e).__name__}"


def sync_neo4j(nodes, edges) -> dict:
    if os.environ.get("VIX_NEO4J", "auto").lower() in ("off", "0", "false"):
        st = {"synced": False, "reason": "disabled by VIX_NEO4J=off"}
        db.set_setting("neo4j_status", st)
        return st
    drv, info = _neo4j_driver()
    if not drv:
        st = {"synced": False, "reason": info}
        db.set_setting("neo4j_status", st)
        return st
    db_name = os.environ.get("NEO4J_DATABASE") or None
    with drv.session(database=db_name) as s:
        r = sync_cypher(lambda q, p: s.run(q, p).consume(), nodes, edges)
    drv.close()
    st = {"synced": True, "uri": info, **r}
    db.set_setting("neo4j_status", st)
    return st


WRITE_WORDS = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b|CALL\s+(dbms|db\.|apoc)", re.I)


def cypher(query: str, params: dict | None = None, limit: int = 200) -> dict:
    """Read-only Cypher against Neo4j: write clauses are refused and the session is opened in READ mode."""
    if WRITE_WORDS.search(query):
        raise ValueError("read-only console: CREATE/MERGE/DELETE/SET/REMOVE/DROP/LOAD CSV/procedure calls are not allowed")
    drv, info = _neo4j_driver()
    if not drv:
        raise ValueError(info)
    from neo4j import READ_ACCESS
    with drv.session(default_access_mode=READ_ACCESS, database=os.environ.get("NEO4J_DATABASE") or None) as s:
        res = s.run(query, params or {})
        keys = res.keys()
        rows = []
        for i, rec in enumerate(res):
            if i >= limit:
                break
            rows.append({k: _plain(rec[k]) for k in keys})
    drv.close()
    return {"columns": list(keys), "rows": rows}


def _plain(v):
    if hasattr(v, "items") and hasattr(v, "labels"):
        return {"labels": list(v.labels), **dict(v.items())}
    if hasattr(v, "type") and hasattr(v, "items") and hasattr(v, "start_node"):
        return {"type": v.type, **dict(v.items())}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v
