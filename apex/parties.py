"""The Parties pages: /api/register (everyone, with engagement), /api/parties/{code} (one profile) and
/api/engagement-policy. Contacts are masked here, on the server; no full value ever leaves the API.
"""

from apex import engagement, explorer, queries
from apex.workstreams import CONDITIONS

AR = ("A", "R")


def mask_email(value):
    if not value or "@" not in value:
        return None
    local, domain = value.split("@", 1)
    return f"{local[0]}{'•' * max(3, len(local) - 2)}{local[-1]}@{domain}"


def mask_phone(value):
    return f"{value[:-3]}•••" if value else None


def _context(tx):
    """Everything the engagement calculation reads, in one READ transaction."""
    as_of = queries.deal(tx)["as_of"]
    items = queries.all_items(tx)
    floats = engagement.schedule_float(items, queries.all_dependencies(tx), queries.all_deadline_limits(tx),
                                       set(queries.hard_dependency_kinds(tx)))
    return {
        "as_of": as_of, "items": items, "floats": floats,
        "people": queries.people(tx), "organisations": queries.organisation_parties(tx),
        "raci": queries.all_raci(tx), "events": queries.workstream_events(tx),
        "engagement_types": queries.subtypes(tx, queries.ENGAGEMENT_TYPES),
        "outreach_types": queries.subtypes(tx, queries.OUTREACH_TYPES),
        "reviews": queries.authority_reviews(tx),
    }


def _assess(ctx, party, employees=()):
    return engagement.assess(party, ctx["as_of"], ctx["items"], ctx["raci"], ctx["events"], ctx["engagement_types"],
                             ctx["reviews"], ctx["floats"], employees, outreach_types=ctx["outreach_types"])


def _party_of(ctx, code):
    """(party dict for engagement.assess, employees) for a person or organisation code, or (None, ())."""
    orgs = {o["code"]: o for o in ctx["organisations"]}
    for p in ctx["people"]:
        if p["code"] == code:
            emp = p["employer"]
            return ({"code": code, "name": p["name"], "kind": "person", "employer": emp["code"] if emp else None,
                     "authority": bool(emp and orgs.get(emp["code"], {}).get("authority"))}, ())
    if code in orgs:
        o = orgs[code]
        staff = tuple(p["code"] for p in ctx["people"] if p["employer"] and p["employer"]["code"] == code)
        return ({"code": code, "name": o["name"], "kind": "organisation", "employer": None, "authority": o["authority"]}, staff)
    return None, ()


def _delivery(ctx, code):
    """Items the party is Accountable or Responsible for that are at risk, delayed or overdue (delivery risk,
    reported apart from engagement)."""
    out = []
    for r in ctx["raci"]:
        it = ctx["items"][r["item"]]
        if r["holder"] != code or r["letter"] not in AR or it["status"] == "complete":
            continue
        overdue = it["due"] and it["due"] < ctx["as_of"] and it["type"] != "deadline"
        if it["status"] in engagement.PROBLEMS or overdue:
            out.append({"code": r["item"], "name": it["name"], "status": it["status"], "due": it["due"],
                        "reason": "overdue" if overdue else it["status"]})
    return sorted(out, key=lambda d: (d["due"] or "", d["code"]))


def _summary(ctx, party, employees):
    code = party["code"]
    items = ctx["items"]
    mine = [r for r in ctx["raci"] if r["holder"] == code]
    open_mine = [r for r in mine if items[r["item"]]["status"] != "complete"]
    result = _assess(ctx, party, employees)
    members = {code, *employees}
    engaged = [e for e in ctx["events"] if e["actor"]["code"] in members or members & {a["code"] for a in e["attendees"]}]
    wss = {items[r["item"]]["workstream"] or r["item"] for r in mine}
    return {
        "engagement": {k: result[k] for k in ("level", "category", "profile", "headline", "flags", "no_engagement_recorded")},
        "letters": {letter: sum(r["letter"] == letter for r in open_mine) for letter in "ARCI"},
        "outstanding": sum(r["letter"] in AR for r in open_mine),
        "delivery_risk": _delivery(ctx, code),
        "workstreams": sorted(w for w in wss if w and w.startswith("WS-")),
        "last_activity": max((e["when"] for e in engaged), default=None),
    }, result


