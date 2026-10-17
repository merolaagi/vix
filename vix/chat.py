"""Ask vix: a chatbot over the whole company, grounded in the systems of record.

Two engines, same tools:
  * local LLM via Ollama (VIX_OLLAMA_URL, default http://localhost:11434; VIX_CHAT_MODEL or the first available of
    a preference list) with tool calling, so answers come from tool results, not memory. No paid API.
  * built-in router when no model is available: intent rules pick the tool and format the answer.
Every answer lists the tools it used so it can be checked."""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter

import httpx

from . import bi, crm, db, graph, products
from .config import REPORT_DIR

OLLAMA = os.environ.get("VIX_OLLAMA_URL", os.environ.get("OLLAMA_URL", "http://localhost:11434"))
PREFERRED = ["qwen3.6:27b", "qwen3:32b", "qwen3:14b", "qwen3-coder:latest", "llama3.1:8b", "qwen2.5:7b"]


def money(v):
    return "n/a" if v is None else f"${v:,.0f}"


# ---------------------------------------------------------------- tools


def t_kpis():
    from .company import engine
    k = engine.kpis()
    fin = db.setting("fin_last") or {}
    return {**{x: k[x] for x in ("sim_date", "mrr_usd", "ebitda_margin", "cabinet_util", "fleet_pue", "tenants", "active_customers",
                                 "open_decisions", "remote_hands_sla_30d", "net_retention_90d", "data_quality_score", "network")},
            "last_close": {k2: fin.get(k2) for k2 in ("sim_date", "mrr", "ebitda", "power_cost", "opex", "tech_labour", "sla_credits", "net_support")}}


