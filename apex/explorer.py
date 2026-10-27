"""The Relationship Explorer's answers, composed from the TypeQL in apex/queries.py.

The graph shows organisations and the relations between them, read generically from TypeDB with their
role names; people, obligations and events appear on demand in the detail panel. The only things
worked out here are grouping nodes for the layout (from the relations TypeDB returns) and, for the
group structure, listing each ownership route, checked against TypeDB's own recursive figures.
"""

from decimal import Decimal

from apex import queries


def _dec(value):
    """Exact decimal as short text: Decimal("0.70") -> "0.7", Decimal("1.0") -> "1"."""
    text = format(Decimal(value).normalize(), "f")
    return text


class OwnershipRoutesMismatch(Exception):
    """The routes listed from direct holdings don't add up to TypeDB's recursive figures."""


def type_path(type_label, parents):
    """["law-firm", "professional-services-firm", "organisation", "party"] from the schema hierarchy."""
    path = [type_label]
    while path[-1] in parents:
        path.append(parents[path[-1]])
    return path


def graph(tx):
    deal = queries.deal(tx)
    hierarchy = queries.type_hierarchy(tx)
    orgs = {o["code"]: {**o, "type_path": type_path(o["type"], hierarchy["parties"])} for o in queries.organisations(tx)}
    relations = [r for r in queries.organisation_relations(tx)
                 if sum(p["code"] in orgs for p in r["players"]) >= 2]
    acquirer, target = deal["acquirer"]["code"], deal["target"]["code"]
    members = set(queries.group_members(tx, target))

    clusters = {}

    def put(code, cluster, label, order):
        if "cluster" not in orgs[code]:
            orgs[code]["cluster"] = cluster
            clusters.setdefault(cluster, {"key": cluster, "label": label, "order": order})

    put(acquirer, "acquirer", "Acquirer", 2)
    put(target, "target", "Target", 4)
    for code in sorted(members):
        put(code, "target-group", f"{deal['target']['name']} group", 5)
    for r in relations:
        roles = {p["role"]: p for p in r["players"]}
        if r["type"] == "ownership" and roles["owned"]["code"] in members and roles["owner"]["code"] in orgs:
            put(roles["owner"]["code"], "outside-shareholders", "Outside shareholders in the group", 6)
    for r in relations:
        roles = {p["role"]: p for p in r["players"]}
        if "adviser" in roles and "client" in roles:
            client = roles["client"]
            side = 1 if client["code"] == acquirer else 3
            put(roles["adviser"]["code"], f"advisers:{client['code']}", f"Advisers to {client['name']}", side)
        if "lender" in roles:
            put(roles["lender"]["code"], "lenders", "Lenders", 0)
    for code, o in orgs.items():
        if "authority" in o["type_path"]:
            put(code, "authorities", "Authorities", 7)
        put(code, "other", "Other organisations", 8)

    used_types = sorted({r["type"] for r in relations})
    return {"deal": {"code": deal["code"], "name": deal["name"], "value": deal["value"], "currency": deal["currency"]},
            "nodes": sorted(orgs.values(), key=lambda o: (clusters[o["cluster"]]["order"], o["code"])),
            "relations": relations,
            "clusters": sorted(clusters.values(), key=lambda c: c["order"]),
            "types": {"parties": hierarchy["parties"],
                      "relations": {t: hierarchy["relations"].get(t, {"parent": None, "roles": []}) for t in used_types}}}


def node(tx, code):
    return queries.party(tx, code)


def ownership(tx, parent_code=None):
    """The group structure below parent_code (default: the deal's target), guarded.

    Effective stakes and route counts come from TypeDB's recursive group_members function. Each route
    is then listed from the direct holdings TypeDB returns, and the listing must reproduce TypeDB's
    figures exactly; if it doesn't, no figures are returned at all.
    """
    if parent_code is None:
        parent_code = queries.deal(tx)["target"]["code"]
    stakes = queries.effective_ownership(tx, parent_code)          # runs both guards first
    consents = queries.group_consents(tx, parent_code)
    edges = queries.ownership_edges(tx, parent_code)

    names = {e["owner"]["code"]: e["owner"]["name"] for e in edges} | {e["owned"]["code"]: e["owned"]["name"] for e in edges}
    below = {}
    for e in edges:
        below.setdefault(e["owner"]["code"], []).append(e)
    routes = {}

    def walk(company, steps, product):
        for e in below.get(company, []):
            child = e["owned"]["code"]
            if any(s["owner"] == child for s in steps) or child == parent_code:
                raise OwnershipRoutesMismatch(f"route revisits {child}")
            step = {"owner": company, "owned": child, "stake": e["stake"]}
            routes.setdefault(child, []).append({"steps": steps + [step], "product": product * e["stake"]})
            walk(child, steps + [step], product * e["stake"])

    walk(parent_code, [], Decimal(1))
    companies = []
    for code, v in stakes.items():
        listed = routes.get(code, [])
        total = sum((r["product"] for r in listed), Decimal(0))
        if total != v["stake"] or len(listed) != v["routes"]:
            raise OwnershipRoutesMismatch(f"{code}: routes add up to {total} over {len(listed)}, "
                                          f"TypeDB says {v['stake']} over {v['routes']}")
        companies.append({"code": code, "name": v["name"], "stake": _dec(v["stake"]), "routes": v["routes"],
                          "paths": [{"stake": _dec(r["product"]),
                                     "steps": [{"owner": s["owner"], "owned": s["owned"], "stake": _dec(s["stake"])}
                                               for s in r["steps"]]} for r in listed]})
    companies.sort(key=lambda c: (-Decimal(c["stake"]), c["code"]))
    outside = sorted({e["owner"]["code"] for e in edges} - set(stakes) - {parent_code})
    obligation_names = queries.names(tx, [c["obligation"] for c in consents])
    return {"parent": parent_code, "parent_name": names.get(parent_code),
            "companies": companies,
            "direct": [{"owner": e["owner"]["code"], "owned": e["owned"]["code"], "stake": _dec(e["stake"]),
                        "inside_group": e["owner"]["code"] not in outside, "description": e.get("description")}
                       for e in edges],
            "outside_shareholders": [{"code": c, "name": names[c]} for c in outside],
            "names": dict(sorted(names.items())),
            "consents": [{**c, "obligation_name": obligation_names.get(c["obligation"]), "stake": _dec(c["stake"])}
                         for c in consents]}
