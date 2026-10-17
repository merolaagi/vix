"""BGP at an internet exchange, simulated message by message.

Each participant is an AS with real BGP-style policy (Gao-Rexford):
  * prefer customer routes over peer routes over provider routes, then shorter AS path,
    then lowest neighbor ASN;
  * export customer routes to everyone, peer/provider routes only to customers;
  * drop paths that already contain your own ASN.
A transparent route server gives every client multilateral peering. With filtering on it
does origin validation (an RPKI-like check that the origin owns the prefix) and a
customer-cone check (an IRR-like check that a client only sends routes it can originate
or carry for its customers).

Events: baseline, withdraw, leak, hijack, fiber_cut. Results: convergence time, update
count, reachability, and how many networks ended up on leaked or hijacked paths."""
from __future__ import annotations

import heapq
import random

from . import db

LP = {"customer": 300, "peer": 200, "provider": 100}
RS = "RS"


class AS:
    def __init__(self, asn, name, kind, prefixes):
        self.asn, self.name, self.kind = asn, name, kind
        self.prefixes = prefixes          # owned prefixes
        self.rel = {}                     # neighbor -> 'customer' | 'peer' | 'provider' (what the neighbor is to me)
        self.rib_in = {}                  # prefix -> {neighbor: path}
        self.best = {}                    # prefix -> (path, neighbor)
        self.leaker = False
        self.hijack = []                  # prefixes it falsely originates


class IX:
    def __init__(self, metro, ases, rs_clients, rs_filtering, seed):
        self.metro = metro
        self.ases = {a.asn: a for a in ases}
        self.rs_clients = set(rs_clients)
        self.rs_filtering = rs_filtering
        self.edge_filtering = False       # carriers validate what customers send them
        self.edge_rejected = 0
        self.rnd = random.Random(seed)
        self.q, self.seq, self.now = [], 0, 0.0
        self.messages = 0
        self.rs_rejected = 0
        self.rs_rib = {}                  # prefix -> {client: path}
        self.owner = {p: a.asn for a in ases for p in a.prefixes}
        self.delay = {}
        self.last_change = 0.0

    # --- topology helpers
    def link(self, a, b, a_is_to_b):
        """a_is_to_b: what b is to a ('provider', 'peer', 'customer')."""
        inv = {"provider": "customer", "customer": "provider", "peer": "peer"}
        self.ases[a].rel[b] = a_is_to_b
        self.ases[b].rel[a] = inv[a_is_to_b]

    def _d(self, a, b):
        k = (a, b)
        if k not in self.delay:
            self.delay[k] = self.rnd.uniform(0.005, 0.060)   # transport + BGP processing, seconds
        return self.delay[k]

    def customer_cone(self, asn, seen=None):
        seen = seen or set()
        if asn in seen:
            return seen
        seen.add(asn)
        for n, r in self.ases[asn].rel.items():
            if r == "customer":
                self.customer_cone(n, seen)
        return seen

    # --- messaging
    def send(self, frm, to, prefix, path):
        self.seq += 1
        d = 0.003 if to == RS or frm == RS else self._d(frm, to)
        heapq.heappush(self.q, (self.now + d, self.seq, to, frm, prefix, path))
        self.messages += 1

    def originate(self, asn, prefix):
        a = self.ases[asn]
        a.best[prefix] = ((asn,), None)
        self.export_from(asn, prefix)

    def rs_handle(self, frm, prefix, path):
        rib = self.rs_rib.setdefault(prefix, {})
        if path is not None and self.rs_filtering:
            origin = path[-1]
            if self.owner.get(prefix) != origin:
                self.rs_rejected += 1
                path = None
            elif origin not in self.customer_cone(frm):
                self.rs_rejected += 1
                path = None
        if path is None:
            if frm not in rib:
                return
            rib.pop(frm, None)
        else:
            rib[frm] = path
        for c in self.rs_clients:
            if c == frm:
                continue
            # transparent: forward the best RS path not learned from this client
            cands = [(len(p), src, p) for src, p in rib.items() if src != c and c not in p]
            best = min(cands)[2] if cands else None
            self.seq += 1
            heapq.heappush(self.q, (self.now + 0.003, self.seq, c, (RS, frm if best else None), prefix, best))
            self.messages += 1

    def receive(self, to, frm, prefix, path):
        a = self.ases[to]
        rib = a.rib_in.setdefault(prefix, {})
        key = frm
        if path is not None and to in path:
            path = None
        if (path is not None and self.edge_filtering and not isinstance(key, tuple) and key != RS
                and a.kind == "carrier" and a.rel.get(key) == "customer"):
            if self.owner.get(prefix) != path[-1] or path[-1] not in self.customer_cone(key):
                self.edge_rejected += 1
                path = None
        if path is None:
            rib.pop(key, None)
        else:
            rib[key] = path
        self.decide(to, prefix)

    def rel_of(self, a, key):
        return "peer" if isinstance(key, tuple) else a.rel.get(key, "peer")

    def decide(self, asn, prefix):
        a = self.ases[asn]
        if prefix in a.prefixes or prefix in a.hijack:
            return
        rib = a.rib_in.get(prefix, {})
        best = None
        for key, path in rib.items():
            rel = self.rel_of(a, key)
            nb = path[0] if path else 10 ** 9
            score = (-LP[rel], len(path), nb)
            if best is None or score < best[0]:
                best = (score, path, key)
        old = a.best.get(prefix)
        new = ((asn,) + best[1], best[2]) if best else None
        if (old and old[0]) != (new and new[0]):
            if new:
                a.best[prefix] = new
            else:
                a.best.pop(prefix, None)
            self.last_change = self.now
            self.export_from(asn, prefix)

    def export_from(self, asn, prefix):
        a = self.ases[asn]
        best = a.best.get(prefix)
        learned_key = best[1] if best else None
        learned_rel = None if learned_key is None else self.rel_of(a, learned_key)
        targets = list(a.rel.items()) + ([(RS, "peer")] if asn in self.rs_clients else [])
        for n, r in targets:
            if best is None:
                self.send(asn, n, prefix, None)
                continue
            if n == learned_key:
                self.send(asn, n, prefix, None)   # never echo a route back; clear anything sent earlier
                continue
            ok = learned_rel in (None, "customer") or r == "customer" or a.leaker
            self.send(asn, n, prefix, best[0] if ok else None)

    def run(self, until=600.0):
        while self.q:
            t, _, to, frm, prefix, path = heapq.heappop(self.q)
            if t > until:
                break
            self.now = t
            if to == RS:
                self.rs_handle(frm, prefix, path)
            else:
                key = RS if isinstance(frm, tuple) else frm   # one RS session per client
                self.receive(to, key, prefix, path)
        return self.now

    # --- measurement
    def reachability(self):
        total = ok = 0
        for a in self.ases.values():
            for p in self.owner:
                if p in a.prefixes:
                    continue
                total += 1
                if p in a.best:
                    ok += 1
        return round(ok / total, 4) if total else 1.0

    def edge(self, a, b):
        """Relationship of hop a->b from a's point of view, RS links count as peering."""
        r = self.ases[a].rel.get(b)
        if r is None:
            return "peer"
        return {"provider": "up", "peer": "peer", "customer": "down"}[r]

    def valley_free(self, path):
        state = "up"
        for a, b in zip(path, path[1:]):
            e = self.edge(a, b)
            if state == "up":
                if e == "peer":
                    state = "peered"
                elif e == "down":
                    state = "down"
            elif state == "peered":
                if e != "down":
                    return False
                state = "down"
            elif state == "down" and e != "down":
                return False
        return True

    def audit(self):
        leaked, hijacked = 0, 0
        for a in self.ases.values():
            for p, (path, _) in a.best.items():
                if p in a.prefixes or p in a.hijack:
                    continue
                if path[-1] != self.owner.get(p):
                    hijacked += 1
                elif not self.valley_free(path):
                    leaked += 1
        return {"leaked_routes_in_use": leaked, "hijacked_routes_in_use": hijacked}


