"""Network operations agents: NOC (Sentinel), Change Management (Tempo), Capacity Planning (Gauge),
and the Chief Network Officer (Meridian). They only see the network through NetworkControl, so the
same logic runs on simulated gear today and real gear after go-live."""
from __future__ import annotations

import datetime as dt
import json
import random
import time

from .. import db, netdrivers, netinfra, products
from ..netdrivers import NetworkControl, Unreachable
from .engine import decision_add, decisions, playbook
from .playbooks import table, usd

SLA_TARGET = 0.9999          # monthly availability per service; 4.3 minutes of allowed downtime
CREDIT_TIERS = [(60, 0.10), (240, 0.25), (10 ** 9, 0.50)]   # outage minutes -> share of the service's MRR
RX_WARN, RX_DEAD = -10.0, -14.0
CAPEX = {"leaf_pair": 220_000, "ix_switch": 350_000, "backbone_400g": 1_400_000}
SUPPORT_PER_DEVICE_MONTH = 900.0


def credit_for(minutes: float, mrr: float) -> float:
    allowed = 30 * 24 * 60 * (1 - SLA_TARGET)
    if minutes <= allowed:
        return 0.0
    for limit, share in CREDIT_TIERS:
        if minutes <= limit:
            return round(mrr * share, 2)
    return 0.0


def _event_for(target):
    return db.one("select * from net_event where target=? and status='open' order by id", (target,))


def _record_incident(day, target, cause, diagnosis, action, start, detected, restored, impacted, summary, severity=None):
    minutes = max(0.0, restored - start)
    credits = 0.0
    rows = []
    for s in impacted:
        c = credit_for(minutes, s["mrr"])
        credits += c
        rows.append((s["tenant"], s["kind"], s["id"], round(minutes, 1), round(s["mrr"], 2), c))
    sev = severity or ("sev1" if len({r[0] for r in rows}) >= 10 else "sev2" if rows else "sev3")
    iid = db.execute("insert into net_incident(sim_date,target,cause,diagnosis,action,severity,start_min,detected_min,"
                     "restored_min,services_down,customers,outage_minutes,credits_usd,status,summary)"
                     " values(?,?,?,?,?,?,?,?,?,?,?,?,?,'resolved',?)",
                     (day, target, cause, diagnosis, action, sev, round(start, 1), round(detected, 1), round(restored, 1),
                      len(rows), len({r[0] for r in rows}), round(minutes * (1 if rows else 0), 1), round(credits, 2), summary))
    with db.conn() as c:
        c.executemany("insert into net_impact values(?,?,?,?,?,?,?)", [(iid,) + r for r in rows])
    return iid, credits


# ---------------------------------------------------------------- NOC


@playbook("noc_watch", "NOC watch", "noc",
          "Sentinel polls every device through the driver layer (facts, interfaces, environment, optics, BGP), "
          "diagnoses faults, remediates (reboot, RMA, optic or PSU swap, reroute), measures customer impact and "
          "SLA credits, and pages a human for live gear.")
