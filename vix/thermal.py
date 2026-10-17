"""Power and cooling physics for a data hall: a lumped thermal model, simple
chiller efficiency against outside air, and failure drills. Approximate on
purpose, but every constant is written down so the numbers can be argued with."""
from __future__ import annotations

import math
import random

from . import db

SETPOINT_C = 22.0          # cold-aisle supply target
ALLOWABLE_MAX_C = 32.0     # ASHRAE A1 allowable inlet upper bound
RECOMMENDED_MAX_C = 27.0   # ASHRAE recommended upper bound
UPS_LOSS = 0.04            # fraction of IT load lost in UPS/distribution
MISC = 0.015               # lighting, security, offices
FAN_FRACTION = 0.03        # CRAH fan power as fraction of heat moved
LIQUID_FRACTION = 0.85     # share of heat a direct-to-chip loop captures; the rest goes to air
PUMP_FRACTION = 0.015      # coolant pumps as fraction of liquid heat moved
KJ_PER_K_PER_KW = 60.0   # lumped heat capacity per kW of IT: total cooling loss gives about +10 C in 10 min


def limits(h: dict) -> dict:
    """Air halls track cold-aisle inlet temperature. Liquid (AI) halls track coolant return
    temperature: warm-water loops supply ~32 C, GPUs throttle near 45 C return, and the
    loop has far less thermal mass than a room of air, so failures move in seconds."""
    if h.get("cooling_type") == "liquid":
        return {"setpoint": 32.0, "recommended": 40.0, "allowable": 45.0, "kj_per_k_per_kw": 6.0,
                "unit": "CDU", "measure": "coolant return", "breach_action": "GPUs throttle; shift jobs, open bypass"}
    return {"setpoint": SETPOINT_C, "recommended": RECOMMENDED_MAX_C, "allowable": ALLOWABLE_MAX_C,
            "kj_per_k_per_kw": KJ_PER_K_PER_KW, "unit": "CRAH", "measure": "inlet",
            "breach_action": "open containment, start load shedding"}


def ambient_c(m: dict, hour: float) -> float:
    """Outside air: daily sine around the metro mean, peak at 15:00."""
    return m["ambient_mean_c"] + m["ambient_swing_c"] * math.sin((hour - 9) / 24 * 2 * math.pi)


def cop(ambient: float) -> float:
    """Coefficient of performance: free cooling below 18 C, chiller COP falling as it gets hot."""
    if ambient < 18:
        return 12.0
    return max(2.5, 7.0 - 0.15 * (ambient - 18))


def hall_power(h: dict, ambient: float) -> dict:
    it = h["it_load_kw"]
    if it <= 0:
        return {"it_kw": 0, "cooling_kw": 0, "facility_kw": 0, "pue": None}
    c = cop(ambient)
    if h.get("cooling_type") == "liquid":
        # warm water rejects heat through dry coolers up to ~30 C outside, trim chillers above
        lc = 30.0 if ambient < 30 else 10.0
        liquid = it * LIQUID_FRACTION
        air = it - liquid
        cooling = liquid / lc + liquid * PUMP_FRACTION + air / c + air * FAN_FRACTION
    else:
        cooling = it / c + it * FAN_FRACTION
    facility = it + cooling + it * UPS_LOSS + it * MISC
    return {"it_kw": round(it, 1), "cooling_kw": round(cooling, 1), "facility_kw": round(facility, 1),
            "pue": round(facility / it, 3), "cop": round(c, 2)}


def steady_inlet(h: dict) -> float:
    lim = limits(h)
    avail = (h["crah_units"] - h["crah_down"]) * h["crah_kw_each"]
    if avail >= h["it_load_kw"]:
        # more headroom keeps the aisle (or coolant) closer to setpoint
        ratio = h["it_load_kw"] / avail if avail else 1
        return round(lim["setpoint"] + (4.0 if lim["unit"] == "CDU" else 2.5) * ratio, 2)
    return 99.0


def day_profile(h: dict, m: dict, record: bool = True) -> dict:
    """Simulate 24 hourly steps for one hall and return the energy picture."""
    import time as _t
    hours, it_kwh, fac_kwh, peak = [], 0.0, 0.0, None
    for hr in range(24):
        amb = ambient_c(m, hr)
        p = hall_power(h, amb)
        hours.append({"hour": hr, "ambient_c": round(amb, 1), **p})
        it_kwh += p["it_kw"]
        fac_kwh += p["facility_kw"]
        if peak is None or (p["pue"] or 0) > (peak["pue"] or 0):
            peak = hours[-1]
    pue = round(fac_kwh / it_kwh, 3) if it_kwh else None
    inlet = steady_inlet(h)
    if record and it_kwh:
        db.execute("insert into telemetry(ts,hall,it_kw,facility_kw,pue,inlet_c,ambient_c) values(?,?,?,?,?,?,?)",
                   (_t.time(), h["id"], round(it_kwh / 24, 1), round(fac_kwh / 24, 1), pue, inlet,
                    round(sum(x["ambient_c"] for x in hours) / 24, 1)))
    return {"hall": h["id"], "it_mwh": round(it_kwh / 1000, 2), "facility_mwh": round(fac_kwh / 1000, 2),
            "pue": pue, "peak_hour": peak, "inlet_c": inlet,
            "cooling_headroom_kw": round((h["crah_units"] - h["crah_down"]) * h["crah_kw_each"] - h["it_load_kw"], 1),
            "power_headroom_kw": round(h["utility_kw"] - h["contracted_kw"], 1)}


