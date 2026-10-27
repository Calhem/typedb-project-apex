"""Connection paths between two things on the deal (/api/connections, decision D12, docs/phase6-plan.md §7).

Fixed-depth TypeQL (one query per path length, 1 to 3 relations) finds every simple path through entities
only, so the transaction itself (a relation) is never a stepping stone and "both are on the deal" is never
offered as a connection. Python ranks the paths by a stated cost (config/connection-policy.yaml), keeps the
best, and explains each step in words from the relation's type, the roles used and its attributes.
"""

import math
import time
import warnings
from datetime import date
from functools import cache
from pathlib import Path

import yaml

from apex import queries

POLICY_FILE = Path(__file__).resolve().parent.parent / "config" / "connection-policy.yaml"
EVENT_TYPES = ("request", "response", "meeting", "delivery", "decision", "notice", "status-change")
DISCLAIMER = ("A path shows records that connect these two. It does not imply a legal or commercial relationship "
              "beyond what each record states.")
LETTERS = {"accountability": "Accountable", "responsibility": "Responsible", "consultation": "Consulted", "notification": "Informed"}

# How each relation reads, from the roles of its players. {role} is replaced by the player's name.
TEMPLATES = {
    "acquisition": "{acquirer} is acquiring {target}",
    "employment": "{employee} works for {employer}",
    "mandate": "{adviser} advises {client}",
    "financing": "{lender} lends to {borrower}",
    "authority-review": "{reviewer} reviews {subject}, applied for by {applicant}",
    "ownership": "{owner} owns {stake} of {owned}",
    "bound-by": "{obligor} is bound by {obligation}",
    "consent-right": "{obligation} needs the consent of {grantor}",
    "deadline-limit": "{limited-item} must happen by {deadline}",
    "risk-exposure": "the risk “{risk}” threatens {exposed}",
    "risk-ownership": "{risk-owner} owns the risk “{owned-risk}”",
    "sequencing": "{prerequisite} must be complete before {dependent}",
    "gating": "{prerequisite} must be satisfied before {dependent}",
    "informing": "{prerequisite} informs {dependent} (information only; it does not move dates)",
    "work-breakdown": "{part} is part of the {whole} workstream",
}
ROLE_ALIASES = {"accountable-item": "item", "buyer": "acquirer", "seller": "target"}


@cache
def policy():
    return yaml.safe_load(POLICY_FILE.read_text())


def _role(label):
    role = label.split(":")[-1]
    return ROLE_ALIASES.get(role, role)


def _kind(type_label):
    if type_label == "person":
        return "person"
    if type_label in ("risk",):
        return "risk"
    if type_label == "workstream":
        return "workstream"
    if type_label in ("activity", "milestone", "regulatory-clearance", "shareholder-approval", "court-sanction",
                      "covenant", "deadline"):
        return "item"
    return "organisation"


def _node(things, code):
    t = things.get(code, {"code": code, "name": code, "type": "unknown"})
    return {**t, "kind": _kind(t["type"])}


def _cost(p, types, stones, links):
    cost = p["hop_cost"] * len(types) + sum(p["relation_weights"].get(t, 1.0) for t in types)
    return round(cost + sum(p["hub_penalty"] * math.log2(1 + links.get(s, 0)) for s in stones), 4)


def _allowed(p, rtype, hops, activity):
    """Activity records are interactions: usable only when asked for, and only in short paths."""
    return rtype not in EVENT_TYPES or (activity and hops <= p["event_max_hops"])


