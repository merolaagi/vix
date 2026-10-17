"""Role catalog: the 20 software and network engineering roles at vix, each mapped to the department and agent
that performs it and the playbooks that do the work. HQ's Roles tab and the MCP tool roles_catalog read this;
any role can be run on demand (company/assign_playbooks.py)."""
from __future__ import annotations

from . import db


ROLES = [
    {"ref": "01", "title": "Staff Software Engineer - Java Backend", "kind": "software",
     "duties": ["Scalable backend services and secure REST APIs", "Relational and NoSQL data stores"],
     "dept": "platform", "playbooks": ["platform_api_review"]},
    {"ref": "02", "title": "Senior Software Engineer - Data Cloud & Marketing Technology", "kind": "software",
     "duties": ["Own the customer data cloud and its integrations", "Data quality and schema migration between orgs",
                "Agent-driven next best actions", "Consent controls with privacy teams"],
     "dept": "datacloud", "playbooks": ["crm_data_cloud", "cdo_data_audit"]},
    {"ref": "03", "title": "Senior Associate Engineer, Product Software (Internet Access)", "kind": "software",
     "duties": ["Backend for the Internet Access product", "Test features, review, improve delivery pipelines",
                "Production troubleshooting"],
     "dept": "productsw", "playbooks": ["product_regression", "ix_fulfill_orders"]},
    {"ref": "04", "title": "Senior Software Engineer - Network Automation Architect", "kind": "network",
     "duties": ["Automation platform for provisioning, configuration, validation and compliance", "Safe multi-vendor changes"],
     "dept": "netauto", "playbooks": ["netauto_compliance", "change_manage"]},
    {"ref": "05", "title": "Network Engineer III", "kind": "network",
     "duties": ["Deploy hardware and features", "Migrations", "Validate equipment, acceptance tests",
                "Use operational data to improve reliability"],
     "dept": "net", "playbooks": ["net_deploy_migrate", "noc_watch"]},
    {"ref": "06", "title": "Network Engineer III (Automation & AIOps)", "kind": "network",
     "duties": ["Predictive monitoring and anomaly detection", "Resilience testing", "Faster incident resolution"],
     "dept": "noc", "playbooks": ["aiops_predict", "noc_watch"]},
    {"ref": "07", "title": "Network Engineer, Product Software (certification)", "kind": "network",
     "duties": ["Certify network OS releases before rollout", "Regression, interoperability and scale tests",
                "Deployment and rollback procedures"],
     "dept": "certlab", "playbooks": ["nos_certify"]},
    {"ref": "08", "title": "SDN Engineer, Product Software (AI inference networking)", "kind": "network",
     "duties": ["Networking for AI inference traffic", "L4/L7 proxying and mTLS", "Evaluate technologies"],
     "dept": "sdn", "playbooks": ["sdn_inference_study"]},
    {"ref": "09", "title": "Network Software Engineer - Observability & AI", "kind": "software",
     "duties": ["Network APIs and MCP servers", "AI agents grounded in telemetry, inventory and topology",
                "Automated troubleshooting and self-service", "Test agent interactions"],
     "dept": "aieng", "playbooks": ["ai_assistant_eval"]},
    {"ref": "10", "title": "Staff Software Engineer (Backend Java) - SDN interconnection platform", "kind": "software",
     "duties": ["Interconnection platform", "Automated tests and integrations", "DevSecOps delivery"],
     "dept": "platform", "playbooks": ["platform_e2e_tests"]},
    {"ref": "11", "title": "Staff Software Engineer - distributed automation", "kind": "software",
     "duties": ["Durable workflows", "Observability of services", "Asynchronous APIs"],
     "dept": "platform", "playbooks": ["platform_reliability"]},
    {"ref": "12", "title": "Staff Software Engineer - AI-focused backend", "kind": "software",
     "duties": ["Agents and copilots", "Retrieval-augmented generation over enterprise data", "Connect enterprise systems"],
     "dept": "aieng", "playbooks": ["ai_assistant_eval"]},
    {"ref": "13", "title": "Staff Software Engineer - Cloud Platform & DevSecOps", "kind": "software",
     "duties": ["Operate the internal engineering platform", "Security in delivery"],
     "dept": "platform", "playbooks": ["platform_devsecops"]},
    {"ref": "14", "title": "Principal Network Service Engineer (SRE)", "kind": "network",
     "duties": ["Service indicators, objectives and error budgets", "Coordinate major incidents", "Automated remediation"],
     "dept": "sre", "playbooks": ["sre_slo_review"]},
    {"ref": "15", "title": "Network Engineer III (Mumbai)", "kind": "network",
     "duties": ["Provision interconnection services", "Investigate network faults", "Scripts for troubleshooting"],
     "dept": "net", "playbooks": ["net_deploy_migrate", "ix_fulfill_orders"]},
    {"ref": "16", "title": "Network Service Engineer Intern", "kind": "network",
     "duties": ["Inventory and data audits", "Operational documentation", "Service validation", "Equipment retirement"],
     "dept": "net", "playbooks": ["net_inventory_audit"]},
    {"ref": "17", "title": "Network Implementation Expert (Global Projects)", "kind": "network",
     "duties": ["Lead enterprise network implementations", "Complex migrations", "Standards and documentation"],
     "dept": "ps", "playbooks": ["ps_projects"]},
    {"ref": "18", "title": "Software Engineer - ServiceNow", "kind": "software",
     "duties": ["IT service management platform: incidents, changes, problems, requests"],
     "dept": "itsm", "playbooks": ["itsm_sync"]},
    {"ref": "19", "title": "Staff Software Engineer - CRM / project delivery", "kind": "software",
     "duties": ["Sales and service CRM processes", "Project delivery"],
     "dept": "ps", "playbooks": ["ps_projects", "crm_sync"]},
    {"ref": "20", "title": "Senior Principal Network Architect, Core Architecture", "kind": "network",
     "duties": ["Next-generation network architecture", "Evaluate network operating systems", "Proofs of concept"],
     "dept": "arch", "playbooks": ["arch_review", "nos_certify"]},
]


def catalog() -> dict:
    from .company import engine
    out = []
    for r in ROLES:
        d = engine.DEPT.get(r["dept"], {})
        pbs = []
        for name in r["playbooks"]:
            last = db.one("select id, status, started, report from runs where playbook=? order by id desc limit 1", (name,))
            pbs.append({"playbook": name, "exists": name in engine.PLAYBOOKS, "last_run": last})
        out.append({**r, "department": d.get("name"), "agent": d.get("agent"), "reports_to": d.get("reports_to"),
                    "playbooks": pbs, "covered": all(p["exists"] for p in pbs),
                    "exercised": all(p["last_run"] and p["last_run"]["status"] == "done" for p in pbs)})
    return {"roles": out, "covered": sum(r["covered"] for r in out),
            "exercised": sum(r["exercised"] for r in out), "total": len(out)}