def noc_watch(ctx):
    rnd = products.rng("noc")
    ctl = NetworkControl("Sentinel")
    day = products.today()
    poll = float(db.setting("net_poll_minutes", 5) or 5)
    incidents, proactive, live_pages, polled, unreachable = [], [], [], 0, 0
    for d in db.rows("select * from net_device order by hostname"):
        polled += 1
        host = d["hostname"]
        try:
            data = ctl.poll(host, ["get_facts", "get_interfaces", "get_environment", "get_optics"])
        except Unreachable as e:
            unreachable += 1
            data = None
            err = str(e)
        except netdrivers.DriverError as e:
            unreachable += 1
            data, err = None, str(e)
        ev = _event_for(host)
        start = ev["start_min"] if ev else 1440 - poll
        detected = start + rnd.uniform(1, poll)
        if data is None:
            if d["mode"] != "sim":
                live_pages.append({"device": host, "issue": "management unreachable", "action": "paged on-call engineer", "detail": err[:90]})
                continue
            # unreachable: software crash is most likely; try a console reboot first
            base = {(s["kind"], s["id"]) for s in netinfra.services_affected(set(), set())}
            impacted = [s for s in netinfra.services_affected({host}, set()) if (s["kind"], s["id"]) not in base]
            r = ctl.reboot(host, rnd)
            t = detected + r["minutes"]
            action = "console reboot"
            if not r["fixed"]:
                r2 = ctl.field_work(host, "rma_swap", rnd)
                t += r2["minutes"]
                action = "console reboot (no recovery), then RMA swap"
            iid, cr = _record_incident(day, host, ev["cause"] if ev else "unknown", "unreachable on management; suspected software crash",
                                       action, start, detected, t, impacted, f"{host} unreachable; {action}")
            incidents.append({"incident": iid, "device": host, "diagnosis": "unreachable", "action": action,
                              "minutes": round(t - start), "customers": len({s['tenant'] for s in impacted}), "credits": cr})
            if ev:
                db.execute("update net_event set status='resolved', incident=?, restore_min=? where id=?", (iid, t, ev["id"]))
            continue
        env, ifs, optics = data["get_environment"], data["get_interfaces"], data["get_optics"]
        lc_crit = any(v.get("is_critical") for v in env.get("temperature", {}).values())
        psu_bad = any(not v.get("status", True) for v in env.get("power", {}).values())
        used = {n: i for n, i in ifs.items() if i.get("description")}
        down_used = [n for n, i in used.items() if i["is_enabled"] and not i["is_up"]]
        if d["mode"] != "sim":
            issues = []
            if lc_crit:
                issues.append("hardware alarm")
            if psu_bad:
                issues.append("PSU failure")
            for n in down_used:
                rx = optics.get(n, {}).get("physical_channels", {}).get("channel", [{}])[0].get("state", {}).get("input_power", {}).get("instant")
                issues.append(f"{n} down" + (f" (rx {rx} dBm: dark optic)" if rx is not None and rx < RX_DEAD else ""))
            if issues:
                live_pages.append({"device": host, "issue": "; ".join(issues), "action": "paged on-call engineer, work order raised", "detail": ""})
            continue
        if lc_crit and len(down_used) >= max(1, len(used) // 2):
            base = {(s["kind"], s["id"]) for s in netinfra.services_affected(set(), set())}
            impacted = [s for s in netinfra.services_affected({host}, set()) if (s["kind"], s["id"]) not in base]
            r = ctl.field_work(host, "rma_swap", rnd)
            t = detected + r["minutes"]
            iid, cr = _record_incident(day, host, "linecard", "hardware alarm on line card, data plane down", "RMA swap",
                                       start, detected, t, impacted, f"{host} line card failure; swapped from spares")
            incidents.append({"incident": iid, "device": host, "diagnosis": "line card", "action": "RMA swap",
                              "minutes": round(t - start), "customers": len({s['tenant'] for s in impacted}), "credits": cr})
            if ev:
                db.execute("update net_event set status='resolved', incident=?, restore_min=? where id=?", (iid, t, ev["id"]))
            continue
        if psu_bad:
            r = ctl.field_work(host, "replace_psu", rnd)
            proactive.append({"device": host, "issue": "PSU failed (redundant supply carrying load)", "action": "replaced PSU"})
            if ev:
                db.execute("update net_event set status='resolved', restore_min=? where id=?", (detected + r["minutes"], ev["id"]))
        for n, opt in optics.items():
            rx = opt["physical_channels"]["channel"][0]["state"]["input_power"]["instant"]
            target = f"{host}:{n}"
            if n in down_used and rx < RX_DEAD:
                ev2 = _event_for(target)
                s0 = ev2["start_min"] if ev2 else 1440 - poll
                det2 = s0 + rnd.uniform(1, poll)
                desc = used[n]["description"]
                impacted = _optic_impact(host, n, desc)
                r = ctl.field_work(target, "replace_optic", rnd)
                t = det2 + r["minutes"]
                iid, cr = _record_incident(day, target, "optic", f"no light (rx {rx} dBm)", "replace optic", s0, det2, t, impacted,
                                           f"dark optic on {target} ({desc})")
                incidents.append({"incident": iid, "device": target, "diagnosis": "dark optic", "action": "replace optic",
                                  "minutes": round(t - s0), "customers": len({s['tenant'] for s in impacted}), "credits": cr})
                if ev2:
                    db.execute("update net_event set status='resolved', incident=?, restore_min=? where id=?", (iid, t, ev2["id"]))
            elif rx < RX_WARN:
                ctl.field_work(target, "replace_optic", rnd)
                proactive.append({"device": target, "issue": f"optic degrading (rx {rx} dBm)", "action": "replaced before failure"})
    # backbone: core routers report BGP sessions to other metros
    for l in db.rows("select * from net_link where status!='up'"):
        ev = _event_for(l["id"])
        if not ev:
            continue
        start = ev["start_min"]
        detected = start + rnd.uniform(1, poll)
        base = {(s["kind"], s["id"]) for s in netinfra.services_affected(set(), set())}
        impacted = [s for s in netinfra.services_affected(set(), {l["id"]}) if (s["kind"], s["id"]) not in base]
        reroute = "traffic rerouted over diverse path" if not impacted else "no diverse path for some circuits"
        repair = ctl.field_work(l["id"], "carrier_repair", rnd)
        restored = detected + (2 if not impacted else repair["minutes"])
        iid, cr = _record_incident(day, l["id"], "fiber_cut", f"BGP sessions down on {l['id']} ({l['km']:.0f} km)",
                                   f"{reroute}; carrier dispatched", start, detected, restored, impacted,
                                   f"fiber cut on {l['id']}; {reroute}", severity="sev2" if impacted else "sev3")
        incidents.append({"incident": iid, "device": l["id"], "diagnosis": "fiber cut", "action": reroute,
                          "minutes": round(restored - start), "customers": len({s['tenant'] for s in impacted}), "credits": cr})
        db.execute("update net_event set status='resolved', incident=?, restore_min=? where id=?", (iid, restored, ev["id"]))
    credits = round(sum(i["credits"] for i in incidents), 2)
    ctx.log(f"polled {polled}, {len(incidents)} incidents, {len(proactive)} proactive fixes, {len(live_pages)} live pages")
    md = (f"Simulated day **{day}**: polled **{polled} devices** through the driver layer ({unreachable} unreachable). "
          f"**{len(incidents)} incidents**, {len(proactive)} problems fixed before they hurt anyone, "
          f"{len(live_pages)} pages for live gear. SLA credits owed today: **{usd(credits)}**.\n\n"
          "## Incidents\n\n" + table(incidents, ["incident", "device", "diagnosis", "action", "minutes", "customers", "credits"]) +
          "\n## Fixed before impact\n\n" + table(proactive, ["device", "issue", "action"]) +
          ("\n## Live devices (human follow-up)\n\n" + table(live_pages, ["device", "issue", "action", "detail"]) if live_pages else "") +
          f"\n_SLA: {SLA_TARGET:.2%} monthly per service; credits 10% of the service's MRR up to 1 h down, 25% to 4 h, 50% beyond._\n")
    return {"day": day, "polled": polled, "incidents": len(incidents), "proactive": len(proactive), "live_pages": len(live_pages),
            "credits_usd": credits, "customers_hit": sum(i["customers"] for i in incidents)}, md


def _optic_impact(host, ifname, desc):
    """Services behind one dark customer or exchange port (others on the device are fine)."""
    if desc.startswith("cust:"):
        pid = int(desc.split("port:")[1])
        p = db.one("select * from ports where id=?", (pid,))
        if not p:
            return []
        others = db.rows("select * from ports where tenant=? and site=? and id!=? and leaf is not null", (p["tenant"], p["site"], pid))
        if others:
            return []
        from .. import world
        out = [{"tenant": p["tenant"], "kind": "port", "id": pid, "mrr": world.PRICES["port_month"].get(p["speed_g"], 800)}]
        for v in db.rows("select * from virtual_circuits where a_port=? or z_port=?", (pid, pid)):
            out.append({"tenant": p["tenant"], "kind": "vc", "id": v["id"], "mrr": v["mbps"] * world.PRICES["vc_per_mbps_month"]})
        return out
    if desc.startswith("ix:"):
        xid = int(desc.split("port:")[1])
        x = db.one("select * from ix_ports where id=?", (xid,))
        if x and not db.one("select id from ix_ports where tenant=? and metro=? and id!=?", (x["tenant"], x["metro"], xid)):
            return [{"tenant": x["tenant"], "kind": "ix_port", "id": xid, "mrr": products.PRICES["ix_port"].get(x["speed_g"], 1200)}]
    return []


# ---------------------------------------------------------------- Change management


def _next_window(after: dt.date) -> dt.date:
    d = after
    while d.weekday() not in (1, 3):     # Tuesday / Thursday nights
        d += dt.timedelta(days=1)
    return d


def _target_version(dev):
    cur = netinfra.firmware(dev["vendor"], dev["os_version"])
    today = products.today_d()
    best = None
    certs = {(r["vendor"], r["version"]): r["status"] for r in db.rows("select * from net_cert")} if db.one(
        "select name from sqlite_master where name='net_cert'") else {}
    gated = any(v == dev["vendor"] for v, _ in certs)
    for f in netinfra.released_firmware(dev["vendor"]):
        if gated and certs.get((f["vendor"], f["version"])) != "certified":
            continue
        age = (today - dt.date.fromisoformat(f["released"])).days
        crit_fix = cur and any(c["severity"] in ("critical", "high") for c in cur["cves"])
        soak = 7 if crit_fix else 30
        if age < soak or f["bug_factor"] > 1.0 or any(c["severity"] in ("critical", "high") for c in f["cves"]):
            continue
        if best is None or f["released"] > best["released"]:
            best = f
    if not best or best["version"] == dev["os_version"]:
        return None, None
    if cur and best["released"] <= cur["released"]:
        return None, None
    reason = ("fixes " + ", ".join(c["id"] for c in cur["cves"]) if cur and cur["cves"] else "newer stable release")
    return best, reason


@playbook("change_manage", "Change management", "change",
          "Tempo plans firmware upgrades (critical/high CVEs first, 30-day soak on new releases), staggers HA pairs, "
          "scores blast radius, notifies affected customers five days ahead, asks the founder for risky or live changes, "
          "and executes today's maintenance window through the driver layer.",
          {"max_new": {"type": "int", "default": 12}})
def change_manage(ctx):
    rnd = products.rng("chg")
    ctl = NetworkControl("Tempo")
    today = products.today_d()
    planned = []
    open_devs = {r["device"] for r in db.rows("select device from net_change where status in ('planned','approved','awaiting_approval')")}
    max_new = ctx.p("max_new", 12, int)
    cands = []
    for d in db.rows("select * from net_device where status='up' order by hostname"):
        if d["hostname"] in open_devs:
            continue
        tgt, reason = _target_version(d)
        if not tgt:
            continue
        cur = netinfra.firmware(d["vendor"], d["os_version"]) or {"cves": []}
        prio = 0 if any(c["severity"] == "critical" for c in cur["cves"]) else 1 if cur["cves"] else 2
        if db.setting("change_freeze", False) and prio > 1:
            continue
        cands.append((prio, d, tgt, reason))
    cands.sort(key=lambda x: (x[0], x[1]["hostname"]))
    windows_used = {}
    for r in db.rows("select device, window_date from net_change where status in ('planned','approved','awaiting_approval')"):
        windows_used.setdefault(r["window_date"], set()).add(r["device"])
    for prio, d, tgt, reason in cands[:max_new]:
        w = _next_window(today + dt.timedelta(days=5))
        while d["ha_peer"] and d["ha_peer"] in windows_used.get(w.isoformat(), set()):
            w = _next_window(w + dt.timedelta(days=1))
        br = netinfra.blast_radius(d["hostname"])
        risk = "low" if br["services"] == 0 else "medium" if br["services"] < 15 else "high"
        needs = risk == "high" or d["mode"] != "sim"
        status = "awaiting_approval" if needs else "approved"
        cid = db.execute("insert into net_change(created,device,kind,from_ver,to_ver,window_date,risk,blast,approval,status,notes,notified)"
                         " values(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (today.isoformat(), d["hostname"], "firmware_upgrade", d["os_version"], tgt["version"], w.isoformat(),
                          risk, br["services"], "founder" if needs else "auto", status, reason, br["customers"]))
        windows_used.setdefault(w.isoformat(), set()).add(d["hostname"])
        if needs:
            decision_add(ctx.agent, "net_change", f"Approve firmware upgrade on {d['hostname']} ({d['os_version']} -> {tgt['version']})",
                         {"change": cid, "device": d["hostname"], "window": w.isoformat(), "risk": risk,
                          "reason": f"{reason}; {br['services']} single-homed services, {br['customers']} customers notified"})
        planned.append({"change": cid, "device": d["hostname"], "from": d["os_version"], "to": tgt["version"], "window": w.isoformat(),
                        "risk": risk, "blast": br["services"], "approval": status, "why": reason})
    done = []
    for ch in db.rows("select * from net_change where window_date<=? and status in ('approved','awaiting_approval','planned')",
                      (today.isoformat(),)):
        if ch["status"] != "approved":
            new_w = _next_window(today + dt.timedelta(days=1)).isoformat()
            db.execute("update net_change set window_date=?, notes=notes || ' | postponed: not approved' where id=?", (new_w, ch["id"]))
            done.append({"change": ch["id"], "device": ch["device"], "result": f"postponed to {new_w} (awaiting approval)"})
            continue
        dev = db.one("select * from net_device where hostname=?", (ch["device"],))
        peer = db.one("select status from net_device where hostname=?", (dev["ha_peer"],)) if dev and dev["ha_peer"] else None
        if not dev or dev["status"] != "up" or (peer and peer["status"] != "up"):
            new_w = _next_window(today + dt.timedelta(days=1)).isoformat()
            db.execute("update net_change set window_date=? where id=?", (new_w, ch["id"]))
            done.append({"change": ch["id"], "device": ch["device"], "result": f"precheck failed (device or HA peer not healthy); moved to {new_w}"})
            continue
        if ch["kind"] == "config":
            try:
                r = ctl.apply_config(ch["device"], ch["notes"] or "", ch["id"])
                db.execute("update net_change set status=?, result=? where id=?",
                           ("done" if r["ok"] else "rolled_back", "config applied" if r["ok"] else r.get("note"), ch["id"]))
                done.append({"change": ch["id"], "device": ch["device"], "result": "done (golden config applied, hitless)"})
            except netdrivers.NotAllowed as e:
                done.append({"change": ch["id"], "device": ch["device"], "result": f"blocked by guardrail: {e}"})
            continue
        try:
            r = ctl.upgrade_firmware(ch["device"], ch["to_ver"], ch["id"], rnd)
        except netdrivers.NotAllowed as e:
            db.execute("update net_change set result=? where id=?", (str(e), ch["id"]))
            done.append({"change": ch["id"], "device": ch["device"], "result": f"blocked by guardrail: {e}"})
            continue
        if r.get("manual"):
            db.execute("update net_change set status='needs_engineer', result=? where id=?", (r["note"], ch["id"]))
            done.append({"change": ch["id"], "device": ch["device"], "result": r["note"]})
            continue
        status = "done" if r["ok"] else "rolled_back"
        db.execute("update net_change set status=?, result=? where id=?",
                   (status, f"{'upgraded' if r['ok'] else r.get('note')} in {r['minutes']:.0f} min", ch["id"]))
        br_rows = netinfra.services_affected({ch["device"]}, set())
        base = {(s["kind"], s["id"]) for s in netinfra.services_affected(set(), set())}
        planned_hit = [s for s in br_rows if (s["kind"], s["id"]) not in base]
        with db.conn() as c:
            c.executemany("insert into net_impact values(?,?,?,?,?,?,?)",
                          [(-ch["id"], s["tenant"], s["kind"], s["id"], round(r["minutes"], 1), round(s["mrr"], 2), 0.0) for s in planned_hit])
        done.append({"change": ch["id"], "device": ch["device"], "result": f"{status} ({r['minutes']:.0f} min, "
                                                                      f"{len(planned_hit)} services in a notified window)"})
    ctx.log(f"planned {len(planned)}, executed {len(done)}")
    md = (f"Simulated day **{products.today()}**. Planned **{len(planned)}** upgrades; worked **{len(done)}** changes in today's window.\n\n"
          "## Executed today\n\n" + table(done, ["change", "device", "result"]) +
          "\n## Newly planned\n\n" + table(planned, ["change", "device", "from", "to", "window", "risk", "blast", "approval", "why"]) +
          "\n_Policy: critical/high CVE fixes after 7 days of soak, other releases after 30; HA peers never share a window; "
          "customers on single-homed services are notified five days ahead, so planned work is outside SLA credits._\n")
    return {"planned": len(planned), "executed": sum(1 for x in done if "upgraded" in x["result"] or x["result"].startswith("done")),
            "rolled_back": sum(1 for x in done if x["result"].startswith("rolled_back")), "worked": len(done)}, md


