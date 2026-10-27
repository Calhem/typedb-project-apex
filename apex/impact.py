"""The Impact Explorer's "what if": delay one item by N days and project what moves.

TypeDB supplies everything (the recursive dependency walk, every item's dates and status, and
the deadlines). The only thing done here is date arithmetic, because TypeDB 3.14.1 can't add
days to a `date`: for each affected item,

    projected(item) = max(planned(item), max over its hard prerequisites of projected(prereq) + lag)

walked in dependency order (a longest-path calculation). Completed items never move. A deadline
is breached when an item it limits is projected after the deadline's date. Results are
projections, never written to the database.
"""

import re
from datetime import date, timedelta
from pathlib import Path

from apex import queries

MAX_DELAY_DAYS = 365


def propagate(start, delay_days, edges, planned, completed=frozenset()):
    """Pure calculation. Returns {code: (projected date, [codes of the prerequisites that set it])}.

    start: the delayed item's code; edges: [{"from", "to", "lag"}] (hard dependencies only);
    planned: {code: date}; completed: codes that are already done and so cannot move.
    """
    if not 0 <= delay_days <= MAX_DELAY_DAYS:
        raise ValueError(f"delay must be 0-{MAX_DELAY_DAYS} days")
    incoming, outgoing = {}, {}
    for e in edges:
        incoming.setdefault(e["to"], []).append(e)
        outgoing.setdefault(e["from"], []).append(e)
    affected = {start} | {e["to"] for e in edges}

    # Kahn's algorithm: process an item only after all its affected prerequisites.
    waiting = {code: sum(1 for e in incoming.get(code, []) if e["from"] in affected) for code in affected}
    ready = sorted(code for code, n in waiting.items() if n == 0)
    projected = {}
    while ready:
        code = ready.pop(0)
        if code == start and code not in completed:
            projected[code] = (planned[code] + timedelta(days=delay_days), [])
        elif code in completed or code == start:
            projected[code] = (planned[code], [])
        else:
            best, causes = planned[code], []
            for e in incoming.get(code, []):
                if e["from"] not in projected:
                    continue
                candidate = projected[e["from"]][0] + timedelta(days=e["lag"])
                if candidate > best:
                    best, causes = candidate, [e["from"]]
                elif candidate == best and candidate > planned[code]:
                    causes.append(e["from"])
            projected[code] = (best, sorted(causes))
        for e in outgoing.get(code, []):
            waiting[e["to"]] -= 1
            if waiting[e["to"]] == 0:
                ready.append(e["to"])
        ready.sort()
    if len(projected) != len(affected):
        raise ValueError("hard dependencies contain a cycle; dates can't be projected")
    return projected


def project(tx, start_code, delay_days):
    """Run the impact queries in transaction `tx` and project a delay of start_code by delay_days.

    Returns {"start", "delay_days", "items": {code: {...}}, "deadlines": [...]}. Only items whose
    projected date differs from the plan appear in "items" (plus the start item).
    """
    items = queries.all_items(tx)
    if start_code not in items:
        raise KeyError(f"no work item with code {start_code}")
    edges = queries.impact_edges(tx, start_code)
    planned = {code: date.fromisoformat(it["due"]) for code, it in items.items() if it["due"]}
    completed = {code for code, it in items.items() if it["status"] == "complete"}
    projected = propagate(start_code, delay_days, edges, planned, completed)

    moved = {}
    for code, (new, causes) in projected.items():
        old = planned[code]
        if new != old or code == start_code:
            it = items[code]
            moved[code] = {"name": it["name"], "type": it["type"], "workstream": it["workstream"],
                           "planned": old, "projected": new, "shift_days": (new - old).days, "caused_by": causes}

    deadlines = []
    for limit in queries.all_deadline_limits(tx):
        if limit["item"] not in moved:
            continue
        due = date.fromisoformat(limit["due"])
        new = moved[limit["item"]]["projected"]
        deadlines.append({**limit, "due": due, "item_projected": new,
                          "breached": new > due, "days_over": max(0, (new - due).days),
                          "headroom_days": max(0, (due - new).days)})
    deadlines.sort(key=lambda d: (d["due"], d["deadline"], d["item"]))
    return {"start": start_code, "delay_days": delay_days, "items": moved, "deadlines": deadlines}


# ---- The Impact Explorer screen -----------------------------------------------------------------

