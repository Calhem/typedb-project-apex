"""The Gantt chart's answer (/api/schedule) and the shared item detail (/api/items/{code}).

Everything comes from queries that already exist (all_items, workstreams, all_dependencies,
all_deadline_limits, deal): there is no second timeline dataset. Python only groups and orders.
"""

from apex import impact, queries


def shape(item):
    """How the chart draws an item: deadlines are lines, items with a planned start are bars, the rest
    are point milestones (the same split as apex/progress.py)."""
    if item["type"] == "deadline":
        return "deadline"
    return "bar" if item.get("planned_start") else "milestone"


def schedule(tx):
    as_of = queries.deal(tx)["as_of"]
    items = queries.all_items(tx)
    hard = set(queries.hard_dependency_kinds(tx))
    dependencies = queries.all_dependencies(tx)
    limits_ = queries.all_deadline_limits(tx)
    floats = impact.schedule_float(items, dependencies, limits_, hard)
    critical = impact.critical_paths(floats)
    rows = []
    for ws in sorted(queries.workstreams(tx, items), key=lambda w: (w["planned_start"], w["due"], w["code"])):
        own = [dict(it, code=c, shape=shape(it), float=floats.get(c, {}).get("float"),
                    float_deadline=floats.get(c, {}).get("deadline"),
                    critical_for=sorted(dl for dl, cp in critical.items() if c in cp["items"]))
               for c, it in items.items() if it["workstream"] == ws["code"]]
        own.sort(key=lambda i: (i.get("planned_start") or i["due"], i["due"], i["code"]))
        rows.append({**ws, "items": own})
    limits = {}
    for limit in limits_:
        dl = limits.setdefault(limit["deadline"], {"code": limit["deadline"], "name": limit["name"], "due": limit["due"],
                                                   "consequence": limit["consequence"], "limits": [],
                                                   "workstream": items[limit["deadline"]]["workstream"]})
        dl["limits"].append(limit["item"])
    deadlines = sorted(({**d, "limits": sorted(d["limits"]), "critical_path": critical.get(d["code"])} for d in limits.values()),
                       key=lambda d: (d["due"], d["code"]))
    dependencies = [{**d, "hard": d["kind"] in hard} for d in dependencies]
    return {"as_of": as_of, "workstreams": rows, "deadlines": deadlines, "dependencies": dependencies}


def item(tx, code):
    """One item's detail, plus whether the Impact Explorer can simulate delaying it (open conditions)."""
    found = queries.item_detail(tx, code)
    if found is None:
        return None
    found["simulate"] = any(c["code"] == code and c["status"] != "complete" for c in queries.conditions(tx))
    items = queries.all_items(tx)
    floats = impact.schedule_float(items, queries.all_dependencies(tx), queries.all_deadline_limits(tx),
                                   set(queries.hard_dependency_kinds(tx)))
    f = floats.get(code, {})
    found["float"], found["float_deadline"] = f.get("float"), f.get("deadline")
    found["critical_for"] = sorted(dl for dl, cp in impact.critical_paths(floats).items() if code in cp["items"])
    return found