# ---------------------------------------------------------------- Capacity


@playbook("capacity_plan", "Network capacity plan", "capacity",
          "Gauge snapshots leaf ports, exchange ports and backbone peaks, forecasts when each crosses 80%, "
          "and files capex decisions (leaf pairs, exchange switches, 400G backbone upgrades).")
def capacity_plan(ctx):
    netinfra.assign_ports()            # place today's new ports first: only ports that truly don't fit count as waiting
    rows = netinfra.capacity_snapshot()
    filed, hot = [], []
    for r in rows:
        days = netinfra.forecast_days(r["kind"], r["ref"])
        r["days_to_80"] = days
        waiting = db.scalar("select count(*) from ports where site=? and leaf is null", (r["ref"],)) if r["kind"] == "leaf_ports" else 0
        urgent = r["util"] >= 0.8 or (days is not None and days <= 90) or waiting
        if not urgent:
            continue
        hot.append(r)
        if r["kind"] == "leaf_ports":
            kind, cost, title = "leaf_pair", CAPEX["leaf_pair"], f"Add a leaf switch pair at {r['ref']}"
        elif r["kind"] == "ix_ports":
            kind, cost, title = "ix_switch", CAPEX["ix_switch"], f"Add an exchange switch in {r['ref']}"
        else:
            kind, cost, title = "backbone_400g", CAPEX["backbone_400g"], f"Add 400G to backbone {r['ref']}"
        did = decision_add(ctx.agent, "network_capex", title,
                           {"action": kind, "ref": r["ref"], "capex_usd": cost, "util": r["util"], "days_to_80": days,
                            "waiting_ports": waiting,
                            "reason": f"{r['kind'].replace('_', ' ')} {r['ref']} at {r['util']:.0%}"
                                      + (f", {waiting} ports waiting" if waiting else "")
                                      + (f", 80% in ~{days} days" if days else "")})
        filed.append({"decision": did, "what": title, "capex": usd(cost)})
    ctx.log(f"{len(hot)} hot spots, {len(filed)} decisions")
    top = sorted(rows, key=lambda r: -r["util"])[:15]
    md = (f"{len(rows)} capacity pools measured; **{len(hot)} need action**.\n\n## Fullest pools\n\n" +
          table(top, ["kind", "ref", "used", "total", "util", "days_to_80"]) +
          "\n## Decisions filed\n\n" + table(filed, ["decision", "what", "capex"]) +
          "\n_Backbone figures are estimated peak = 1.5 x average (base load plus 60% of booked virtual-circuit bandwidth)._\n")
    return {"pools": len(rows), "hot": len(hot), "filed": len(filed)}, md


