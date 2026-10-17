"""Network infrastructure: the source of truth for every device, interface and backbone link,
which customer services ride on which gear, and (for simulated devices only) the physics of
how gear fails and recovers.

Vendors are invented families modeled on real archetypes:
  Corvid Networks  CR-9000  chassis service-provider router  (the big-router archetype)
  Tessell          TX-series merchant-silicon switches         (switch-ASIC archetype)
  Halcyon Optical  HL-800   DWDM transponder
  Bastion Security BX-5000  firewall
Real devices can be imported alongside them (mode live-readonly) with a real NAPALM platform
(ios, iosxr, nxos_ssh, junos, eos); the agents talk to both through vix.netdrivers."""
from __future__ import annotations

import datetime as dt
import heapq
import json
import math
import random

from . import db

SCHEMA = """
create table if not exists net_vendor (id text primary key, name text, archetype text, nos text);
create table if not exists net_firmware (vendor text, version text, released text, bug_factor real, cves text,
  notes text, primary key (vendor, version));
create table if not exists net_device (
  hostname text primary key, site text, metro text, role text, vendor text, model text, platform text,
  mode text default 'sim', mgmt_ip text, secret_ref text, os_version text, serial text, status text default 'up',
  fault text, fault_since text, boot_date text, ha_peer text, ports_total integer, config text, added_ts real
);
create table if not exists net_interface (
  device text, name text, kind text, speed_g integer, enabled integer default 1, oper_up integer default 1,
  rx_dbm real default -3.0, errors integer default 0, description text, peer text, primary key (device, name)
);
create table if not exists net_link (
  id text primary key, a_metro text, z_metro text, km real, capacity_g integer, status text default 'up',
  base_util real, a_dev text, z_dev text, fault_since text
);
create table if not exists net_event (
  id integer primary key, sim_date text, start_min real, target_kind text, target text, cause text,
  status text default 'open', incident integer, restore_min real
);
create table if not exists net_incident (
  id integer primary key, sim_date text, target text, cause text, diagnosis text, action text, severity text,
  start_min real, detected_min real, restored_min real, services_down integer, customers integer,
  outage_minutes real, credits_usd real, status text, summary text
);
create table if not exists net_impact (
  incident integer, tenant integer, service_kind text, service_id integer, minutes real, mrr real, credit_usd real
);
create table if not exists net_change (
  id integer primary key, created text, device text, kind text, from_ver text, to_ver text, window_date text,
  risk text, blast integer, approval text, status text, result text, notes text, notified integer default 0
);
create table if not exists net_capacity_daily (date text, kind text, ref text, used real, total real, util real,
  primary key (date, kind, ref));
create table if not exists net_audit (
  id integer primary key, ts real, sim_date text, actor text, device text, action text, mode text, allowed integer,
  detail text
);
"""

PORT_COLUMNS = [("ports", "leaf", "text"), ("ports", "leaf_if", "text"), ("ix_ports", "ix_switch", "text"),
                ("ix_ports", "ix_if", "text")]

