"""Knowledge Graph department (Loom, under the Chief Data Officer)."""
from __future__ import annotations

from .. import graph
from .engine import playbook
from .playbooks import table, usd


@playbook("graph_sync", "Knowledge graph sync", "graph",
          "Loom rebuilds the company graph (departments, agents, roles, playbooks, customers, products, P&L lines, sites, "
          "devices, links, incidents, decisions, opportunities), stores it, and syncs it to Neo4j when Neo4j is reachable.")
def graph_sync(ctx):
    nodes, edges = graph.build()
    graph.store(nodes, edges)
    neo = graph.sync_neo4j(nodes, edges)
    st = graph.stats()
    mm = graph.money_map()
    ctx.log(f"{st['nodes']} nodes, {st['edges']} edges; neo4j {neo.get('synced')}")
    md = (f"Graph rebuilt: **{st['nodes']:,} nodes, {st['edges']:,} relationships**. Neo4j: "
          + (f"synced to {neo['uri']} (labels VixNode + Vix*, nothing else touched)." if neo.get("synced") else f"not synced ({neo.get('reason')}).")
          + "\n\n## Nodes\n\n" + table(st["by_label"], ["label", "n"]) +
          "\n## Relationships\n\n" + table(st["by_type"], ["type", "n"]) +
          "\n## Money map (from the graph)\n\n" + table([{**r, "revenue_operated": usd(r["revenue_operated"]), "costs_owned": usd(r["costs_owned"])}
                                                         for r in mm["departments"]], ["department", "revenue_operated", "costs_owned"]) +
          "\n_Try in Neo4j Browser:_ `MATCH p=(:VixIncident)-[:IMPACTED]->(:VixCustomer)-[:BUYS]->(:VixProduct)-[:EARNS]->(:VixPnLLine) RETURN p LIMIT 25`\n")
    return {"nodes": st["nodes"], "edges": st["edges"], "neo4j": neo.get("synced"), "neo4j_reason": neo.get("reason")}, md
