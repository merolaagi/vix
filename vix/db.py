"""One SQLite file holds the simulated world and the company's records."""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

SCHEMA = """
create table if not exists metros (
  code text primary key, name text, region text, lat real, lon real,
  grid_gco2_kwh real, renewable_pct real, ambient_mean_c real, ambient_swing_c real,
  power_usd_kwh real
);
create table if not exists sites (
  id text primary key, metro text, name text, opened integer, tier text
);
create table if not exists halls (
  id text primary key, site text, name text,
  cabinets_total integer, cabinets_sold integer,
  utility_kw real, contracted_kw real, it_load_kw real,
  crah_units integer, crah_kw_each real, crah_down integer default 0,
  ups_minutes real, generators integer, build_capex_usd real, built_ts real
);
create table if not exists tenants (
  id integer primary key, name text, kind text, asn integer, home_metro text, prefixes integer,
  since_ts real
);
create table if not exists placements (
  id integer primary key, tenant integer, hall text, cabinets integer, kw real, ts real
);
create table if not exists ports (
  id integer primary key, tenant integer, site text, speed_g integer, ts real
);
create table if not exists cross_connects (
  id integer primary key, site text, a_port integer, z_port integer, ts real
);
create table if not exists virtual_circuits (
  id integer primary key, a_port integer, z_port integer, mbps integer, rtt_ms real, ts real
);
create table if not exists metal_inventory (
  metro text, plan text, total integer, allocated integer, primary key (metro, plan)
);
create table if not exists metal_servers (
  id integer primary key, tenant integer, metro text, plan text, ts real
);
create table if not exists orders (
  id integer primary key, ts real, tenant integer, kind text, params text,
  status text, note text, done_ts real
);
create table if not exists telemetry (
  id integer primary key, ts real, hall text, it_kw real, facility_kw real, pue real,
  inlet_c real, ambient_c real
);
create table if not exists runs (
  id integer primary key, playbook text, department text, params text, status text,
  trigger text, parent integer, started real, finished real, report text, summary text, log text
);
create table if not exists schedules (
  id integer primary key, playbook text, params text, every text, enabled integer default 1,
  last_run real
);
create table if not exists decisions (
  id integer primary key, ts real, agent text, kind text, title text, detail text,
  status text, resolved_ts real, note text
);
create table if not exists settings (key text primary key, value text);
"""

_lock = threading.Lock()


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("pragma journal_mode=wal")
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)


def rows(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def scalar(sql, args=()):
    with conn() as c:
        r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def execute(sql, args=()):
    with _lock, conn() as c:
        return c.execute(sql, args).lastrowid


def setting(key, default=None):
    v = scalar("select value from settings where key=?", (key,))
    return json.loads(v) if v is not None else default


def set_setting(key, value):
    execute("insert into settings(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
            (key, json.dumps(value)))


def now():
    return time.time()
