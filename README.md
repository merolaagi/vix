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
    ├── Quill, CFO: cfo_finance_review
    │   ├── Margin, Deal Desk: discount authority matrix on every quote
    │   ├── Docket, Billing: fin_billing (invoices in local currency, payments, disputes, credit notes)
    │   ├── Reckon, Collections & Credit: fin_collections (dunning, credit holds, agency, write-offs)
    │   ├── Ledger, Finance: fin_period_close (general ledger, three statements), fin_monthly_close (run-rate flash)
    │   ├── Compass, FP&A: fpa_forecast (budget, budget vs actual, six-month forecast)
    │   ├── Anchor, Treasury: treasury_review (revolver, bonds, refinancing, FX, dividends)
    │   └── Herald, Investor Relations: ir_earnings_pack (quarterly earnings release)
    ├── Keystone, Chief Development Officer: dev_review
    │   ├── Scout, Site Selection: site_selection (runway, business cases, hyperscale bids)
    │   ├── Volt, Power Procurement: power_procurement (grid queues, PPAs, renewables, bridge power)
    │   └── Mason, Construction PMO: dev_daily, construction_report
    ├── Summit, Chief Commercial Officer: cco_review
    │   ├── Broker, Sales and Keeper, Account Management (moved here)
    │   ├── Lens, Competitive Intelligence: market_intel (competitors, win/loss)
    │   ├── Portico, Customer Experience & Portal: market_daily, customer_voice (portal, API, NPS)
    │   └── Nexus, Marketplace & Partners: ecosystem_review
    ├── Rampart, Chief Trust & Risk Officer: cro_review
    │   ├── Aegis, Physical Security and Lookout, SOC: trust_daily, security_report
    │   ├── Canon, Compliance: compliance_check (controls with evidence, simulated audit)
    │   ├── Brace, Risk & Resilience: risk_review (risk register, backups, quarterly drills)
    │   └── Clause, Legal & Contracts: legal_report
    ├── Arbiter, Governance: governance_review (decision outcomes, department budgets, agent grades)
    ├── Atlas, Chief Operator: operations departments
    │   └── Bridge, Professional Services: ps_projects
    ├── Euler, Chief Scientist: R&D
    │   └── Mesh, SDN & AI Networking: sdn_inference_study
    ├── Meridian, Chief Network Officer: network health, live readiness
    │   ├── Sentinel, NOC: noc_watch, aiops_predict (prediction, anomalies, resilience tests)
    │   ├── Tempo, Change Management: change_manage (firmware, CVEs, windows, rollback)
    │   ├── Gauge, Network Capacity: capacity_plan (ports, backbone, capex decisions)
    │   ├── Pathfinder, Network Engineering: net_deploy_migrate, net_inventory_audit, net_bgp_study
    │   ├── Cadence, Network Automation: netauto_compliance (golden config, drift, safe changes)
    │   ├── Proof, Certification Lab: nos_certify (regression, interop, scale before rollout)
    │   ├── Beacon, SRE: sre_slo_review (SLOs, error budgets, change freeze, postmortems)
    │   └── Vantage, Network Architecture: arch_review (architecture decision records)
    ├── Lattice, Chief Technology Officer: cto_engineering_review
    │   ├── Anvil, Platform Engineering: platform_api_review, platform_reliability, platform_e2e_tests, platform_devsecops
    │   ├── Keel, Product Software: product_regression (Internet Access and every product)
    │   ├── Echo, AI Engineering: ai_assistant_eval (tests Ask vix)
    │   └── Tally, IT Service Management: itsm_sync (ServiceNow-shaped incidents, changes, problems, CMDB)
    └── Vega, Chief Data Officer: cdo_data_audit (reconciliation + REA evidence)
        ├── Scribe, CRM Operations: crm_sync
        ├── Prism, Business Intelligence: bi_nightly_load, bi_export
        ├── Signal, Data Cloud: crm_data_cloud (consent, duplicates, segments, next best actions)
        └── Loom, Knowledge Graph: graph_sync (company graph, Neo4j sync)
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
| Super | Office of the CTO | **Lattice** | `cto_engineering_review` |
| Dept | Platform Engineering | Anvil | `platform_api_review`, `platform_reliability`, `platform_e2e_tests`, `platform_devsecops` |
| Dept | Product Software | Keel | `product_regression` |
| Dept | AI Engineering | Echo | `ai_assistant_eval` |
| Dept | IT Service Management | Tally | `itsm_sync` |
| Dept | Data Cloud & Marketing Technology | Signal | `crm_data_cloud` |
| Dept | Knowledge Graph | Loom | `graph_sync` |
| Dept | Network Automation | Cadence | `netauto_compliance` |
| Dept | Network Certification Lab | Proof | `nos_certify` |
| Dept | Site Reliability Engineering | Beacon | `sre_slo_review` |
| Dept | Network Architecture | Vantage | `arch_review` |
| Dept | SDN & AI Networking | Mesh | `sdn_inference_study` |
| Dept | Professional Services | Bridge | `ps_projects` |

