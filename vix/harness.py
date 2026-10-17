"""Agent harnesses: the policy knobs each agent works with (thresholds, discount scales, payout ratios), kept as one
versioned set of values. The agents read them here instead of hard-coding numbers, which is what lets the
self-improvement lab (RRSI-style) propose, test and promote changes. The live harness only changes when the
founder approves an adoption decision. A sandbox evaluation overrides values through VIX_HARNESS."""
from __future__ import annotations

import json
import os

from . import db

COMPONENTS = {
    # name: (agent, department, description, default, min, max, step)
    "dealdesk.discount_scale": ("Margin", "dealdesk", "Multiplier on the base discount the Deal Desk offers", 1.0, 0.4, 1.6, 0.15),
    "dealdesk.gm_floor": ("Margin", "dealdesk", "Lowest gross margin the CFO accepts on a discounted deal", 0.25, 0.15, 0.40, 0.03),
    "collections.hold_days": ("Reckon", "collections", "Days overdue before a credit hold", 45, 30, 75, 5),
    "collections.writeoff_days": ("Reckon", "collections", "Days overdue before write-off and termination", 120, 90, 180, 10),
    "treasury.min_cash_musd": ("Anchor", "treasury", "Cash floor ($M) below which the revolver is drawn", 120, 60, 250, 15),
    "treasury.payout": ("Anchor", "treasury", "Share of AFFO recommended as the quarterly dividend", 0.70, 0.50, 0.90, 0.05),
    "sites.hurdle_irr": ("Scout", "sites", "IRR a new hall or campus must clear", 0.10, 0.07, 0.15, 0.01),
    "power.ppa_max_premium": ("Volt", "power", "Most a PPA may cost above the grid price", 0.05, 0.0, 0.12, 0.02),
    "capacity.capex_util": ("Gauge", "capacity", "Utilisation at which network capex is recommended", 0.80, 0.65, 0.92, 0.03),
    "cx.hire_sla": ("Concierge", "cx", "30-day remote-hands SLA (%) below which hiring is recommended", 93, 88, 97, 1),
    "accounts.escalator_pct": ("Keeper", "accounts", "Annual price escalator written into renewals (%)", 3.0, 1.0, 5.0, 0.5),
    "portal.self_serve_max_mrr": ("Portico", "portal", "Largest monthly order customers may place without sales", 15000, 3000, 40000, 3000),
}


def defaults() -> dict:
    return {k: v[3] for k, v in COMPONENTS.items()}


def live() -> dict:
    h = defaults()
    try:
        h.update((db.setting("harness") or {}).get("values", {}))
    except Exception:  # noqa: BLE001
        pass
    return h


def version() -> int:
    try:
        return int((db.setting("harness") or {}).get("version", 0))
    except Exception:  # noqa: BLE001
        return 0


def _override() -> dict:
    raw = os.environ.get("VIX_HARNESS")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def get(name: str):
    ov = _override()
    if name in ov:
        return ov[name]
    if ov.get("__base__") == "defaults":
        return COMPONENTS[name][3]
    return live().get(name, COMPONENTS[name][3])


def set_live(values: dict, note: str = "") -> dict:
    clean = {k: clamp(k, v) for k, v in values.items() if k in COMPONENTS}
    cur = db.setting("harness") or {"version": 0, "values": {}, "history": []}
    hist = cur.get("history", [])[-30:]
    hist.append({"version": cur.get("version", 0), "values": cur.get("values", {}), "note": note})
    new = {"version": int(cur.get("version", 0)) + 1, "values": {**cur.get("values", {}), **clean}, "history": hist}
    db.set_setting("harness", new)
    return {"version": new["version"], "values": new["values"]}


def clamp(name, v):
    _, _, _, d, lo, hi, step = COMPONENTS[name]
    v = max(lo, min(hi, float(v)))
    if isinstance(d, int) and float(step).is_integer():
        return int(round(v))
    return round(v, 4)


def catalog() -> list[dict]:
    lv = live()
    return [{"component": k, "agent": a, "department": d, "description": desc, "default": dv, "min": lo, "max": hi, "step": st,
             "live": lv.get(k, dv)} for k, (a, d, desc, dv, lo, hi, st) in COMPONENTS.items()]