def _stem(w):
    for suf in ("ations", "ation", "ing", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


STOP = {"who", "which", "the", "does", "what", "handles", "handle", "agent", "role", "job", "responsible", "for", "and", "our",
        "charge", "owns", "own", "does", "do", "with", "about", "before", "after", "department"}


def t_roles(query: str = ""):
    """Rank departments and roles by the rarer words they share with the question (idf-weighted)."""
    from . import roles
    from .company import engine
    words = {_stem(w) for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 2 and w not in STOP}
    entries = []
    for d in engine.DEPARTMENTS:
        pbs = [p for p in engine.PLAYBOOKS.values() if p.department == d["id"]]
        text = " ".join([d["name"], d["mission"]] + [p.title + " " + p.description for p in pbs])
        entries.append(({"agent": d["agent"], "department": d["name"], "reports_to": d["reports_to"],
                         "playbooks": [p.name for p in pbs]}, text, 1.0))
    for r in roles.ROLES:
        d = engine.DEPT.get(r["dept"], {})
        entries.append(({"agent": d.get("agent"), "department": d.get("name"), "role": r["title"], "playbooks": r["playbooks"]},
                        r["title"] + " " + " ".join(r["duties"]), 0.8))
    toks = [{_stem(w) for w in re.findall(r"[a-z0-9]+", t.lower())} for _, t, _ in entries]
    n = len(entries)
    out = []
    for (info, _, wt), tk in zip(entries, toks):
        score = sum(math.log(1 + n / (1 + sum(w in t2 for t2 in toks))) for w in words if w in tk) * wt
        if score:
            out.append({"score": round(score, 2), **info})
    out.sort(key=lambda x: -x["score"])
    seen, uniq = set(), []
    for o in out:
        if o["agent"] not in seen:
            seen.add(o["agent"])
            uniq.append(o)
    return uniq[:5]


def t_soql(soql: str):
    return crm.query(soql, max_rows=50)


def t_explore(spec: dict):
    return bi.explore(spec)


def t_metric(name: str, days: int = 90):
    return bi.metric(name, days)


def t_graph_path(a: str, b: str):
    return graph.path(a, b)


def t_graph_neighbors(name: str):
    n = graph.neighborhood(name, 1, 40)
    return {"center": n["center"], "neighbors": [{"name": x["name"], "label": x["label"]} for x in n["nodes"]][:40],
            "relationships": [f"{e['src']} -{e['type']}-> {e['dst']}" for e in n["edges"]][:40]}


def t_money_map():
    return graph.money_map()


def t_network():
    from .company.net_playbooks import network_kpis
    k = network_kpis(30)
    inc = db.rows("select sim_date, target, cause, action, customers, credits_usd from net_incident order by id desc limit 5")
    return {"kpis_30d": k, "recent_incidents": inc}


def t_decisions():
    return {"open": db.scalar("select count(*) from decisions where status='open'"),
            "latest": db.rows("select id, agent, kind, title from decisions where status='open' order by id desc limit 10")}


def t_at_risk():
    return products.at_risk(90, 8)


_INDEX = {"files": None, "df": None, "docs": None}


def t_search_reports(query: str, k: int = 3):
    """BM25 over every agent report: the assistant's retrieval layer."""
    files = sorted(REPORT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:1500]
    if _INDEX["files"] != [f.name for f in files]:
        docs = []
        for f in files:
            toks = re.findall(r"[a-z0-9]+", f.read_text(errors="ignore").lower())
            docs.append((f, Counter(toks), len(toks)))
        df = Counter()
        for _, c, _ in docs:
            df.update(set(c))
        _INDEX.update(files=[f.name for f in files], df=df, docs=docs)
    docs, df = _INDEX["docs"], _INDEX["df"]
    if not docs:
        return []
    n = len(docs)
    avg = sum(d[2] for d in docs) / n
    q = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    scored = []
    for f, c, ln in docs:
        s = 0.0
        for w in q:
            if w in c:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * c[w] * 2.2 / (c[w] + 1.2 * (0.25 + 0.75 * ln / avg))
        if s:
            scored.append((s, f))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, f in scored[:k]:
        text = f.read_text(errors="ignore")
        first = next((l for l in text.splitlines() if l and not l.startswith(("#", "_", "|", "-"))), "")
        out.append({"report": f.name, "score": round(s, 2), "excerpt": first[:300]})
    return out


TOOLS = {
    "company_kpis": (t_kpis, "Headline KPIs and the latest monthly close (MRR, EBITDA, costs, network, customers).", {}),
    "who_does": (t_roles, "Find which agent and department do a job or role (e.g. 'network automation', 'SLA', 'consent').",
                 {"query": {"type": "string"}}),
    "crm_soql": (t_soql, "Query the CRM with SOQL (Account, Contact, Opportunity, Case, Asset, Contract).", {"soql": {"type": "string"}}),
    "explore": (t_explore, "Warehouse query: {source, measures, dims, filters: [{field, op, value}], days, sort, desc, limit}. Sources: "
                           + ", ".join(f"{k} (measures {list(v['measures'])}; dims {list(v['dims'])})" for k, v in bi.SEMANTIC.items()),
                {"spec": {"type": "object"}}),
    "bi_metric": (t_metric, f"Named metric over N days: {', '.join(bi.METRICS)}.", {"name": {"type": "string"}, "days": {"type": "integer"}}),
    "graph_path": (t_graph_path, "Shortest connection between two things in the company graph (departments, agents, customers, "
                                 "devices, products, P&L lines, incidents).", {"a": {"type": "string"}, "b": {"type": "string"}}),
    "graph_neighbors": (t_graph_neighbors, "What is directly connected to one thing in the company graph.", {"name": {"type": "string"}}),
    "money_map": (t_money_map, "Revenue each department operates and costs it owns (from the graph).", {}),
    "network_status": (t_network, "Network availability, incidents, MTTR, credits and recent incidents.", {}),
    "open_decisions": (t_decisions, "Decisions waiting for the founder.", {}),
    "renewals_at_risk": (t_at_risk, "Customers whose contracts end soon, lowest renewal odds first.", {}),
    "search_reports": (t_search_reports, "Full-text search over every agent report.", {"query": {"type": "string"}}),
}


def run_tool(name, args):
    fn = TOOLS[name][0]
    return fn(**(args or {}))


# ---------------------------------------------------------------- engines


def _ollama_model():
    want = os.environ.get("VIX_CHAT_MODEL")
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=2).json().get("models", [])
    except Exception:  # noqa: BLE001
        return None
    names = [t["name"] for t in tags]
    if want and want in names:
        return want
    for p in PREFERRED:
        if p in names:
            return p
    chatty = [n for n in names if "embed" not in n]
    return chatty[0] if chatty else None


