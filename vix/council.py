"""The council: a bench of models that deliberates on tough decisions before the founder decides.

A decision is tough when the stakes are high (capital, debt, dividends, product exits, big discounts), when Helm's
rules cannot settle it (hold or review), or when Arbiter's track record for that kind of decision is poor. Each
member gets the same dossier (the proposal, Helm's rule-based view, the company's numbers, how similar decisions
turned out) and returns a verdict, a confidence and its reasons. Votes are weighted by confidence and by each
member's record on decisions that have since been scored.

One seat belongs to Claude on the founder's claude.ai subscription. vix never calls it; the dossier waits in
MCP (council_pending) until Claude reads it and votes (council_vote) while the founder is working with it.

By default the council advises and the founder decides. Settings can let a unanimous council (or a clear
majority) decide on the founder's behalf."""
from __future__ import annotations

import json
import time

from . import db, llm, products

SCHEMA = """
create table if not exists council_session (
  id integer primary key, decision integer unique, opened real, sim_date text, status text, dossier text, helm_verdict text,
  helm_why text, verdict text, score real, summary text, decided_by_council integer default 0
);
create table if not exists council_vote (
  id integer primary key, session integer, member text, verdict text, confidence real, reasons text, risks text,
  change_mind text, ts real, latency_s real, status text, error text
);
"""
HIGH_STAKES = {"dev_project", "build_hall", "hyperscale_bid", "issue_bonds", "refinance_bond", "declare_dividend", "sunset_metal",
               "discount_approval", "adopt_harness", "network_standard", "net_live_writes", "sign_ppa", "onsite_power"}
SYSTEM = ("You sit on the council of vix, a simulated digital-infrastructure company run by AI agents. The founder decides; you "
          "advise. Read the dossier and judge the decision on its merits for the company over the next two years: value, risk, "
          "customers, cash and leverage. Disagree with Helm's rule when the evidence says so. Reply with one JSON object: "
          '{"verdict": "approve" | "reject" | "hold", "confidence": 0.0-1.0, "reasons": "under 80 words", '
          '"risks": ["..."], "what_would_change_my_mind": "one sentence"}')


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)


def settings() -> dict:
    return llm.load()["council"]


def members() -> list[str]:
    return [m for m in settings().get("members", []) if m]


def is_tough(d: dict, verdict: str) -> bool:
    det = d["detail"]
    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    money = max(num(det.get(k)) for k in ("capex_usd", "budget_usd", "amount_usd", "total_usd", "vix_capex_usd")) + 12 * num(det.get("list_mrr"))
    if d["kind"] in HIGH_STAKES or verdict in ("hold", "review") or money >= 20e6:
        return True
    try:
        r = db.one("select avg(score) s, count(score) n from gov_outcome where kind=? and status='scored' and score is not null", (d["kind"],))
        if r and r["n"] >= 3 and (r["s"] or 0) < 0.6:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def dossier(d: dict, verdict: str, why: str) -> dict:
    from .company import engine
    k = engine.kpis()
    track = []
    try:
        track = db.rows("select title, verdict, detail from gov_outcome where kind=? and status='scored' order by approved_on desc limit 5", (d["kind"],))
    except Exception:  # noqa: BLE001
        pass
    grade = None
    try:
        grade = db.scalar("select grade from gov_scorecard where agent=? order by sim_date desc limit 1", (d["agent"],))
    except Exception:  # noqa: BLE001
        pass
    return {"decision": {"id": d["id"], "kind": d["kind"], "title": d["title"], "filed_by": d["agent"], "filer_grade": grade, "detail": d["detail"]},
            "helm": {"verdict": verdict, "why": why},
            "company": {"sim_date": k.get("sim_date"), "mrr_usd": k.get("mrr_usd"), "ebitda_margin": k.get("ebitda_margin"),
                        "finance": k.get("finance"), "development": k.get("development"), "market": k.get("market"), "trust": k.get("trust"),
                        "network_availability_30d": (k.get("network") or {}).get("availability_30d"), "open_decisions": k.get("open_decisions")},
            "similar_decisions": track}