Super agents delegate: their runs start child runs in the departments and write a
summary that links each child report. Irreversible changes (a new hall, a network
standard, extra cooling) become **decisions** the founder approves in HQ or from chat.

## Running it like a public company (v0.6 to v0.9)

**Money (Quill).** Every opportunity gets a quote from the Deal Desk under an authority matrix: up to 10% discount
on its own, up to 20% with the CFO (only if gross margin stays at 25% or more), more than that needs the founder
for deals of $40k a month or more. Billing invoices every customer on the first of the month in their own currency
(recurring in advance, remote hands in arrears, network SLA credits applied). Customers pay late or not at all
according to their credit rating; disputes become credit notes or are rejected; Collections runs the dunning ladder
to credit hold, agency and write-off, and terminates customers who never pay. Everything posts to a double-entry
general ledger, and the month-end close derives the income statement, balance sheet and cash-flow statement from it
(the balance sheet balances to the cent and the cash flow ties out). Treasury keeps cash with a revolver, terms it
out with bond issues and refinances maturities (founder decisions); Investor Relations publishes a quarterly earnings
release with FFO, AFFO per share, leverage and guidance; the board declares the dividend. Finance tab in HQ.

**Growth (Keystone).** New halls, AI halls, campuses and hyperscale joint ventures are projects that move through
land, design, permits, utility power, construction and commissioning with real lead times (about 11 months for a
hall, 2 years or more for a campus), permit and supply-chain delays, cost overruns and grid queues. Spend is
capitalised as construction in progress and becomes PP&E when the hall opens. Each project is approved on an IRR
business case and checked against what it actually earns 6, 12 and 24 months after it goes live. Volt buys power
purchase agreements toward 100% renewable energy by 2030; they fix the price of part of the load. Atlas's
build_hall decisions now start projects instead of halls appearing overnight. Growth tab.

**Market (Summit).** Three invented competitors with prices, capacity and reputations by metro move every week.
Who wins each deal depends on price after discount, ecosystem density, service reputation and capacity, seen
through the customer's persona; every loss has a reason and a winner. Customers order and raise requests (including
letters of authorization) in the customer portal at /portal or through its API with their own key; partners
source deals for commission; marketplace listings connect buyers to sellers. NPS comes from what each customer
experienced. Forge can retire bare metal: no new sales, a migration offer for every customer, end of life in 180
days. Market tab.

**Trust (Rampart) and governance (Arbiter).** Badges, visitors and a daily sweep that removes access for customers
who leave; a security operations centre; 16 controls tested weekly against vix's own records with evidence files
(data/evidence) and a simulated audit; weekly backups with a restore test; quarterly continuity drills; a risk
register built from live data; non-standard contract terms and data-residency commitments. Arbiter scores every
approved decision against its outcome (did the hire fix the SLA, did the hall earn its case, did the PPA save
money), compares department costs with budget, and grades every agent A to D for the board pack. Trust &
governance tab. Control references are generic framework families; it is a simulation, not an attestation.

## Customer portal and API

Open http://127.0.0.1:47530/portal. As the operator you can open the portal as any customer (it issues them an API
key, stored only as a hash). Customers' own tools use the same key:

```
curl -H "Authorization: Bearer <key>" http://127.0.0.1:47530/portal/api/v1/me
```

Endpoints: `GET /me`, `GET /invoices`, `GET /catalog`, `POST /orders {kind, params}`, `POST /requests {kind}` under
`/portal/api/v1`.

## Engineering roles (v0.5)