VENDORS = [
    ("corvid", "Corvid Networks", "chassis service-provider router", "CorvOS-XR"),
    ("tessell", "Tessell", "merchant-silicon data-center switch", "TessOS"),
    ("halcyon", "Halcyon Optical", "DWDM transponder", "HaloOS"),
    ("bastion", "Bastion Security", "next-generation firewall", "BastionOS"),
]
FIRMWARE = [
    # vendor, version, released, bug factor (crash multiplier), CVEs, notes
    ("corvid", "7.9.1", "2025-06-02", 1.6, [{"id": "VSA-2025-0731", "severity": "high", "cvss": 7.8}], "old long-term release"),
    ("corvid", "7.11.2", "2026-03-10", 1.0, [{"id": "VSA-2026-0412", "severity": "critical", "cvss": 9.1}], "BGP parser flaw"),
    ("corvid", "7.11.4", "2026-11-03", 0.9, [], "fixes VSA-2026-0412"),
    ("corvid", "8.0.1", "2027-01-20", 1.4, [], "new major release, early defects"),
    ("tessell", "4.2.3", "2025-09-15", 1.2, [], ""),
    ("tessell", "4.4.0", "2026-07-01", 1.0, [{"id": "VSA-2026-0577", "severity": "medium", "cvss": 5.9}], ""),
    ("tessell", "4.4.2", "2026-11-24", 0.8, [], "fixes VSA-2026-0577, ASIC memory leak"),
    ("halcyon", "2.1.0", "2025-04-01", 1.0, [], ""),
    ("halcyon", "2.3.0", "2026-11-12", 0.9, [], ""),
    ("bastion", "10.1.4", "2026-01-12", 1.0, [{"id": "VSA-2026-0098", "severity": "critical", "cvss": 9.8}], "VPN portal flaw"),
    ("bastion", "10.1.7", "2026-10-28", 0.9, [], "fixes VSA-2026-0098"),
]
MODELS = {"core": ("corvid", "CR-9010", 36, 400), "spine": ("tessell", "TX-3200", 32, 400),
          "leaf": ("tessell", "TX-1048", 56, 100), "ix": ("tessell", "TX-6400", 64, 100),
          "firewall": ("bastion", "BX-5000", 8, 100), "optical": ("halcyon", "HL-800", 8, 400)}
ROLE_FAIL = {"core": 0.0006, "spine": 0.0008, "leaf": 0.0012, "ix": 0.0008, "firewall": 0.0010, "optical": 0.0006}
BACKBONE = [("SJC", "ASH"), ("SJC", "NYC"), ("ASH", "NYC"), ("NYC", "LHR"), ("ASH", "LHR"), ("ASH", "FRA"),
            ("LHR", "FRA"), ("FRA", "SIN"), ("LHR", "SIN"), ("SIN", "NRT"), ("SIN", "SYD"), ("NRT", "SYD"),
            ("SJC", "NRT"), ("SJC", "SYD")]
LEAF_CUSTOMER_PORTS = 48
IX_PORTS = 64


def init():
    with db.conn() as c:
        c.executescript(SCHEMA)
        for table, col, decl in PORT_COLUMNS:
            have = {r[1] for r in c.execute(f"pragma table_info({table})")}
            if col not in have:
                c.execute(f"alter table {table} add column {col} {decl}")


def today():
    from . import products
    return products.today()


def firmware(vendor, version):
    r = db.one("select * from net_firmware where vendor=? and version=?", (vendor, version))
    if r:
        r["cves"] = json.loads(r["cves"] or "[]")
    return r


def released_firmware(vendor):
    out = db.rows("select * from net_firmware where vendor=? and released<=? order by released", (vendor, today()))
    for r in out:
        r["cves"] = json.loads(r["cves"] or "[]")
    return out


# ---------------------------------------------------------------- seed / upgrade


def _config(h, role, os_version):
    return (f"hostname {h}\n! role {role}\n! version {os_version}\nntp server 192.0.2.123\n"
            f"logging host 192.0.2.50\nsnmp-server community <ref> ro\n")


