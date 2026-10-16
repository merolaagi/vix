"""R&D router lab: a slot-level simulation of one switch output port.

Bursty on/off sources (heavy-tailed Pareto ON periods, like real traffic) feed a FIFO
output queue served at line rate. We sweep buffer sizes and offered loads and measure
loss and queueing delay, then judge which buffer-sizing rule a new switch design
should follow and whether that buffer fits in on-chip SRAM.

Caveat that every report repeats: the traffic is open-loop. Real TCP backs off on loss,
which is why small buffers (the C*RTT/sqrt(n) rule) work better in practice than an
open-loop model suggests."""
from __future__ import annotations

import math

import numpy as np

PKT_BYTES = 1500
ONCHIP_SRAM_MB = 64.0


def arrivals(slots: int, sources: int, load: float, duty: float = 0.2, mean_on: float = 100.0,
             alpha: float = 1.5, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lam = load / sources                  # mean packets per slot per source
    peak = min(1.0, lam / duty)           # packets per slot while ON
    duty = lam / peak
    mean_off = mean_on * (1 - duty) / duty
    xm_on = mean_on * (alpha - 1) / alpha
    xm_off = mean_off * (alpha - 1) / alpha
    times = []
    for _ in range(sources):
        t = rng.uniform(0, mean_on + mean_off)
        while t < slots:
            on = xm_on / rng.random() ** (1 / alpha)
            end = min(slots, t + on)
            times.append(np.arange(t, end, 1 / peak))
            t = end + xm_off / rng.random() ** (1 / alpha)
    if not times:
        return np.zeros(slots, dtype=np.int64)
    return np.bincount(np.concatenate(times).astype(np.int64), minlength=slots)[:slots]


def queue(arr: np.ndarray, buffer_pkts: int) -> dict:
    q = 0
    dropped = 0
    qs = np.empty(len(arr), dtype=np.int64)
    for i, a in enumerate(arr.tolist()):
        q += a
        if q > buffer_pkts:
            dropped += q - buffer_pkts
            q = buffer_pkts
        qs[i] = q
        if q:
            q -= 1
    total = int(arr.sum())
    return {"loss": dropped / total if total else 0.0, "q_mean": float(qs.mean()),
            "q_p99": float(np.percentile(qs, 99)), "q_max": int(qs.max())}


def buffer_rules(link_gbps: float, rtt_ms: float, flows: int) -> dict:
    bdp_pkts = link_gbps * 1e9 * rtt_ms / 1000 / 8 / PKT_BYTES
    return {
        "rule_of_thumb (C x RTT)": int(bdp_pkts),
        "stanford (C x RTT / sqrt n)": int(bdp_pkts / math.sqrt(max(1, flows))),
        "shallow (1 MB)": int(1e6 / PKT_BYTES),
        "tiny (64 KB)": int(64e3 / PKT_BYTES),
    }


def study(link_gbps: float = 100.0, rtt_ms: float = 10.0, flows: int = 400, sources: int = 32,
          loads: list[float] | None = None, slots: int = 200_000, loss_target: float = 0.001,
          ports: int = 32, design_load: float = 0.9, seed: int = 0) -> dict:
    loads = loads or [0.5, 0.7, 0.9, 0.95]
    rules = buffer_rules(link_gbps, rtt_ms, flows)
    slot_us = PKT_BYTES * 8 / (link_gbps * 1e3)      # microseconds to send one packet
    grid = []
    for li, load in enumerate(loads):
        arr = arrivals(slots, sources, load, seed=seed + li)
        for name, b in rules.items():
            r = queue(arr, max(1, b))
            grid.append({"load": load, "rule": name, "buffer_pkts": b,
                         "buffer_mb": round(b * PKT_BYTES / 1e6, 2),
                         "loss_pct": round(100 * r["loss"], 4),
                         "delay_mean_us": round(r["q_mean"] * slot_us, 2),
                         "delay_p99_us": round(r["q_p99"] * slot_us, 2),
                         "q_max_pkts": r["q_max"]})
    # pick the smallest rule meeting the loss target at the design load
    top = design_load if design_load in loads else max(loads)
    ok = [g for g in grid if g["load"] == top and g["loss_pct"] / 100 <= loss_target]
    pick = min(ok, key=lambda g: g["buffer_pkts"]) if ok else max(
        (g for g in grid if g["load"] == top), key=lambda g: g["buffer_pkts"])
    per_port_mb = pick["buffer_mb"]
    chip_mb = per_port_mb * ports
    design = {
        "ports": ports, "port_gbps": link_gbps,
        "switching_tbps": round(ports * link_gbps * 2 / 1000, 2),
        "buffer_rule": pick["rule"], "buffer_per_port_mb": per_port_mb,
        "buffer_total_mb": round(chip_mb, 1),
        "fits_onchip_sram": chip_mb <= ONCHIP_SRAM_MB,
        "memory": "on-chip SRAM" if chip_mb <= ONCHIP_SRAM_MB else "needs off-chip HBM (more power, cost, latency)",
        "est_power_w": round(ports * link_gbps * 0.012 + (0 if chip_mb <= ONCHIP_SRAM_MB else 60) + 80),
        "design_load": top, "meets_loss_target": bool(ok),
    }
    return {"link_gbps": link_gbps, "rtt_ms": rtt_ms, "flows": flows, "sources": sources,
            "slots": slots, "simulated_ms": round(slots * slot_us / 1000, 3), "rules": rules,
            "loss_target_pct": loss_target * 100, "grid": grid, "design": design,
            "caveat": "Open-loop on/off traffic with Pareto bursts; no TCP congestion control. "
                      "Real TCP would make small buffers look better than here."}