FUNCTIONS_TQL = Path(__file__).resolve().parent.parent / "schema" / "functions.tql"

# Scenario INPUTS, not results: named delays worth trying for a given item. Where deadlines break is
# never listed here; it is found from the data (breach_from_days below).
REFERENCE_SCENARIOS = {
    "CP-CMA": [(28, "CMA Phase 1 clock stopped", "The statutory clock stops for four weeks until an information request is answered in full"),
               (168, "CMA Phase 2 reference", "A full 24-week Phase 2 investigation")],
}
PROBLEM_STATUSES = ("at-risk", "delayed")


def function_definitions(names):
    """The text of saved functions, as defined in schema/functions.tql (shown beside the queries)."""
    lines = FUNCTIONS_TQL.read_text().splitlines()
    out = []
    for name in names:
        start = next((i for i, l in enumerate(lines) if l.startswith(f"fun {name}(")), None)
        if start is None:
            continue
        end = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("return"))
        first = start
        while first > 0 and lines[first - 1].startswith("#"):       # the function's own comment
            first -= 1
        out.append({"name": name, "definition": "\n".join(lines[first:end + 1]).strip()})
    return out


def breach_from_days(start, edges, planned, completed, item, due):
    """The smallest delay (0..MAX) at which `item` is projected after `due`, or None. Projections only
    ever move later as the delay grows, so a binary search over whole days finds it exactly."""
    def late(d):
        return propagate(start, d, edges, planned, completed)[item][0] > due
    if not late(MAX_DELAY_DAYS):
        return None
    lo, hi = 0, MAX_DELAY_DAYS
    while lo < hi:
        mid = (lo + hi) // 2
        if late(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def default_subject(tx):
    """The condition to simulate when none is chosen: the first one TypeDB records as at risk or
    delayed (by due date), else the first not yet complete."""
    open_ = [c for c in queries.conditions(tx) if c["status"] != "complete"]
    risky = [c for c in open_ if c["status"] in PROBLEM_STATUSES]
    return (risky or open_)[0]["code"] if open_ else None


def scenario(tx, start_code, delay_days):
    """Everything the Impact Explorer shows for "delay start_code by delay_days". Read-only."""
    items = queries.all_items(tx)
    if start_code not in items:
        raise KeyError(start_code)
    deal = queries.deal(tx)
    hard_kinds = queries.hard_dependency_kinds(tx)
    edges = queries.impact_edges(tx, start_code)                       # recursive, in TypeDB
    every_dep = queries.all_dependencies(tx)
    limits = queries.all_deadline_limits(tx)
    deadline_info = {d["code"]: d for d in queries.deadlines(tx)}
    raci = queries.impact_raci(tx, start_code)                         # recursive, in TypeDB
    try:
        stakes = {c["obligation"]: c for c in queries.group_consents(tx, deal["target"]["code"])}  # guarded
        ownership_note = None
    except queries.OwnershipDataError as error:
        stakes, ownership_note = {}, str(error)

    planned = {c: date.fromisoformat(it["due"]) for c, it in items.items() if it["due"]}
    completed = {c for c, it in items.items() if it["status"] == "complete"}
    projected = propagate(start_code, delay_days, edges, planned, completed)
    reach = set(projected)                                             # start + everything downstream
    edge_by = {(e["from"], e["to"]): e for e in edges}
    ws_name = {c: it["name"] for c, it in items.items() if it["type"] == "workstream"}

    def path_to(code):
        chain = [code]
        while projected[chain[-1]][1]:
            chain.append(projected[chain[-1]][1][0])
        return list(reversed(chain))

    def item_view(code, role):
        it = items[code]
        new, causes = projected.get(code, (planned.get(code), []))
        old = planned.get(code)
        view = {"code": code, "name": it["name"], "type": it["type"], "status": it["status"], "role": role,
                "workstream": {"code": it["workstream"], "name": ws_name.get(it["workstream"])} if it["workstream"] else None,
                "planned": old and old.isoformat(), "projected": new and new.isoformat(),
                "shift_days": (new - old).days if new and old else 0, "completed": code in completed,
                "caused_by": [{"code": c, "kind": edge_by[(c, code)]["kind"], "lag": edge_by[(c, code)]["lag"]} for c in causes],
                "path": path_to(code) if code in projected else [code],
                "raci": raci.get(code, [])}
        return view

    nodes = {c: item_view(c, "start" if c == start_code else "downstream") for c in sorted(reach)}

    deadlines = {}
    for lim in limits:
        if lim["item"] not in reach:
            continue
        dl = deadlines.setdefault(lim["deadline"], {
            **item_view(lim["deadline"], "deadline"), "due": lim["due"], "consequence": lim["consequence"],
            "legal_basis": deadline_info.get(lim["deadline"], {}).get("legal_basis"),
            "bound": deadline_info.get(lim["deadline"], {}).get("bound", []),
            "consent_from": deadline_info.get(lim["deadline"], {}).get("consent_from", []),
            "group_obligation": None, "limits": []})
        due = date.fromisoformat(lim["due"])
        new = projected[lim["item"]][0]
        dl["limits"].append({"code": lim["item"], "name": items[lim["item"]]["name"],
                             "planned": planned[lim["item"]].isoformat(), "projected": new.isoformat(),
                             "breached": new > due, "days_over": max(0, (new - due).days),
                             "headroom_days": max(0, (due - new).days),
                             "breach_from_days": breach_from_days(start_code, edges, planned, completed, lim["item"], due)})
        if lim["deadline"] in stakes:
            c = stakes[lim["deadline"]]
            dl["group_obligation"] = {"owed_by": c["owed_by"], "consent_from": c["consent_from"],
                                      "effective_stake": format(c["stake"].normalize(), "f")}
    for dl in deadlines.values():
        dl["limits"].sort(key=lambda x: x["code"])
        dl["breached"] = any(x["breached"] for x in dl["limits"])
        dl["days_over"] = max(x["days_over"] for x in dl["limits"])
        dl["headroom_days"] = min(x["headroom_days"] for x in dl["limits"])
        froms = [x["breach_from_days"] for x in dl["limits"] if x["breach_from_days"] is not None]
        dl["breach_from_days"] = min(froms) if froms else None

    # Soft (informing) links touching the affected items: shown for context, never move dates.
    context = {}
    soft = [d for d in every_dep if d["kind"] not in hard_kinds and (d["from"] in reach or d["to"] in reach)]
    for d in soft:
        for c in (d["from"], d["to"]):
            if c not in reach and c not in context:
                context[c] = item_view(c, "context")

    graph_edges = ([{**e, "hard": True, "moved": e["from"] in [x["code"] for x in nodes[e["to"]]["caused_by"]]
                     and nodes[e["to"]]["shift_days"] > 0} for e in sorted(edges, key=lambda e: (e["from"], e["to"]))]
                   + [{**d, "hard": False, "moved": False} for d in soft]
                   + [{"from": x["code"], "to": code, "kind": "deadline-limit", "lag": None, "hard": False,
                       "moved": x["breached"]} for code, dl in deadlines.items() for x in dl["limits"]])

    moved = [n for n in nodes.values() if n["shift_days"] > 0]
    breached = [dl for dl in deadlines.values() if dl["breached"]]
    people = {}
    # People to act: RACI on items this scenario actually moves, and on deadlines it breaches.
    for n in [nodes[c] for c in sorted(nodes)] + list(deadlines.values()):
        if not (n["shift_days"] > 0 or n.get("breached")):
            continue
        for r in n["raci"]:
            p = people.setdefault(r["code"], {"code": r["code"], "name": r["name"], "type": r["type"], "employer": r["employer"],
                                              "title": r["title"], "items": []})
            p["items"].append({"code": n["code"], "name": n["name"], "letter": r["letter"], "via": r["via"],
                               "kind": "deadline" if n["role"] == "deadline" else "item",
                               "breached": n.get("breached", False), "shift_days": n["shift_days"]})
    strength = {l: i for i, l in enumerate("ARCI")}
    people_list = sorted(people.values(), key=lambda p: (min(strength[i["letter"]] for i in p["items"]), -len(p["items"]), p["name"]))

    scenarios = [{"days": 0, "label": "Base plan", "detail": "No delay: the plan as recorded in TypeDB", "kind": "baseline"}]
    scenarios += [{"days": d, "label": label, "detail": detail, "kind": "reference"}
                  for d, label, detail in REFERENCE_SCENARIOS.get(start_code, [])]
    for code, dl in sorted(deadlines.items(), key=lambda kv: (kv[1]["breach_from_days"] or 10**6)):
        if dl["breach_from_days"] is not None:
            scenarios.append({"days": dl["breach_from_days"], "label": f"Deadline breached: {dl['name']}",
                              "detail": "The shortest delay at which this deadline is projected to be missed", "kind": "threshold",
                              "deadline": code})
    seen_days, unique = set(), []
    for s in sorted(scenarios, key=lambda s: (s["days"], s["kind"] != "baseline")):
        if s["days"] not in seen_days:
            seen_days.add(s["days"])
            unique.append(s)

    level = "breach" if breached else "delayed" if moved else "on-plan"
    shown_deps = [d for d in every_dep if d["from"] in nodes or d["to"] in nodes or d["from"] in deadlines
                  or d["to"] in deadlines or d["from"] in context or d["to"] in context]
    names = {c: items[c]["name"] for d in shown_deps for c in (d["from"], d["to"]) if c in items}
    statuses = {c: items[c]["status"] for c in names}
    return {
        "start": nodes[start_code], "delay_days": delay_days, "as_of": deal["as_of"], "max_delay_days": MAX_DELAY_DAYS,
        "level": level, "hard_kinds": hard_kinds,
        "items": [nodes[c] for c in sorted(nodes, key=lambda c: (nodes[c]["planned"] or "", c))],
        "deadlines": sorted(deadlines.values(), key=lambda d: (d["due"], d["code"])),
        "context": sorted(context.values(), key=lambda n: n["code"]),
        "edges": graph_edges,
        "dependencies": shown_deps, "names": dict(sorted(names.items())), "statuses": dict(sorted(statuses.items())),
        "summary": {"moved": len(moved), "workstreams": sorted({n["workstream"]["name"] for n in moved if n["workstream"]}),
                    "breached": [d["code"] for d in breached], "people": people_list},
        "scenarios": unique, "ownership_note": ownership_note,
        "functions": function_definitions(["impact_edges", "downstream"]),
    }


# ---- Schedule float (docs/phase6-plan.md §5.4): one calculation for every screen ---------------

def schedule_float(items, dependencies, limits, hard_kinds):
    """{item code: {"float", "deadline", "by_deadline": {deadline: float}}} for every item not yet complete.

    The float to a deadline is the largest delay the item can absorb before that deadline breaks: the
    smallest breaching delay found by breach_from_days (the Impact Explorer's search), minus one. An item's
    float is the smallest over the deadlines it can reach; None when no deadline breaks within range."""
    def d(value):
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    edges = [{"from": e["from"], "to": e["to"], "lag": e["lag"] or 0} for e in dependencies if e["kind"] in hard_kinds]
    nxt = {}
    for e in edges:
        nxt.setdefault(e["from"], []).append(e)
    planned = {c: d(i["due"]) for c, i in items.items() if i.get("due")}
    completed = {c for c, i in items.items() if i["status"] == "complete"}
    out = {}
    for code, item in items.items():
        if code in completed or not item.get("due"):
            continue
        reach, todo, own = {code}, [code], []        # this item's downstream edges, as impact_edges returns them
        while todo:
            for e in nxt.get(todo.pop(), []):
                own.append(e)
                if e["to"] not in reach:
                    reach.add(e["to"])
                    todo.append(e["to"])
        by_deadline = {}
        for limit in limits:
            if limit["item"] not in reach:
                continue
            b = breach_from_days(code, own, planned, completed, limit["item"], d(limit["due"]))
            if b is not None:
                by_deadline[limit["deadline"]] = min(b - 1, by_deadline.get(limit["deadline"], b - 1))
        best = min(by_deadline.items(), key=lambda kv: (kv[1], kv[0]), default=None)
        out[code] = {"float": best[1] if best else None, "deadline": best[0] if best else None, "by_deadline": by_deadline}
    return out


def critical_paths(floats):
    """{deadline: {"float", "items"}}: for each deadline, its exposure (the smallest float into it) and its
    deadline-critical chain, the open items sharing that smallest float. Not a conventional (CPM) critical path,
    which runs to the end of the project: this is measured against each contractual deadline."""
    out = {}
    for code, f in floats.items():
        for deadline, value in f["by_deadline"].items():
            cur = out.get(deadline)
            if cur is None or value < cur["float"]:
                out[deadline] = {"float": value, "items": [code]}
            elif value == cur["float"]:
                cur["items"].append(code)
    return {k: {**v, "items": sorted(v["items"])} for k, v in out.items()}