def upgrade(seed: int = 42) -> bool:
    if (db.setting("world_version") or 1) >= 4:
        init()
        return False
    init()
    rnd = random.Random(seed + 4)
    with db.conn() as c:
        for v in VENDORS:
            c.execute("insert or ignore into net_vendor values(?,?,?,?)", v)
        for f in FIRMWARE:
            c.execute("insert or ignore into net_firmware values(?,?,?,?,?,?)", (f[0], f[1], f[2], f[3], json.dumps(f[4]), f[5]))
    ip = [0]

    def mgmt():
        ip[0] += 1
        return f"198.18.{ip[0] // 250}.{ip[0] % 250 + 1}"   # RFC 2544 benchmarking range: never routable

    def initial_version(vendor):
        rel = [f for f in FIRMWARE if f[0] == vendor and f[2] <= today()]
        weights = [1, 3, 2, 1][:len(rel)]
        return rnd.choices([f[1] for f in rel], weights=weights[:len(rel)] or None)[0]

    sites = db.rows("select s.id, s.metro from sites s order by s.id")
    first_site = {}
    for s in sites:
        first_site.setdefault(s["metro"], s["id"])
    devices, ifaces = [], []

    def add(h, site, metro, role, peer=None):
        vendor, model, nports, speed = MODELS[role]
        ver = initial_version(vendor)
        devices.append((h, site, metro, role, vendor, model, "sim-" + vendor, "sim", mgmt(), None, ver,
                        f"{vendor[:2].upper()}{rnd.randint(10**8, 10**9 - 1)}", "up", None, None,
                        (dt.date.fromisoformat(today()) - dt.timedelta(days=rnd.randint(5, 400))).isoformat(),
                        peer, nports, _config(h, role, ver), db.now()))
        for i in range(1, nports + 1):
            if role == "leaf":
                kind, sp = ("customer", 100) if i <= LEAF_CUSTOMER_PORTS else ("uplink", 400)
                name = f"Ethernet{i}"
            elif role == "ix":
                kind, sp, name = "ix", 100 if i <= 48 else 400, f"Ethernet{i}"
            elif role == "core":
                kind, sp, name = ("uplink" if i <= 4 else "backbone" if i <= 12 else "spare"), 400, f"et-0/0/{i - 1}"
            elif role == "spine":
                kind, sp, name = ("uplink" if i <= 2 else "fabric"), 400, f"Ethernet{i}"
            elif role == "optical":
                kind, sp, name = ("line" if i <= 2 else "client"), 400, (f"line-{i}" if i <= 2 else f"client-{i - 2}")
            else:
                kind, sp, name = "inside" if i <= 4 else "outside", 100, f"port{i}"
            ifaces.append((h, name, kind, sp, 1, 1, round(rnd.uniform(-4.5, -1.5), 2), 0, "", None))
    for s in sites:
        sid, metro = s["id"], s["metro"]
        p = sid.lower()
        for r, n in (("core", 2), ("spine", 2), ("firewall", 2)):
            for i in (1, 2):
                add(f"{p}-{r}{i}", sid, metro, r, peer=f"{p}-{r}{3 - i}")
        for h in db.rows("select id from halls where site=? order by id", (sid,)):
            hn = h["id"].lower()
            add(f"{hn}-leafa", sid, metro, "leaf", peer=f"{hn}-leafb")
            add(f"{hn}-leafb", sid, metro, "leaf", peer=f"{hn}-leafa")
        if first_site[metro] == sid:
            for i in (1, 2):
                add(f"{metro.lower()}-ix{i}", sid, metro, "ix", peer=f"{metro.lower()}-ix{3 - i}")
                add(f"{metro.lower()}-opt{i}", sid, metro, "optical", peer=f"{metro.lower()}-opt{3 - i}")
    with db.conn() as c:
        c.executemany("insert or ignore into net_device values(" + ",".join("?" * 20) + ")", devices)
        c.executemany("insert or ignore into net_interface values(?,?,?,?,?,?,?,?,?,?)", ifaces)
        from .world import great_circle_km, _METRO_GEO
        for a, z in BACKBONE:
            km = round(great_circle_km(_METRO_GEO[a], _METRO_GEO[z]) * 1.3)
            c.execute("insert or ignore into net_link values(?,?,?,?,?,'up',?,?,?,null)",
                      (f"{a}-{z}", a, z, km, rnd.choice([800, 1200, 1600]), round(rnd.uniform(0.25, 0.42), 3),
                       f"{first_site[a].lower()}-core1", f"{first_site[z].lower()}-core1"))
    assign_ports()
    db.set_setting("net_poll_minutes", 5)
    db.set_setting("net_live_writes", False)
    db.set_setting("world_version", 4)
    return True


