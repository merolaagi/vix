"""Device drivers and guardrails: the only way agents touch network gear.

Every agent calls NetworkControl, never the simulator. NetworkControl picks a driver per device:

  mode 'sim'            SimDriver: answers from the simulated network in NAPALM's data formats
  mode 'live-readonly'  NapalmDriver: real device over SSH/NETCONF/eAPI, getters only
  mode 'live'           NapalmDriver with config changes allowed, but only when ALL of these hold:
                          * the global kill switch net_live_writes is on (a founder decision)
                          * the change is approved and today is its maintenance window
                          * the commit uses NAPALM commit-confirm (revert_in) and is confirmed only
                            after post-checks pass; otherwise it is rolled back

Because SimDriver returns the same shapes as NAPALM (get_facts, get_interfaces, get_environment,
get_optics, get_bgp_neighbors ...), the NOC's detection and diagnosis code is identical for simulated
and real routers. Credentials are never stored: devices carry a secret reference
(env:USER_VAR:PASS_VAR, keychain:<service>:<username>, or none for NAPALM's mock driver).
Every call is written to net_audit."""
from __future__ import annotations

import json
import os
import subprocess
import time

from . import db

NAPALM_PLATFORMS = {"ios", "iosxr", "iosxr_netconf", "nxos", "nxos_ssh", "junos", "eos", "mock"}
READ_GETTERS = {"get_facts", "get_interfaces", "get_interfaces_counters", "get_environment", "get_optics",
                "get_bgp_neighbors", "get_lldp_neighbors", "get_config", "get_arp_table", "get_snmp_information"}


class DriverError(Exception):
    pass


class Unreachable(DriverError):
    pass


class NotAllowed(DriverError):
    pass


# ---------------------------------------------------------------- simulated device


