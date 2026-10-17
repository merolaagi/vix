"""Sandbox evaluation for the self-improvement lab (run as: python -m vix.evolve_eval '<json spec>').

Copies a snapshot of the company into a throwaway folder, applies a candidate harness, fixes the random seed,
switches off model calls, applies a scenario, and runs the company forward. The founder is simulated as following
Helm's recommendation on every decision. Prints one JSON line with the reward and the guard metrics."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def main():
    spec = json.loads(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix="vix-evolve-"))
    (tmp / "data").mkdir()
    src = Path(spec["snapshot"])
    for name in ("vix.db", "warehouse.db"):
        if (src / name).exists():
            with sqlite3.connect(src / name) as s, sqlite3.connect(tmp / "data" / name) as d:
                s.backup(d)
    import random
    random.seed(f"global-{spec['seed']}")
    os.environ.update({"VIX_HOME": str(tmp), "VIX_HARNESS": json.dumps(spec.get("harness") or {}), "VIX_SIM_SEED": str(spec["seed"]),
                       "VIX_NO_LLM": "1", "VIX_NEO4J": "off", "VIX_SANDBOX": "1"})
    if spec.get("placebo"):
        os.environ["VIX_PLACEBO"] = "sales"
    if spec.get("scenario") == "demand_slump":
        os.environ["VIX_DEMAND_SCALE"] = "0.6"
    try:
        out = run(spec)
    except Exception as e:  # noqa: BLE001
        import traceback
        out = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(out, default=str))


def run(spec):
    from . import db, fin, market, products, trust, world  # noqa: F401  (after the environment is set)
    from .company import engine
    from .company.playbooks import _recommend
    db.init()
    world.seed()
    engine.seed_v5_schedules()
    sc = spec.get("scenario", "base")
    if sc == "price_war":
        for c in db.rows("select * from mkt_competitor"):
            pres = json.loads(c["presence"])
            for m in pres:
                pres[m]["price"] = round(pres[m]["price"] * 0.9, 3)
            db.execute("update mkt_competitor set presence=?, aggressiveness=min(1.0, aggressiveness+0.3) where id=?", (json.dumps(pres), c["id"]))
    elif sc == "power_shock":
        idx = db.setting("power_index") or {}
        db.set_setting("power_index", {m["code"]: round(idx.get(m["code"], 1.0) * 1.3, 4) for m in db.rows("select code from metros")})
    start = measure()
    start_date = products.today()
    if spec.get("placebo"):
        os.environ["VIX_PLACEBO_UNTIL"] = (products.today_d() + __import__("datetime").timedelta(days=1)).isoformat()
    days = int(spec.get("days", 30))
    decided = {"approve": 0, "reject": 0}
    for _ in range(max(1, days // 10)):
        r = engine.run_now("exec_operating_cycle", {"days": 10})
        if r["status"] != "done":
            return {"ok": False, "error": (r.get("log") or "")[-800:]}
        for d in engine.decisions("open"):
            v, _ = _recommend(d)
            if v in ("approve", "reject"):
                try:
                    engine.decide(d["id"], v == "approve", "sandbox founder follows Helm")
                    decided[v] += 1
                except Exception:  # noqa: BLE001
                    pass
    end = measure()
    writeoffs = db.scalar("select coalesce(sum(amount),0) from fin_gl where account='bad_debt' and sim_date>?", (start_date,)) or 0
    projects = 0.0
    for p in db.rows("select budget_usd, share, irr from dev_project where created>?", (start_date,)):
        irr = p["irr"] if p["irr"] is not None else 0.0
        projects += (p["budget_usd"] or 0) * (p["share"] or 1) * max(-1.0, (irr - 0.08) / 0.08) * 0.5
    capex = db.scalar("select coalesce(sum(amount),0) from fin_gl where account='ppe' and ref like 'decision:%' and sim_date>?", (start_date,)) or 0
    reward = (24 * (end["ebitda_rr"] - start["ebitda_rr"]) - writeoffs + projects - 0.5 * capex) / 1e6
    return {"ok": True, "reward": round(reward, 4), "start": start, "end": end, "writeoffs": round(writeoffs), "project_value": round(projects),
            "capex": round(capex), "decided": decided, "days": days, "scenario": sc, "seed": spec["seed"]}


def measure():
    from . import db, fin, market, products
    rr = fin.run_rate()
    ebitda = rr["revenue"]["total"] - rr["power_cost"] - rr["site_opex"] - rr["tech_labour"] - rr["net_support"] - rr["sga"] - rr["sla_credits"]
    last = fin.closed_periods(1)
    nk = db.setting("net_kpis") or {}
    try:
        nps = market.nps(60)["nps"]
    except Exception:  # noqa: BLE001
        nps = None
    return {"mrr": round(rr["revenue"]["total"]), "ebitda_rr": round(ebitda), "nps": nps, "dso": fin.ar_aging()["dso_days"],
            "leverage": last[0]["metrics"]["net_debt_to_ebitda"] if last else None,
            "net_retention": products.retention(90)["net_retention"], "sla": products.sla_window(30)["sla_pct"],
            "availability": nk.get("availability_pct"), "readiness": (db.setting("compliance_readiness") or {}).get("score"),
            "cash": round(fin.balances()["cash"]), "credit_holds": fin.ar_aging()["on_hold"]}


if __name__ == "__main__":
    main()