def assign_ports() -> dict:
    """Place customer ports on leaf switches and exchange ports on exchange switches. New ports from
    orders land here every simulated day; ports that don't fit stay unassigned for Capacity to see."""
    init()
    placed = unplaced = 0
    with db.conn() as c:
        used = {}
        for (dev, n) in c.execute("select leaf, count(*) from ports where leaf is not null group by leaf"):
            used[dev] = n
        leaves = {}
        for (h, site) in c.execute("select hostname, site from net_device where role='leaf' and mode='sim'"):
            leaves.setdefault(site, []).append(h)
        for pid, site, tid in c.execute("select id, site, tenant from ports where leaf is null").fetchall():
            # keep a tenant's second port on the other leaf of the pair when possible (redundancy)
            mine = {r[0] for r in c.execute("select leaf from ports where tenant=? and site=? and leaf is not null", (tid, site))}
            cands = [l for l in leaves.get(site, []) if used.get(l, 0) < LEAF_CUSTOMER_PORTS]
            if not cands:
                unplaced += 1
                continue
            cands.sort(key=lambda l: (l in mine, used.get(l, 0)))
            leaf = cands[0]
            n = used.get(leaf, 0) + 1
            used[leaf] = n
            ifn = f"Ethernet{n}"
            c.execute("update ports set leaf=?, leaf_if=? where id=?", (leaf, ifn, pid))
            c.execute("update net_interface set description=? where device=? and name=?", (f"cust:{tid} port:{pid}", leaf, ifn))
            placed += 1
        ix_used = {}
        for (dev, n) in c.execute("select ix_switch, count(*) from ix_ports where ix_switch is not null group by ix_switch"):
            ix_used[dev] = n
        for xid, metro, tid in c.execute("select id, metro, tenant from ix_ports where ix_switch is null").fetchall():
            sw = [r[0] for r in c.execute("select hostname from net_device where metro=? and role='ix' and mode='sim' order by hostname", (metro,))]
            mine = {r[0] for r in c.execute("select ix_switch from ix_ports where tenant=? and metro=? and ix_switch is not null", (tid, metro))}
            cands = [s for s in sw if ix_used.get(s, 0) < IX_PORTS]
            if not cands:
                unplaced += 1
                continue
            cands.sort(key=lambda s: (s in mine, ix_used.get(s, 0)))
            s = cands[0]
            n = ix_used.get(s, 0) + 1
            ix_used[s] = n
            c.execute("update ix_ports set ix_switch=?, ix_if=? where id=?", (s, f"Ethernet{n}", xid))
            c.execute("update net_interface set description=? where device=? and name=?", (f"ix:{tid} port:{xid}", s, f"Ethernet{n}"))
            placed += 1
    return {"placed": placed, "unplaced": unplaced}


# ---------------------------------------------------------------- reachability and impact


def _down_devices():
    return {r["hostname"] for r in db.rows("select hostname from net_device where status in ('down','unreachable')")}


def _status_maps(down):
    devs = db.rows("select hostname, site, metro, role from net_device")
    core_ok, fabric_ok = {}, {}
    for d in devs:
        if d["role"] == "core":
            core_ok[d["metro"]] = core_ok.get(d["metro"], False) or d["hostname"] not in down
        if d["role"] == "spine":
            fabric_ok[d["site"]] = fabric_ok.get(d["site"], False) or d["hostname"] not in down
    return core_ok, fabric_ok


def metro_has_core(metro, down):
    return _status_maps(down)[0].get(metro, False)


def backbone_path(a, z, down_links=None, down=None, core_ok=None):
    """Shortest metro path (km) over links that are up, between metros that still have a core router."""
    if a == z:
        return [a]
    down = down if down is not None else _down_devices()
    down_links = down_links if down_links is not None else _down_link_ids()
    core_ok = core_ok if core_ok is not None else _status_maps(down)[0]
    adj = {}
    for l in db.rows("select * from net_link"):
        if l["id"] in down_links:
            continue
        adj.setdefault(l["a_metro"], []).append((l["z_metro"], l["km"]))
        adj.setdefault(l["z_metro"], []).append((l["a_metro"], l["km"]))
    if not core_ok.get(a) or not core_ok.get(z):
        return None
    dist, prev, q = {a: 0}, {}, [(0, a)]
    while q:
        d, u = heapq.heappop(q)
        if u == z:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w in adj.get(u, []):
            if not core_ok.get(v):
                continue
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(q, (nd, v))
    if z not in dist:
        return None
    path = [z]
    while path[-1] != a:
        path.append(prev[path[-1]])
    return path[::-1]


