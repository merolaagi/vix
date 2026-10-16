"""MCP server: how Claude (claude.ai custom connector via Cloudflare Tunnel, Claude
Desktop, or a local agent) operates the company.

Run:  python -m vix.mcp_server   (streamable HTTP on 127.0.0.1:47531/mcp)
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time

from mcp.server.mcpserver import MCPServer

from . import __version__, db, netsim, routerlab, thermal, world
from .company import charter as charter_mod
from .company import engine
from .config import MCP_PORT

mcp = MCPServer(
    name="vix",
    version=__version__,
    instructions=(
        "vix is a headless, fully simulated digital-infrastructure company (colocation, interconnection, "
        "bare metal) in 8 metros. Start with company_overview: charter, departments, KPIs, open decisions, "
        "schedules. Two super agents run it: Atlas (Chief Operator: exec_operating_cycle, exec_weekly_review) "
        "and Euler (Chief Scientist: rnd_research_cycle). company_run starts a playbook and returns a run id; "
        "poll company_run_status, then read company_report. Super agents file decisions; only call "
        "company_decide when the founder explicitly says to approve or reject. The world_* tools read the "
        "simulated sites, halls, tenants and orders; place_order adds demand. sim_bgp, sim_drill and "
        "sim_router run the network, facility and switch simulators directly for R&D questions. "
        "Every number is simulated; nothing is a real facility or customer."
    ),
)


# ---------- company ----------

@mcp.tool()
def company_overview() -> dict:
    """Charter, departments (with super agents) and their playbooks and last runs, KPIs,
    open decisions and next scheduled runs."""
    return engine.overview()


@mcp.tool()
def company_run(playbook: str, params: dict | None = None) -> dict:
    """Start a playbook in the background and return its run id. Super agents:
    exec_operating_cycle (daily: demand -> colocation -> interconnection -> metal -> site health -> energy),
    exec_weekly_review (books + capacity review, files decisions), rnd_research_cycle {metro}.
    Departments: sales_demand_cycle {days, orders_per_day, new_logo_rate}, ops_fulfill_space,
    ix_fulfill_orders, metal_fulfill_orders, ops_daily_health {fault_rate},
    ops_incident_drill {hall, kind: cooling|utility|fiber, units_lost, repair_min},
    net_bgp_study {metro, event: baseline|withdraw|leak|hijack|fiber_cut, rs_filtering, edge_filtering},
    net_latency_map, energy_report, fin_monthly_close,
    rnd_router_study {link_gbps, rtt_ms, flows, ports, design_load}."""
    return {"run_id": engine.start(playbook, params or {}, trigger="mcp"),
            "next": "poll company_run_status(run_id); when done, company_report(report)"}


@mcp.tool()
def company_run_status(run_id: int) -> dict:
    """Status, summary, report file name, child runs and the agent log of one run."""
    r = engine.run_get(run_id)
    if not r:
        return {"error": f"no run {run_id}"}
    r["log"] = (r.get("log") or "")[-3000:]
    return r


@mcp.tool()
def company_runs(limit: int = 20, department: str | None = None) -> list[dict]:
    """Recent runs, newest first, optionally for one department
    (exec, rnd, sales, ix, ops, metal, net, energy, finance)."""
    out = engine.runs(limit, department)
    for r in out:
        r.pop("log", None)
    return out


@mcp.tool()
def company_report(name: str) -> str:
    """Read a run report (markdown) by file name."""
    return engine.safe_report(name).read_text()


@mcp.tool()
def company_decisions(status: str | None = "open") -> list[dict]:
    """Decisions filed by the super agents: 'open', 'approved', 'rejected', or None for all."""
    return engine.decisions(status)


@mcp.tool()
def company_decide(decision_id: int, approve: bool, note: str = "") -> dict:
    """Approve or reject a decision. Approving applies it to the world (builds the hall,
    adopts the network standard, adds cooling). Only call this when the founder has
    explicitly said to approve or reject that decision."""
    return engine.decide(decision_id, approve, note)


@mcp.tool()
def company_schedule(action: str, playbook: str | None = None, every: str | None = None,
                     params: dict | None = None, schedule_id: int | None = None) -> list[dict]:
    """Manage schedules. action: 'list' | 'add' (playbook, every, params) | 'pause' | 'resume' |
    'delete' (schedule_id). every: 'hourly', 'daily HH:MM' or 'weekly mon HH:MM' in the Mac's
    local time. Schedules fire while the web process is running."""
    if action == "add":
        engine.schedule_add(playbook, every or "", params or {})
    elif action in ("pause", "resume"):
        engine.schedule_set(schedule_id, enabled=(action == "resume"))
    elif action == "delete":
        engine.schedule_set(schedule_id, delete=True)
    return engine.schedules()


@mcp.tool()
def company_update_charter(update: dict) -> dict:
    """Change charter fields (tagline, mission, model, scope, non_goals, principles, targets).
    Only the founder should ask for this; confirm with them before calling."""
    return charter_mod.save(update)


# ---------- world ----------

@mcp.tool()
def world_metros() -> list[dict]:
    """Every metro: climate, grid carbon, power price, sites, cabinets, IT load, cross-connects, tenants present."""
    return world.metros_summary()


@mcp.tool()
def world_sites() -> list[dict]:
    """Every site with its halls, cabinets, IT load, ports and cross-connects."""
    return world.sites()


@mcp.tool()
def world_halls(metro: str | None = None) -> list[dict]:
    """Halls with cabinets, power (utility, contracted, IT load), cooling units and utilisation."""
    return world.halls(metro)


@mcp.tool()
def world_tenants(kind: str | None = None, limit: int = 50) -> list[dict]:
    """Tenants (carrier, cloud, content, enterprise, financial) with ASN, cabinets and ports."""
    return world.tenants(kind, limit)


@mcp.tool()
def world_tenant(tenant_id: int) -> dict:
    """One tenant: placements, ports, metal servers and monthly recurring revenue."""
    return world.tenant(tenant_id) or {"error": f"no tenant {tenant_id}"}


@mcp.tool()
def world_orders(status: str | None = None, limit: int = 50) -> list[dict]:
    """Orders, newest first; status: pending, waitlisted, done, rejected."""
    q, args = "select * from orders", ()
    if status:
        q, args = q + " where status=?", (status,)
    return db.rows(q + " order by id desc limit ?", args + (limit,))


@mcp.tool()
def place_order(tenant_id: int, kind: str, params: dict) -> dict:
    """Add a customer order (fulfilled by the next department run). kind and params:
    cabinets {metro, cabinets, kw_per_cabinet}; port {site, speed_g}; cross_connect {site, z_tenant};
    vc {a_metro, z_metro, mbps}; metal {metro, plan: s1.small|m1.large|g1.gpu, count}."""
    return world.place_order(tenant_id, kind, params)


@mcp.tool()
def world_latency() -> dict:
    """Round-trip-time matrix between all metros and the model behind it."""
    return world.latency_matrix()


# ---------- simulators ----------

@mcp.tool()
async def sim_bgp(metro: str = "ASH", event: str = "leak", rs_filtering: bool | None = None,
                  edge_filtering: bool | None = None, target_asn: int | None = None, max_as: int = 48) -> dict:
    """Run the BGP exchange simulator directly (no report). event: baseline | withdraw | leak |
    hijack | fiber_cut. Filtering defaults to the company standard."""
    return await asyncio.to_thread(netsim.study, metro, event, target_asn, rs_filtering, edge_filtering, max_as)


@mcp.tool()
def sim_drill(hall: str, kind: str = "cooling", units_lost: int = 2, repair_min: int = 45) -> dict:
    """Run a facility failure drill directly: kind cooling | utility | fiber."""
    h = next((x for x in world.halls() if x["id"] == hall), None)
    if not h:
        return {"error": f"no hall {hall}"}
    return thermal.drill(h, world.metro(h["metro"]), kind, units_lost=units_lost, repair_min=repair_min)


@mcp.tool()
async def sim_router(link_gbps: float = 100, rtt_ms: float = 10, flows: int = 400, ports: int = 32,
                     design_load: float = 0.9, loads: list[float] | None = None) -> dict:
    """Run the switch output-port simulator: loss and delay across loads and buffer-sizing rules,
    plus a sized switch design. Open-loop traffic; see the caveat in the result."""
    return await asyncio.to_thread(routerlab.study, link_gbps, rtt_ms, flows, 32, loads, 200_000, 0.001,
                                   ports, design_load)


class _TokenGate:
    """ASGI wrapper used when VIX_MCP_TOKEN is set. A request is let in if either
    - its path is /mcp/<token> (the token is part of the connector URL), or
    - it sends 'Authorization: Bearer <token>'.
    OAuth discovery paths get 404 so clients don't assume a sign-in is needed."""

    OAUTH_PATHS = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server",
                   "/.well-known/openid-configuration", "/register", "/authorize", "/token")

    def __init__(self, app, token):
        self.app, self.token = app, token
        self.secret_prefix = f"/mcp/{token}"

    @staticmethod
    async def _reply(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    def _log(self, scope, status, note=""):
        client = (scope.get("client") or ("?", 0))[0]
        path = scope.get("path", "").replace(self.token, "<token>")
        print(f"{time.strftime('%H:%M:%S')} {client} {scope.get('method')} {path} -> {status}"
              f"{' (' + note + ')' if note else ''}", flush=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith(self.OAUTH_PATHS):
            self._log(scope, 404, "no OAuth on this server")
            return await self._reply(send, 404, b'{"error":"not found"}')
        ok, how = False, ""
        if path == self.secret_prefix or path.startswith(self.secret_prefix + "/"):
            ok, how = True, "token in URL"
            rest = path[len(self.secret_prefix):] or ""
            scope = dict(scope, path="/mcp" + rest, raw_path=("/mcp" + rest).encode())
        else:
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            if hmac.compare_digest(auth.encode(), f"Bearer {self.token}".encode()):
                ok, how = True, "bearer header"
            else:
                how = "missing or wrong token"
        if not ok:
            self._log(scope, 401, how)
            return await self._reply(send, 401, b'{"error":"unauthorized"}')
        status = {}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)
        await self.app(scope, receive, _send)
        self._log(scope, status.get("code", "?"), how)


def main():
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    db.init()
    world.seed()
    token = os.environ.get("VIX_MCP_TOKEN", "").strip()
    public_host = os.environ.get("VIX_MCP_PUBLIC_HOST", "").strip()
    security = None
    if public_host:
        if not token:
            raise SystemExit("VIX_MCP_PUBLIC_HOST is set but VIX_MCP_TOKEN is empty: refusing to serve publicly without a token.")
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", f"127.0.0.1:{MCP_PORT}", "localhost", f"localhost:{MCP_PORT}", public_host],
            allowed_origins=[f"https://{public_host}", "https://claude.ai"])
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security)
    if token:
        app = _TokenGate(app, token)
    uvicorn.run(app, host="127.0.0.1", port=MCP_PORT, log_level="info", access_log=not token)


if __name__ == "__main__":
    main()