# ---------------------------------------------------------------- CNO


def network_kpis(days: int = 30) -> dict:
    since = (products.today_d() - dt.timedelta(days=days)).isoformat()
    inc = db.rows("select * from net_incident where sim_date>?", (since,))
    services = (db.scalar("select count(*) from ports") + db.scalar("select count(*) from virtual_circuits") +
                db.scalar("select count(*) from cloud_connections") + db.scalar("select count(*) from ix_ports") +
                db.scalar("select count(*) from edge_devices"))
    down_min = db.scalar("select coalesce(sum(i.minutes),0) from net_impact i join net_incident n on n.id=i.incident"
                         " where n.sim_date>?", (since,)) or 0
    avail = 1 - down_min / (services * days * 1440) if services else 1
    ch = db.rows("select status, count(*) n from net_change where window_date>? group by status", (since,))
    chd = {c["status"]: c["n"] for c in ch}
    exposed = db.rows("select d.vendor, d.os_version, f.cves, count(*) n from net_device d join net_firmware f on f.vendor=d.vendor"
                      " and f.version=d.os_version group by d.vendor, d.os_version")
    crit = sum(e["n"] for e in exposed if any(c["severity"] in ("critical", "high") for c in json.loads(e["cves"] or "[]")))
    return {
        "window_days": days, "incidents": len(inc),
        "customer_impacting": sum(1 for i in inc if i["services_down"]),
        "availability_pct": round(100 * avail, 4),
        "mttd_min": round(sum(i["detected_min"] - i["start_min"] for i in inc) / len(inc), 1) if inc else None,
        "mttr_min": round(sum(i["restored_min"] - i["start_min"] for i in inc) / len(inc), 1) if inc else None,
        "credits_usd": round(sum(i["credits_usd"] for i in inc), 2),
        "by_cause": {c: sum(1 for i in inc if i["cause"] == c) for c in {i["cause"] for i in inc}},
        "changes_done": chd.get("done", 0), "changes_rolled_back": chd.get("rolled_back", 0),
        "changes_waiting": db.scalar("select count(*) from net_change where status in ('approved','awaiting_approval')"),
        "devices": db.scalar("select count(*) from net_device"),
        "devices_down": db.scalar("select count(*) from net_device where status in ('down','unreachable')"),
        "devices_critical_cve": crit,
        "live_devices": db.scalar("select count(*) from net_device where mode!='sim'"),
        "live_writes": bool(db.setting("net_live_writes", False)),
    }