def engine_name():
    if os.environ.get("VIX_CHAT_ENGINE", "auto") == "router":
        return "built-in router"
    m = _ollama_model()
    return f"Ollama {m}" if m else "built-in router"


SYSTEM = ("You are Ask vix, the assistant for vix, a simulated digital-infrastructure company run by AI agents. Answer only from "
          "tool results; call tools for every number. Be brief and specific: lead with the answer, then one or two supporting facts. "
          "Use dollars with commas. If a tool fails, say so.")


def _answer_ollama(model, message, history):
    tools = [{"type": "function", "function": {"name": n, "description": d,
                                               "parameters": {"type": "object", "properties": p, "required": list(p)}}}
             for n, (_, d, p) in TOOLS.items()]
    msgs = [{"role": "system", "content": SYSTEM}] + (history or [])[-6:] + [{"role": "user", "content": message}]
    used = []
    for _ in range(5):
        r = httpx.post(f"{OLLAMA}/api/chat", json={"model": model, "messages": msgs, "tools": tools, "stream": False,
                                                   "options": {"temperature": 0.1}}, timeout=180).json()
        m = r.get("message", {})
        calls = m.get("tool_calls") or []
        if not calls:
            text = re.sub(r"<think>.*?</think>", "", m.get("content", ""), flags=re.S).strip()
            return {"answer": text, "tools": used, "engine": f"Ollama {model}"}
        msgs.append(m)
        for c in calls:
            name, args = c["function"]["name"], c["function"].get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args or "{}")
            try:
                res = run_tool(name, args) if name in TOOLS else {"error": f"no tool {name}"}
            except Exception as e:  # noqa: BLE001
                res = {"error": str(e)}
            used.append({"tool": name, "args": args})
            msgs.append({"role": "tool", "content": json.dumps(res, default=str)[:6000], "tool_name": name})
    return {"answer": "I could not finish within five tool calls.", "tools": used, "engine": f"Ollama {model}"}


