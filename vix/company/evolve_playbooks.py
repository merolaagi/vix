"""Agent Improvement Lab (Helix, under the CTO) and the council sessions Helm convenes."""
from __future__ import annotations

import json

from .. import council, db, evolve, harness, llm
from . import engine
from .engine import playbook
from .playbooks import table

engine.add_department({"id": "evolve", "name": "Agent Improvement Lab", "agent": "Helix", "reports_to": "cto",
                       "mission": "Improve the agents' own policies the way RRSI improves agent harnesses: propose an edit with a "
                                  "hypothesis, screen it for gaming, test it in parallel sandboxes against noise and guards, keep "
                                  "what holds up on held-out scenarios, and ask the founder before anything goes live.",
                       "kpis": ["value added by adopted edits", "edits falsified"]}, before="exec")


def _boot():
    llm.init()
    council.init()
    engine.add_schedules([("evolve_run", "weekly sat 01:30"), ("council_session", "daily 07:05")])


engine.BOOT.append(_boot)


@playbook("evolve_run", "Self-improvement run", "evolve",
          "Helix: RRSI-style rounds on a snapshot of the company. Each round proposes two edits to the agents' policies "
          "(from Claude via MCP, the proposer model, or a heuristic), screens them, runs both in parallel sandboxes on the "
          "evolve set, and keeps a winner only if it beats the noise band, pays for its departure from the defaults and "
          "breaks no guard. The final frontier is tested on a price war and a demand slump; if it holds up, Helix asks the "
          "founder to adopt it.",
          {"rounds": {"type": "int", "default": 3}, "days": {"type": "int", "default": 45}})
def evolve_run(ctx):
    if getattr(ctx, "is_inline", False) or __import__("os").environ.get("VIX_SANDBOX"):
        return {"skipped": "not inside a fast-forward or sandbox"}, "Skipped.\n"
    rounds, days = ctx.p("rounds", 3, int), ctx.p("days", 45, int)
    r = evolve.run(rounds=rounds, days=days, progress=lambda x: ctx.log(f"round {x['round']}: winner {x['winner']}, frontier gain {x['frontier_gain']}"))
    st, res = r["state"], r["result"]
    rows = []
    for rd in r["rounds"]:
        for c in rd["candidates"]:
            rows.append({"round": rd["round"], "component": c["component"], "change": f"{c['from']} -> {c['value']}", "by": c["by"],
                         "gain_musd": c["gain"], "verdict": c["verdict"], "hypothesis": (c.get("hypothesis") or "")[:120]})
        for c in rd["screened_out"]:
            rows.append({"round": rd["round"], "component": c.get("component"), "change": str(c.get("value")), "by": c.get("by"),
                         "gain_musd": None, "verdict": "screened out: " + str(c.get("critic"))[:60], "hypothesis": (c.get("hypothesis") or "")[:120]})
    md = (f"Run **{r['run']}** on a snapshot taken {st['sim_date']}: {len(r['rounds'])} rounds of {st['days']} simulated days on "
          f"{len(st['seeds'])} seeds. Noise band {st['delta']} $M; frontier gain over the live policies on the evolve set "
          f"**{st.get('frontier_gain')} $M**.\n\n## Candidates\n\n" + table(rows, ["round", "component", "change", "by", "gain_musd", "verdict", "hypothesis"]) +
          "\n## Frontier vs live\n\n" + table(res["changes"], ["component", "from", "to"]) +
          ("\n## Held-out scenarios\n\n" + table(res["heldout"], ["scenario", "live", "frontier", "gain", "guards"]) if res["heldout"] else "") +
          f"\n**{'Adoption filed as decision #' + str(res['decision']) if res.get('adopt') else 'Not adopted'}**: {res.get('why', 'no change survived')}.\n\n"
          "_Rewards are 24x the change in monthly run-rate EBITDA, minus bad debt written off and capex, plus a value for new "
          "projects by their IRR, in $M over the sandbox horizon. The sandbox founder follows Helm's recommendations._\n")
    return {"run": r["run"], "rounds": len(r["rounds"]), "frontier_gain": st.get("frontier_gain"), "noise_band": st["delta"],
            "changes": len(res["changes"]), "adopt": res.get("adopt"), "decision": res.get("decision")}, md


@playbook("council_session", "Council session", "ceo",
          "Helm convenes the council on every open decision that is tough (high stakes, unresolved by the rules, or of a kind "
          "that has gone badly before). Each model on the bench votes with reasons; Claude's seat waits for you in claude.ai.")
def council_session(ctx):
    from .playbooks import _recommend
    if __import__("os").environ.get("VIX_SANDBOX"):
        return {"skipped": True}, "Skipped in sandbox.\n"
    council.init()
    out = []
    for d in engine.decisions("open"):
        v, why = _recommend(d)
        t = council.convene(d, v, why)
        if t:
            out.append({"decision": d["id"], "title": d["title"], "helm": v, "council": t["verdict"], "votes": t["votes"],
                        "awaiting_claude": t["awaiting"], "summary": t["summary"]})
    ctx.log(f"{len(out)} sessions")
    md = (f"Council members: {', '.join(council.members()) or 'none configured'}. Mode: {council.settings().get('mode', 'advise')}.\n\n" +
          table(out, ["decision", "title", "helm", "council", "votes", "awaiting_claude", "summary"]) +
          ("\nClaude's seat: ask Claude in claude.ai to review the pending council dossiers (MCP tool council_pending).\n"
           if any(o["awaiting_claude"] for o in out) else ""))
    return {"sessions": len(out), "awaiting_claude": sum(1 for o in out if o["awaiting_claude"])}, md


def _eff_adopt(det, d):
    return harness.set_live(det["values"], note=f"adopted from run {det['run']} (decision #{d['id']})")


def _rec_adopt(d):
    det = d["detail"]
    hg = det.get("heldout_gain_musd")
    if hg is not None and hg >= 0 and (det.get("evolve_gain_musd") or 0) > (det.get("noise_band") or 0):
        return "approve", (f"+{det['evolve_gain_musd']} $M on the evolve set (noise {det['noise_band']}), {hg:+} $M on held-out scenarios, "
                           f"no guard breached; changes: " + ", ".join(f"{c['component']} {c['from']}->{c['to']}" for c in det["changes"]))
    return "reject", "did not hold up on held-out scenarios"


engine.DECISION_EFFECTS["adopt_harness"] = _eff_adopt
engine.RECOMMENDERS["adopt_harness"] = _rec_adopt
engine.DEDUPE_KEY["adopt_harness"] = "run"