@playbook("cno_network_review", "Network review", "cno",
          "Chief Network Officer: delegate the capacity plan, then report availability, incidents, detection and "
          "repair times, SLA credits, change success, CVE exposure and live readiness to the CEO.")
def cno_network_review(ctx):
    cap = ctx.child("capacity_plan")
    sre = ctx.child("sre_slo_review")
    ai = ctx.child("aiops_predict")
    arch = ctx.child("arch_review")
    k7, k30 = network_kpis(7), network_kpis(30)
    worst = db.rows("select sim_date, target, cause, action, customers, round(restored_min-start_min) minutes, credits_usd"
                    " from net_incident where sim_date>? order by credits_usd desc, customers desc limit 8",
                    ((products.today_d() - dt.timedelta(days=30)).isoformat(),))
    ready = db.setting("net_readiness") or {}
    escalate = []
    if k7["availability_pct"] < 99.99:
        escalate.append(f"7-day availability {k7['availability_pct']}% is below the 99.99% SLA")
    if k30["devices_critical_cve"]:
        escalate.append(f"{k30['devices_critical_cve']} devices still run firmware with critical/high CVEs")
    if k30["changes_waiting"]:
        escalate.append(f"{k30['changes_waiting']} changes waiting (some need founder approval)")
    ss = sre["summary"] or {}
    if ss.get("freeze"):
        escalate.append(f"error budget exhausted ({ss.get('budget_burn_pct')}%): change freeze on")
    if (ss.get("slos_missed") or 0) > 0:
        escalate.append(f"{ss['slos_missed']} SLO(s) missed")
    db.set_setting("net_kpis", {**k30, "as_of": products.today()})
    ctx.log(f"availability 30d {k30['availability_pct']}%, {k30['incidents']} incidents")
    md = (f"Network health on **{products.today()}** (simulated).\n\n## KPIs\n\n" +
          table([{"window": "7 days", **{k: v for k, v in k7.items() if k not in ('by_cause', 'window_days')}},
                 {"window": "30 days", **{k: v for k, v in k30.items() if k not in ('by_cause', 'window_days')}}],
                ["window", "incidents", "customer_impacting", "availability_pct", "mttd_min", "mttr_min", "credits_usd",
                 "changes_done", "changes_rolled_back", "devices_critical_cve"]) +
          f"\nIncidents by cause (30 days): {k30['by_cause'] or 'none'}.\n\n## Costliest incidents (30 days)\n\n" +
          table(worst, ["sim_date", "target", "cause", "action", "customers", "minutes", "credits_usd"]) +
          f"\n## Capacity\n\nGauge: {(cap['summary'] or {}).get('hot', 0)} hot spots, {(cap['summary'] or {}).get('filed', 0)} "
          f"decisions filed. Report: {cap['report']}\n\n## Team reports\n\n"
          f"- Beacon (SRE): budget burn {(sre['summary'] or {}).get('budget_burn_pct')}%, SLOs missed {(sre['summary'] or {}).get('slos_missed')}. {sre['report']}\n"
          f"- Sentinel (AIOps): {(ai['summary'] or {}).get('prevented')} failures prevented, {(ai['summary'] or {}).get('spof_devices')} single points of failure. {ai['report']}\n"
          f"- Vantage (architecture): {(arch['summary'] or {}).get('adrs')} ADRs. {arch['report']}\n"
          f"\n## Live readiness\n\n"
          + (f"Level **{ready.get('level')}** as of {ready.get('date')}: {ready.get('summary')}" if ready else
             "Not assessed yet: run net_golive_readiness.") +
          "\n\n## Escalated to the CEO\n\n" + ("".join(f"- {e}\n" for e in escalate) or "- Nothing.\n"))
    return {"availability_7d": k7["availability_pct"], "availability_30d": k30["availability_pct"], "incidents_30d": k30["incidents"],
            "mttr_30d": k30["mttr_min"], "credits_30d": k30["credits_usd"], "critical_cve_devices": k30["devices_critical_cve"],
            "escalations": escalate, "readiness": ready.get("level")}, md