def services_affected(down: set, down_links: set) -> list[dict]:
    """Every billed active service that is down given failed devices and links. A port is up if its leaf
    and the site fabric are up, or the tenant has another working port at the same site (redundancy).
    Cross-connects are passive fiber and are not affected by device failures."""
    from . import products, world
    core_ok, fabric_ok = _status_maps(down)
    out = []
    ports = db.rows("select * from ports")
    pmap = {p["id"]: p for p in ports}
    metro_of_site = {s["id"]: s["metro"] for s in db.rows("select id, metro from sites")}

    def single(p):
        return p["leaf"] is None or (p["leaf"] not in down and fabric_ok.get(p["site"], False))
    single_ok = {p["id"]: single(p) for p in ports}
    site_ok = {}
    for p in ports:
        if single_ok[p["id"]]:
            site_ok[(p["tenant"], p["site"])] = True
    ok_port = {p["id"]: single_ok[p["id"]] or site_ok.get((p["tenant"], p["site"]), False) for p in ports}
    metro_ok = {}
    for p in ports:
        k = (p["tenant"], metro_of_site[p["site"]])
        metro_ok[k] = metro_ok.get(k, False) or ok_port[p["id"]]
    P = world.PRICES
    for p in ports:
        if not ok_port[p["id"]]:
            out.append({"tenant": p["tenant"], "kind": "port", "id": p["id"], "mrr": P["port_month"].get(p["speed_g"], 800)})
    path_cache = {}
    for v in db.rows("select * from virtual_circuits"):
        a, z = pmap.get(v["a_port"]), pmap.get(v["z_port"])
        if not a or not z:
            continue
        key = (metro_of_site[a["site"]], metro_of_site[z["site"]])
        if key not in path_cache:
            path_cache[key] = backbone_path(key[0], key[1], down_links, down, core_ok) is not None
        if not (ok_port[a["id"]] and ok_port[z["id"]] and path_cache[key]):
            out.append({"tenant": a["tenant"], "kind": "vc", "id": v["id"], "mrr": v["mbps"] * P["vc_per_mbps_month"]})
    for cc in db.rows("select * from cloud_connections"):
        has_ports = (cc["tenant"], cc["metro"]) in metro_ok
        if not core_ok.get(cc["metro"], True) or (has_ports and not metro_ok[(cc["tenant"], cc["metro"])]):
            out.append({"tenant": cc["tenant"], "kind": "cloud_connection", "id": cc["id"],
                        "mrr": cc["mbps"] * products.PRICES["cloud_connection_per_mbps"]})
    ixp = db.rows("select * from ix_ports where ix_switch is not null")
    ix_alive = {}
    for x in ixp:
        if x["ix_switch"] not in down:
            ix_alive[(x["tenant"], x["metro"])] = True
    for x in ixp:
        if x["ix_switch"] in down and not ix_alive.get((x["tenant"], x["metro"])):
            out.append({"tenant": x["tenant"], "kind": "ix_port", "id": x["id"],
                        "mrr": products.PRICES["ix_port"].get(x["speed_g"], 1200)})
    for e in db.rows("select * from edge_devices"):
        if not core_ok.get(e["metro"], True):
            out.append({"tenant": e["tenant"], "kind": "edge_device", "id": e["id"], "mrr": e["vcpu"] * products.PRICES["edge_per_vcpu"]})
    return out


def blast_radius(hostname: str) -> dict:
    """What would go down if this one device failed now (used for change risk)."""
    down = _down_devices() | {hostname}
    base = {(s["kind"], s["id"]) for s in services_affected(_down_devices(), _down_link_ids())}
    hit = [s for s in services_affected(down, _down_link_ids()) if (s["kind"], s["id"]) not in base]
    return {"services": len(hit), "customers": len({s["tenant"] for s in hit}), "mrr": round(sum(s["mrr"] for s in hit), 2)}


def _down_link_ids():
    return {r["id"] for r in db.rows("select id from net_link where status!='up'")}


# ---------------------------------------------------------------- physics (simulated devices only)


