"""Calling an agent: any agent can be asked to run the work it is assigned, on demand.

An agent's assignments are its department's own playbooks plus the playbooks of every engineering role it holds
(roles.py). Some role playbooks belong to a neighbouring department (Pathfinder's Network Engineer III watches the
NOC with Sentinel); those are run as child runs by the owning agent, so each report says who did the work.

  agent_assignment {agent, role?, playbook?}   one agent runs everything assigned to it, one role's work, or one playbook
  roll_call                                     Helm asks every agent holding an engineering role to run that role's work

Playbooks that move the simulated calendar or run for minutes (the operating cycle, the self-improvement run) and
these two playbooks themselves are never part of an assignment."""
from __future__ import annotations

import json

from .. import db, roles
from . import engine
from .engine import DEPT, PLAYBOOKS, playbook
from .playbooks import table

NOT_ASSIGNABLE = {"exec_operating_cycle", "evolve_run", "agent_assignment", "roll_call"}


def resolve(who: str) -> str:
    """Agent name (any case) or department id -> department id."""
    w = (who or "").strip().lower()
    if not w:
        raise ValueError("say which agent to call (agent=Anvil, or a department id) or which role (role=05)")
    for d in engine.DEPARTMENTS:
        if w in (d["id"].lower(), d["agent"].lower(), d["name"].lower()):
            return d["id"]
    raise ValueError(f"no agent or department '{who}'; agents: {', '.join(sorted(d['agent'] for d in engine.DEPARTMENTS))}")


def role_playbooks(ref: str) -> tuple[str, list[str]]:
    r = next((x for x in roles.ROLES if x["ref"] == str(ref).zfill(2)), None)
    if not r:
        raise ValueError(f"no role {ref}; roles are 01 to {len(roles.ROLES):02d}")
    return r["dept"], [p for p in r["playbooks"] if p in PLAYBOOKS and p not in NOT_ASSIGNABLE]


def assignments(who: str) -> dict:
    dept = resolve(who)
    d = DEPT[dept]
    held = [r for r in roles.ROLES if r["dept"] == dept]
    items, seen = [], set()
    for r in held:
        for p in r["playbooks"]:
            if p in PLAYBOOKS and p not in NOT_ASSIGNABLE and p not in seen:
                seen.add(p)
                items.append({"playbook": p, "via": f"role {r['ref']}: {r['title']}"})
    for name, pb in PLAYBOOKS.items():
        if pb.department == dept and name not in NOT_ASSIGNABLE and name not in seen:
            seen.add(name)
            items.append({"playbook": name, "via": "department"})
    for it in items:
        pb = PLAYBOOKS[it["playbook"]]
        it.update(title=pb.title, performed_by=DEPT[pb.department]["agent"], department=DEPT[pb.department]["name"])
        last = db.one("select id, status, started, report from runs where playbook=? order by id desc limit 1", (it["playbook"],))
        it["last_run"] = last
    return {"agent": d["agent"], "department": dept, "name": d["name"], "reports_to": DEPT.get(d.get("reports_to"), {}).get("agent", "founder"),
            "roles": [{"ref": r["ref"], "title": r["title"]} for r in held], "assignments": items}


def _run_child(ctx, name, params=None) -> dict:
    """Run one playbook as a child of this run, logged as the owning agent doing it."""
    pb = PLAYBOOKS[name]
    owner = DEPT[pb.department]["agent"]
    ctx.log(f"runs {name}" + ("" if owner == ctx.agent else f" with {owner} ({DEPT[pb.department]['name']})"))
    rid = engine._create(name, params or {}, trigger=f"{ctx.agent} · run {ctx.run_id}", parent=ctx.run_id)
    engine._execute(rid)
    r = engine.run_get(rid)
    ctx.log(f"{name}: {r['status']}")
    return r


