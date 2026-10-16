from .. import db

DEFAULT = {
    "name": "vix",
    "tagline": "Virtual Interconnection eXchange: a headless digital-infrastructure company",
    "mission": "Run a global colocation and interconnection business end to end in simulation: "
               "sites, power, cooling, cross-connects, fabric, bare metal, energy and finance, "
               "with research that designs and tests the network gear on the way.",
    "model": "Headless: no staff. Departments are agents that run playbooks on a schedule or on request. "
             "Two super agents sit above them: the Chief Operator (Atlas) runs the business and files "
             "decisions; the Chief Scientist (Euler) runs research and proposes standards. The founder sets "
             "the charter and approves decisions. Claude operates the company through its MCP tools.",
    "scope": [
        "Simulated data-center sites in 8 metros with halls, cabinets, power and cooling",
        "Interconnection: ports, cross-connects, fabric virtual circuits between metros",
        "Network engineering: BGP at the exchange, route-server policy, latency",
        "Bare-metal compute on mock inventory",
        "Energy, carbon and PUE reporting; monthly financial close",
        "R&D: switch/router buffer design, routing-security standards",
    ],
    "non_goals": [
        "No real customers, real facilities or real network traffic; every site, tenant and number is simulated",
        "No use of any real company's name, branding or confidential data",
        "No paid model APIs; agents are rule-based and the founder's own Claude subscription does the reasoning",
    ],
    "principles": [
        "Every run leaves a report",
        "Every number says which model produced it",
        "Irreversible changes (new halls, network standards) need founder approval",
        "Reproducible on one Mac from a fixed seed",
    ],
    "targets": {"max_hall_power_util": 0.85, "max_pue": 1.5, "min_reachability": 0.999},
}

EDITABLE = {"tagline", "mission", "model", "scope", "non_goals", "principles", "targets"}


def load() -> dict:
    c = dict(DEFAULT)
    c.update(db.setting("charter", {}) or {})
    return c


def save(update: dict) -> dict:
    bad = set(update) - EDITABLE
    if bad:
        raise ValueError(f"not editable: {sorted(bad)}; editable fields: {sorted(EDITABLE)}")
    cur = db.setting("charter", {}) or {}
    cur.update(update)
    db.set_setting("charter", cur)
    return load()