class SimDriver:
    """A simulated device that speaks NAPALM's getter formats."""
    _candidates: dict[str, str] = {}
    _rollback: dict[str, str] = {}

    def __init__(self, device: dict):
        self.d = device

    def open(self):
        d = db.one("select * from net_device where hostname=?", (self.d["hostname"],))
        self.d = d
        if d["status"] in ("down", "unreachable") and d["fault"] == "crash":
            raise Unreachable(f"{d['hostname']} ({d['mgmt_ip']}): connection timed out")

    def close(self):
        pass

    def _ifaces(self):
        if getattr(self, "_if_cache", None) is None:
            self._if_cache = db.rows("select * from net_interface where device=? order by rowid", (self.d["hostname"],))
        return self._if_cache

    def _dataplane_up(self):
        return self.d["status"] not in ("down", "unreachable")

    def get_facts(self):
        import datetime as dt
        from . import products
        boot = dt.date.fromisoformat(self.d["boot_date"] or products.today())
        up = (products.today_d() - boot).days * 86400 + 3600
        return {"hostname": self.d["hostname"], "fqdn": f"{self.d['hostname']}.net.vix.example",
                "vendor": self.d["vendor"], "model": self.d["model"], "os_version": self.d["os_version"],
                "serial_number": self.d["serial"], "uptime": float(up),
                "interface_list": [i["name"] for i in self._ifaces()]}

    def get_interfaces(self):
        dp = self._dataplane_up()
        return {i["name"]: {"is_up": bool(i["oper_up"]) and dp and bool(i["enabled"]), "is_enabled": bool(i["enabled"]),
                            "description": i["description"] or "", "last_flapped": -1.0, "speed": float(i["speed_g"] * 1000),
                            "mtu": 9216, "mac_address": "02:00:00:00:00:00"} for i in self._ifaces()}

    def get_interfaces_counters(self):
        return {i["name"]: {"rx_errors": i["errors"], "tx_errors": 0, "rx_discards": 0, "tx_discards": 0,
                            "rx_octets": 0, "tx_octets": 0, "rx_unicast_packets": 0, "tx_unicast_packets": 0,
                            "rx_multicast_packets": 0, "tx_multicast_packets": 0, "rx_broadcast_packets": 0,
                            "tx_broadcast_packets": 0} for i in self._ifaces()}

    def get_environment(self):
        lc_crit = self.d["fault"] == "linecard"
        psu_bad = self.d["fault"] == "psu"
        return {"fans": {"fan-tray-0": {"status": True}, "fan-tray-1": {"status": True}},
                "temperature": {"chassis": {"temperature": 38.0, "is_alert": False, "is_critical": False},
                                "linecard-0": {"temperature": 96.0 if lc_crit else 51.0, "is_alert": lc_crit, "is_critical": lc_crit}},
                "power": {"PSU-0": {"status": not psu_bad, "capacity": 3000.0, "output": 0.0 if psu_bad else 1450.0},
                          "PSU-1": {"status": True, "capacity": 3000.0, "output": 1450.0}},
                "cpu": {"0": {"%usage": 21.0}}, "memory": {"available_ram": 32768, "used_ram": 11800}}

    def get_optics(self):
        out = {}
        for i in self._ifaces():
            if not i["description"]:
                continue
            rx = i["rx_dbm"] if self._dataplane_up() else -40.0
            out[i["name"]] = {"physical_channels": {"channel": [{"index": 0, "state": {
                "input_power": {"instant": rx, "avg": rx, "min": rx - 0.2, "max": rx + 0.2},
                "output_power": {"instant": -1.0, "avg": -1.0, "min": -1.2, "max": -0.8},
                "laser_bias_current": {"instant": 6.5, "avg": 6.5, "min": 6.4, "max": 6.6}}}]}}
        return out

    def get_bgp_neighbors(self):
        peers = {}
        if self.d["role"] == "core":
            for l in db.rows("select * from net_link where a_dev=? or z_dev=?", (self.d["hostname"], self.d["hostname"])):
                other = l["z_metro"] if l["a_dev"] == self.d["hostname"] else l["a_metro"]
                peers[f"10.255.{abs(hash(l['id'])) % 250}.1"] = {
                    "local_as": 65000, "remote_as": 65000, "remote_id": f"core.{other.lower()}", "is_up": l["status"] == "up",
                    "is_enabled": True, "description": f"backbone {l['id']}", "uptime": 86400,
                    "address_family": {"ipv4": {"received_prefixes": 900, "accepted_prefixes": 900, "sent_prefixes": 900}}}
        return {"global": {"router_id": self.d["mgmt_ip"], "peers": peers}}

    def get_lldp_neighbors(self):
        return {}

    def get_config(self, retrieve="all"):
        return {"running": self.d["config"] or "", "candidate": self._candidates.get(self.d["hostname"], ""), "startup": self.d["config"] or ""}

    # config lifecycle (NAPALM semantics)
    def load_merge_candidate(self, config: str):
        """Merge like a CLI: 'no <line>' removes matching lines, other lines are added if absent."""
        lines = (self.d["config"] or "").splitlines()
        for raw in config.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            if line.startswith("no "):
                target = line[3:].strip()
                lines = [x for x in lines if not x.strip().startswith(target)]
            elif line not in lines:
                lines.append(line)
        self._candidates[self.d["hostname"]] = "\n".join(lines) + "\n"

    def compare_config(self):
        cand = self._candidates.get(self.d["hostname"])
        if cand is None:
            return ""
        old, new = (self.d["config"] or "").splitlines(), cand.splitlines()
        return "\n".join(["+" + line for line in new if line not in old] + ["-" + line for line in old if line not in new])

    def commit_config(self, revert_in: int | None = None):
        cand = self._candidates.pop(self.d["hostname"], None)
        if cand is None:
            raise DriverError("no candidate config loaded")
        self._rollback[self.d["hostname"]] = self.d["config"] or ""
        db.execute("update net_device set config=? where hostname=?", (cand, self.d["hostname"]))

    def confirm_commit(self):
        return True

    def discard_config(self):
        self._candidates.pop(self.d["hostname"], None)

    def rollback(self):
        prev = self._rollback.pop(self.d["hostname"], None)
        if prev is not None:
            db.execute("update net_device set config=? where hostname=?", (prev, self.d["hostname"]))

    # simulated-only operations
    def upgrade_firmware(self, version: str, rnd) -> dict:
        from . import netinfra
        fw = netinfra.firmware(self.d["vendor"], version)
        if not fw:
            raise DriverError(f"no firmware {version} for {self.d['vendor']}")
        fail = rnd.random() < 0.03 * fw["bug_factor"]
        if fail:
            return {"ok": False, "minutes": rnd.uniform(30, 45), "note": "post-upgrade checks failed; rolled back"}
        db.execute("update net_device set os_version=?, boot_date=? where hostname=?",
                   (version, netinfra.today(), self.d["hostname"]))
        return {"ok": True, "minutes": rnd.uniform(9, 16)}


# ---------------------------------------------------------------- real device (NAPALM)