def _as_agent(ctx, dept):
    """This run is done by the called agent, not by the playbook's nominal owner."""
    ctx.agent = DEPT[dept]["agent"]
    db.execute("update runs set department=? where id=?", (dept, ctx.run_id))


def _row(name, r):
    s = r.get("summary") or {}
    gist = ", ".join(f"{k}={v}" for k, v in list(s.items())[:4] if not isinstance(v, (dict, list))) if isinstance(s, dict) else str(s)
    return {"playbook": name, "by": DEPT[PLAYBOOKS[name].department]["agent"], "status": r["status"],
            "result": gist[:140] or ("" if r["status"] == "done" else (s.get("error", "") if isinstance(s, dict) else ""))[:140],
            "report": r.get("report") or ""}


@playbook("agent_assignment", "Assigned work", "ceo",
          "Ask one agent to run the work it is assigned: everything (its department's playbooks and its engineering "
          "roles), one role's work, or one playbook. Each piece runs as a child run with its own report.",
          {"agent": {"type": "str", "default": ""}, "role": {"type": "str", "default": ""}, "playbook": {"type": "str", "default": ""}})
def agent_assignment(ctx):
    role, one = str(ctx.p("role", "") or "").strip(), str(ctx.p("playbook", "") or "").strip()
    if role:
        dept, todo = role_playbooks(role)
        what = f"role {role.zfill(2)}"
    else:
        dept = resolve(ctx.p("agent", ""))
        allowed = [a["playbook"] for a in assignments(dept)["assignments"]]
        if one:
            if one not in allowed:
                raise ValueError(f"{one} is not assigned to {DEPT[dept]['agent']}; assigned: {', '.join(allowed)}")
            todo, what = [one], one
        else:
            todo, what = allowed, "all assigned work"
    _as_agent(ctx, dept)
    ctx.log(f"asked to run {what}: {len(todo)} playbook(s)")
    rows = [_row(n, _run_child(ctx, n)) for n in todo]
    ok = sum(1 for r in rows if r["status"] == "done")
    d = DEPT[dept]
    md = (f"**{d['agent']}** ({d['name']}, reports to {DEPT.get(d.get('reports_to'), {}).get('agent', 'the founder')}) was asked to run "
          f"{what}. {ok} of {len(rows)} finished.\n\n" + table(rows, ["playbook", "by", "status", "result", "report"]))
    return {"agent": d["agent"], "scope": what, "ran": len(rows), "done": ok, "failed": len(rows) - ok}, md


@playbook("roll_call", "Engineering roll call", "ceo",
          "Helm asks every agent that holds one of the 20 engineering roles to run that role's work now, one role at a "
          "time, and reports which roles delivered.")
def roll_call(ctx):
    rows = []
    for r in roles.ROLES:
        dept, todo = role_playbooks(r["ref"])
        agent = DEPT[dept]["agent"]
        ctx.log(f"asks {agent} to run role {r['ref']} ({r['title']})")
        rid = engine._create("agent_assignment", {"role": r["ref"]}, trigger=f"Helm · run {ctx.run_id}", parent=ctx.run_id)
        engine._execute(rid)
        rr = engine.run_get(rid)
        s = rr.get("summary") or {}
        rows.append({"role": r["ref"], "title": r["title"], "agent": agent, "chief": DEPT.get(DEPT[dept].get("reports_to"), {}).get("agent", ""),
                     "playbooks": ", ".join(todo), "status": rr["status"] if rr["status"] != "done" else ("done" if not s.get("failed") else f"{s.get('failed')} failed"),
                     "report": rr.get("report") or ""})
    ok = sum(1 for x in rows if x["status"] == "done")
    md = (f"Helm called on the {len(rows)} engineering roles. **{ok} of {len(rows)} delivered** (every playbook of the role "
          f"finished).\n\n" + table(rows, ["role", "title", "agent", "chief", "playbooks", "status", "report"]))
    return {"roles": len(rows), "delivered": ok, "failed": len(rows) - ok}, md