def register(tx):
    ctx = _context(tx)
    contacts = queries.contacts(tx)
    people = []
    for p in ctx["people"]:
        party, staff = _party_of(ctx, p["code"])
        summary, _ = _summary(ctx, party, staff)
        email, phone = contacts.get(p["code"], (None, None))
        people.append({**p, "authority": party["authority"], **summary,
                       "contact": {"email": mask_email(email), "phone": mask_phone(phone)}})
    organisations = []
    for o in ctx["organisations"]:
        party, staff = _party_of(ctx, o["code"])
        summary, _ = _summary(ctx, party, staff)
        organisations.append({**o, "people": len(staff), **summary})
    names = {w["code"]: w["name"] for w in queries.workstreams(tx, ctx["items"])}
    return {"as_of": ctx["as_of"], "people": people, "organisations": organisations, "workstream_names": names}


def profile(tx, code):
    """One party's profile, or None if no person or organisation has that code."""
    ctx = _context(tx)
    party, staff = _party_of(ctx, code)
    if party is None:
        return None
    items = ctx["items"]
    summary, result = _summary(ctx, party, staff)
    names = {w["code"]: w["name"] for w in queries.workstreams(tx, items)}
    members = {code, *staff}
    addressed = {code} | ({party["employer"]} if party["employer"] else set())
    assignments = []
    for r in (r for r in ctx["raci"] if r["holder"] == code):
        it = items[r["item"]]
        fl = ctx["floats"].get(r["item"], {})
        assignments.append({"item": r["item"], "name": it["name"], "type": it["type"], "letter": r["letter"], "via": r["via"],
                            "status": it["status"], "due": it["due"], "criticality": it.get("criticality"),
                            "workstream": {"code": it["workstream"] or r["item"], "name": names.get(it["workstream"] or r["item"])},
                            "float": fl.get("float"), "float_deadline": fl.get("deadline"),
                            "simulate": it["type"] in CONDITIONS and it["status"] != "complete"})
    assignments.sort(key=lambda a: (a["status"] == "complete", engagement.LETTER_ORDER.index(a["letter"]), a["due"] or "", a["item"]))
    history = [e for e in ctx["events"] if e["actor"]["code"] in members or members & {a["code"] for a in e["attendees"]}
               or addressed & {r["code"] for r in e["recipients"]} or members & {r["code"] for r in e["recipients"]}]
    requests_to = [e for e in history if e["type"] == "request" and (addressed | members) & {r["code"] for r in e["recipients"]}]
    requests_from = [e for e in history if e["type"] == "request" and e["actor"]["code"] in members]
    node = explorer.node(tx, code)
    contact = None
    if party["kind"] == "person":
        email, phone = queries.contacts(tx).get(code, (None, None))
        contact = {"email": mask_email(email), "phone": mask_phone(phone)}
    person = next((p for p in ctx["people"] if p["code"] == code), None)
    org = next((o for o in ctx["organisations"] if o["code"] == code), None)
    return {
        "as_of": ctx["as_of"], "code": code, "name": party["name"], "kind": party["kind"], "authority": party["authority"],
        "type": org["type"] if org else "person", "jurisdiction": org.get("jurisdiction") if org else None,
        "title": person["title"] if person else None, "employer": person["employer"] if person else None,
        "contact": contact, **summary, "assessments": result["assessments"],
        "assignments": assignments, "requests_to": requests_to, "requests_from": requests_from, "history": history,
        "people": [p for p in ctx["people"] if p["code"] in staff],
        "relations": node["relations"] if node else [],
        "workstream_names": names,
    }