The 20 software and network engineering roles from the founder's research file are each
mapped to the department and agent that does the work and the playbooks that do it
(`vix/roles.py`, HQ **Roles** tab, MCP `roles_catalog`). Every role is covered and each
playbook shows its last run. Examples: Java backend becomes Anvil's API review (latency
budgets, index fixes from query plans, contract checks); network automation becomes
Cadence's golden-config compliance; network OS certification becomes Proof's lab; the
SRE role becomes Beacon's SLOs and error budgets; the ServiceNow role becomes Tally's
ITSM tables (`GET /api/now/table/{incident|change_request|problem|cmdb_ci}` with
`sysparm_query`); the observability and AI role becomes Echo's tests of Ask vix.

## Knowledge graph and Neo4j

Loom rebuilds a company graph every day: founder, departments, agents, roles, playbooks,
products, P&L lines (revenue, costs, EBITDA), metros, sites, halls, vendors, devices,
backbone links, customers, incidents, decisions and opportunities, joined by
relationships such as REPORTS_TO, OPERATES, EARNS, ADDS_TO, ACCOUNTABLE_FOR,
SUBTRACTS_FROM, BUYS, CONNECTED_VIA, IMPACTED and HANDLED_BY. HQ's **Graph** tab
draws neighbourhoods, finds paths ("how is Sentinel connected to EBITDA?") and shows
the money map (revenue each department operates, costs it owns).

The graph works without Neo4j. When Neo4j is running (default `bolt://localhost:7687`;
set `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` in `.env`), Loom
syncs to it. Every vix node carries the label `VixNode` plus `Vix<Label>` and
`vix=true`, and only those are ever deleted or rewritten, so it can share a database
with another graph (such as PrimeKG). The HQ Cypher console and `graph_cypher` are
read-only. Try:

```
MATCH p=(:VixIncident)-[:IMPACTED]->(:VixCustomer)-[:BUYS]->(:VixProduct)-[:EARNS]->(:VixPnLLine) RETURN p LIMIT 25
```

Set `VIX_NEO4J=off` to skip the sync.

## Explore (self-service BI, Tableau-style)

HQ's **Explore (BI)** tab is a drag-and-drop workbook over the warehouse. Pick a data
source; the left pane lists its **Dimensions** (blue) and **Measures** (green). Drag
fields onto the **Columns**, **Rows** and **Filters** shelves (or click a field to drop
it where it fits). Show Me picks the chart: a line when a time dimension is on a
shelf (one line per category when there is a second dimension), bars otherwise,
horizontal when the dimension is on Rows. Swap, sort, date range, and **Pin to
dashboard** (it appears in Insights under Saved explorations). The underlying rows and
the SQL are one click away. Only fields from the semantic model are accepted, so the
SQL is always safe. Same over MCP: `bi_explore_model`, `bi_explore`, `bi_pin_explore`.
For Tableau itself, the Tableau export writes a `.hyper` extract (Insights tab).

## Light and dark mode

HQ follows your system setting; the button next to the logo switches and remembers it.

## Ask vix (chatbot)

The **Ask vix** button in HQ (and MCP `ask_vix`) answers questions about the company
from the systems of record: P&L, revenue by product or segment, customers, renewals at
risk, network incidents, open decisions, who does a job, and how two things connect.
It uses a local model through Ollama when one is running (`VIX_OLLAMA_URL`, default
`http://localhost:11434`; `VIX_CHAT_MODEL` to pick one) with tool calling, so numbers
come from tools rather than memory, and falls back to a built-in router otherwise. No
paid API is used. Every answer lists the tools it called. Set `VIX_CHAT_ENGINE=router`
to force the router.

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

Run the new `install.sh` over the old install. v0.5 to v0.9 opens the books from the company you already have
(opening balance sheet, three bond tranches, a revolver), rates every customer's credit, assigns personas and
badges, seeds competitors, partners and marketplace listings, and adds the CFO, CDO (development), CCO and CRO
reviews and the governance review to the schedules. The first invoices go out on the next billing run; the first
month-end close happens on the first simulated day of the next month.

Run the new `install.sh` over the old install. v0.4 to v0.5 adds the CTO organisation
and the new network departments with their schedules, the ServiceNow-shaped tables,
consent fields on CRM contacts, the Internet Access product, the knowledge graph and the
`neo4j` driver. The first compliance sweep queues a config change for every device that
drifts from the golden config; Tempo works through them in maintenance windows.

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
