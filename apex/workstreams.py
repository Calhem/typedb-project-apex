"""The Workstreams pages: /api/workstreams (one row per workstream) and /api/workstreams/{code}.

Three measures are kept apart, as on the Overview: activity progress (duration-weighted, apex/progress.py),
milestone completion (point items achieved), and readiness to close (conditions satisfied, only for the
workstreams that hold conditions). Everything comes from TypeDB; Python groups, orders and counts days
from the stored scenario date.
"""

from datetime import date

from apex import impact, queries
from apex.schedule import shape

CONDITIONS = ("regulatory-clearance", "shareholder-approval", "court-sanction")


def _days(later, earlier):
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def _summaries(tx, code=None):
    as_of = queries.deal(tx)["as_of"]
    items = queries.all_items(tx)
    progress = {w["code"]: w for w in queries.workstreams(tx, items)}
    events = queries.workstream_events(tx, code)
    risks = queries.workstream_risks(tx)
    reach = queries.deadlines_in_reach(tx, code)
    organisations = queries.workstream_organisations(tx)
    rows = []
    for ws in sorted(progress.values(), key=lambda w: (w["planned_start"], w["due"], w["code"])):
        if code and ws["code"] != code:
            continue
        own = {c: dict(it, code=c) for c, it in items.items() if it["workstream"] == ws["code"]}
        points = sorted((i for i in own.values() if shape(i) == "milestone" and i["status"] != "complete"),
                        key=lambda i: (i["due"], i["code"]))
        conditions = [i for i in own.values() if i["type"] in CONDITIONS]
        deadlines = {dl: {"via": [], "from": []} for dl, i in own.items() if i["type"] == "deadline"}
        deadlines.update(reach.get(ws["code"], {}))
        latest = next((e for e in events if e["workstream"] == ws["code"]), None)
        rows.append({
            **{k: ws[k] for k in ("code", "name", "description", "status", "planned_start", "due", "progress",
                                  "progress_basis", "milestones_achieved", "milestones_total", "item_statuses")},
            "lead": ws["accountable"],
            "items": len(own),
            "activities": sum(shape(i) == "bar" and i["type"] == "activity" for i in own.values()),
            "conditions_satisfied": sum(i["status"] == "complete" for i in conditions),
            "conditions_total": len(conditions),
            "organisations": organisations.get(ws["code"], []),
            "next_milestone": {**{k: points[0][k] for k in ("code", "name", "due", "status")},
                               "days_remaining": _days(points[0]["due"], as_of)} if points else None,
            "deadlines": sorted(({"code": dl, "name": items[dl]["name"], "due": items[dl]["due"],
                                  "days_remaining": _days(items[dl]["due"], as_of), "owned": dl in own,
                                  "via": e["via"], "from": e["from"]} for dl, e in deadlines.items()),
                                key=lambda d: (d["due"], d["code"])),
            "risks": risks.get(ws["code"], []),
            "latest_event": latest,
            "events": sum(e["workstream"] == ws["code"] for e in events),
        })
    return as_of, items, events, rows, {c: w["name"] for c, w in progress.items()}


def summaries(tx):
    as_of, _, _, rows, _ = _summaries(tx)
    return {"as_of": as_of, "workstreams": rows}


def detail(tx, code):
    """One workstream: its summary, items (with open prerequisites at any depth), dependencies (crossing
    workstreams flagged), obligations, risks and every event, newest first. None if no such workstream."""
    as_of, items, events, rows, names = _summaries(tx, code)
    if not rows:
        return None
    summary = rows[0]
    blocked = queries.open_prerequisites(tx, code)
    own = sorted((dict(it, code=c, shape=shape(it)) for c, it in items.items() if it["workstream"] == code),
                 key=lambda i: (i.get("planned_start") or i["due"], i["due"], i["code"]))
    for it in own:
        it["open_prerequisites"] = [{"code": u, "name": items[u]["name"], "workstream": items[u]["workstream"]}
                                    for u in blocked.get(it["code"], [])]
    hard = set(queries.hard_dependency_kinds(tx))
    all_deps = queries.all_dependencies(tx)
    floats = impact.schedule_float(items, all_deps, queries.all_deadline_limits(tx), hard)
    critical = impact.critical_paths(floats)
    for it in own:
        f = floats.get(it["code"], {})
        it.update(float=f.get("float"), float_deadline=f.get("deadline"),
                  critical_for=sorted(dl for dl, cp in critical.items() if it["code"] in cp["items"]))
    dependencies = []
    for d in all_deps:
        a, b = items.get(d["from"]), items.get(d["to"])
        if not a or not b or code not in (a["workstream"], b["workstream"]):
            continue
        dependencies.append({**d, "hard": d["kind"] in hard,
                             "from_name": a["name"], "to_name": b["name"],
                             "from_workstream": {"code": a["workstream"], "name": names.get(a["workstream"])},
                             "to_workstream": {"code": b["workstream"], "name": names.get(b["workstream"])},
                             "crosses": a["workstream"] != b["workstream"]})
    open_conditions = [{"code": i["code"], "name": i["name"]} for i in own if i["type"] in CONDITIONS and i["status"] != "complete"]
    return {"as_of": as_of, "summary": summary, "items": own, "dependencies": dependencies, "open_conditions": open_conditions,
            "obligations": queries.workstream_obligations(tx, code), "events": events}
