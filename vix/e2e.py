"""End-to-end tests against a throwaway copy of the live database (run as: python -m vix.e2e).
Used by Platform Engineering (Anvil) and Product Software (Keel); production data is never touched."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KINDS = {
    "port": lambda m, s: {"site": s, "speed_g": 10},
    "cross_connect": lambda m, s: {"site": s},
    "vc": lambda m, s: {"a_metro": m, "z_metro": "LHR" if m != "LHR" else "SJC", "mbps": 100},
    "cabinets": lambda m, s: {"metro": m, "cabinets": 1, "kw_per_cabinet": 4},
    "ai_cabinets": lambda m, s: {"metro": "ASH", "cabinets": 1, "kw_per_cabinet": 30},
    "cloud_connection": lambda m, s: None,
    "cloud_router": lambda m, s: {"metro": m, "package": "premium"},
    "edge_device": lambda m, s: {"metro": m, "role": "firewall", "size": "small", "ha": False},
    "ix_port": lambda m, s: {"metro": m, "speed_g": 10},
    "internet_access": lambda m, s: {"metro": m, "commit_mbps": 200},
    "metal": lambda m, s: {"metro": m, "plan": "s1.small", "count": 1},
}


def main():
    kinds = list(KINDS)
    if "--kinds" in sys.argv:
        kinds = sys.argv[sys.argv.index("--kinds") + 1].split(",")
    src = Path(os.environ.get("VIX_HOME", ROOT)) / "data" / "vix.db"
    tmp = Path(tempfile.mkdtemp(prefix="vix-e2e-"))
    (tmp / "data").mkdir()
    with sqlite3.connect(src) as s, sqlite3.connect(tmp / "data" / "vix.db") as d:
        s.backup(d)
    os.environ["VIX_HOME"] = str(tmp)
    from . import crm, db, netinfra, products, world
    tests = []

    def check(name, ok, detail=""):
        tests.append({"test": name, "ok": bool(ok), "detail": str(detail)[:140]})
    t = db.one("select p.tenant, p.site, s.metro from ports p join sites s on s.id=p.site join contracts c on c.tenant=p.tenant"
               " where c.status='active' and s.metro in ('ASH','SJC','NYC') order by p.id limit 1")
    check("sandbox has an active customer", t, t)
    if not t:
        print(json.dumps({"ok": False, "tests": tests}))
        return
    rev0 = products.revenue_breakdown()["total"]
    ids = {}
    for k in kinds:
        p = KINDS[k](t["metro"], t["site"])
        if k == "cloud_connection":
            on = db.one("select cloud, metro from cloud_onramps where metro=? and capacity_mbps-used_mbps>1000", (t["metro"],))
            p = {"metro": on["metro"], "cloud": on["cloud"], "mbps": 100} if on else None
        if p is None:
            check(f"order {k}", True, "skipped: no on-ramp with spare capacity in this metro")
            continue
        ids[k] = world.place_order(t["tenant"], k, p)["order_id"]
    res = {r["id"]: r for r in world.process_orders(list(ids))}
    for k, oid in ids.items():
        r = res.get(oid) or db.one("select * from orders where id=?", (oid,))
        check(f"order {k} fulfilled", r["status"] in ("done", "waitlisted"), f"{r['status']}: {r['note']}")
    rev1 = products.revenue_breakdown()["total"]
    check("billing reflects new services", rev1 > rev0, f"${rev0:,.0f} -> ${rev1:,.0f}")
    netinfra.assign_ports()
    check("new ports placed on a switch", db.scalar("select count(*) from ports where tenant=? and leaf is null", (t["tenant"],)) == 0)
    before = db.scalar("select count(*) from crm_asset where Status='Installed'")
    crm.sync()
    after = db.scalar("select count(*) from crm_asset where Status='Installed'")
    check("CRM assets created for new services", after > before, f"{before} -> {after}")
    acct = crm.query(f"SELECT Id, MRR__c FROM Account WHERE Tenant__c = {t['tenant']}")
    check("CRM account MRR updated", acct["records"] and acct["records"][0]["MRR__c"] > 0, acct["records"][:1])
    s = netinfra.services_affected(set(), set())
    check("network impact model runs", isinstance(s, list), f"{len(s)} services down at baseline")
    leaf = db.scalar("select leaf from ports where tenant=? and leaf is not null limit 1", (t["tenant"],))
    br = netinfra.blast_radius(leaf) if leaf else {"services": 0}
    check("blast radius computes for the customer's switch", "services" in br, br)
    print(json.dumps({"ok": all(x["ok"] for x in tests), "tests": tests}))


if __name__ == "__main__":
    main()