def _answer_router(message):
    q = message.lower()
    used = []

    def call(_tool, **a):
        used.append({"tool": _tool, "args": a})
        return run_tool(_tool, a)
    mm = (re.search(r"between\s+(.+?)\s+and\s+(.+?)[\?\.]?$", q)
          or re.search(r"how (?:is|are|does|do) (.+?) (?:connected|connect|linked|link|relate|related|tied) to (.+?)[\?\.]?$", q)
          or re.search(r"(?:connect|link|path from)\s+(.+?)\s+(?:and|to|with)\s+(.+?)[\?\.]?$", q))
    if mm:
        a, b = mm.group(1).replace("the ", "").strip(), mm.group(2).replace("the ", "").strip()
        p = call("graph_path", a=a, b=b)
        if not p.get("path"):
            return {"answer": f"I couldn't find a connection between '{a}' and '{b}' in the company graph ({p.get('note')}).", "tools": used}
        steps = " → ".join([p["path"][0]["from"]] + [f"[{s['rel']}] {s['to']}" for s in p["path"]])
        return {"answer": f"{p['from']} connects to {p['to']} in {p['hops']} steps: {steps}", "tools": used}
    if re.search(r"\b(who|which agent|which department|responsible|in charge|owns?|job|role)\b", q) and not re.search(r"\bcustomer\b", q):
        r = call("who_does", query=q)
        if r:
            top = r[0]
            extra = f" (role: {top['role']})" if top.get("role") else ""
            return {"answer": f"{top['agent']} in {top['department']}{extra}. Playbooks: {', '.join(top['playbooks'][:4])}."
                              + (f" Also involved: {', '.join(x['agent'] for x in r[1:3])}." if len(r) > 1 else ""), "tools": used}
    if re.search(r"\b(profit|loss|p&l|pnl|ebitda|margin|cost|costs|spend)\b", q):
        k = call("company_kpis")
        mm = call("money_map")
        c = k["last_close"]
        top_cost = max(mm["departments"], key=lambda d: d["costs_owned"]) if mm["departments"] else None
        return {"answer": f"Last close ({c['sim_date']}): revenue {money(c['mrr'])}, EBITDA {money(c['ebitda'])} "
                          f"({(k['ebitda_margin'] or 0):.1%} margin). Biggest costs: power {money(c['power_cost'])}, site opex {money(c['opex'])}, "
                          f"technicians {money(c['tech_labour'])}, network support {money(c['net_support'])}, SLA credits {money(c['sla_credits'])}."
                          + (f" {top_cost['department']} owns the most cost ({money(top_cost['costs_owned'])}/month)." if top_cost else ""),
                "tools": used}
    if re.search(r"\b(mrr|revenue|sales|money)\b", q):
        if "product" in q or "segment" in q or "metro" in q:
            dim = "product" if "product" in q else "segment" if "segment" in q else "home metro"
            r = call("explore", spec={"source": "revenue", "measures": ["MRR"], "dims": [dim], "limit": 6})
            lines = "; ".join(f"{x[dim]}: {money(x['MRR'])}" for x in r["rows"])
            return {"answer": f"MRR by {dim} (as of {r['as_of']}): {lines}.", "tools": used}
        k = call("company_kpis")
        return {"answer": f"MRR is {money(k['mrr_usd'])} as of {k['sim_date']}, EBITDA margin {(k['ebitda_margin'] or 0):.1%}, "
                          f"{k['active_customers']} active customers.", "tools": used}
    if re.search(r"\b(top|biggest|largest)\b.*\b(customer|account)s?\b", q):
        r = call("crm_soql", soql="SELECT Name, MRR__c FROM Account WHERE Type = 'Customer' ORDER BY MRR__c DESC LIMIT 5")
        recs = r["records"]
        return {"answer": f"Top customer is {recs[0]['Name']} at {money(recs[0]['MRR__c'])}/month. Next: "
                          + ", ".join(f"{x['Name']} ({money(x['MRR__c'])})" for x in recs[1:]) + ".", "tools": used}
    if re.search(r"\b(incident|outage|down|network|availability|mttr)\b", q):
        r = call("network_status")
        k = r["kpis_30d"]
        last = r["recent_incidents"][0] if r["recent_incidents"] else None
        return {"answer": f"Network over the last 30 days: {k['incidents']} incidents ({k['customer_impacting']} hit customers), "
                          f"availability {k['availability_pct']}%, MTTR {k['mttr_min']} min, SLA credits {money(k['credits_usd'])}."
                          + (f" Latest: {last['cause']} on {last['target']} ({last['sim_date']}), {last['action']}." if last else ""),
                "tools": used}
    if re.search(r"\b(win rate|pipeline|opportunit)", q):
        wr = call("bi_metric", name="win_rate", days=30)
        r = call("crm_soql", soql="SELECT COUNT() FROM Opportunity WHERE IsClosed = false")
        return {"answer": f"Win rate over 30 days is {wr['value']}%; {r['totalSize']} opportunities are open.", "tools": used}
    if re.search(r"\b(renew|churn|at risk|retention)\b", q):
        r = call("renewals_at_risk")
        nr = call("bi_metric", name="net_retention", days=90)
        return {"answer": f"Net retention over 90 days is {nr['value']}%. Most at risk: "
                          + "; ".join(f"{x['name']} ({x['p_renew']:.0%}, {money(x['mrr'])}/mo, ends {x['end_date']})" for x in r[:3]) + ".",
                "tools": used}
    if re.search(r"\b(decision|approve|approval)s?\b", q):
        r = call("open_decisions")
        return {"answer": f"{r['open']} decisions wait for you" + (". Latest: " + "; ".join(f"#{d['id']} {d['title']} (from {d['agent']})" for d in r["latest"][:5]) if r["open"] else ".")
                          + (". Helm's board pack has a recommendation on each." if r["open"] else ""), "tools": used}
    if re.search(r"\b(sla|ticket|remote hands)\b", q):
        k = call("company_kpis")
        return {"answer": f"Remote-hands SLA over 30 days is {k['remote_hands_sla_30d']}%.", "tools": used}
    if re.search(r"\b(pue|energy|carbon|co2|power usage)\b", q):
        r = call("explore", spec={"source": "operations", "measures": ["PUE", "IT kW"], "dims": ["cooling"]})
        return {"answer": "PUE by cooling type: " + "; ".join(f"{x['cooling']}: {x['PUE']} ({x['IT kW']:,.0f} kW IT)" for x in r["rows"]) + ".",
                "tools": used}
    hits = call("search_reports", query=message)
    if hits:
        return {"answer": "From the agents' reports: " + " | ".join(f"{h['report']}: {h['excerpt']}" for h in hits), "tools": used}
    return {"answer": "I don't know that yet. Try asking about revenue, costs, customers, the network, decisions, or who does a job.",
            "tools": used}