def find(tx, a, b, max_hops=3, results=None, activity=True, workstreams=False):
    """Paths from a to b, best first. Raises KeyError for an unknown code and ValueError for bad options.

    Met in the middle: one query for each end's neighbours gives every one- and two-step path; for three
    steps, one more query finds the relations joining the two neighbour sets. Simple paths only (no thing
    or relation twice), checked here on each relation's identity."""
    p = policy()
    results = results or p["default_results"]
    if not 1 <= max_hops <= p["max_hops"] or not 1 <= results <= p["max_results"]:
        raise ValueError(f"max_hops must be 1–{p['max_hops']} and results 1–{p['max_results']}")
    if a == b:
        raise ValueError("choose two different things")
    things = queries.connectables(tx)
    for code in (a, b):
        if code not in things:
            raise KeyError(code)
    links = queries.entity_links(tx)
    started = time.perf_counter()

    def hop_rows(code):
        return [(r.get("r").get_iid(), r.get("t").get_label(), r.get("mc").as_attribute().get_value())
                for r in queries.rows(tx, queries.neighbours_query(code, workstreams))]
    na, nb = hop_rows(a), hop_rows(b)
    by_end = {}
    for iid, t, m in nb:
        by_end.setdefault(m, []).append((iid, t))
    candidates = []

    def add(iids, types, stones):
        candidates.append({"iids": iids, "types": types, "stones": stones, "cost": _cost(p, types, stones, links)})

    for iid, t, m in na:                                   # one step: a relation joining a and b
        if m == b and _allowed(p, t, 1, activity):
            add([iid], [t], [])
    depths = [{"hops": 1, "paths": len(candidates), "capped": False, "skipped": False, "note": None}]
    if max_hops >= 2:                                      # two steps: a shared neighbour
        before = len(candidates)
        for i1, t1, m in na:
            if m in (a, b) or not _allowed(p, t1, 2, activity):
                continue
            for i2, t2 in by_end.get(m, []):
                if i2 != i1 and _allowed(p, t2, 2, activity):
                    add([i1, i2], [t1, t2], [m])
        depths.append({"hops": 2, "paths": len(candidates) - before, "capped": False, "skipped": False, "note": None})
    if max_hops >= 3:                                      # three steps: neighbours joined in the middle
        spent = (time.perf_counter() - started) * 1000
        starts = {m for _, t, m in na if m not in (a, b) and _allowed(p, t, 3, activity)}
        ends = {m for _, t, m in nb if m not in (a, b) and _allowed(p, t, 3, activity)}
        if spent > p["budget_ms"]:
            depths.append({"hops": 3, "paths": 0, "capped": False, "skipped": True,
                           "note": f"Skipped: the time budget ({p['budget_ms']} ms) was used by shorter paths."})
        elif starts and ends:
            from_a = {}
            for iid, t, m in na:
                if _allowed(p, t, 3, activity):
                    from_a.setdefault(m, []).append((iid, t))
            before = len(candidates)
            middle = queries.rows(tx, queries.middle_query(sorted(starts), sorted(ends), workstreams, p["row_cap"]))
            for r in middle:
                i2, t2 = r.get("r").get_iid(), r.get("t").get_label()
                m, n = r.get("mc").as_attribute().get_value(), r.get("nc").as_attribute().get_value()
                if len({a, b, m, n}) < 4:
                    continue
                for i1, t1 in from_a.get(m, []):
                    for i3, t3 in by_end.get(n, []):
                        if len({i1, i2, i3}) == 3 and _allowed(p, t3, 3, activity):
                            add([i1, i2, i3], [t1, t2, t3], [m, n])
            depths.append({"hops": 3, "paths": len(candidates) - before, "capped": len(middle) >= p["row_cap"],
                           "skipped": False, "note": None})
        else:
            depths.append({"hops": 3, "paths": 0, "capped": False, "skipped": False, "note": None})
    candidates.sort(key=lambda c: (c["cost"], len(c["types"]), [*c["stones"]], c["types"], c["iids"]))
    best, rest = candidates[:results], candidates[results:]

    details = _relation_details(tx, sorted({i for c in best for i in c["iids"]}))
    deal = queries.deal(tx)                     # the transaction is a relation: named for context, never a step
    named = {**things, deal["code"]: {"code": deal["code"], "name": deal["name"], "type": "acquisition"}}
    paths = [_explain(c, n + 1, a, b, named, links, details, p) for n, c in enumerate(best)]
    groups = {}
    for c in rest:
        key = c["stones"][0] if c["stones"] else "direct"
        groups[key] = groups.get(key, 0) + 1
    more = sorted(({"through": _node(things, k) if k != "direct" else None, "count": n} for k, n in groups.items()),
                  key=lambda g: (-g["count"], g["through"]["name"] if g["through"] else ""))
    return {"from": _node(things, a), "to": _node(things, b), "max_hops": max_hops, "activity": activity,
            "workstreams": workstreams, "total": len(candidates), "paths": paths, "more": more, "depths": depths,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "disclaimer": DISCLAIMER}


def _relation_details(tx, iids):
    """{iid: {"players": [(role, code)], "attributes": {type: value}}} for the relations on the best paths."""
    out = {i: {"players": [], "attributes": {}} for i in iids}
    if not iids:
        return out
    for r in queries.rows(tx, queries.relation_players_query(iids)):
        out[r.get("r").get_iid()]["players"].append((_role(r.get("role").get_label()), r.get("c").as_attribute().get_value()))
    for r in queries.rows(tx, queries.relation_attributes_query(iids)):
        attr = r.get("attr").as_attribute()
        with warnings.catch_warnings():         # typedb-driver 3.14.0 reads datetimes with a deprecated call
            warnings.simplefilter("ignore", DeprecationWarning)
            value = attr.get_value()
        out[r.get("r").get_iid()]["attributes"][attr.get_type().get_label()] = value
    for d in out.values():
        d["players"] = sorted(set(d["players"]))
    return out


