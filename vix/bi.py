"""Headless BI: a separate warehouse loaded nightly from operations and the CRM,
one metrics layer every report reads from, dashboard specs, and exports that real
Tableau can open (.hyper via Tableau's Hyper API when installed, CSV always)."""
from __future__ import annotations

import csv
import json
import sqlite3
import time

from . import db
from .config import DATA_DIR, DB_PATH

WH_PATH = DATA_DIR / "warehouse.db"
DASH_DIR = DATA_DIR / "dashboards"
EXPORT_DIR = DATA_DIR / "exports"

SCHEMA = """
create table if not exists dim_tenant (tenant integer primary key, account_id text, name text, kind text,
  home_metro text, status text);
create table if not exists dim_site (site text primary key, metro text, techs integer);
create table if not exists fact_revenue_daily (date text, tenant integer, product text, mrr real,
  primary key (date, tenant, product));
create table if not exists fact_ops_daily (date text, hall text, site text, metro text, cooling text,
  cabinets_total integer, cabinets_sold integer, utility_kw real, contracted_kw real, it_kw real,
  facility_kw real, primary key (date, hall));
create table if not exists fact_tickets_daily (date text, site text, kind text, tickets integer, met integer,
  billed real, primary key (date, site, kind));
create table if not exists fact_pipeline_daily (date text, type text, stage text, n integer, mrr real,
  primary key (date, type, stage));
create table if not exists fact_bookings_daily (date text, type text, won integer, lost integer, won_mrr real,
  lost_mrr real, primary key (date, type));
create table if not exists fact_contract_events (date text, tenant integer, event text, mrr_before real,
  mrr_after real);
create table if not exists fact_energy_daily (date text, metro text, it_mwh real, facility_mwh real, tco2 real,
  primary key (date, metro));
create table if not exists fact_network_daily (date text primary key, incidents integer, customer_impacting integer,
  outage_service_minutes real, credits real, services integer, changes_done integer, changes_rolled_back integer,
  devices_down integer);
create table if not exists load_log (date text primary key, loaded_ts real, rows integer, seconds real);
"""

FACT_TABLES = ["fact_revenue_daily", "fact_ops_daily", "fact_tickets_daily", "fact_pipeline_daily",
               "fact_bookings_daily", "fact_contract_events", "fact_energy_daily", "fact_network_daily"]
DIM_TABLES = ["dim_tenant", "dim_site"]