def resolve_secret(ref: str | None) -> tuple[str, str]:
    """env:USER_VAR:PASS_VAR | keychain:<service>:<username> | none. Values are never logged or stored."""
    if not ref or ref == "none":
        return "vix", "vix"
    kind, _, rest = ref.partition(":")
    if kind == "env":
        u, _, p = rest.partition(":")
        if u not in os.environ or p not in os.environ:
            raise DriverError(f"environment variables {u}/{p} are not set")
        return os.environ[u], os.environ[p]
    if kind == "keychain":
        service, _, user = rest.partition(":")
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", user, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise DriverError(f"no keychain item for service {service} / account {user}")
        return user, r.stdout.strip()
    raise DriverError("secret_ref must be env:USER_VAR:PASS_VAR, keychain:<service>:<username> or none")


class NapalmDriver:
    def __init__(self, device: dict):
        self.d = device
        self.conn = None

    def open(self):
        try:
            from napalm import get_network_driver
        except ImportError as e:
            raise DriverError("NAPALM is not installed: .venv/bin/pip install napalm") from e
        if self.d["platform"] not in NAPALM_PLATFORMS:
            raise DriverError(f"platform {self.d['platform']} is not a NAPALM driver")
        user, pw = resolve_secret(self.d.get("secret_ref"))
        opts = json.loads(self.d.get("config") or "{}") if self.d["platform"] == "mock" else {}
        drv = get_network_driver(self.d["platform"])
        self.conn = drv(hostname=self.d["mgmt_ip"], username=user, password=pw, timeout=30, optional_args=opts)
        try:
            self.conn.open()
        except Exception as e:  # noqa: BLE001
            raise Unreachable(f"{self.d['hostname']} ({self.d['mgmt_ip']}): {type(e).__name__}: {str(e)[:120]}") from e

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def __getattr__(self, name):
        if self.conn is None:
            raise DriverError("not connected")
        return getattr(self.conn, name)


# ---------------------------------------------------------------- guardrails


def driver_for(device: dict):
    return SimDriver(device) if device["mode"] == "sim" else NapalmDriver(device)


def audit(actor, device, action, mode, allowed, detail=""):
    from . import products
    db.execute("insert into net_audit(ts,sim_date,actor,device,action,mode,allowed,detail) values(?,?,?,?,?,?,?,?)",
               (time.time(), products.today(), actor, device, action, mode, int(allowed), str(detail)[:500]))