# ---------------------------------------------------------------- go-live readiness


@playbook("net_golive_readiness", "Go-live readiness", "cno",
          "Chief Network Officer: check that inventory, service mapping, drivers, credentials and guardrails are ready "
          "to run real gear; poll every live device read-only and compare it with inventory.")
def net_golive_readiness(ctx):
    checks = []

    def add(name, ok, detail, level="monitor"):
        checks.append({"check": name, "ok": "pass" if ok else "FAIL", "needed for": level, "detail": detail})
    devs = db.rows("select * from net_device")
    missing = [d["hostname"] for d in devs if not (d["site"] and d["role"] and d["mgmt_ip"] and d["platform"])]
    add("Inventory complete (site, role, mgmt address, platform)", not missing, f"{len(devs)} devices; {len(missing)} incomplete")
    unplaced = db.scalar("select count(*) from ports where leaf is null")
    add("Every customer port mapped to a switch", unplaced == 0,
        f"{unplaced} ports waiting for switch capacity (Gauge has filed capex)" if unplaced else "all mapped", "accuracy")
    try:
        import napalm  # noqa: F401
        nap = True
    except ImportError:
        nap = False
    live = [d for d in devs if d["mode"] != "sim"]
    add("NAPALM installed", nap, "pip install napalm in the vix venv" if not nap else "ok")
    reach, drift, secret_bad = [], [], []
    ctl = NetworkControl("Meridian")
    for d in live:
        try:
            netdrivers.resolve_secret(d["secret_ref"])
        except netdrivers.DriverError as e:
            secret_bad.append(f"{d['hostname']}: {e}")
            continue
        try:
            r = ctl.poll(d["hostname"], ["get_facts", "get_interfaces"])
            reach.append(d["hostname"])
            f = r["get_facts"]
            db.execute("update net_device set os_version=?, model=?, serial=? where hostname=?",
                       (f.get("os_version", "unknown"), f.get("model", d["model"]), f.get("serial_number", ""), d["hostname"]))
            if f.get("hostname") and f["hostname"].split(".")[0] != d["hostname"].split(".")[0]:
                drift.append(f"{d['hostname']} reports hostname {f['hostname']}")
        except netdrivers.DriverError as e:
            drift.append(f"{d['hostname']}: {str(e)[:100]}")
    add("Live devices onboarded", bool(live), f"{len(live)} live device(s)" if live else "none yet: import with net_import_devices")
    add("Credentials resolve (by reference, never stored)", not secret_bad, "; ".join(secret_bad) or f"{len(live)} ok")
    add("Live devices reachable read-only", live and len(reach) == len(live), f"{len(reach)}/{len(live)} answered get_facts")
    add("No drift between live devices and inventory", not drift, "; ".join(drift[:5]) or "none")
    add("Kill switch default off", not db.setting("net_live_writes", False),
        "writes disabled" if not db.setting("net_live_writes", False) else "writes ENABLED by founder decision", "changes")
    add("Commit-confirm with automatic rollback", True, "apply_config commits with revert_in=300 s and confirms only after post-checks",
        "changes")
    add("Changes need approval + window", True, "live changes always require founder approval and today's window", "changes")
    add("Audit log", True, f"{db.scalar('select count(*) from net_audit')} entries", "changes")
    mon = all(c["ok"] == "pass" for c in checks if c["needed for"] == "monitor" and not c["check"].startswith("No drift"))
    level = "simulation only" if not live else ("read-only monitoring" if mon else "not ready")
    if level == "read-only monitoring" and db.setting("net_live_writes", False):
        level = "supervised changes"
    summary = {"simulation only": "every device is simulated; import real devices to start read-only monitoring",
               "read-only monitoring": "real devices are polled by the NOC; changes stay manual until the founder enables writes",
               "supervised changes": "approved changes in windows run with commit-confirm and rollback",
               "not ready": "fix the failing checks before polling real gear"}[level]
    since = db.setting("net_monitoring_since")
    if level in ("read-only monitoring", "supervised changes"):
        if not since:
            db.set_setting("net_monitoring_since", products.today())
    elif since:
        db.set_setting("net_monitoring_since", None)
    db.set_setting("net_readiness", {"level": level, "date": products.today(), "summary": summary,
                                     "monitoring_since": db.setting("net_monitoring_since")})
    ctx.log(f"readiness: {level}")
    md = (f"Readiness level: **{level}**. {summary}.\n\n" + table(checks, ["check", "ok", "needed for", "detail"]) +
          "\n## How to go live\n\n"
          "1. `pip install napalm` in the vix venv.\n"
          "2. Store credentials outside vix: environment variables or a macOS Keychain item.\n"
          "3. Import devices with `net_import_devices` (hostname, mgmt_ip, platform: ios | iosxr | nxos_ssh | junos | eos, site, "
          "role, secret_ref). They arrive read-only and the NOC starts polling them.\n"
          "4. Run this check again. When it says read-only monitoring, the NOC is watching real gear.\n"
          "5. Only if you want automated changes: approve a `net_live_writes` decision, then per-device `net_device_mode` "
          "decisions. Every change still needs approval, a window, commit-confirm and post-checks.\n")
    return {"level": level, "live_devices": len(live), "reachable": len(reach),
            "failing": [c["check"] for c in checks if c["ok"] != "pass"]}, md