def wh():
    c = sqlite3.connect(WH_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    c.executescript(SCHEMA)
    return c


def wrows(sql, args=()):
    with wh() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def wscalar(sql, args=()):
    with wh() as c:
        r = c.execute(sql, args).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- nightly load (Prism)


def load(date: str | None = None) -> dict:
    """Load one simulated day into the warehouse (idempotent: the day is replaced)."""
    from . import crm, products
    t0 = time.time()
    date = date or products.today()
    lines = products.lines_by_tenant()
    with wh() as c:
        c.execute("attach database ? as ops", (str(DB_PATH),))
        for t in FACT_TABLES:
            c.execute(f"delete from {t} where date=?", (date,))
        c.execute("delete from dim_tenant")
        c.execute("insert into dim_tenant select t.id, ?, t.name, t.kind, t.home_metro, coalesce(k.status,'active')"
                  " from ops.tenants t left join ops.contracts k on k.tenant=t.id", ("",))
        c.executemany("update dim_tenant set account_id=? where tenant=?",
                      [(crm.account_id(r[0]), r[0]) for r in c.execute("select tenant from dim_tenant").fetchall()])
        c.execute("delete from dim_site")
        c.execute("insert into dim_site select id, metro, techs from ops.sites")
        c.executemany("insert into fact_revenue_daily values(?,?,?,?)",
                      [(date, tid, k, round(v, 2)) for tid, d in lines.items() for k, v in d.items() if v])
        c.execute("insert into fact_ops_daily select ?, h.id, h.site, s.metro, h.cooling_type, h.cabinets_total,"
                  " h.cabinets_sold, h.utility_kw, h.contracted_kw, h.it_load_kw,"
                  " coalesce((select facility_kw from ops.telemetry t where t.hall=h.id order by t.id desc limit 1), h.it_load_kw*1.4)"
                  " from ops.halls h join ops.sites s on s.id=h.site", (date,))
        # tickets: today plus any earlier day not yet in the warehouse (backfills history on first load)
        c.execute("insert into fact_tickets_daily select sim_date, site, kind, count(*), sum(met), sum(billed_usd)"
                  " from ops.tickets where sim_date=? or sim_date not in (select distinct date from fact_tickets_daily)"
                  " group by sim_date, site, kind", (date,))
        c.execute("insert into fact_pipeline_daily select ?, Type, StageName, count(*), sum(MRR__c) from ops.crm_opportunity"
                  " where IsClosed=0 group by Type, StageName", (date,))
        c.execute("insert into fact_bookings_daily select ?, Type, sum(IsWon), sum(1-IsWon),"
                  " sum(case when IsWon=1 then MRR__c else 0 end), sum(case when IsWon=0 then MRR__c else 0 end)"
                  " from ops.crm_opportunity where IsClosed=1 and LastModifiedDate=? group by Type", (date, date))
        # contract events are few: mirror the full history every load
        c.execute("delete from fact_contract_events")
        c.execute("insert into fact_contract_events select sim_date, tenant, event, mrr_before, mrr_after"
                  " from ops.contract_events")
        c.execute("insert into fact_energy_daily select ?, s.metro, sum(t.it_kw)*24/1000, sum(t.facility_kw)*24/1000,"
                  " sum(t.facility_kw)*24/1000 * m.grid_gco2_kwh/1000 * (1 - m.renewable_pct/100.0)"
                  " from ops.telemetry t join (select hall, max(id) id from ops.telemetry group by hall) l on l.id=t.id"
                  " join ops.halls h on h.id=t.hall join ops.sites s on s.id=h.site join ops.metros m on m.code=s.metro"
                  " group by s.metro", (date,))
        if c.execute("select name from ops.sqlite_master where name='net_incident'").fetchone():
            services = sum(c.execute(f"select count(*) from ops.{t}").fetchone()[0]
                           for t in ("ports", "virtual_circuits", "cloud_connections", "ix_ports", "edge_devices"))
            c.execute("insert into fact_network_daily select ?, count(*), sum(services_down>0),"
                      " coalesce((select sum(i.minutes) from ops.net_impact i join ops.net_incident n2 on n2.id=i.incident"
                      " where n2.sim_date=?),0), coalesce(sum(credits_usd),0), ?,"
                      " (select count(*) from ops.net_change where window_date=? and status='done'),"
                      " (select count(*) from ops.net_change where window_date=? and status='rolled_back'),"
                      " (select count(*) from ops.net_device where status in ('down','unreachable'))"
                      " from ops.net_incident where sim_date=?", (date, date, services, date, date, date))
        n = sum(c.execute(f"select count(*) from {t} where date=?", (date,)).fetchone()[0] for t in FACT_TABLES)
        secs = round(time.time() - t0, 3)
        c.execute("insert or replace into load_log values(?,?,?,?)", (date, time.time(), n, secs))
        c.commit()
        c.execute("detach database ops")
    db.set_setting("bi_loaded", date)
    return {"date": date, "rows": n, "seconds": secs}


# ---------------------------------------------------------------- metrics layer

LATEST = "(select max(date) from fact_revenue_daily)"

METRICS = {
    "mrr": {"title": "Monthly recurring revenue", "unit": "usd", "kind": "scalar",
            "sql": f"select round(sum(mrr)) from fact_revenue_daily where date={LATEST}"},
    "mrr_trend": {"title": "MRR by day", "unit": "usd", "kind": "series",
                  "sql": "select date x, round(sum(mrr)) y from fact_revenue_daily where date>=date({LATEST},'-' || :days || ' day')"
                         " group by date order by date"},
    "mrr_by_product": {"title": "MRR by product", "unit": "usd", "kind": "table",
                       "sql": f"select product, round(sum(mrr)) mrr from fact_revenue_daily where date={LATEST}"
                              " group by product order by mrr desc"},
    "mrr_by_metro": {"title": "MRR by customer home metro", "unit": "usd", "kind": "table",
                     "sql": f"select d.home_metro metro, round(sum(f.mrr)) mrr from fact_revenue_daily f join dim_tenant d"
                            f" on d.tenant=f.tenant where f.date={LATEST} group by d.home_metro order by mrr desc"},
    "mrr_by_segment": {"title": "MRR by customer segment", "unit": "usd", "kind": "table",
                       "sql": f"select d.kind segment, round(sum(f.mrr)) mrr, count(distinct f.tenant) customers"
                              f" from fact_revenue_daily f join dim_tenant d on d.tenant=f.tenant where f.date={LATEST}"
                              " group by d.kind order by mrr desc"},
    "top_customers": {"title": "Top customers", "unit": "usd", "kind": "table",
                      "sql": f"select d.name customer, d.kind segment, round(sum(f.mrr)) mrr from fact_revenue_daily f"
                             f" join dim_tenant d on d.tenant=f.tenant where f.date={LATEST} group by f.tenant"
                             " order by mrr desc limit 10"},
    "bookings_trend": {"title": "New MRR booked per day", "unit": "usd", "kind": "series",
                       "sql": "select date x, round(sum(won_mrr)) y from fact_bookings_daily where type!='Renewal'"
                              f" and date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
    "win_rate": {"title": "Win rate (new business and expansion)", "unit": "pct", "kind": "scalar",
                 "sql": "select round(100.0*sum(won)/nullif(sum(won)+sum(lost),0),1) from fact_bookings_daily"
                        f" where type!='Renewal' and date>=date({LATEST},'-' || :days || ' day')"},
    "pipeline_by_stage": {"title": "Open pipeline by stage", "unit": "usd", "kind": "table",
                          "sql": f"select stage, type, n, round(mrr) mrr from fact_pipeline_daily where date={LATEST}"
                                 " order by type, stage"},
    "net_retention": {"title": "Net revenue retention of renewing customers", "unit": "pct", "kind": "scalar",
                      "sql": "select round(100.0*sum(mrr_after)/nullif(sum(mrr_before),0),1) from fact_contract_events"
                             f" where date>date({LATEST},'-' || :days || ' day')"},
    "churned_mrr": {"title": "MRR lost to churn and downsizing", "unit": "usd", "kind": "scalar",
                    "sql": "select coalesce(round(sum(mrr_before-mrr_after)),0) from fact_contract_events where event!='renewed'"
                           f" and date>date({LATEST},'-' || :days || ' day')"},
    "fleet_pue_trend": {"title": "Fleet PUE by day", "unit": "ratio", "kind": "series",
                        "sql": "select date x, round(sum(facility_kw)/sum(it_kw),3) y from fact_ops_daily"
                               f" where date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
    "utilization_trend": {"title": "Cabinet utilisation by day", "unit": "pct", "kind": "series",
                          "sql": "select date x, round(100.0*sum(cabinets_sold)/sum(cabinets_total),1) y from fact_ops_daily"
                                 f" where date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
    "ai_load_trend": {"title": "AI (liquid-cooled) IT load, MW", "unit": "mw", "kind": "series",
                      "sql": "select date x, round(sum(it_kw)/1000,2) y from fact_ops_daily where cooling='liquid'"
                             f" and date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
    "sla_trend": {"title": "Remote-hands SLA met, %", "unit": "pct", "kind": "series",
                  "sql": "select date x, round(100.0*sum(met)/sum(tickets),1) y from fact_tickets_daily"
                         f" where date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
    "sla_by_site": {"title": "Remote-hands SLA by site", "unit": "pct", "kind": "table",
                    "sql": "select site, sum(tickets) tickets, round(100.0*sum(met)/sum(tickets),1) sla_pct, round(sum(billed)) billed"
                           f" from fact_tickets_daily where date>=date({LATEST},'-' || :days || ' day') group by site order by sla_pct"},
    "tco2_trend": {"title": "Carbon, tCO2 per day (market-based)", "unit": "t", "kind": "series",
                   "sql": "select date x, round(sum(tco2),1) y from fact_energy_daily"
                          f" where date>=date({LATEST},'-' || :days || ' day') group by date order by date"},
}

METRICS.update({
    "network_availability_trend": {"title": "Network availability by day, %", "unit": "pct", "kind": "series",
                                   "sql": "select date x, round(100.0*(1 - outage_service_minutes/(services*1440.0)),4) y"
                                          " from fact_network_daily where date>=date({LATEST},'-' || :days || ' day') order by date"},
    "network_incidents_trend": {"title": "Network incidents per day", "unit": "count", "kind": "series",
                                "sql": "select date x, incidents y from fact_network_daily"
                                       " where date>=date({LATEST},'-' || :days || ' day') order by date"},
    "network_credits": {"title": "Network SLA credits", "unit": "usd", "kind": "scalar",
                        "sql": "select coalesce(round(sum(credits)),0) from fact_network_daily"
                               " where date>date({LATEST},'-' || :days || ' day')"},
    "network_changes": {"title": "Maintenance changes (done / rolled back)", "unit": "count", "kind": "table",
                        "sql": "select sum(changes_done) done, sum(changes_rolled_back) rolled_back from fact_network_daily"
                               " where date>date({LATEST},'-' || :days || ' day')"},
})

for _m in METRICS.values():
    _m["sql"] = _m["sql"].replace("{LATEST}", LATEST)

DEFAULT_DASHBOARDS = {
    "executive": {"title": "Executive", "owner": "Prism", "tiles": [
        {"metric": "mrr", "chart": "kpi"}, {"metric": "net_retention", "chart": "kpi"},
        {"metric": "win_rate", "chart": "kpi"}, {"metric": "churned_mrr", "chart": "kpi"},
        {"metric": "mrr_trend", "chart": "line", "wide": True}, {"metric": "mrr_by_product", "chart": "bar"},
        {"metric": "mrr_by_segment", "chart": "bar"}]},
    "sales": {"title": "Sales", "owner": "Prism", "tiles": [
        {"metric": "win_rate", "chart": "kpi"}, {"metric": "bookings_trend", "chart": "line", "wide": True},
        {"metric": "pipeline_by_stage", "chart": "table"}, {"metric": "top_customers", "chart": "table"},
        {"metric": "mrr_by_metro", "chart": "bar"}]},
    "network": {"title": "Network", "owner": "Prism", "tiles": [
        {"metric": "network_credits", "chart": "kpi"}, {"metric": "network_changes", "chart": "table"},
        {"metric": "network_availability_trend", "chart": "line", "wide": True},
        {"metric": "network_incidents_trend", "chart": "line", "wide": True}]},
    "operations": {"title": "Operations", "owner": "Prism", "tiles": [
        {"metric": "fleet_pue_trend", "chart": "line"}, {"metric": "utilization_trend", "chart": "line"},
        {"metric": "ai_load_trend", "chart": "line"}, {"metric": "tco2_trend", "chart": "line"},
        {"metric": "sla_trend", "chart": "line"}, {"metric": "sla_by_site", "chart": "table"}]},
}


def metric(name: str, days: int = 90) -> dict:
    if name not in METRICS:
        raise ValueError(f"unknown metric {name}; one of {sorted(METRICS)}")
    m = METRICS[name]
    with wh() as c:
        cur = c.execute(m["sql"], {"days": int(days)})
        if m["kind"] == "scalar":
            r = cur.fetchone()
            value = r[0] if r else None
        else:
            value = [dict(r) for r in cur.fetchall()]
    return {"name": name, "title": m["title"], "unit": m["unit"], "kind": m["kind"], "days": days, "value": value,
            "as_of": wscalar("select max(date) from fact_revenue_daily")}


def metrics_catalog() -> list[dict]:
    return [{"name": k, "title": v["title"], "unit": v["unit"], "kind": v["kind"]} for k, v in METRICS.items()]


def dashboards() -> dict:
    out = dict(DEFAULT_DASHBOARDS)
    if DASH_DIR.exists():
        for f in sorted(DASH_DIR.glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text())
            except ValueError:
                pass
    return out


def save_dashboard(slug: str, spec: dict) -> dict:
    if not slug.replace("_", "").replace("-", "").isalnum():
        raise ValueError("slug: letters, digits, - and _ only")
    for t in spec.get("tiles", []):
        if t.get("metric") not in METRICS:
            raise ValueError(f"unknown metric {t.get('metric')}")
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    (DASH_DIR / f"{slug}.json").write_text(json.dumps(spec, indent=2))
    return {"saved": slug, "tiles": len(spec.get("tiles", []))}


def dashboard(slug: str, days: int = 90) -> dict:
    d = dashboards().get(slug)
    if not d:
        raise ValueError(f"no dashboard {slug}; one of {list(dashboards())}")
    return {**d, "slug": slug, "days": days, "tiles": [{**t, "data": metric(t["metric"], t.get("days", days))}
                                                         for t in d["tiles"]]}


# ---------------------------------------------------------------- exports


def export(date: str | None = None) -> dict:
    """Write every warehouse table to CSV, and to one .hyper extract when Tableau's Hyper API is installed."""
    from . import products
    date = date or products.today()
    out_dir = EXPORT_DIR / date
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = DIM_TABLES + FACT_TABLES
    counts = {}
    with wh() as c:
        for t in tables:
            cur = c.execute(f"select * from {t}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            with open(out_dir / f"{t}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
            counts[t] = len(rows)
    hyper = _hyper(out_dir / "vix_warehouse.hyper", tables)
    (out_dir / "README.txt").write_text(
        "vix warehouse export (simulated data).\n"
        "Open vix_warehouse.hyper in Tableau Desktop or Tableau Public (Connect > More... > Tableau extract),\n"
        "or load the CSV files into any BI tool. Join facts to dim_tenant on tenant and dim_site on site.\n")
    return {"dir": str(out_dir), "tables": counts, "hyper": hyper}


def _hyper(path, tables) -> dict:
    try:
        from tableauhyperapi import (Connection, CreateMode, HyperProcess, Inserter, SqlType, TableDefinition,
                                     TableName, Telemetry)
    except ImportError:
        return {"written": False, "reason": "tableauhyperapi not installed (pip install tableauhyperapi)"}
    if path.exists():
        path.unlink()
    with wh() as c, HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(DATA_DIR)}) as hp, \
            Connection(endpoint=hp.endpoint, database=str(path), create_mode=CreateMode.CREATE_AND_REPLACE) as hc:
        hc.catalog.create_schema("Extract")
        for t in tables:
            info = c.execute(f"pragma table_info({t})").fetchall()
            cols = [TableDefinition.Column(r[1], SqlType.big_int() if "int" in (r[2] or "").lower()
                                           else SqlType.double() if "real" in (r[2] or "").lower() else SqlType.text())
                    for r in info]
            td = TableDefinition(TableName("Extract", t), cols)
            hc.catalog.create_table(td)
            rows = c.execute(f"select * from {t}").fetchall()
            with Inserter(hc, td) as ins:
                ins.add_rows([tuple(r) for r in rows])
                ins.execute()
    return {"written": True, "path": str(path), "bytes": path.stat().st_size}


def exports() -> list[dict]:
    if not EXPORT_DIR.exists():
        return []
    out = []
    for d in sorted(EXPORT_DIR.iterdir(), reverse=True):
        if d.is_dir():
            out.append({"date": d.name, "files": sorted(f.name for f in d.iterdir())})
    return out


def status() -> dict:
    return {"warehouse": str(WH_PATH), "last_load": wrows("select * from load_log order by date desc limit 1"),
            "days_loaded": wscalar("select count(*) from load_log"),
            "tables": {t: wscalar(f"select count(*) from {t}") for t in DIM_TABLES + FACT_TABLES}}