class NetworkControl:
    """What agents use. Reads work in every mode; writes are gated."""

    def __init__(self, actor: str):
        self.actor = actor

    def device(self, hostname):
        d = db.one("select * from net_device where hostname=?", (hostname,))
        if not d:
            raise DriverError(f"no device {hostname}")
        return d

    def poll(self, hostname: str, getters: list[str]) -> dict:
        d = self.device(hostname)
        bad = [g for g in getters if g not in READ_GETTERS]
        if bad:
            raise NotAllowed(f"not a read-only getter: {bad}")
        drv = driver_for(d)
        out = {}
        try:
            drv.open()
            for g in getters:
                out[g] = getattr(drv, g)()
        finally:
            drv.close()
        if d["mode"] != "sim":
            audit(self.actor, hostname, "poll:" + ",".join(getters), d["mode"], True)
        return out

    def _write_gate(self, d, action, change_id=None):
        from . import products
        if d["mode"] == "sim":
            return
        if d["mode"] != "live":
            audit(self.actor, d["hostname"], action, d["mode"], False, "device is read-only")
            raise NotAllowed(f"{d['hostname']} is {d['mode']}: writes are not allowed")
        if not db.setting("net_live_writes", False):
            audit(self.actor, d["hostname"], action, d["mode"], False, "kill switch off")
            raise NotAllowed("live writes are disabled (kill switch). The founder must approve a net_live_writes decision.")
        ch = db.one("select * from net_change where id=?", (change_id,)) if change_id else None
        if not ch or ch["status"] not in ("approved", "in_progress"):
            audit(self.actor, d["hostname"], action, d["mode"], False, "no approved change")
            raise NotAllowed("live writes need an approved change")
        if ch["window_date"] != products.today():
            audit(self.actor, d["hostname"], action, d["mode"], False, "outside window")
            raise NotAllowed(f"change {change_id} is scheduled for {ch['window_date']}, not today")

    def reboot(self, hostname: str, rnd) -> dict:
        d = self.device(hostname)
        if d["mode"] != "sim":
            audit(self.actor, hostname, "reboot", d["mode"], False, "remote reboot of live gear is manual")
            return {"fixed": False, "minutes": 0, "manual": True, "note": "paged on-call engineer for console reboot"}
        from . import netinfra
        r = netinfra.remediate(hostname, "reboot", rnd)
        audit(self.actor, hostname, "reboot", "sim", True, r)
        return r

    def field_work(self, target: str, work: str, rnd) -> dict:
        """Physical work (RMA swap, optic or PSU replacement, fiber repair). Simulated gear: physics.
        Live gear: a work order for people on site."""
        from . import netinfra
        host = target.split(":")[0]
        d = db.one("select * from net_device where hostname=?", (host,))
        if d and d["mode"] != "sim":
            audit(self.actor, target, "work_order:" + work, d["mode"], True, "dispatched to site staff")
            return {"fixed": False, "minutes": 0, "manual": True, "note": f"work order raised: {work}"}
        r = netinfra.remediate(target, work, rnd)
        return r

    def apply_config(self, hostname: str, snippet: str, change_id: int | None, revert_in: int = 300) -> dict:
        d = self.device(hostname)
        self._write_gate(d, "apply_config", change_id)
        drv = driver_for(d)
        try:
            drv.open()
            drv.load_merge_candidate(config=snippet)
            diff = drv.compare_config()
            if not diff:
                drv.discard_config()
                return {"ok": True, "diff": "", "note": "no change needed"}
            drv.commit_config(revert_in=revert_in) if d["mode"] == "live" else drv.commit_config()
            post = drv.get_facts()
            if post.get("hostname") is None:
                drv.rollback()
                audit(self.actor, hostname, "apply_config", d["mode"], True, "post-check failed: rolled back")
                return {"ok": False, "diff": diff, "note": "post-check failed: rolled back"}
            if d["mode"] == "live":
                drv.confirm_commit()
            audit(self.actor, hostname, "apply_config", d["mode"], True, diff[:300])
            return {"ok": True, "diff": diff}
        finally:
            drv.close()

    def upgrade_firmware(self, hostname: str, version: str, change_id: int | None, rnd) -> dict:
        d = self.device(hostname)
        if d["mode"] != "sim":
            self._write_gate(d, "upgrade_firmware", change_id)
            audit(self.actor, hostname, "upgrade_firmware", d["mode"], True, "method of procedure issued to engineer")
            return {"ok": None, "manual": True, "note": "firmware upgrades on live gear follow a written procedure run by an engineer"}
        return SimDriver(d).upgrade_firmware(version, rnd)


# ---------------------------------------------------------------- live onboarding


def import_devices(rows: list[dict]) -> dict:
    """Add real devices as live-readonly. Each row: hostname, mgmt_ip, platform (NAPALM driver), site, role,
    vendor, model, secret_ref, and for platform 'mock' a fixtures path. Passwords are refused."""
    from . import netinfra
    netinfra.init()
    added, errors = [], []
    for r in rows:
        if any(k.lower() in ("password", "secret", "pass") for k in r):
            errors.append({"row": r.get("hostname"), "error": "do not send passwords; use secret_ref"})
            continue
        missing = [k for k in ("hostname", "mgmt_ip", "platform", "site", "role") if not r.get(k)]
        if missing:
            errors.append({"row": r.get("hostname"), "error": f"missing {missing}"})
            continue
        if r["platform"] not in NAPALM_PLATFORMS:
            errors.append({"row": r["hostname"], "error": f"platform must be one of {sorted(NAPALM_PLATFORMS)}"})
            continue
        site = db.one("select id, metro from sites where id=?", (r["site"],))
        if not site:
            errors.append({"row": r["hostname"], "error": f"no site {r['site']}"})
            continue
        opts = json.dumps({"path": r["fixtures"]}) if r["platform"] == "mock" and r.get("fixtures") else None
        db.execute("insert or replace into net_device(hostname,site,metro,role,vendor,model,platform,mode,mgmt_ip,secret_ref,"
                   "os_version,serial,status,ports_total,config,added_ts) values(?,?,?,?,?,?,?,'live-readonly',?,?,?,?,'up',0,?,?)",
                   (r["hostname"], site["id"], site["metro"], r["role"], r.get("vendor", "unknown"), r.get("model", "unknown"),
                    r["platform"], r["mgmt_ip"], r.get("secret_ref", "none"), "unknown", "", opts, time.time()))
        added.append(r["hostname"])
    return {"added": added, "errors": errors}
