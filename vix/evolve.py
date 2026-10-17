"""Agent Improvement Lab (Helix): regularized recursive self-improvement of the agents' harnesses.

Modelled on RRSI (google-research/rrsi, "Regularized Recursive Self-Improvement of Agent Harnesses"): the model
stays fixed and the harness around it is evolved, but the search is regularized because tuning against a fixed
evolve set overfits. Each round:

  1. Analyse    digest how the current frontier differs from the live harness, and the edit history.
  2. Propose    two candidate edits, each tagged with a component, a hypothesis and a value. The edit budget is
                annealed (big steps early, small late); falsified hypotheses are never retried; a stalled run is
                pushed to components it has not tried. Proposals come from Claude via MCP (queued), the
                proposer model, or a heuristic when no model is configured.
  3. Screen     a critic rejects edits that reference the evaluation itself (leakage), break bounds, exceed the
                round's budget, or touch a component other than the one tagged; an optional critic model reviews
                the hypothesis, with one repair.
  4. Evaluate   both candidates in parallel sandboxes on the evolve set (seeded copies of the company run forward).
  5. Select     noise floor (calibrated), a cost rule (bigger departures from the defaults must pay for themselves),
                non-compensatory guards (NPS, availability, DSO, leverage, retention, SLA, compliance), a
                within-band rule (prefer the smaller edit) and finally argmax.
  6. Update     the winner becomes the frontier; history is logged; components that keep failing are pruned.

After the last round the frontier is tested on held-out scenarios (a price war, a demand slump) against the live
harness. Only if it holds up there does Helix file an adoption decision; the founder decides."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import db, harness, llm, products
from .config import DATA_DIR, DB_PATH

ROOT = DATA_DIR / "evolve"
QUEUE = ROOT / "queue.json"
GUARDS = {  # metric: (direction, tolerance) -- a candidate fails if it is worse than the frontier by more than tolerance
    "nps": ("higher", 8), "availability": ("higher", 0.01), "net_retention": ("higher", 0.03), "sla": ("higher", 2.0),
    "readiness": ("higher", 10), "dso": ("lower", 10), "leverage": ("lower", 0.4)}
LEAKAGE = re.compile(r"\b(seeds?|sandbox(es)?|evolve set|held[- ]?out|scenarios?|benchmarks?|test sets?|rewards?|evaluat\w*)\b", re.I)
BETA = 0.15      # $M of gain needed per default-step of departure from the defaults (cost rule)
K_STEPS = 3      # largest edit in steps in round one; annealed to 1


def _dir(run_id):
    return ROOT / run_id


def _save(run_id, state):
    p = _dir(run_id) / "state.json"
    p.write_text(json.dumps(state, indent=1, default=str))


def _load(run_id):
    return json.loads((_dir(run_id) / "state.json").read_text())


def _log(run_id, entry):
    with open(_dir(run_id) / "history.jsonl", "a") as f:
        f.write(json.dumps({**entry, "ts": time.time()}, default=str) + "\n")


def history(run_id) -> list[dict]:
    p = _dir(run_id) / "history.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def runs() -> list[dict]:
    if not ROOT.exists():
        return []
    out = []
    for d in sorted(ROOT.iterdir(), reverse=True):
        if (d / "state.json").exists():
            s = json.loads((d / "state.json").read_text())
            status = s.get("status")
            if status in ("starting", "running", "calibrating") and time.time() - (d / "state.json").stat().st_mtime > 3 * 3600:
                status = "stalled"   # the process ended (restart, crash) without finishing; start a new run
            out.append({"run": d.name, "status": status, "round": s.get("round"), "rounds": s.get("rounds"),
                        "delta": s.get("delta"), "frontier_gain": s.get("frontier_gain"), "decision": s.get("decision"),
                        "changes": _diff(s.get("live", {}), s.get("frontier", {})), "started": s.get("started")})
    return out[:20]


def _diff(a, b):
    return [{"component": k, "from": a.get(k), "to": v} for k, v in b.items() if a.get(k) != v]


# ---------------------------------------------------------------- evaluation


def snapshot(run_id) -> Path:
    d = _dir(run_id) / "snapshot"
    d.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(d / "vix.db") as dst:
        db.conn().backup(dst)
    wh = DB_PATH.parent / "warehouse.db"
    if wh.exists():
        with sqlite3.connect(wh) as s, sqlite3.connect(d / "warehouse.db") as dst:
            s.backup(dst)
    return d


def evaluate_many(specs: list[dict], timeout=1800) -> list[dict]:
    """Run sandbox evaluations in parallel subprocesses."""
    code = str(Path(__file__).resolve().parents[1])
    procs = []
    for sp in specs:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("VIX_HARNESS", "VIX_PLACEBO", "VIX_SIM_SEED"))}
        env["PYTHONPATH"] = code
        env["PYTHONHASHSEED"] = "0"
        procs.append(subprocess.Popen([sys.executable, "-m", "vix.evolve_eval", json.dumps(sp)], cwd=code, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    out = []
    for p in procs:
        try:
            so, se = p.communicate(timeout=timeout)
            line = (so.strip().splitlines() or ["{}"])[-1]
            out.append(json.loads(line) if line.startswith("{") else {"ok": False, "error": (se or so)[-600:]})
        except Exception as e:  # noqa: BLE001
            p.kill()
            out.append({"ok": False, "error": str(e)})
    return out


def _specs(state, values, seeds, scenario="base"):
    return [{"snapshot": state["snapshot"], "harness": values, "seed": s, "days": state["days"], "scenario": scenario} for s in seeds]


# ---------------------------------------------------------------- proposal, critic, selection


def _budget(state):
    T, t = state["rounds"], state["round"]
    return max(1, math.ceil(K_STEPS * (1 - t / max(1, T))))


def _dist(values):
    return sum(abs(float(values.get(k, c[3])) - c[3]) / c[6] for k, c in harness.COMPONENTS.items())


def queue_proposal(component: str, value, hypothesis: str, by: str = "claude:subscription") -> dict:
    """Claude (or the founder) suggests an edit for the next round of any run."""
    if component not in harness.COMPONENTS:
        raise ValueError(f"unknown component {component}; one of {list(harness.COMPONENTS)}")
    if not str(hypothesis or "").strip():
        raise ValueError("give a hypothesis about the business mechanism")
    if LEAKAGE.search(hypothesis):
        raise ValueError("the hypothesis refers to the evaluation (seeds, sandbox, reward, benchmark) rather than the business")
    ROOT.mkdir(parents=True, exist_ok=True)
    q = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
    q.append({"component": component, "value": harness.clamp(component, value), "hypothesis": hypothesis[:400], "by": by, "queued": time.time()})
    QUEUE.write_text(json.dumps(q, indent=1))
    return {"queued": len(q)}


def _take_queue():
    if not QUEUE.exists():
        return []
    q = json.loads(QUEUE.read_text())
    QUEUE.write_text("[]")
    return q


def digest(state) -> str:
    """Analyst: what the last round showed, in a few lines."""
    hist = state.get("last_round") or []
    lines = []
    for c in hist:
        m = c.get("metrics_delta") or {}
        moved = ", ".join(f"{k} {v:+.3g}" for k, v in m.items() if v)
        lines.append(f"{c['component']} -> {c['value']}: gain {c.get('gain')} ({c.get('verdict')}); {moved or 'no guard metric moved'}")
    return "\n".join(lines) or "first round"


def _llm_proposals(state, budget, n=2):
    if not llm.available("proposer"):
        return []
    cat = [{"component": k, "agent": c[0], "what": c[2], "now": state["frontier"].get(k, c[3]), "min": c[4], "max": c[5],
            "max_change_this_round": round(budget * c[6], 4)} for k, c in harness.COMPONENTS.items() if k not in state["prune"]]
    falsified = [h for h in history(state["run"]) if h.get("falsified")]
    prompt = ("You improve the policies of AI agents that run a simulated digital-infrastructure company. Propose exactly "
              f"{n} edits that you expect to raise long-run economic value without hurting customers, reliability, liquidity or "
              "compliance. Each edit changes ONE component within max_change_this_round. State a falsifiable hypothesis about "
              "the business mechanism. Do not repeat a falsified hypothesis.\n\nComponents:\n" + json.dumps(cat, indent=1) +
              "\n\nWhat the last round showed:\n" + digest(state) + "\n\nFalsified so far:\n" +
              json.dumps([{"component": h["component"], "value": h["value"], "hypothesis": h["hypothesis"]} for h in falsified][-12:]) +
              "\n\nUntried components: " + ", ".join(k for k in harness.COMPONENTS if k not in state["tried"] and k not in state["prune"]) +
              ("\nThe run has stalled: choose untried components." if state.get("stall", 0) >= 2 else "") +
              '\n\nReply with JSON: {"edits": [{"component": "...", "value": number, "hypothesis": "..."}]}')
    try:
        r = llm.ask("proposer", [{"role": "user", "content": prompt}], json_mode=True, max_tokens=900)
        return [dict(e, by=f"{r['provider']}:{r['model']}") for e in llm.parse_json(r["text"]).get("edits", [])][:n]
    except Exception:  # noqa: BLE001
        return []


def _heuristic(state, budget, rnd, exclude):
    cands = [k for k in harness.COMPONENTS if k not in state["prune"] and k not in exclude]
    untried = [k for k in cands if k not in state["tried"]]
    pool = untried if (untried and (state.get("stall", 0) >= 2 or rnd.random() < 0.6)) else cands
    k = rnd.choice(pool)
    c = harness.COMPONENTS[k]
    cur = float(state["frontier"].get(k, c[3]))
    tried_dirs = {h["value"] for h in history(state["run"]) if h["component"] == k}
    for _ in range(6):
        v = harness.clamp(k, cur + rnd.choice([-1, 1]) * rnd.randint(1, budget) * c[6])
        if v != cur and v not in tried_dirs:
            break
    return {"component": k, "value": v, "by": "heuristic",
            "hypothesis": f"moving {c[2].lower()} from {cur} to {v} changes {c[0]}'s trade-off in a way that adds value"}


def critic(state, edit, budget) -> tuple[bool, str]:
    k = edit.get("component")
    if k not in harness.COMPONENTS:
        return False, "unknown component"
    if k in state["prune"]:
        return False, "component pruned in this run"
    c = harness.COMPONENTS[k]
    try:
        v = float(edit["value"])
    except (TypeError, ValueError, KeyError):
        return False, "no numeric value"
    if v < c[4] or v > c[5]:
        return False, f"outside bounds {c[4]}..{c[5]}"
    cur = float(state["frontier"].get(k, c[3]))
    if abs(v - cur) > budget * c[6] + 1e-9:
        return False, f"bigger than this round's budget ({budget} steps of {c[6]})"
    if v == cur:
        return False, "no change"
    if LEAKAGE.search(edit.get("hypothesis", "")):
        return False, "hypothesis refers to the evaluation rather than the business (leakage)"
    for h in history(state["run"]):
        if h.get("falsified") and h["component"] == k and float(h["value"]) == v:
            return False, "this edit was already falsified"
    if llm.available("critic"):
        try:
            r = llm.ask("critic", [{"role": "user", "content": "An AI agent's policy edit is proposed for a simulated company. Reject it if it "
                                    "games a metric (looks better on a measure while hurting the business), or if the hypothesis is "
                                    "about the test rather than the business.\n" + json.dumps({"component": k, "meaning": c[2],
                                    "from": cur, "to": v, "hypothesis": edit.get("hypothesis")}) +
                                    '\nReply JSON: {"ok": true|false, "concern": "..."}'}], json_mode=True, max_tokens=300)
            j = llm.parse_json(r["text"])
            if not j.get("ok", True):
                return False, "critic model: " + str(j.get("concern", ""))[:200]
        except Exception:  # noqa: BLE001
            pass
    return True, "ok"


def _guard_breaches(cand_end, front_end):
    out = []
    for m, (direction, tol) in GUARDS.items():
        a, b = cand_end.get(m), front_end.get(m)
        if a is None or b is None:
            continue
        worse = (b - a) if direction == "higher" else (a - b)
        if worse > tol:
            out.append(f"{m} {b} -> {a}")
    return out


def _score(cands, front, state):
    """Paired gains vs the frontier on the same seeds, guards, floor, cost rule."""
    delta = state["delta"]
    for c in cands:
        res = c["results"]
        if not all(r.get("ok") for r in res):
            c.update(verdict="error", gain=None, reason="; ".join(str(r.get("error"))[:200] for r in res if not r.get("ok")))
            continue
        gains = [r["reward"] - f["reward"] for r, f in zip(res, front)]
        c["gain"] = round(sum(gains) / len(gains), 4)
        breaches = sorted({b for r, f in zip(res, front) for b in _guard_breaches(r["end"], f["end"])})
        cost = BETA * max(0.0, _dist({**state["frontier"], c["component"]: c["value"]}) - _dist(state["frontier"]))
        c["metrics_delta"] = {m: round(sum((r["end"].get(m) or 0) - (f["end"].get(m) or 0) for r, f in zip(res, front)) / len(res), 4)
                              for m in GUARDS}
        if breaches:
            c.update(verdict="guard", reason="guards: " + ", ".join(breaches))
        elif c["gain"] <= delta:
            c.update(verdict="below noise", reason=f"gain {c['gain']} <= noise band {delta}")
        elif c["gain"] <= delta + cost:
            c.update(verdict="not worth it", reason=f"gain {c['gain']} does not pay for the departure (needs {round(delta + cost, 3)})")
        else:
            c.update(verdict="eligible", reason=f"gain {c['gain']} > {round(delta + cost, 3)}")
        c["falsified"] = c["gain"] is not None and c["gain"] < -delta
    elig = [c for c in cands if c["verdict"] == "eligible"]
    if not elig:
        return None
    elig.sort(key=lambda c: -c["gain"])
    if len(elig) > 1 and abs(elig[0]["gain"] - elig[1]["gain"]) < delta:   # within band: prefer the smaller edit
        elig.sort(key=lambda c: abs(float(c["value"]) - float(state["frontier"].get(c["component"], harness.COMPONENTS[c["component"]][3]))) /
                  harness.COMPONENTS[c["component"]][6])
    return elig[0]


# ---------------------------------------------------------------- the loop


def start(rounds=4, days=45, seeds=(11, 12, 13), heldout=(("price_war", 21), ("demand_slump", 22))) -> str:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    _dir(run_id).mkdir(parents=True, exist_ok=True)
    live = harness.live()
    state = {"run": run_id, "status": "starting", "rounds": int(rounds), "round": 0, "days": int(days), "seeds": list(seeds),
             "heldout": [list(h) for h in heldout], "live": live, "frontier": dict(live), "tried": {}, "prune": [], "stall": 0,
             "delta": None, "frontier_rewards": None, "started": time.time(), "sim_date": products.today(),
             "harness_version": harness.version()}
    state["snapshot"] = str(snapshot(run_id))
    _save(run_id, state)
    return run_id


def stop(run_id):
    (_dir(run_id) / "STOP").touch()


def calibrate(state):
    """Noise band from a placebo: the live policies again on the same seeds with one tiny early perturbation. The paired
    difference is how far a run drifts on its own, which is the least a real edit must beat."""
    specs = _specs(state, state["frontier"], state["seeds"])
    specs += [dict(sp, placebo=True) for sp in _specs(state, state["frontier"], state["seeds"])]
    res = evaluate_many(specs)
    ok = [r for r in res if r.get("ok")]
    if len(ok) < len(res):
        raise RuntimeError("baseline evaluation failed: " + "; ".join(str(r.get("error"))[:300] for r in res if not r.get("ok")))
    n = len(state["seeds"])
    diffs = [abs(ok[i]["reward"] - ok[i + n]["reward"]) for i in range(n)]
    state["delta"] = round(max(0.05, sum(diffs) / len(diffs) / math.sqrt(n)), 3)   # gains are averaged over n seeds
    state["placebo_diffs"] = [round(d, 4) for d in diffs]
    state["frontier_rewards"] = ok[:n]
    state["baseline_rewards"] = ok[:n]


def step(run_id, rnd=None) -> dict:
    """One round. Returns the round record."""
    state = _load(run_id)
    rnd = rnd or random.Random(f"{run_id}-{state['round']}")
    if state["delta"] is None:
        calibrate(state)
        state["status"] = "running"
        _save(run_id, state)
    budget = _budget(state)
    edits = [dict(e, by=e.get("by", "queue")) for e in _take_queue()][:2]
    if len(edits) < 2:
        edits += _llm_proposals(state, budget, 2 - len(edits))
    screened, rejected = [], []
    seen = set()
    for e in edits:
        ok, why = critic(state, e, budget)
        if ok and e["component"] not in seen:
            screened.append({**e, "value": harness.clamp(e["component"], e["value"])})
            seen.add(e["component"])
        else:
            rejected.append({**e, "critic": why})
    tries = 0
    while len(screened) < 2 and tries < 10:
        e = _heuristic(state, budget, rnd, seen)
        ok, why = critic(state, e, budget)
        tries += 1
        if ok and e["component"] not in seen:
            screened.append(e)
            seen.add(e["component"])
    specs, idx = [], []
    for i, e in enumerate(screened):
        vals = {**state["frontier"], e["component"]: e["value"]}
        for sp in _specs(state, vals, state["seeds"]):
            specs.append(sp)
            idx.append(i)
    res = evaluate_many(specs)
    cands = []
    for i, e in enumerate(screened):
        cands.append({**e, "from": state["frontier"].get(e["component"], harness.COMPONENTS[e["component"]][3]),
                      "results": [r for r, j in zip(res, idx) if j == i]})
    winner = _score(cands, state["frontier_rewards"], state)
    for c in cands:
        state["tried"][c["component"]] = state["tried"].get(c["component"], 0) + 1
        fals = sum(1 for h in history(run_id) if h["component"] == c["component"] and h.get("falsified")) + int(c.get("falsified", False))
        if fals >= 2 and c["component"] not in state["prune"]:
            state["prune"].append(c["component"])
        _log(run_id, {"round": state["round"] + 1, "component": c["component"], "from": c["from"], "value": c["value"],
                      "hypothesis": c.get("hypothesis"), "by": c.get("by"), "gain": c.get("gain"), "verdict": c.get("verdict"),
                      "reason": c.get("reason"), "falsified": c.get("falsified", False), "winner": c is winner,
                      "metrics_delta": c.get("metrics_delta"), "rewards": [r.get("reward") for r in c["results"]]})
    for e in rejected:
        _log(run_id, {"round": state["round"] + 1, "component": e.get("component"), "value": e.get("value"), "hypothesis": e.get("hypothesis"),
                      "by": e.get("by"), "verdict": "screened out", "reason": e.get("critic"), "falsified": False, "winner": False})
    if winner:
        state["frontier"][winner["component"]] = winner["value"]
        state["frontier_rewards"] = winner["results"]
        state["stall"] = 0
    else:
        state["stall"] = state.get("stall", 0) + 1
    state["round"] += 1
    state["last_round"] = [{k: c.get(k) for k in ("component", "value", "gain", "verdict", "metrics_delta")} for c in cands]
    base = state["baseline_rewards"]
    state["frontier_gain"] = round(sum(f["reward"] - b["reward"] for f, b in zip(state["frontier_rewards"], base)) / len(base), 4)
    _save(run_id, state)
    return {"round": state["round"], "budget_steps": budget, "candidates": [{k: c.get(k) for k in ("component", "from", "value", "by", "hypothesis",
                                                                                                    "gain", "verdict", "reason")} for c in cands],
            "screened_out": rejected, "winner": winner["component"] if winner else None, "frontier_gain": state["frontier_gain"]}


def finish(run_id) -> dict:
    """Held-out test of the frontier against the live harness, then an adoption decision if it holds up."""
    state = _load(run_id)
    changes = _diff(state["live"], state["frontier"])
    out = {"changes": changes, "heldout": [], "adopt": False}
    if changes:
        specs = []
        for sc, seed in state["heldout"]:
            specs += [{"snapshot": state["snapshot"], "harness": state["live"], "seed": seed, "days": state["days"], "scenario": sc},
                      {"snapshot": state["snapshot"], "harness": state["frontier"], "seed": seed, "days": state["days"], "scenario": sc}]
        res = evaluate_many(specs)
        gains, breaches = [], []
        for i, (sc, seed) in enumerate(state["heldout"]):
            a, b = res[2 * i], res[2 * i + 1]
            if a.get("ok") and b.get("ok"):
                g = round(b["reward"] - a["reward"], 4)
                br = _guard_breaches(b["end"], a["end"])
                gains.append(g)
                breaches += [f"{sc}: {x}" for x in br]
                out["heldout"].append({"scenario": sc, "live": a["reward"], "frontier": b["reward"], "gain": g, "guards": br})
            else:
                out["heldout"].append({"scenario": sc, "error": (a.get("error") or b.get("error"))})
        hg = round(sum(gains) / len(gains), 4) if gains else None
        out["heldout_gain"] = hg
        out["adopt"] = bool(gains) and hg >= 0 and not breaches and (state.get("frontier_gain") or 0) > state["delta"]
        out["why"] = ("holds up out of distribution" if out["adopt"] else
                      "overfits the evolve set: worse or guard breaches on held-out scenarios" if gains else "held-out evaluation failed")
        if out["adopt"]:
            from .company.engine import decision_add
            did = decision_add("Helix", "adopt_harness", f"Adopt improved agent policies ({len(changes)} change{'s' if len(changes) > 1 else ''})",
                               {"run": run_id, "changes": changes, "evolve_gain_musd": state["frontier_gain"], "noise_band": state["delta"],
                                "heldout_gain_musd": hg, "heldout": out["heldout"], "values": state["frontier"], "harness_version": state["harness_version"]})
            state["decision"] = did
            out["decision"] = did
    state["status"] = "done"
    state["result"] = out
    _save(run_id, state)
    return out


def run(rounds=4, days=45, seeds=(11, 12, 13), progress=None) -> dict:
    run_id = start(rounds, days, seeds)
    log = []
    for _ in range(rounds):
        if (_dir(run_id) / "STOP").exists():
            break
        r = step(run_id)
        log.append(r)
        if progress:
            progress(r)
    res = finish(run_id)
    return {"run": run_id, "rounds": log, "result": res, "state": _load(run_id)}