def drill(h: dict, m: dict, kind: str, minutes: int = 90, units_lost: int = 2,
          repair_min: int = 45, gen_fail_p: float = 0.02, seed: int | None = None) -> dict:
    """Minute-by-minute failure drill.
    kind: 'cooling' (units_lost CRAH units trip, repaired after repair_min),
          'utility' (grid loss: UPS carries the load, generators start in 10-15 s with
                     gen_fail_p chance each fails; cooling stops until generators carry it, 60 s),
          'fiber'   (handled by the network simulator; here only reports no facility impact)."""
    rnd = random.Random(seed)
    lim = limits(h)
    unit = lim["unit"]
    it = h["it_load_kw"]
    mass = max(1.0, it) * lim["kj_per_k_per_kw"]  # kJ per K
    temp = steady_inlet(h)
    timeline, actions = [], []
    breach_min = None
    gens_ok = h["generators"]
    if kind == "utility":
        gens_ok = sum(1 for _ in range(h["generators"]) if rnd.random() > gen_fail_p)
        need = math.ceil((it * 1.6) / 1500)  # each generator ~1.5 MW, cover IT + cooling
        actions.append(f"t+0: utility lost; UPS carries {it:.0f} kW for up to {h['ups_minutes']} min")
        actions.append(f"t+0: {gens_ok}/{h['generators']} generators started (need {need})")
        if gens_ok < need:
            actions.append("generator shortfall: shed non-critical load; page on-call; customer notices sent")
    if kind == "fiber":
        return {"hall": h["id"], "kind": kind, "facility_impact": "none",
                "note": "fiber cuts are simulated in the network study (net_bgp_study with event=fiber_cut)"}
    for t in range(minutes + 1):
        avail_units = h["crah_units"] - h["crah_down"]
        if kind == "cooling":
            avail_units -= units_lost if t < repair_min else 0
            if t == 0:
                actions.append(f"t+0: {units_lost} {unit} units trip; {avail_units} of {h['crah_units']} left")
            if t == repair_min:
                actions.append(f"t+{t}: units restored by maintenance")
        if kind == "utility":
            need = math.ceil((it * 1.6) / 1500)
            on_gen = t >= 1 and gens_ok >= need
            if not on_gen:
                avail_units = 0 if t < 1 else int(avail_units * min(1, gens_ok / max(need, 1)))
        removed = min(it, max(0, avail_units) * h["crah_kw_each"])
        # heat balance: (IT - removed) kW for 60 s into the thermal mass
        temp += (it - removed) * 60 / mass
        # cooling recovers towards setpoint when there is surplus capacity
        if removed >= it and temp > lim["setpoint"] + 2:
            surplus = max(0, avail_units) * h["crah_kw_each"] - it
            temp -= min(temp - (lim["setpoint"] + 2), surplus * 60 / mass)
        if temp > lim["allowable"] and breach_min is None:
            breach_min = t
            actions.append(f"t+{t}: {lim['measure']} {temp:.1f} C passed {lim['allowable']} C; {lim['breach_action']}")
        if kind == "utility" and t == h["ups_minutes"] and gens_ok < math.ceil((it * 1.6) / 1500):
            actions.append(f"t+{t}: UPS exhausted without enough generators: critical load drop")
            timeline.append({"min": t, "inlet_c": round(temp, 2), "outage": True})
            return {"hall": h["id"], "kind": kind, "outage": True, "breach_min": breach_min,
                    "peak_inlet_c": round(max(x["inlet_c"] for x in timeline), 2), "timeline": timeline[::5],
                    "actions": actions}
        timeline.append({"min": t, "inlet_c": round(temp, 2)})
    peak = max(x["inlet_c"] for x in timeline)
    return {"hall": h["id"], "kind": kind, "cooling_type": h.get("cooling_type", "air"), "measure": lim["measure"],
            "outage": False, "breach_min": breach_min,
            "peak_inlet_c": round(peak, 2), "timeline": timeline[::5], "actions": actions,
            "verdict": ("held within recommended range" if peak <= lim["recommended"] else
                        "within allowable range" if peak <= lim["allowable"] else "breached allowable range")}