def _member_weight(member: str) -> float:
    """Members who have been right more often count for more (needs three scored decisions)."""
    rows = db.rows("select v.verdict, o.score from council_vote v join council_session s on s.id=v.session join gov_outcome o on o.decision=s.decision"
                   " where v.member=? and v.status='cast' and o.status='scored' and o.score is not null", (member,)) if _has_gov() else []
    if len(rows) < 3:
        return 1.0
    right = sum(1 for r in rows if (r["verdict"] == "approve") == (r["score"] >= 0.5))
    return 0.5 + right / len(rows)


def _has_gov():
    return bool(db.one("select name from sqlite_master where name='gov_outcome'"))


def tally(session_id: int) -> dict:
    votes = db.rows("select * from council_vote where session=? and status='cast'", (session_id,))
    num = den = 0.0
    for v in votes:
        w = (v["confidence"] or 0.5) * _member_weight(v["member"])
        num += w * {"approve": 1, "reject": -1}.get(v["verdict"], 0)
        den += w
    score = num / den if den else 0.0
    verdict = "approve" if score >= 0.34 else "reject" if score <= -0.34 else "split"
    unanimous = len(votes) >= 2 and len({v["verdict"] for v in votes}) == 1
    pending = db.scalar("select count(*) from council_vote where session=? and status='awaiting'", (session_id,)) or 0
    s = db.one("select * from council_session where id=?", (session_id,))
    summary = "; ".join(f"{v['member'].split(':', 1)[-1]}: {v['verdict']} ({(v['confidence'] or 0):.0%})" for v in votes)
    status = "awaiting Claude" if pending else "closed"
    db.execute("update council_session set verdict=?, score=?, summary=?, status=? where id=?",
               (verdict, round(score, 3), summary, status, session_id))
    out = {"session": session_id, "decision": s["decision"], "verdict": verdict, "score": round(score, 3), "votes": len(votes),
           "unanimous": unanimous, "awaiting": pending, "summary": summary}
    _maybe_decide(out, s)
    return out


def _maybe_decide(t: dict, s: dict):
    mode = settings().get("mode", "advise")
    if mode == "advise" or t["awaiting"] or t["verdict"] == "split":
        return
    d = db.one("select status from decisions where id=?", (s["decision"],))
    if not d or d["status"] != "open":
        return
    quorum = int(settings().get("quorum") or 2)
    if t["votes"] < quorum:
        return
    if mode == "unanimous" and not t["unanimous"]:
        return
    from .company import engine
    engine.decide(s["decision"], t["verdict"] == "approve", f"decided by the council ({t['summary']})")
    db.execute("update council_session set decided_by_council=1 where id=?", (s["id"],))


def convene(d: dict, verdict: str, why: str, force: bool = False) -> dict | None:
    """Open a session for one decision (once) and collect a vote from every configured model."""
    init()
    old = db.one("select id from council_session where decision=?", (d["id"],))
    if old and not force:
        return tally(old["id"])
    if not force and not is_tough(d, verdict):
        return None
    ms = members()
    if not ms:
        return None
    doss = dossier(d, verdict, why)
    if old:
        sid = old["id"]
        db.execute("delete from council_vote where session=?", (sid,))
        db.execute("update council_session set dossier=?, helm_verdict=?, helm_why=?, opened=? where id=?",
                   (json.dumps(doss, default=str), verdict, why, time.time(), sid))
    else:
        sid = db.execute("insert into council_session(decision,opened,sim_date,status,dossier,helm_verdict,helm_why) values(?,?,?,?,?,?,?)",
                         (d["id"], time.time(), products.today(), "open", json.dumps(doss, default=str), verdict, why))
    prompt = "Dossier:\n" + json.dumps(doss, default=str, indent=1)[:12000]
    for m in ms:
        if m == llm.CLAUDE_SEAT:
            db.execute("insert into council_vote(session,member,ts,status) values(?,?,?,'awaiting')", (sid, m, time.time()))
            continue
        try:
            r = llm.call(m, [{"role": "user", "content": prompt}], system=SYSTEM, json_mode=True, role="council", temperature=0.2, max_tokens=600)
            v = llm.parse_json(r["text"])
            verdict_m = str(v.get("verdict", "hold")).lower()
            if verdict_m not in ("approve", "reject", "hold"):
                verdict_m = "hold"
            db.execute("insert into council_vote(session,member,verdict,confidence,reasons,risks,change_mind,ts,latency_s,status) values(?,?,?,?,?,?,?,?,?,'cast')",
                       (sid, m, verdict_m, max(0.0, min(1.0, float(v.get("confidence") or 0.5))), str(v.get("reasons", ""))[:800],
                        json.dumps(v.get("risks") or [])[:800], str(v.get("what_would_change_my_mind", ""))[:300], time.time(), r["latency_s"]))
        except Exception as e:  # noqa: BLE001
            db.execute("insert into council_vote(session,member,ts,status,error) values(?,?,?,'failed',?)", (sid, m, time.time(), f"{type(e).__name__}: {str(e)[:200]}"))
    return tally(sid)