def _value(v):
    if isinstance(v, date):
        return v.isoformat()
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _explain(c, rank, a, b, things, links, details, p):
    nodes = [a, *c["stones"], b]
    steps, notes = [], []
    for i, (iid, rtype) in enumerate(zip(c["iids"], c["types"])):
        d = details[iid]
        frm, to = nodes[i], nodes[i + 1]
        roles_of = {}
        for role, code in d["players"]:
            roles_of.setdefault(code, []).append(role)
        attrs = {k: _value(v) for k, v in d["attributes"].items()}
        steps.append({
            "from": frm, "to": to, "type": rtype, "code": attrs.get("code"),
            "from_role": (roles_of.get(frm) or ["?"])[0], "to_role": (roles_of.get(to) or ["?"])[0],
            "sentence": _sentence(rtype, d["players"], frm, to, attrs, things),
            "others": [{"role": role, "code": code, "name": things.get(code, {}).get("name") or code}
                       for role, code in d["players"] if code not in (frm, to)],
            "attributes": {k: v for k, v in attrs.items() if k in ("code", "summary", "occurred-at", "stake", "lag-days",
                                                                     "mandate-type", "job-title", "review-phase", "legal-basis")},
        })
    for s in c["stones"]:
        if links.get(s, 0) >= p["hub_note_links"]:
            notes.append(f"Goes through {things[s]['name']}, a hub with {links[s]} links: the connection may be general.")
    if any(t in EVENT_TYPES for t in c["types"]):
        notes.append("Includes an activity record (a meeting, request or other event): an interaction, not a standing relationship.")
    if "work-breakdown" in c["types"]:
        notes.append("Includes a shared workstream: being in the same workstream is a weak connection.")
    return {"rank": rank, "hops": len(c["types"]), "cost": c["cost"], "direct": len(c["types"]) == 1,
            "nodes": [_node(things, n) for n in nodes], "steps": steps, "notes": notes,
            "text": " → ".join([things[a]["name"], *[f"[{t}] {things[n]['name']}" for t, n in zip(c["types"], nodes[1:])]])}


def _sentence(rtype, players, frm, to, attrs, things):
    name = lambda code: things.get(code, {}).get("name") or code   # noqa: E731
    by_role = {}
    for role, code in players:
        by_role.setdefault(role, []).append(code)
    ends = {frm, to}

    def fill(role):
        codes = by_role.get(role, [])
        pick = [c for c in codes if c in ends] or codes
        return name(pick[0]) if pick else role

    if rtype in LETTERS:
        return f"{fill('holder')} is {LETTERS[rtype]} for {fill('item')}" + (" (under their firm’s mandate)" if by_role.get("via") else "")
    if rtype in TEMPLATES:
        text = TEMPLATES[rtype]
        for role in sorted(by_role, key=len, reverse=True):
            text = text.replace("{" + role + "}", fill(role))
        text = text.replace("{stake}", f"{float(attrs['stake']) * 100:g}%" if "stake" in attrs else "a share")
        lag = attrs.get("lag-days")
        if rtype in ("sequencing", "gating") and lag:
            text += f", with a {lag}-day lag"
        if rtype == "employment" and attrs.get("job-title"):
            text += f" as {attrs['job-title']}"
        if rtype == "mandate" and attrs.get("mandate-type"):
            text += f" ({attrs['mandate-type']})"
        return text
    if rtype in EVENT_TYPES:
        when = str(attrs.get("occurred-at", ""))[:10]
        what = f"the {rtype.replace('-', ' ')} “{attrs.get('summary', '')}”" + (f" on {date.fromisoformat(when):%d %b %Y}" if when else "")
        roles = {code: role for role, code in players if code in ends}
        rf, rt = roles.get(frm), roles.get(to)
        if rtype == "request" and {rf, rt} == {"actor", "recipient"}:
            sender, receiver = (frm, to) if rf == "actor" else (to, frm)
            return f"{name(sender)} sent {name(receiver)} {what}"
        if rtype == "meeting" and rf in ("actor", "attendee") and rt in ("actor", "attendee"):
            return f"{name(frm)} and {name(to)} were both at {what}"
        if "agenda-item" in (rf, rt) or "subject" in (rf, rt):
            item = frm if rf in ("agenda-item", "subject") else to
            person = to if item == frm else frm
            verb = "discussed" if roles.get(item) == "agenda-item" else "is about"
            return f"{what[0].upper()}{what[1:]} {verb} {name(item)}; {name(person)} took part as {roles.get(person)}"
        return f"{name(frm)} ({rf}) and {name(to)} ({rt}) both appear in {what}"
    return f"{name(frm)} ({by_role and next((r for r, c in players if c == frm), '?')}) is linked to {name(to)} by {rtype}"
