# vix: Virtual Interconnection eXchange

A headless digital-infrastructure company that exists entirely in simulation:
colocation sites in 8 metros (air halls and liquid-cooled AI halls), cross-connects,
a fabric of virtual circuits, private cloud on-ramps and a cloud router, Network Edge
virtual devices, internet exchange ports, bare metal, remote hands, contracts with
renewals and churn, energy, finance, and an R&D lab that designs network gear. Agents run every department; two super agents run the agents;
the founder approves decisions. Claude operates it from chat over MCP.

Every site, tenant and number is simulated and seeded. No real company is modeled.

## Org chart

| Tier | Department | Agent | Playbooks |
|---|---|---|---|
| Super | Office of the Chief Operator | **Atlas** | `exec_operating_cycle {days}` (one simulated day, or fast-forward up to 365), `exec_weekly_review` (files decisions) |
| Super | Research & Development (Chief Scientist) | **Euler** | `rnd_research_cycle` (delegates studies, proposes standards), `rnd_router_study`, `rnd_cooling_study` |
| Dept | Sales & Customers | Broker | `sales_demand_cycle` |
| Dept | Interconnection | Weaver | `ix_fulfill_orders` (ports, cross-connects, virtual circuits, cloud connections, exchange ports) |
| Dept | Digital Services | Relay | `digital_fulfill_orders` (cloud routers, Network Edge) |
| Dept | Customer Operations | Concierge | `cx_ticket_desk` (remote hands, SLAs, staffing decisions) |
| Dept | Account Management | Keeper | `accounts_renewals` (escalators, renewals, downsizing, churn) |
| Dept | Site Operations | Warden | `ops_fulfill_space`, `ops_daily_health`, `ops_incident_drill` |
| Dept | Compute (Metal) | Forge | `metal_fulfill_orders` |
| Dept | Network Engineering | Pathfinder | `net_bgp_study`, `net_latency_map` |
| Dept | Energy & Sustainability | Steward | `energy_report` |
| Dept | Finance | Ledger | `fin_monthly_close` |

Super agents delegate: their runs start child runs in the departments and write a
summary that links each child report. Irreversible changes (a new hall, a network
standard, extra cooling) become **decisions** the founder approves in HQ or from chat.

## Calendar

The company runs on a simulated calendar starting 2026-10-16. Each operating cycle
advances it one day; `exec_operating_cycle` with `days: 90` fast-forwards a quarter
(about 50 seconds on one core) and writes one week-by-week report. Renewals,
remote-hands SLAs and retention all run on this calendar.

## Simulators

- **Facilities** (`vix/thermal.py`): hourly outside-air model per metro, free cooling
  below 18 C, chiller COP falling with heat, UPS and fan losses, PUE; minute-by-minute
  drills for cooling loss and utility outage (UPS ride-through, generator starts).
- **Network** (`vix/netsim.py`): message-level BGP at each exchange with Gao-Rexford
  policy, a transparent route server, and events: withdraw, leak, hijack, fiber cut.
  Optional route-server filtering (origin validation + customer cone) and carrier
  edge filtering.
- **R&D router lab** (`vix/routerlab.py`): slot-level output-port simulation with
  heavy-tailed bursty sources; compares buffer-sizing rules and sizes a switch design
  against on-chip SRAM.
- **Liquid cooling**: AI halls capture 85% of heat in a warm-water loop rejected by dry
  coolers below 30 C outside; drills track coolant return against a 45 C throttle limit.
- **Remote hands**: Poisson tickets per site, priority queue across the site's
  technicians, SLA per ticket kind, billing per 30 minutes.
- **Renewals**: renewal odds rise with cross-connects and cloud connections (stickiness)
  and fall with SLA misses and steep escalators; losses are downsizes or full exits.
- **Latency**: great-circle distance x 1.45 route factor at 200 km/ms.

## Upgrading from v0.1

Run the new `install.sh` over the old install. The database is migrated in place
(new tables and columns) and the new product lines are seeded around your existing
world, tenants, runs and decisions.

## Run

```
./install.sh          # copies into ~/Sites/vix, venv, deps, git commit
cd ~/Sites/vix && ./serve.sh
```

- HQ: http://127.0.0.1:47530
- MCP: http://127.0.0.1:47531/mcp

To reach it from claude.ai, set `VIX_MCP_TOKEN` and `VIX_MCP_PUBLIC_HOST` in `.env`,
route a hostname to port 47531 in your Cloudflare Tunnel, and add
`https://<host>/mcp/<token>` as a custom connector.

Default schedules (Mac local time): operating cycle daily 02:10, weekly review
Monday 06:40, research cycle Saturday 03:20. Schedules fire while `serve.sh`'s web
process is running.