def vote(session_id: int, verdict: str, confidence: float, reasons: str, member: str = llm.CLAUDE_SEAT, risks=None, change_mind: str = "") -> dict:
    """A seat that answers outside the router (Claude on the subscription, through MCP) casts its vote."""
    init()
    verdict = verdict.lower()
    if verdict not in ("approve", "reject", "hold"):
        raise ValueError("verdict must be approve, reject or hold")
    if not db.one("select id from council_session where id=?", (session_id,)):
        raise ValueError(f"no council session {session_id}")
    row = db.one("select id from council_vote where session=? and member=?", (session_id, member))
    vals = (verdict, max(0.0, min(1.0, float(confidence))), reasons[:800], json.dumps(risks or []), change_mind[:300], time.time())
    if row:
        db.execute("update council_vote set verdict=?, confidence=?, reasons=?, risks=?, change_mind=?, ts=?, status='cast', error=null where id=?", vals + (row["id"],))
    else:
        db.execute("insert into council_vote(session,member,verdict,confidence,reasons,risks,change_mind,ts,status) values(?,?,?,?,?,?,?,?,'cast')",
                   (session_id, member) + vals)
    return tally(session_id)


def pending_for_claude() -> list[dict]:
    init()
    out = []
    for s in db.rows("select s.* from council_session s join council_vote v on v.session=s.id where v.member=? and v.status='awaiting'"
                     " and exists (select 1 from decisions d where d.id=s.decision and d.status='open') order by s.id", (llm.CLAUDE_SEAT,)):
        out.append({"session": s["id"], "decision": s["decision"], "dossier": json.loads(s["dossier"]),
                    "other_votes": db.rows("select member, verdict, confidence, reasons from council_vote where session=? and status='cast'", (s["id"],))})
    return out


def sessions(limit: int = 30) -> list[dict]:
    init()
    out = []
    for s in db.rows("select s.*, d.title, d.kind, d.status decision_status from council_session s join decisions d on d.id=s.decision order by s.id desc limit ?", (limit,)):
        s["votes"] = db.rows("select member, verdict, confidence, reasons, risks, change_mind, status, error, latency_s from council_vote where session=?", (s["id"],))
        s.pop("dossier", None)
        out.append(s)
    return out


def record() -> list[dict]:
    """Each member's record on decisions that have since been scored by Arbiter."""
    init()
    if not _has_gov():
        return []
    out = []
    for m in db.rows("select distinct member from council_vote where status='cast'"):
        rows = db.rows("select v.verdict, o.score from council_vote v join council_session s on s.id=v.session join gov_outcome o on o.decision=s.decision"
                       " where v.member=? and v.status='cast' and o.status='scored' and o.score is not null", (m["member"],))
        n = db.scalar("select count(*) from council_vote where member=? and status='cast'", (m["member"],))
        right = sum(1 for r in rows if (r["verdict"] == "approve") == (r["score"] >= 0.5))
        out.append({"member": m["member"], "votes": n, "scored": len(rows), "right": right,
                    "accuracy": round(right / len(rows), 3) if rows else None, "weight": round(_member_weight(m["member"]), 2)})
    return out