def simulate_day(rnd: random.Random) -> dict:
    """Ground truth for one simulated day: hardware and software faults, optics aging, fiber cuts.
    Writes device/link/interface state that the drivers expose; the NOC must find it by polling."""
    init()
    day = today()
    events = []
    devs = db.rows("select d.*, f.bug_factor from net_device d left join net_firmware f on f.vendor=d.vendor"
                   " and f.version=d.os_version where d.mode='sim' and d.status='up'")
    for d in devs:
        p = ROLE_FAIL.get(d["role"], 0.001) * (d["bug_factor"] or 1.0)
        if rnd.random() < p:
            cause = rnd.choices(["crash", "linecard", "psu"], [0.55, 0.3, 0.15])[0]
            start = rnd.uniform(0, 1380)
            if cause == "psu":
                db.execute("update net_device set status='degraded', fault='psu', fault_since=? where hostname=?",
                           (f"{day} {start:.0f}", d["hostname"]))
            else:
                db.execute("update net_device set status='down', fault=?, fault_since=? where hostname=?",
                           (cause, f"{day} {start:.0f}", d["hostname"]))
            events.append(("device", d["hostname"], cause, start))
    for l in db.rows("select * from net_link where status='up'"):
        if rnd.random() < l["km"] / 1000 * 0.0007:
            start = rnd.uniform(0, 1380)
            db.execute("update net_link set status='down', fault_since=? where id=?", (f"{day} {start:.0f}", l["id"]))
            events.append(("link", l["id"], "fiber_cut", start))
    # optics: a few start degrading, degrading ones keep losing light, the worst go dark
    with db.conn() as c:
        rows = c.execute("select i.device, i.name, i.rx_dbm from net_interface i join net_device d on d.hostname=i.device"
                         " where d.mode='sim' and i.kind in ('customer','uplink','ix','backbone','line')"
                         " and i.description!='' and i.oper_up=1").fetchall()
        for dev, name, rx in rows:
            if rx < -7.5 or rnd.random() < 0.00008:
                new = round(rx - rnd.uniform(0.4, 1.4), 2)
                c.execute("update net_interface set rx_dbm=?, errors=errors+? where device=? and name=?",
                          (new, int(max(0, -new - 8) ** 3 * 10), dev, name))
                if new < -14:
                    c.execute("update net_interface set oper_up=0 where device=? and name=?", (dev, name))
                    events.append(("interface", f"{dev}:{name}", "optic", rnd.uniform(0, 1380)))
    for kind, target, cause, start in events:
        db.execute("insert into net_event(sim_date,start_min,target_kind,target,cause,status) values(?,?,?,?,?,'open')",
                   (day, round(start, 1), kind, target, cause))
    # links come back when the carrier finishes the splice
    for l in db.rows("select * from net_link where status='down'"):
        since_day = l["fault_since"].split()[0]
        if since_day < day and rnd.random() < 0.7:
            db.execute("update net_link set status='up', fault_since=null where id=?", (l["id"],))
    return {"events": len(events), "by_cause": {c: sum(1 for e in events if e[2] == c) for c in
                                                ("crash", "linecard", "psu", "fiber_cut", "optic")}}


def remediate(hostname_or_target: str, action: str, rnd: random.Random) -> dict:
    """Physics of a remediation on simulated gear. Returns minutes until service is restored, and whether
    the action actually fixed the fault (a reboot does not fix broken hardware)."""
    if action == "replace_optic":
        dev, name = hostname_or_target.split(":")
        db.execute("update net_interface set rx_dbm=?, errors=0, oper_up=1 where device=? and name=?",
                   (round(rnd.uniform(-3.5, -2), 2), dev, name))
        return {"fixed": True, "minutes": rnd.uniform(25, 60)}
    if action == "carrier_repair":
        return {"fixed": True, "minutes": rnd.uniform(360, 1080)}
    d = db.one("select * from net_device where hostname=?", (hostname_or_target,))
    if not d:
        return {"fixed": False, "minutes": 0}
    fault = d["fault"]
    if action == "reboot":
        if fault == "crash":
            _clear(d["hostname"], reboot=True)
            return {"fixed": True, "minutes": rnd.uniform(8, 18)}
        return {"fixed": False, "minutes": rnd.uniform(15, 25)}
    if action == "rma_swap":
        spare_delay = 0 if rnd.random() < 0.85 else rnd.uniform(240, 600)   # spare on site 85% of the time
        _clear(d["hostname"], reboot=True)
        return {"fixed": True, "minutes": rnd.uniform(70, 160) + spare_delay}
    if action == "replace_psu":
        _clear(d["hostname"], reboot=False)
        return {"fixed": True, "minutes": rnd.uniform(30, 60)}
    return {"fixed": False, "minutes": 0}


