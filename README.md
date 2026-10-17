# vix: Virtual Interconnection eXchange

A headless digital-infrastructure company that exists entirely in simulation:
colocation sites in 8 metros (air halls and liquid-cooled AI halls), cross-connects,
a fabric of virtual circuits, private cloud on-ramps and a cloud router, Network Edge
virtual devices, internet exchange ports, bare metal, remote hands, contracts with
renewals and churn, energy, finance, an R&D lab that designs network gear, and a
headless data stack: a Salesforce-shaped CRM and a Tableau-style BI layer. Agents run
every department, chiefs run the agents, a CEO agent reports to the founder, and
Claude operates it from chat over MCP.

Every site, tenant and number is simulated and seeded. No real company is modeled.

## Chain of command

```
Founder (you)
└── Helm, CEO: ceo_board_pack (weekly board pack with a recommendation on every decision)
    ├── Atlas, Chief Operator: operations departments
    ├── Euler, Chief Scientist: R&D
    ├── Meridian, Chief Network Officer: network health, live readiness
    │   ├── Sentinel, NOC: noc_watch (poll, diagnose, remediate, SLA credits)
    │   ├── Tempo, Change Management: change_manage (firmware, CVEs, windows, rollback)
    │   ├── Gauge, Network Capacity: capacity_plan (ports, backbone, capex decisions)
    │   └── Pathfinder, Network Engineering: net_bgp_study, net_latency_map
    └── Vega, Chief Data Officer: cdo_data_audit (reconciliation + REA evidence)
        ├── Scribe, CRM Operations: crm_sync
        └── Prism, Business Intelligence: bi_nightly_load, bi_export
```

## Org chart

| Tier | Department | Agent | Playbooks |
|---|---|---|---|
| Chief | Office of the CEO | **Helm** | `ceo_board_pack` |
| Super | Office of the Chief Data Officer | **Vega** | `cdo_data_audit` |
| Dept | CRM Operations | Scribe | `crm_sync` |
| Dept | Business Intelligence | Prism | `bi_nightly_load`, `bi_export` |
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

## Network infrastructure (simulated now, real-gear ready)

Every site has core routers, spine and leaf switches, firewalls; each metro has exchange
switches and DWDM optical gear; 14 backbone links with diverse paths join the metros.
Vendors are invented families modeled on the real archetypes (Corvid Networks chassis
routers, Tessell merchant-silicon switches, Halcyon optical, Bastion firewalls), each with
firmware releases, bug rates and security advisories. Every customer port, exchange port,
virtual circuit and cloud connection is mapped to the gear it rides on, so a failed
switch takes down exactly the single-homed customers behind it.

**Driver layer.** Agents never touch the simulator. They call `NetworkControl`, which picks
a driver per device: `SimDriver` returns NAPALM's data formats from the simulation;
`NapalmDriver` talks to real gear (Cisco IOS/IOS-XR/NX-OS, Juniper Junos, Arista EOS via
NAPALM). The NOC's detection and diagnosis code is identical for both.

**Guardrails for real gear.** Imported devices arrive `live-readonly`: polled, never
changed. Automated changes need all of: the global kill switch on (founder decision), the
device set to `live` (founder decision), an approved change, today's maintenance window,
and NAPALM commit-confirm with post-checks (rolled back otherwise). Firmware upgrades on
live gear are never automated; Tempo issues a procedure for an engineer. Credentials are
referenced (env vars or macOS Keychain), never stored. Every call is in `net_audit`.

**Going live:** `pip install napalm` in the venv, import devices with `net_import_devices`,
run `net_golive_readiness`. Try it safely first with NAPALM's mock driver and the bundled
`netfixtures/mock-router` lab router (it has a dark optic the NOC will page on).

## Headless CRM (Salesforce-shaped)

Accounts, Contacts, Opportunities, Cases, Assets and Contracts with Salesforce's field
names and key prefixes (001 Account, 006 Opportunity, 500 Case...). Sales and renewals
run through Opportunities: new business is won or lost at its close date and a win
places the order; every contract gets a Renewal opportunity 90 days out. Scribe syncs
assets and cases from operations every simulated day.

```
GET /services/data/v60.0/sobjects
GET /services/data/v60.0/sobjects/Account/describe
GET /services/data/v60.0/query?q=SELECT Name, MRR__c FROM Account ORDER BY MRR__c DESC LIMIT 5
```

## Headless BI (Tableau-style)

Prism loads each simulated day into a separate warehouse (`data/warehouse.db`): revenue,
operations, tickets, pipeline, bookings, contract events, energy. One metrics layer
(`/api/bi/metrics`) feeds every dashboard; HQ's Insights tab renders them. `bi_export`
writes CSV plus a `.hyper` extract that Tableau Desktop or Tableau Public opens directly.

## REA (Reverse Engineer Anything)

The armory runs [REA](https://github.com/morluto/rea) locally through npx:
`rea_inspect_db` (REA's SQLite inspector on snapshots of the CRM and warehouse, giving
content-hashed schema evidence), `api_contract_check` (records a HAR of vix's own
Salesforce-shaped and BI APIs, validates the contract, and has REA inspect it) and
`public_recon` (public pages only, never sign-in pages, for design reference). REA's
SQLite inspector needs Node 24.15+, so the armory pins Node 24 via npx; override with
`VIX_REA_CMD`. Vega's weekly audit includes this evidence.

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

## Upgrading

Run the new `install.sh` over the old install. v0.2 to v0.3 creates the CRM from your
current world (accounts, contacts, contracts, assets, cases), loads the warehouse, and
adds the weekly data audit and board pack schedules.

### From v0.1

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
