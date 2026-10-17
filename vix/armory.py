"""Tools Armory: on-demand evidence tools, most backed by REA (Reverse Engineer Anything,
github.com/morluto/rea), run locally through npx.

  rea_doctor          is REA ready on this machine?
  rea_inspect_db      REA's SQLite inspector on a consistent snapshot of the ops, CRM or warehouse
                      database: independent schema/row evidence with a content hash
  api_contract_check  call vix's own Salesforce-shaped and BI APIs, record a HAR, check every
                      response against the contract the agents rely on, and have REA inspect the HAR
  public_recon        capture one PUBLIC page (no sign-in, no cookies) to HAR and have REA inspect it,
                      e.g. a vendor's published API documentation, for design reference

REA's SQLite inspector needs Node 24.15+, so by default REA runs under a pinned Node 24 from npm.
Override with VIX_REA_CMD (e.g. "rea" if installed globally on a new enough Node)."""
from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA_DIR, DB_PATH, WEB_PORT

ARMORY_DIR = DATA_DIR / "armory"
DEFAULT_REA = "npx -y -p node@24 -p rea-agents@latest rea"


def rea_cmd() -> list[str]:
    return shlex.split(os.environ.get("VIX_REA_CMD", DEFAULT_REA))


def run_rea(args: list[str], timeout: int = 240) -> dict:
    cmd = rea_cmd() + args + ["--format", "json"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(DATA_DIR))
    except FileNotFoundError:
        return {"ok": False, "error": "npx not found: install Node.js 22+ (brew install node)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"REA timed out after {timeout}s"}
    out = p.stdout.strip()
    try:
        data = json.loads(out[out.index("{"):]) if "{" in out else None
    except ValueError:
        data = None
    if data is None:
        return {"ok": False, "error": (p.stderr or out)[-800:], "seconds": round(time.time() - t0, 1)}
    if "code" in data and "evidence_id" not in data:
        return {"ok": False, "error": data.get("message", str(data))[:800], "code": data.get("code"),
                "seconds": round(time.time() - t0, 1)}
    return {"ok": True, "evidence": data, "seconds": round(time.time() - t0, 1)}


def _snapshot(which: str) -> Path:
    from . import bi
    src = {"ops": DB_PATH, "crm": DB_PATH, "warehouse": bi.WH_PATH}.get(which)
    if not src:
        raise ValueError("db must be ops, crm or warehouse")
    ARMORY_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARMORY_DIR / f"snapshot-{which}.db"
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst


# ---------------------------------------------------------------- tools


def rea_doctor(_: dict) -> dict:
    r = run_rea(["doctor"], timeout=240)
    if not r["ok"]:
        return r
    ev = r["evidence"]
    return {"ok": True, "summary": {k: ev[k] for k in list(ev)[:6]}, "seconds": r["seconds"]}


def rea_inspect_db(args: dict) -> dict:
    which = args.get("db", "crm")
    snap = _snapshot(which)
    a = ["inspect-sqlite-database", str(snap)]
    table = args.get("table")
    if table:
        a += ["--table", table, "--rowLimit", str(int(args.get("rows", 5)))]
    else:
        a += ["--schemaScope", "inventory"]
    r = run_rea(a)
    if not r["ok"]:
        return r
    ev = r["evidence"]
    res = ev.get("normalized_result") or {}
    tables = (res.get("schema") or {}).get("tables", [])
    if which == "crm":
        tables = [t for t in tables if t["name"].startswith("crm_")] if not table else tables
    out = {"ok": True, "db": which, "evidence_id": ev.get("evidence_id"), "sha256": (ev.get("subject") or {}).get("digest", {}).get("sha256"),
           "engine": (res.get("engine") or {}).get("version"), "tables": [{"name": t["name"], "columns": t.get("column_count")} for t in tables],
           "seconds": r["seconds"]}
    if table:
        out["columns"] = [c["name"] for c in (tables[0].get("columns") if tables else [])]
        rows = res.get("rows") or {}
        vals = rows.get("values", []) if isinstance(rows, dict) else []
        out["rows"] = [[c.get("value") if isinstance(c, dict) else c for c in row] for row in vals]
    return out


CONTRACT = [
    # (path, check name, validator)
    ("/services/data/v60.0/sobjects", "sobjects list", lambda j: isinstance(j.get("sobjects"), list) and len(j["sobjects"]) >= 6),
    ("/services/data/v60.0/sobjects/Account/describe", "Account describe",
     lambda j: j.get("keyPrefix") == "001" and any(f["name"] == "Name" for f in j.get("fields", []))),
    ("/services/data/v60.0/query?q=SELECT+Id,Name,MRR__c+FROM+Account+WHERE+Type='Customer'+ORDER+BY+MRR__c+DESC+LIMIT+5",
     "SOQL accounts", lambda j: {"totalSize", "done", "records"} <= set(j) and all("attributes" in r for r in j["records"])),
    ("/services/data/v60.0/query?q=SELECT+COUNT()+FROM+Opportunity+WHERE+IsClosed=false", "SOQL count",
     lambda j: isinstance(j.get("totalSize"), int)),
    ("/services/data/v60.0/query?q=SELECT+Id,StageName,MRR__c+FROM+Opportunity+LIMIT+3", "SOQL opportunities",
     lambda j: isinstance(j.get("records"), list)),
    ("/api/bi/metric/mrr", "BI metric mrr", lambda j: j.get("kind") == "scalar" and "value" in j),
    ("/api/bi/dashboard/executive", "BI dashboard", lambda j: isinstance(j.get("tiles"), list) and len(j["tiles"]) > 0),
]


def api_contract_check(args: dict) -> dict:
    import httpx
    base = args.get("base", f"http://127.0.0.1:{WEB_PORT}")
    entries, checks = [], []
    with httpx.Client(base_url=base, timeout=30) as c:
        for path, name, ok in CONTRACT:
            t0 = time.time()
            started = dt.datetime.now(dt.timezone.utc).isoformat()
            try:
                r = c.get(path)
            except httpx.HTTPError as e:
                return {"ok": False, "error": f"web API not reachable at {base}: {e}"}
            ms = round((time.time() - t0) * 1000, 1)
            try:
                body = r.json()
                passed = r.status_code == 200 and bool(ok(body))
            except ValueError:
                body, passed = None, False
            checks.append({"check": name, "status": r.status_code, "passed": passed, "ms": ms})
            entries.append(_har_entry(started, ms, str(r.request.url), r))
    ARMORY_DIR.mkdir(parents=True, exist_ok=True)
    har = ARMORY_DIR / f"api-contract-{int(time.time())}.har"
    har.write_text(json.dumps({"log": {"version": "1.2", "creator": {"name": "vix armory", "version": "0.3"},
                                        "entries": entries}}))
    rea = run_rea(["inspect-web-network-capture", str(har), "har"])
    out = {"ok": all(c["passed"] for c in checks), "checks": checks, "har": har.name,
           "passed": sum(c["passed"] for c in checks), "total": len(checks)}
    if rea["ok"]:
        ev = rea["evidence"]
        nr = ev.get("normalized_result") or {}
        out["rea"] = {"evidence_id": ev.get("evidence_id"), "sha256": (ev.get("subject") or {}).get("digest", {}).get("sha256"),
                      "records": len(nr.get("records") or nr.get("entries") or []) or nr.get("record_count"),
                      "seconds": rea["seconds"]}
    else:
        out["rea"] = {"error": rea.get("error")}
    return out


def _har_entry(started, ms, url, r):
    u = urlparse(url)
    return {
        "startedDateTime": started, "time": ms,
        "request": {"method": "GET", "url": url, "httpVersion": "HTTP/1.1", "headers": [],
                    "queryString": [{"name": k, "value": v} for k, _, v in [q.partition("=") for q in u.query.split("&") if q]],
                    "cookies": [], "headersSize": -1, "bodySize": 0},
        "response": {"status": r.status_code, "statusText": r.reason_phrase, "httpVersion": "HTTP/1.1",
                     "headers": [{"name": "content-type", "value": r.headers.get("content-type", "")}], "cookies": [],
                     "content": {"size": len(r.content), "mimeType": r.headers.get("content-type", "")},
                     "redirectURL": "", "headersSize": -1, "bodySize": len(r.content)},
        "cache": {}, "timings": {"send": 0, "wait": ms, "receive": 0},
    }


BLOCKED_PATH_WORDS = ("login", "signin", "sign-in", "auth", "account", "checkout", "billing", "admin", "secur")


def public_recon(args: dict) -> dict:
    url = args.get("url", "")
    u = urlparse(url)
    if u.scheme != "https" or not u.netloc:
        return {"ok": False, "error": "give a full https:// URL of a public page"}
    if any(w in u.path.lower() for w in BLOCKED_PATH_WORDS):
        return {"ok": False, "error": "public pages only: sign-in, account and admin pages are out of scope"}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright is not installed: .venv/bin/pip install playwright && "
                                      ".venv/bin/python -m playwright install chromium"}
    ARMORY_DIR.mkdir(parents=True, exist_ok=True)
    har = ARMORY_DIR / f"recon-{u.netloc}-{int(time.time())}.har"
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(record_har_path=str(har), record_har_content="omit")
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=45000)
        title = page.title()
        ctx.close()
        b.close()
    data = json.loads(har.read_text())
    ents = data["log"]["entries"]
    hosts = {}
    for e in ents:
        h = urlparse(e["request"]["url"]).netloc
        hosts[h] = hosts.get(h, 0) + 1
    api_like = [e["request"]["url"].split("?")[0] for e in ents
                if "json" in (e["response"].get("content", {}).get("mimeType") or "")][:25]
    rea = run_rea(["inspect-web-network-capture", str(har), "har"])
    return {"ok": True, "url": url, "title": title, "requests": len(ents), "hosts": hosts, "json_endpoints": api_like,
            "har": har.name, "rea": {"evidence_id": rea["evidence"].get("evidence_id")} if rea["ok"] else {"error": rea.get("error")}}


TOOLS = {
    "rea_doctor": (rea_doctor, "Check that REA can run on this machine.", {}),
    "rea_inspect_db": (rea_inspect_db, "REA SQLite inspector on a snapshot of the ops, CRM or warehouse database.",
                       {"db": "crm | warehouse | ops", "table": "optional table name", "rows": "rows to read (default 5)"}),
    "api_contract_check": (api_contract_check, "Exercise vix's Salesforce-shaped and BI APIs, validate the contract, "
                                               "and have REA inspect the recorded HAR.", {}),
    "public_recon": (public_recon, "Capture one public https page to HAR and have REA inspect it (no sign-in pages).",
                     {"url": "https URL of a public page"}),
}


def tools() -> list[dict]:
    return [{"name": k, "description": v[1], "params": v[2]} for k, v in TOOLS.items()]


def run(name: str, args: dict | None = None) -> dict:
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}; one of {list(TOOLS)}")
    return TOOLS[name][0](args or {})