def answer(message: str, history: list | None = None) -> dict:
    eng = os.environ.get("VIX_CHAT_ENGINE", "auto")
    if eng != "router":
        model = _ollama_model()
        if model:
            try:
                return _answer_ollama(model, message, history)
            except Exception as e:  # noqa: BLE001
                r = _answer_router(message)
                r["note"] = f"local model failed ({type(e).__name__}); answered with the built-in router"
                r["engine"] = "built-in router"
                return r
    r = _answer_router(message)
    r["engine"] = "built-in router"
    return r


# ---------------------------------------------------------------- evaluation (used by Echo)


def evaluate() -> list[dict]:
    from .company import engine
    k = engine.kpis()
    top = crm.query("SELECT Name FROM Account WHERE Type = 'Customer' ORDER BY MRR__c DESC LIMIT 1")["records"]
    wr = bi.metric("win_rate", 30)["value"]
    od = db.scalar("select count(*) from decisions where status='open'")
    from .company.net_playbooks import network_kpis
    ni = network_kpis(30)["incidents"]
    golden = [
        ("What is our MRR?", "company_kpis", f"{k['mrr_usd']:,.0f}"),
        ("Who handles network outages?", "who_does", "Sentinel"),
        ("Which agent does network automation and config compliance?", "who_does", "Cadence"),
        ("Who certifies firmware before rollout?", "who_does", "Proof"),
        ("How many network incidents happened recently?", "network_status", f"{ni} incidents"),
        ("What is the win rate?", "bi_metric", f"{wr}%"),
        ("Who is our top customer?", "crm_soql", top[0]["Name"] if top else ""),
        ("How many decisions are waiting for approval?", "open_decisions", f"{od} decisions"),
        ("How is the NOC connected to EBITDA?", "graph_path", "steps"),
        ("What is our profit and loss?", "company_kpis", "EBITDA"),
    ]
    out = []
    for qn, tool, expect in golden:
        r = _answer_router(qn)
        tools = [t["tool"] for t in r["tools"]]
        ok = tool in tools and expect.lower() in r["answer"].lower()
        out.append({"question": qn, "tool": ",".join(tools), "expected": expect, "ok": ok, "answer": r["answer"][:200]})
    return out