def _clear(hostname, reboot):
    db.execute("update net_device set status='up', fault=null, fault_since=null" + (", boot_date=?" if reboot else "") +
               " where hostname=?", ((today(), hostname) if reboot else (hostname,)))


# ---------------------------------------------------------------- capacity


def capacity_snapshot() -> list[dict]:
    day = today()
    rows = []
    for r in db.rows("select site, count(*) leaves from net_device where role='leaf' group by site"):
        used = db.scalar("select count(*) from ports where site=? and leaf is not null", (r["site"],)) or 0
        waiting = db.scalar("select count(*) from ports where site=? and leaf is null", (r["site"],)) or 0
        total = r["leaves"] * LEAF_CUSTOMER_PORTS
        rows.append({"kind": "leaf_ports", "ref": r["site"], "used": used + waiting, "total": total})
    for r in db.rows("select metro, count(*) n from net_device where role='ix' group by metro"):
        used = db.scalar("select count(*) from ix_ports where metro=?", (r["metro"],)) or 0
        rows.append({"kind": "ix_ports", "ref": r["metro"], "used": used, "total": r["n"] * IX_PORTS})
    load = {l["id"]: l["capacity_g"] * l["base_util"] for l in db.rows("select * from net_link")}
    links = {(l["a_metro"], l["z_metro"]): l["id"] for l in db.rows("select * from net_link")}
    links.update({(z, a): i for (a, z), i in list(links.items())})
    metro_of_port = {p["id"]: p["metro"] for p in db.rows("select p.id, s.metro from ports p join sites s on s.id=p.site")}
    flows = {}
    for v in db.rows("select a_port, z_port, mbps from virtual_circuits"):
        a, z = metro_of_port.get(v["a_port"]), metro_of_port.get(v["z_port"])
        if a and z and a != z:
            flows[(a, z)] = flows.get((a, z), 0) + v["mbps"]
    for (a, z), mbps in flows.items():
        path = backbone_path(a, z)
        if path:
            for u, w in zip(path, path[1:]):
                load[links[(u, w)]] += mbps / 1000 * 0.6
    for l in db.rows("select * from net_link"):
        rows.append({"kind": "backbone_peak", "ref": l["id"], "used": round(load[l["id"]] * 1.5, 1), "total": l["capacity_g"]})
    with db.conn() as c:
        c.executemany("insert or replace into net_capacity_daily values(?,?,?,?,?,?)",
                      [(day, r["kind"], r["ref"], r["used"], r["total"], round(r["used"] / r["total"], 4) if r["total"] else 0)
                       for r in rows])
    for r in rows:
        r["util"] = round(r["used"] / r["total"], 4) if r["total"] else 0
    return rows


def forecast_days(kind, ref, threshold=0.8):
    hist = db.rows("select date, util from net_capacity_daily where kind=? and ref=? order by date desc limit 30", (kind, ref))
    if len(hist) < 7:
        return None
    hist = hist[::-1]
    xs = [(dt.date.fromisoformat(h["date"]) - dt.date.fromisoformat(hist[0]["date"])).days for h in hist]
    ys = [h["util"] for h in hist]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs) or 1
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    if ys[-1] >= threshold:
        return 0
    if slope <= 1e-6:
        return None
    return math.ceil((threshold - ys[-1]) / slope)