def build(metro: str, max_as: int = 48, rs_filtering: bool = False, seed: int = 7) -> IX:
    rnd = random.Random(seed)
    rows = db.rows("select distinct t.id, t.name, t.kind, t.asn, t.prefixes from tenants t"
                   " join ports p on p.tenant=t.id join sites s on s.id=p.site where s.metro=?"
                   " union select t.id, t.name, t.kind, t.asn, t.prefixes from tenants t"
                   " join ix_ports x on x.tenant=t.id where x.metro=? order by 4", (metro, metro))
    members = {r["tenant"] for r in db.rows("select distinct tenant from ix_ports where metro=?", (metro,))}
    asn_of = {r["id"]: r["asn"] for r in rows}
    if len(rows) > max_as:
        carriers = [r for r in rows if r["kind"] == "carrier"]
        rnd.shuffle(carriers)
        rest = [r for r in rows if r["kind"] != "carrier"]
        keep = carriers[: max_as // 3]
        keep += rnd.sample(rest, min(len(rest), max_as - len(keep)))
        rows = sorted(keep, key=lambda r: r["asn"])
    ases = []
    for r in rows:
        n = max(1, min(3, r["prefixes"] // 8 or 1))
        ases.append(AS(r["asn"], r["name"], r["kind"], [f"{r['asn']}.{i}/24" for i in range(n)]))
    by = {k: [a.asn for a in ases if a.kind == k] for k in ("carrier", "cloud", "content", "enterprise", "financial")}
    # exchange members (tenants holding an IX port here) are the route-server clients
    rs_clients = {asn_of[t] for t in members if t in asn_of} if members else set(by["cloud"] + by["content"] + by["carrier"])
    rs_clients &= {a.asn for a in ases}
    legacy = not members
    ix = IX(metro, ases, (), rs_filtering, seed)
    carriers = by["carrier"] or [ases[0].asn]
    # carriers peer bilaterally with each other
    for i, a in enumerate(carriers):
        for b in carriers[i + 1:]:
            ix.link(a, b, "peer")
    # everyone else buys transit from one or two carriers
    for k in ("cloud", "content", "enterprise", "financial"):
        for a in by[k]:
            ups = rnd.sample(carriers, min(len(carriers), 2 if k in ("enterprise", "financial") else 1))
            for u in ups:
                if u != a:
                    ix.link(a, u, "provider")
            if legacy and k in ("enterprise", "financial") and rnd.random() < 0.3:
                rs_clients.add(a)
    ix.rs_clients = rs_clients
    return ix


def study(metro: str = "ASH", event: str = "baseline", target_asn: int | None = None,
          rs_filtering: bool | None = None, edge_filtering: bool | None = None,
          max_as: int = 48, seed: int = 7) -> dict:
    """rs_filtering: route server does origin validation + customer-cone checks.
    edge_filtering: carriers apply the same checks to what their customers send them.
    Both default to the company's current network standard (settings)."""
    if rs_filtering is None:
        rs_filtering = bool(db.setting("rs_filtering", False))
    if edge_filtering is None:
        edge_filtering = bool(db.setting("edge_filtering", False))
    ix = build(metro, max_as, rs_filtering, seed)
    ix.edge_filtering = edge_filtering
    if len(ix.ases) < 3:
        return {"error": f"not enough networks at {metro}"}
    for a in ix.ases.values():
        for p in a.prefixes:
            ix.originate(a.asn, p)
    base_t = ix.run()
    base = {"converged_s": round(base_t, 3), "updates": ix.messages, "reachability": ix.reachability(), **ix.audit()}
    rnd = random.Random(seed + 1)
    ix.messages, t0 = 0, ix.now
    ix.last_change = t0
    detail = {}
    multi = [a for a in ix.ases.values() if sum(1 for r in a.rel.values() if r == "provider") >= 2]
    if event == "withdraw":
        a = ix.ases.get(target_asn) or rnd.choice(list(ix.ases.values()))
        for p in list(a.prefixes):
            a.prefixes.remove(p)
            a.best.pop(p, None)
            ix.export_from(a.asn, p)
            ix.owner.pop(p, None)
        detail = {"withdrawn_by": f"AS{a.asn} {a.name}"}
    elif event == "leak":
        a = ix.ases.get(target_asn) or (rnd.choice(multi) if multi else rnd.choice(list(ix.ases.values())))
        a.leaker = True
        ix.rs_clients.add(a.asn)
        for p in list(a.best):
            if p not in a.prefixes:
                ix.export_from(a.asn, p)
        detail = {"leaker": f"AS{a.asn} {a.name} ({a.kind})"}
    elif event == "hijack":
        a = ix.ases.get(target_asn) or rnd.choice([x for x in ix.ases.values() if x.kind != "carrier"] or list(ix.ases.values()))
        victim = rnd.choice([x for x in ix.ases.values() if x.asn != a.asn and x.prefixes])
        p = victim.prefixes[0]
        a.hijack.append(p)
        a.best[p] = ((a.asn,), None)
        ix.rs_clients.add(a.asn)
        ix.export_from(a.asn, p)
        detail = {"hijacker": f"AS{a.asn} {a.name}", "victim": f"AS{victim.asn} {victim.name}", "prefix": p}
    elif event == "fiber_cut":
        carriers = [x for x in ix.ases.values() if x.kind == "carrier"]
        a = ix.ases.get(target_asn) or rnd.choice(carriers or list(ix.ases.values()))
        for n in list(a.rel):
            del ix.ases[n].rel[a.asn]
            for p, rib in ix.ases[n].rib_in.items():
                if a.asn in rib:
                    rib.pop(a.asn)
                    ix.decide(n, p)
        a.rel.clear()
        ix.rs_clients.discard(a.asn)
        for p in list(ix.rs_rib):
            if a.asn in ix.rs_rib[p]:
                ix.rs_handle(a.asn, p, None)
        detail = {"cut": f"AS{a.asn} {a.name} lost every session at {metro}"}
    elif event != "baseline":
        raise ValueError("event must be baseline, withdraw, leak, hijack or fiber_cut")
    ix.run()
    after = {"converged_s": round(max(0.0, ix.last_change - t0), 3), "updates": ix.messages,
             "reachability": ix.reachability(), **ix.audit()}
    return {"metro": metro, "event": event, "rs_filtering": rs_filtering, "edge_filtering": edge_filtering,
            "edge_rejected": ix.edge_rejected, "networks": len(ix.ases),
            "rs_clients": len(ix.rs_clients),
            "prefixes": len(ix.owner), "rs_rejected": ix.rs_rejected, "baseline": base,
            "after_event": after if event != "baseline" else None, "detail": detail,
            "model": "Gao-Rexford policy, transparent route server, 5-60 ms per-session delay, no MRAI timers"}