def add_leaf_pair(site: str) -> dict:
    n = db.scalar("select count(*) from net_device where site=? and role='leaf' and hostname like ?", (site, f"{site.lower()}-x%")) or 0
    p = f"{site.lower()}-x{n // 2 + 1}"
    metro = db.scalar("select metro from sites where id=?", (site,))
    ver = db.scalar("select version from net_firmware where vendor='tessell' and released<=? order by bug_factor, released desc limit 1",
                    (today(),))
    for s in ("a", "b"):
        h = f"{p}-leaf{s}"
        db.execute("insert into net_device values(" + ",".join("?" * 20) + ")",
                   (h, site, metro, "leaf", "tessell", "TX-1048", "sim-tessell", "sim", f"198.19.{random.randint(0, 250)}.{random.randint(1, 250)}",
                    None, ver, f"TE{random.randint(10**8, 10**9 - 1)}", "up", None, None, today(),
                    f"{p}-leaf{'b' if s == 'a' else 'a'}", 56, _config(h, "leaf", ver), db.now()))
        with db.conn() as c:
            c.executemany("insert into net_interface values(?,?,?,?,?,?,?,?,?,?)",
                          [(h, f"Ethernet{i}", "customer" if i <= 48 else "uplink", 100 if i <= 48 else 400, 1, 1, -2.5, 0, "", None)
                           for i in range(1, 57)])
    placed = assign_ports()
    return {"added": [f"{p}-leafa", f"{p}-leafb"], "ports_placed": placed["placed"]}


def add_ix_switch(metro: str) -> dict:
    n = (db.scalar("select count(*) from net_device where metro=? and role='ix'", (metro,)) or 0) + 1
    h = f"{metro.lower()}-ix{n}"
    site = db.scalar("select site from net_device where metro=? and role='ix' limit 1", (metro,)) or db.scalar(
        "select id from sites where metro=? order by id", (metro,))
    ver = db.scalar("select version from net_firmware where vendor='tessell' and released<=? order by bug_factor, released desc limit 1",
                    (today(),))
    db.execute("insert into net_device values(" + ",".join("?" * 20) + ")",
               (h, site, metro, "ix", "tessell", "TX-6400", "sim-tessell", "sim", f"198.19.{random.randint(0, 250)}.{random.randint(1, 250)}",
                None, ver, f"TE{random.randint(10**8, 10**9 - 1)}", "up", None, None, today(), f"{metro.lower()}-ix{n - 1}",
                64, _config(h, "ix", ver), db.now()))
    with db.conn() as c:
        c.executemany("insert into net_interface values(?,?,?,?,?,?,?,?,?,?)",
                      [(h, f"Ethernet{i}", "ix", 100 if i <= 48 else 400, 1, 1, -2.5, 0, "", None) for i in range(1, 65)])
    return {"added": h}


def upgrade_link(link_id: str, add_g: int = 400) -> dict:
    db.execute("update net_link set capacity_g=capacity_g+? where id=?", (add_g, link_id))
    return {"link": link_id, "capacity_g": db.scalar("select capacity_g from net_link where id=?", (link_id,))}


# ---------------------------------------------------------------- views


def inventory(site=None, role=None) -> list[dict]:
    q, args = "select hostname, site, metro, role, vendor, model, platform, mode, mgmt_ip, os_version, status, fault, ha_peer from net_device", []
    w = []
    if site:
        w.append("site=?")
        args.append(site)
    if role:
        w.append("role=?")
        args.append(role)
    if w:
        q += " where " + " and ".join(w)
    return db.rows(q + " order by hostname", tuple(args))


def summary() -> dict:
    init()
    return {
        "devices": db.rows("select role, count(*) n, sum(status='up') up, sum(status='degraded') degraded,"
                           " sum(status in ('down','unreachable')) down from net_device group by role"),
        "modes": db.rows("select mode, count(*) n from net_device group by mode"),
        "firmware": db.rows("select d.vendor, d.os_version, count(*) n, f.cves from net_device d left join net_firmware f"
                            " on f.vendor=d.vendor and f.version=d.os_version group by d.vendor, d.os_version"),
        "links": db.rows("select id, km, capacity_g, status from net_link order by id"),
        "open_incidents": db.scalar("select count(*) from net_incident where status!='resolved'"),
        "unassigned_ports": db.scalar("select count(*) from ports where leaf is null"),
        "live_writes": bool(db.setting("net_live_writes", False)),
    }
