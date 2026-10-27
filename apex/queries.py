"""Every TypeQL query Apex runs to answer questions (the loader's inserts are in seed.py).

Each function takes an open transaction, so callers decide its kind: the app only ever opens
READ transactions; the loader runs the same checks inside its WRITE transaction before
committing. Queries are explained in docs/queries.md (Q-numbers below).
"""

import re

from apex.db import fetch, rows, string, to_decimal


class OwnershipDataError(Exception):
    """Ownership data would give misleading effective stakes, so none are returned."""

    def __init__(self, problems):
        self.problems = problems
        parts = []
        if problems["cycles"]:
            parts.append("ownership cycle through " + ", ".join(problems["cycles"]))
        if problems["over_owned"]:
            parts.append("more than 100% owned: " + ", ".join(
                f"{code} ({total * 100}%)" for code, total in problems["over_owned"].items()))
        super().__init__("Ownership data inconsistent: " + "; ".join(parts))


# ---- Ownership guards (Q2.7, Q2.8) ----------------------------------------------------------

OWNERSHIP_CYCLES = """
match let $c in ownership_cycles(); $c has code $code;
select $code; distinct;
"""

OVER_OWNED = """
match ownership (owned: $c), has stake $s; reduce $total = sum($s) groupby $c;
match $total > 1.0dec; $c has code $code;
fetch { "company": $code, "total": $total };
"""


def ownership_problems(tx):
    """Companies in an ownership cycle, and companies whose direct stakes add up to over 100%."""
    cycles = sorted({r.get("code").as_attribute().get_value() for r in rows(tx, OWNERSHIP_CYCLES)})
    over = {d["company"]: to_decimal(d["total"]) for d in fetch(tx, OVER_OWNED)}
    return {"cycles": cycles, "over_owned": over}


# ---- Effective ownership (Q2.6) --------------------------------------------------------------

def effective_ownership_query(parent_code):
    return f"""
match $p isa company, has code {string(parent_code)}; let $c, $e in group_members($p);
reduce $effective = sum($e), $routes = count groupby $c;
match $c has code $code, has name $name;
fetch {{ "company": $code, "name": $name, "effective stake": $effective, "routes": $routes }};
"""


def effective_ownership(tx, parent_code):
    """{company code: {"name", "stake" (Decimal), "routes"}} for every company parent_code holds.

    Runs the guards first, in the same transaction, and raises OwnershipDataError instead of
    returning figures if either finds anything: group_members is only correct without cycles.
    """
    problems = ownership_problems(tx)
    if problems["cycles"] or problems["over_owned"]:
        raise OwnershipDataError(problems)
    return {
        d["company"]: {"name": d["name"], "stake": to_decimal(d["effective stake"]), "routes": d["routes"]}
        for d in fetch(tx, effective_ownership_query(parent_code))
    }


def group_consents_query(parent_code):
    return f"""
match $p isa company, has code {string(parent_code)}; let $c, $e in group_members($p);
      bound-by (obligor: $c, obligation: $o);
      consent-right (grantor: $g, obligation: $o);
reduce $effective = sum($e) groupby $o, $c, $g;
match $o has code $oc; $c has code $cc; $g has code $gc;
fetch {{ "obligation": $oc, "owed by": $cc, "consent from": $gc, "effective stake": $effective }};
"""


def group_consents(tx, parent_code):
    """Change-of-control consents owed anywhere in parent_code's group (Q2.10), guarded like
    effective_ownership: [{"obligation", "owed_by", "consent_from", "stake"}]."""
    problems = ownership_problems(tx)
    if problems["cycles"] or problems["over_owned"]:
        raise OwnershipDataError(problems)
    return sorted(({"obligation": d["obligation"], "owed_by": d["owed by"], "consent_from": d["consent from"],
                    "stake": to_decimal(d["effective stake"])} for d in fetch(tx, group_consents_query(parent_code))),
                  key=lambda r: r["obligation"])


# ---- Data-quality checks (Q1.8) --------------------------------------------------------------

DATA_QUALITY_CHECKS = {
    "no_accountable": """
match $x isa deal-item, has code $c; not { accountability (accountable-item: $x); };
fetch { "code": $c, "name": $x.name };
""",
    "overdue": """
match $d isa acquisition, has as-of-date $today;
      $x isa deal-item, has code $c, has due-date $due; $due < $today;
      not { $x has status "complete"; };
fetch { "code": $c, "name": $x.name };
""",
    "clearance_without_authority": """
match $x isa regulatory-clearance, has code $c; not { authority-review (subject: $x, reviewer: $r); };
fetch { "code": $c, "name": $x.name };
""",
}

# What each check means, in words for the "Attention needed" card: (title, explanation).
CHECK_DESCRIPTIONS = {
    "no_accountable": ("No accountable owner", "Work items with nobody Accountable (the A in RACI)."),
    "overdue": ("Overdue", "Items due before the deal's as-of date that are not complete."),
    "clearance_without_authority": ("Clearance with no reviewing authority",
                                    "Regulatory clearances with no authority recorded as reviewing them."),
}


def data_quality_details(tx):
    """{check name: [{"code", "name"}] sorted by code}. On the seed data every list is empty."""
    out = {}
    for name, q in DATA_QUALITY_CHECKS.items():
        found = {d["code"]: d.get("name") for d in fetch(tx, q)}
        out[name] = [{"code": c, "name": found[c]} for c in sorted(found)]
    return out


def data_quality_checks(tx):
    """{check name: sorted item codes}. On the seed data every list is empty."""
    return {name: [i["code"] for i in items] for name, items in data_quality_details(tx).items()}


# ---- Impact inputs (Q3.1, Q3.2, Q3.3) --------------------------------------------------------

def impact_edges_query(start_code):
    return f"""
match $start isa deal-item, has code {string(start_code)};
      let $from, $dep, $to in impact_edges($start);
      $dep isa! $kind; $from has code $fc; $to has code $tc;
fetch {{ "from": $fc, "to": $tc, "lag": $dep.lag-days, "kind": $kind }};
"""


ALL_ITEMS = """
match $x isa deal-item; $x isa! $t;
fetch { "item": { $x.* }, "type": $t,
        "workstream": [ match work-breakdown (whole: $w, part: $x); fetch { "code": $w.code }; ] };
"""

ALL_DEADLINE_LIMITS = """
match deadline-limit (limited-item: $x, deadline: $dl);
fetch { "item": $x.code, "deadline": { $dl.* },
        "bound": [ match bound-by (obligor: $o, obligation: $dl); fetch { "code": $o.code }; ] };
"""


def impact_edges(tx, start_code):
    """Every hard-dependency step downstream of start_code: [{"from", "to", "lag", "kind"}]."""
    edges = {(d["from"], d["to"]): d for d in fetch(tx, impact_edges_query(start_code))}
    return [{"from": f, "to": t, "lag": d["lag"], "kind": d["kind"]["label"]} for (f, t), d in edges.items()]


def all_items(tx):
    """{code: {"name", "type", "status", "criticality", "due", "planned_start", "actual_start", "progress",
    "workstream"}} for every deal-item."""
    out = {}
    for d in fetch(tx, ALL_ITEMS):
        item = d["item"]
        out[item["code"]] = {
            "name": item.get("name"), "type": d["type"]["label"], "status": item.get("status"),
            "criticality": item.get("criticality"), "due": item.get("due-date"),
            "planned_start": item.get("planned-start"), "actual_start": item.get("actual-start-date"),
            "progress": item.get("progress"), "workstream": d["workstream"][0]["code"] if d["workstream"] else None,
        }
    return out


def all_deadline_limits(tx):
    """[{"item", "deadline", "name", "due", "consequence", "bound": [codes]}]."""
    out = []
    for d in fetch(tx, ALL_DEADLINE_LIMITS):
        dl = d["deadline"]
        out.append({"item": d["item"], "deadline": dl["code"], "name": dl.get("name"), "due": dl["due-date"],
                    "consequence": dl.get("consequence"), "bound": sorted({b["code"] for b in d["bound"]})})
    return out


# ---- Counting, used by the loader to prove everything arrived --------------------------------

def count_exact(tx, type_label):
    """How many instances have exactly this type (not counting subtypes)."""
    result = rows(tx, f"match $x isa! {type_label}; reduce $n = count;")
    return result[0].get("n").get() if result else 0


def counts_by_type(tx, type_labels):
    return {label: count_exact(tx, label) for label in type_labels}


# ---- Deal Overview (Q1.1-Q1.7) ---------------------------------------------------------------
# The database holds one deal, so these queries find "the" acquisition rather than naming it.

class NoDealError(Exception):
    """The database doesn't hold exactly one acquisition, so the Overview can't be answered."""


CODE_TEXT = re.compile(r"^[A-Z]{2,5}-[A-Z0-9]+(-[A-Z0-9]+)*$")
CRITICALITIES = ("low", "medium", "high", "critical")    # the schema's @values for criticality
RACI_LETTERS = {"responsibility": "R", "accountability": "A", "consultation": "C", "notification": "I"}


def _label(type_doc):
    return type_doc["label"]


def _when(value):
    """A fetched datetime ("2026-09-02T23:59:00.000000000") without the nanoseconds."""
    return value.split(".")[0] if value else value


def money(value):
    """An exact decimal amount as plain text: "2400000000.0" -> "2400000000"."""
    amount = to_decimal(value)
    return str(amount.quantize(1)) if amount == amount.to_integral_value() else format(amount, "f")


def _item(doc, type_label=None):
    """The common fields of a deal-item fetched with { $x.* }."""
    out = {"code": doc["code"], "name": doc.get("name"), "status": doc.get("status"),
           "criticality": doc.get("criticality"), "due": doc.get("due-date"),
           "progress": doc.get("progress"), "completed": doc.get("completed-date")}
    if type_label:
        out["type"] = type_label
    return out


def _org(doc):
    return {"code": doc["code"], "name": doc.get("name"), "description": doc.get("description"),
            "jurisdiction": doc.get("jurisdiction"), "sector": doc.get("sector")}


DEAL = """
match $d isa acquisition (acquirer: $buyer, target: $target);
fetch { "deal": { $d.* }, "acquirer": { $buyer.* }, "target": { $target.* } };
"""


def deal(tx):
    """Q1.1: the deal's own attributes, with the acquirer and target."""
    found = fetch(tx, DEAL)
    if len(found) != 1:
        raise NoDealError(f"expected exactly one acquisition in the database, found {len(found)}")
    d = found[0]["deal"]
    return {"code": d["code"], "name": d.get("name"), "description": d.get("description"),
            "value": money(d["deal-value"]), "currency": d.get("currency"),
            "structure": d.get("deal-structure"), "as_of": d.get("as-of-date"),
            "acquirer": _org(found[0]["acquirer"]), "target": _org(found[0]["target"])}


CONDITIONS = """
match $c isa condition, has due-date $due; $c isa! $t;
sort $due asc;
fetch { "condition": { $c.* }, "type": $t };
"""


def conditions(tx):
    """Every condition to completion (clearances, shareholder approval, court sanction)."""
    return [_item(d["condition"], _label(d["type"])) for d in fetch(tx, CONDITIONS)]


COMMITTED_FINANCING = """
match $d isa acquisition; financing (purpose: $d), has deal-value $v, has currency $cur;
reduce $total = sum($v) groupby $cur;
fetch { "currency": $cur, "total": $total };
"""


def committed_financing(tx):
    """Total committed debt financing per currency: [{"currency", "total"}]."""
    return sorted(({"currency": d["currency"], "total": money(d["total"])} for d in fetch(tx, COMMITTED_FINANCING)),
                  key=lambda r: r["currency"])


def timeline_query(criticalities=None):
    filter_ = ""
    if criticalities:
        unknown = set(criticalities) - set(CRITICALITIES)
        if unknown:
            raise ValueError(f"unknown criticality: {sorted(unknown)}")
        # Only the schema's own criticality words reach the query, so the pattern is safe.
        filter_ = f' $crit like {string("^(" + "|".join(sorted(set(criticalities))) + ")$")};'
    return f"""
match {{ $x isa milestone; }} or {{ $x isa condition; }};
      $x isa! $t, has due-date $due, has criticality $crit;{filter_}
sort $due asc;
fetch {{ "item": {{ $x.* }}, "type": $t,
        "workstream": [ match work-breakdown (whole: $w, part: $x); fetch {{ "code": $w.code, "name": $w.name }}; ],
        "accountable": [ match accountability (holder: $p, accountable-item: $x); fetch {{ "code": $p.code, "name": $p.name }}; ] }};
"""


def timeline(tx, criticalities=None):
    """Q1.3: milestones and conditions by due date, optionally only some criticalities."""
    out = []
    for d in fetch(tx, timeline_query(criticalities)):
        item = _item(d["item"], _label(d["type"]))
        item["workstream"] = d["workstream"][0] if d["workstream"] else None
        item["accountable"] = d["accountable"][0] if d["accountable"] else None
        out.append(item)
    return out


DEADLINES = """
match $x isa deadline, has due-date $due;
sort $due asc;
fetch { "deadline": { $x.* },
        "limits": [ match deadline-limit (deadline: $x, limited-item: $i);
                    fetch { "code": $i.code, "name": $i.name, "due": $i.due-date, "status": $i.status }; ],
        "bound": [ match bound-by (obligation: $x, obligor: $o); fetch { "code": $o.code, "name": $o.name }; ],
        "consent from": [ match consent-right (obligation: $x, grantor: $g); fetch { "code": $g.code, "name": $g.name }; ] };
"""


def deadlines(tx):
    """Contractual deadlines, what each one limits, who is bound and whose consent it concerns."""
    out = []
    for d in fetch(tx, DEADLINES):
        dl = d["deadline"]
        item = _item(dl, "deadline")
        item.update(consequence=dl.get("consequence"), legal_basis=dl.get("legal-basis"),
                    limits=sorted(d["limits"], key=lambda i: i["code"]),
                    bound=sorted(d["bound"], key=lambda o: o["code"]),
                    consent_from=sorted(d["consent from"], key=lambda o: o["code"]))
        out.append(item)
    return out


APPROVALS = """
match $x isa regulatory-clearance, has due-date $due;
sort $due asc;
fetch { "clearance": { $x.* },
        "reviews": [ match $r isa authority-review (reviewer: $a, subject: $x);
                     fetch { "code": $a.code, "name": $a.name, "jurisdiction": $a.jurisdiction,
                             "legal basis": $r.legal-basis, "phase": $r.review-phase,
                             "applicant": [ match $r links (applicant: $app); fetch { "name": $app.name }; ] }; ],
        "accountable": [ match accountability (holder: $p, accountable-item: $x); fetch { "code": $p.code, "name": $p.name }; ],
        "latest": [ match $e isa deal-event (subject: $x), has occurred-at $t; sort $t desc; limit 1;
                    fetch { "when": $t, "summary": $e.summary }; ] };
"""


def approvals(tx):
    """Q1.5: every regulatory clearance with its reviewing authorities (an n-ary relation) and
    its latest recorded event. A clearance with no authority still appears, with no reviews."""
    out = []
    for d in fetch(tx, APPROVALS):
        c = d["clearance"]
        item = _item(c, "regulatory-clearance")
        item.update(
            description=c.get("description"), legal_basis=c.get("legal-basis"),
            reviews=sorted(({"code": r["code"], "name": r["name"], "jurisdiction": r.get("jurisdiction"),
                             "legal_basis": r.get("legal basis"), "phase": r.get("phase"),
                             "applicant": r["applicant"][0]["name"] if r["applicant"] else None}
                            for r in d["reviews"]), key=lambda r: r["code"]),
            accountable=d["accountable"][0] if d["accountable"] else None,
            latest={"when": _when(d["latest"][0]["when"]), "summary": d["latest"][0]["summary"]} if d["latest"] else None)
        out.append(item)
    return out


def activity_query(limit):
    if not (isinstance(limit, int) and 1 <= limit <= 50):
        raise ValueError("limit must be a whole number from 1 to 50")
    return f"""
match $e isa deal-event (actor: $who, subject: $what), has occurred-at $t; $who isa! $wt;
sort $t desc; limit {limit};
fetch {{ "when": $t, "summary": $e.summary, "kind": $e.event-kind, "actor type": $wt,
        "actor": {{ "code": $who.code, "name": $who.name }},
        "employer": [ match employment (employee: $who, employer: $org); fetch {{ "name": $org.name }}; ],
        "item": {{ "code": $what.code, "name": $what.name }} }};
"""


def activity(tx, limit=10):
    """Q1.6: the most recent deal events, newest first."""
    return [{"when": _when(d["when"]), "summary": d["summary"], "kind": d.get("kind"),
             "actor": {**d["actor"], "type": _label(d["actor type"]),
                       "employer": d["employer"][0]["name"] if d["employer"] else None},
             "item": d["item"]} for d in fetch(tx, activity_query(limit))]


def _team(org_var):
    """Sub-query: the organisation's people who hold any RACI letter on this deal (one query on
    the parent relation type covers all four letters)."""
    return f"""[ match employment (employee: $p, employer: {org_var}), has job-title $title; raci-assignment (holder: $p);
             select $p, $title; distinct; fetch {{ "code": $p.code, "name": $p.name, "title": $title }}; ]"""


PRINCIPALS = f"""
match $d isa acquisition (acquirer: $buyer, target: $target);
fetch {{ "acquirer": {{ $buyer.* }}, "acquirer team": {_team("$buyer")},
        "target": {{ $target.* }}, "target team": {_team("$target")} }};
"""

ADVISERS = f"""
match $d isa acquisition; $m isa mandate (client: $client, adviser: $adviser, engagement: $d); $adviser isa! $kind;
fetch {{ "mandate": $m.code, "mandate type": $m.mandate-type,
        "client": {{ "code": $client.code, "name": $client.name }},
        "adviser": {{ $adviser.* }}, "adviser type": $kind, "team": {_team("$adviser")} }};
"""

LENDERS = f"""
match $d isa acquisition; $f isa financing (purpose: $d, lender: $l, borrower: $b); $l isa! $kind;
fetch {{ "facility": {{ $f.* }}, "borrower": {{ "code": $b.code, "name": $b.name }},
        "lender": {{ $l.* }}, "lender type": $kind, "team": {_team("$l")} }};
"""

AUTHORITIES = """
match $a isa authority; $a isa! $kind;
fetch { "authority": { $a.* }, "type": $kind,
        "reviews": [ match authority-review (reviewer: $a, subject: $x);
                     fetch { "code": $x.code, "name": $x.name, "status": $x.status }; ],
        "raci": [ match $ra isa raci-assignment (holder: $a, item: $i); $ra isa! $letter;
                  fetch { "letter": $letter, "code": $i.code, "name": $i.name }; ] };
"""


def _people(team):
    return sorted(team, key=lambda p: p["code"])


def parties(tx):
    """Q1.2: everyone on the deal, by the part they play: principals, advisers (by mandate),
    lenders (by facility) and authorities (what they review, or the RACI letters they hold)."""
    p = fetch(tx, PRINCIPALS)[0]
    principals = [{"role": "Acquirer", "organisation": _org(p["acquirer"]), "team": _people(p["acquirer team"])},
                  {"role": "Target", "organisation": _org(p["target"]), "team": _people(p["target team"])}]
    advisers = sorted(({"mandate": d["mandate"], "mandate_type": d.get("mandate type"), "client": d["client"],
                        "organisation": _org(d["adviser"]), "type": _label(d["adviser type"]),
                        "team": _people(d["team"])} for d in fetch(tx, ADVISERS)),
                      key=lambda a: (a["client"]["code"] != principals[0]["organisation"]["code"], a["mandate"]))
    lenders = sorted(({"facility": {"code": d["facility"]["code"], "name": d["facility"].get("name"),
                                    "value": money(d["facility"]["deal-value"]),
                                    "currency": d["facility"].get("currency")},
                       "borrower": d["borrower"], "organisation": _org(d["lender"]), "type": _label(d["lender type"]),
                       "team": _people(d["team"])} for d in fetch(tx, LENDERS)),
                     key=lambda l: l["facility"]["code"])
    authorities = sorted(({"organisation": _org(d["authority"]), "type": _label(d["type"]),
                           "reviews": sorted(d["reviews"], key=lambda r: r["code"]),
                           "raci": sorted(({"letter": RACI_LETTERS[r["letter"]["label"]], "code": r["code"],
                                            "name": r["name"]} for r in d["raci"]), key=lambda r: r["code"])}
                          for d in fetch(tx, AUTHORITIES)),
                         key=lambda a: (not a["reviews"], a["organisation"]["code"]))
    return {"principals": principals, "advisers": advisers, "lenders": lenders, "authorities": authorities}


WORKSTREAMS = """
match $ws isa workstream, has planned-start $start;
sort $start asc;
fetch { "workstream": { $ws.* },
        "accountable": [ match accountability (holder: $p, accountable-item: $ws); fetch { "code": $p.code, "name": $p.name }; ] };
"""

ITEM_STATUS_COUNTS = """
match $ws isa workstream, has code $w; work-breakdown (whole: $ws, part: $i); $i has status $s;
reduce $n = count groupby $w, $s;
fetch { "workstream": $w, "status": $s, "items": $n };
"""


def workstreams(tx, items=None):
    """Q1.4: each workstream's status, its items' status counts, and progress DERIVED from its items
    (apex/progress.py: duration-weighted activity completion, and milestone readiness). Pass `items`
    (from all_items) when the caller already has them, so the query isn't run twice."""
    from apex import progress
    counts = {}
    for d in fetch(tx, ITEM_STATUS_COUNTS):
        counts.setdefault(d["workstream"], {})[d["status"]] = d["items"]
    items = all_items(tx) if items is None else items
    out = []
    for d in fetch(tx, WORKSTREAMS):
        ws = _item(d["workstream"], "workstream")
        derived = progress.summary([dict(it, code=c) for c, it in items.items() if it["workstream"] == ws["code"]])
        ws.update(planned_start=d["workstream"].get("planned-start"), description=d["workstream"].get("description"),
                  accountable=d["accountable"][0] if d["accountable"] else None,
                  item_statuses=counts.get(ws["code"], {}), progress=derived["percent"],
                  progress_basis=derived["basis"], milestones_achieved=derived["milestone_readiness"]["achieved"],
                  milestones_total=derived["milestone_readiness"]["total"])
        out.append(ws)
    return out


def raci_items_query(workstream_code):
    return f"""
match $ws isa workstream, has code {string(workstream_code)}; work-breakdown (whole: $ws, part: $i);
      $i has due-date $due; $i isa! $t;
sort $due asc;
fetch {{ "workstream": $ws.name, "item": {{ $i.* }}, "type": $t }};
"""


def raci_query(workstream_code):
    return f"""
match $ws isa workstream, has code {string(workstream_code)}; work-breakdown (whole: $ws, part: $i);
      $r isa raci-assignment (holder: $h, item: $i); $r isa! $kind; $h isa! $htype;
fetch {{ "item": $i.code, "letter": $kind, "holder": {{ "code": $h.code, "name": $h.name }}, "holder type": $htype,
        "employer": [ match employment (employee: $h, employer: $o); fetch {{ "code": $o.code, "name": $o.name }}; ],
        "via": [ match $r links (via: $m); $m isa mandate; fetch {{ "mandate": $m.code, "type": $m.mandate-type }}; ] }};
"""


def raci(tx, workstream_code):
    """Q1.7: compact RACI matrix for one workstream. The letter is the relation's own type.

    Returns {"workstream", "items": [...], "holders": [...], "cells": [{"item", "holder", "letter", "via"}]},
    or None if there is no such workstream. Holders are ordered strongest letter first (A, R, C, I).
    """
    items = fetch(tx, raci_items_query(workstream_code))
    if not items:
        exists = rows(tx, f"match $ws isa workstream, has code {string(workstream_code)};")
        if not exists:
            return None
    cells, holders = [], {}
    for d in fetch(tx, raci_query(workstream_code)):
        letter = RACI_LETTERS[_label(d["letter"])]
        h = d["holder"]["code"]
        holders.setdefault(h, {"code": h, "name": d["holder"]["name"], "type": _label(d["holder type"]),
                               "employer": d["employer"][0]["name"] if d["employer"] else None, "letters": set()})
        holders[h]["letters"].add(letter)
        cells.append({"item": d["item"], "holder": h, "letter": letter,
                      "via": d["via"][0]["mandate"] if d["via"] else None})
    strength = {letter: i for i, letter in enumerate("ARCI")}
    ordered = sorted(holders.values(), key=lambda h: (min(strength[x] for x in h["letters"]), h["name"]))
    return {"workstream": {"code": workstream_code, "name": items[0]["workstream"] if items else None},
            "items": [_item(d["item"], _label(d["type"])) for d in items],
            "holders": [{k: v for k, v in h.items() if k != "letters"} for h in ordered],
            "cells": sorted(cells, key=lambda c: (c["item"], c["holder"]))}


# ---- Relationship Explorer (Q2.1-Q2.3, Q2.9) -------------------------------------------------
# Generic on purpose: relations are read with `$r links ($role: $p)`, so any relation type (including
# one added later) is drawn with its own role names, and n-ary relations keep all their players.

PARTY_TYPES = """
match $t sub party; $t sub! $parent;
fetch { "type": $t, "parent": $parent };
"""

RELATION_TYPES = """
match relation $r; $r relates $role; not { $r sub! $p; $p relates $role; };
fetch { "relation": $r, "role": $role,
        "parent": [ match $r sub! $parent; fetch { "label": $parent }; ] };
"""

ORGANISATIONS = """
match $o isa organisation; $o isa! $t;
fetch { "organisation": { $o.* }, "type": $t,
        "people": ( match employment (employee: $p, employer: $o); return count($p); ) };
"""

ORGANISATION_RELATIONS = """
match $o isa organisation; $r links ($o); $r isa! $rt;
      not { $r isa employment; }; not { $r isa raci-assignment; }; not { $r isa deal-event; };
fetch { "type": $rt, "attributes": { $r.* },
        "players": [ match $r links ($role: $p); $p isa! $pt;
                     fetch { "role": $role, "type": $pt, "player": { $p.* } }; ] };
"""


def group_members_query(parent_code):
    """Companies the parent controls directly or indirectly: reachability only (owners_above), so it
    is safe even if the ownership data had a cycle. Used to group nodes, never for percentages."""
    return f"""
match $parent isa company, has code {string(parent_code)}; $c isa company, has code $code;
      let $above in owners_above($c); $above is $parent;
select $code; distinct;
"""


def _role(role_doc):
    """"mandate:adviser" -> "adviser"."""
    return role_doc["label"].split(":", 1)[1]


def _player(doc):
    p = doc["player"]
    return {"role": _role(doc["role"]), "code": p.get("code"), "name": p.get("name"),
            "type": _label(doc["type"]), "kind": doc["type"]["kind"]}


def _attributes(doc):
    """Relation attributes for display: decimals as exact text, everything else as given."""
    out = {}
    for k, v in (doc or {}).items():
        out[k] = money(v) if k in ("deal-value",) else (format(to_decimal(v), "f") if k == "stake" else v)
    return out


def _relation_key(rtype, attributes, players):
    """A stable identifier for a relation (TypeDB's internal IDs can't be fetched): its code if it has
    one, otherwise its type, players and attributes, so two events by the same person stay distinct."""
    if attributes.get("code"):
        return attributes["code"]
    parts = sorted(f"{p['role']}={p['code']}" for p in players) + sorted(f"{k}={v}" for k, v in attributes.items())
    return rtype + ":" + ",".join(parts)


def type_hierarchy(tx):
    """{"parties": {type: parent}, "relations": {type: {"parent", "roles"}}} straight from the schema."""
    parties = {d["type"]["label"]: d["parent"]["label"] for d in fetch(tx, PARTY_TYPES)}
    relations = {}
    for d in fetch(tx, RELATION_TYPES):
        r = relations.setdefault(d["relation"]["label"], {"parent": None, "roles": []})
        r["parent"] = d["parent"][0]["label"]["label"] if d["parent"] else None
        r["roles"].append(_role(d["role"]))
    for r in relations.values():
        r["roles"].sort()
    return {"parties": parties, "relations": relations}


def organisations(tx):
    return [{**_org(d["organisation"]), "type": _label(d["type"]), "people": d["people"] or 0}
            for d in fetch(tx, ORGANISATIONS)]


def organisation_relations(tx):
    """Every relation that has an organisation among its players (except employment and RACI, which
    belong to people), each once, with all its players and their roles."""
    out = {}
    for d in fetch(tx, ORGANISATION_RELATIONS):
        players = sorted((_player(p) for p in d["players"]), key=lambda p: (p["role"], p["code"] or ""))
        attributes = _attributes(d["attributes"])
        rtype = _label(d["type"])
        key = _relation_key(rtype, attributes, players)
        out[key] = {"key": key, "type": rtype, "attributes": attributes, "players": players}
    return sorted(out.values(), key=lambda r: (r["type"], r["key"]))


def group_members(tx, parent_code):
    return sorted(r.get("code").as_attribute().get_value() for r in rows(tx, group_members_query(parent_code)))


def party_relations_query(code):
    return f"""
match $x isa party, has code {string(code)}; $r links ($myrole: $x); $r isa! $rt;
      not {{ $r isa raci-assignment; }}; not {{ $r isa deal-event; }};
fetch {{ "type": $rt, "my role": $myrole, "attributes": {{ $r.* }},
        "others": [ match $r links ($role: $p); not {{ $p is $x; }}; $p isa! $pt;
                    fetch {{ "role": $role, "type": $pt, "player": {{ $p.* }} }}; ] }};
"""


def party_query(code):
    return f"""
match $x isa party, has code {string(code)}; $x isa! $t;
fetch {{ "party": {{ $x.* }}, "type": $t }};
"""


def people_query(org_code):
    return f"""
match $o isa organisation, has code {string(org_code)}; employment (employee: $p, employer: $o), has job-title $title;
fetch {{ "person": {{ $p.* }}, "title": $title,
        "raci": [ match $a isa raci-assignment (holder: $p, item: $i); $a isa! $letter;
                  fetch {{ "letter": $letter, "item": {{ "code": $i.code, "name": $i.name }},
                          "via": [ match $a links (via: $m); fetch {{ "code": $m.code, "type": $m.mandate-type }}; ] }}; ] }};
"""


def raci_of_query(code):
    return f"""
match $h isa party, has code {string(code)}; $a isa raci-assignment (holder: $h, item: $i); $a isa! $letter;
      $i has due-date $due; $i isa! $it;
sort $due asc;
fetch {{ "letter": $letter, "item": {{ "code": $i.code, "name": $i.name, "status": $i.status, "due": $due }},
        "item type": $it,
        "via": [ match $a links (via: $m); fetch {{ "code": $m.code, "type": $m.mandate-type }}; ] }};
"""


def _raci_entry(d):
    return {"letter": RACI_LETTERS[_label(d["letter"])], "item": d["item"],
            "via": d["via"][0]["code"] if d["via"] else None}


def party(tx, code):
    """One party with its exact type, every relation it plays a role in (role names kept, all other
    players listed), its people (organisations) and its RACI letters. None if no such party."""
    found = fetch(tx, party_query(code))
    if not found:
        return None
    p, ptype = found[0]["party"], _label(found[0]["type"])
    relations = {}
    for d in fetch(tx, party_relations_query(code)):
        others = sorted((_player(o) for o in d["others"]), key=lambda o: (o["role"], o["code"] or ""))
        attributes = _attributes(d["attributes"])
        rtype = _label(d["type"])
        mine = _role(d["my role"])
        key = _relation_key(rtype, attributes, others + [{"role": mine, "code": code}])
        relations[key] = {"key": key, "type": rtype, "role": mine, "attributes": attributes, "others": others}
    out = {"code": code, "name": p.get("name"), "type": ptype, "description": p.get("description"),
           "jurisdiction": p.get("jurisdiction"), "sector": p.get("sector"),
           "relations": sorted(relations.values(), key=lambda r: (r["type"], r["key"])),
           "raci": [{**_raci_entry(d), "item_type": _label(d["item type"])} for d in fetch(tx, raci_of_query(code))],
           "people": []}
    if ptype != "person":
        out["people"] = sorted(({"code": d["person"]["code"], "name": d["person"].get("name"), "title": d["title"],
                                 "raci": sorted((_raci_entry(r) for r in d["raci"]), key=lambda r: ("ARCI".index(r["letter"]), r["item"]["code"]))}
                                for d in fetch(tx, people_query(code))), key=lambda p: p["code"])
    return out


def ownership_edges_query(parent_code):
    """Q2.9: every direct holding into a company in the parent's group, incl. outside shareholders."""
    return f"""
match $p isa company, has code {string(parent_code)}; let $c, $e in group_members($p);
      $o isa ownership (owner: $owner, owned: $c), has stake $s;
select $o, $owner, $c, $s; distinct;
fetch {{ "owner": {{ "code": $owner.code, "name": $owner.name }}, "owned": {{ "code": $c.code, "name": $c.name }}, "stake": $s,
        "description": $o.description }};
"""


def ownership_edges(tx, parent_code):
    """Direct stakes [{"owner", "owned", "stake" (Decimal)}]. De-duplicated in Python as well."""
    seen = {}
    for d in fetch(tx, ownership_edges_query(parent_code)):
        seen[(d["owner"]["code"], d["owned"]["code"])] = {"owner": d["owner"], "owned": d["owned"],
                                                          "stake": to_decimal(d["stake"]), "description": d.get("description")}
    return sorted(seen.values(), key=lambda e: (e["owned"]["code"], e["owner"]["code"]))


def names_query(codes):
    # Codes are validated identifiers (capitals, digits, hyphens), so they are safe in a pattern.
    pattern = "^(" + "|".join(sorted(set(codes))) + ")$"
    return f"""
match $x has code $c, has name $n; $c like {string(pattern)};
fetch {{ "code": $c, "name": $n }};
"""


def names(tx, codes):
    """{code: name} for any coded things (parties, deal items, financing...)."""
    if not codes:
        return {}
    if not all(CODE_TEXT.match(c) for c in codes):
        raise ValueError("not a valid code")
    return {d["code"]: d["name"] for d in fetch(tx, names_query(codes))}


# ---- Impact Explorer (Q3.4, Q3.5) -------------------------------------------------------------
# impact_edges (recursive), all_items and all_deadline_limits above supply the projection itself;
# these add context: every dependency (to inspect any item), which kinds are "hard" (from the
# schema), and who holds RACI letters on everything the recursion reaches.

HARD_DEPENDENCY_KINDS = """
match $k sub hard-dependency;
fetch { "kind": $k };
"""

ALL_DEPENDENCIES = """
match $d isa dependency (prerequisite: $a, dependent: $b); $d isa! $k;
fetch { "from": $a.code, "to": $b.code, "kind": $k, "lag": $d.lag-days };
"""


def hard_dependency_kinds(tx):
    """The dependency types that move dates: hard-dependency and its subtypes, from the schema."""
    return sorted(d["kind"]["label"] for d in fetch(tx, HARD_DEPENDENCY_KINDS))


def all_dependencies(tx):
    """Every dependency, hard or soft: [{"from", "to", "kind", "lag"}]."""
    return sorted(({"from": d["from"], "to": d["to"], "kind": _label(d["kind"]), "lag": d.get("lag")}
                   for d in fetch(tx, ALL_DEPENDENCIES)), key=lambda e: (e["from"], e["to"]))


def impact_raci_query(start_code):
    """Q3.5: RACI on the start item, on everything downstream of it (recursive), and on the
    deadlines that limit those items."""
    return f"""
match $start isa deal-item, has code {string(start_code)};
      {{ $x is $start; }} or {{ let $x in downstream($start); }} or
      {{ let $y in downstream($start); deadline-limit (limited-item: $y, deadline: $x); }};
      $r isa raci-assignment (holder: $h, item: $x); $r isa! $letter; $h isa! $htype;
fetch {{ "item": $x.code, "letter": $letter, "holder": {{ "code": $h.code, "name": $h.name }}, "holder type": $htype,
        "employer": [ match employment (employee: $h, employer: $o), has job-title $t; fetch {{ "code": $o.code, "name": $o.name, "title": $t }}; ],
        "via": [ match $r links (via: $m); fetch {{ "code": $m.code, "type": $m.mandate-type }}; ] }};
"""


def impact_raci(tx, start_code):
    """{item code: [{"letter", "code", "name", "type", "employer", "title", "via"}]}, de-duplicated
    in Python (`or` branches can repeat a row, and distinct doesn't remove repeats after `or`)."""
    seen = {}
    for d in fetch(tx, impact_raci_query(start_code)):
        emp = d["employer"][0] if d["employer"] else None
        entry = {"letter": RACI_LETTERS[_label(d["letter"])], "code": d["holder"]["code"], "name": d["holder"]["name"],
                 "type": _label(d["holder type"]), "employer": emp and {"code": emp["code"], "name": emp["name"]},
                 "title": emp and emp["title"], "via": d["via"][0] if d["via"] else None}
        seen[(d["item"], entry["letter"], entry["code"])] = entry
    out = {}
    for (item, _, _), entry in sorted(seen.items(), key=lambda kv: (kv[0][0], "ARCI".index(kv[0][1]), kv[0][2])):
        out.setdefault(item, []).append(entry)
    return out


# ---- Shared item detail (Gantt, and later Workstreams, Parties and Impact) --------------------

def item_detail_query(code):
    """Everything about one work item in one fetch: its own attributes, workstream, RACI (with the
    mandate each adviser acts under), dependencies both ways, deadline limits both ways, who is bound
    and whose consent it needs, its events and the risks that threaten it."""
    return f"""
match $x isa deal-item, has code {string(code)}; $x isa! $t;
fetch {{ "item": {{ $x.* }}, "type": $t,
  "workstream": [ match work-breakdown (whole: $w, part: $x); fetch {{ "code": $w.code, "name": $w.name }}; ],
  "raci": [ match $r isa raci-assignment (holder: $h, item: $x); $r isa! $l; $h isa! $ht;
            fetch {{ "letter": $l, "holder": {{ "code": $h.code, "name": $h.name }}, "holder type": $ht,
                     "employer": [ match employment (employee: $h, employer: $o); fetch {{ "name": $o.name }}; ],
                     "via": [ match $r links (via: $m); fetch {{ "code": $m.code }}; ] }}; ],
  "depends on": [ match $d isa dependency (prerequisite: $a, dependent: $x); $d isa! $k;
                  fetch {{ "code": $a.code, "name": $a.name, "kind": $k, "lag": $d.lag-days }}; ],
  "needed by": [ match $d isa dependency (prerequisite: $x, dependent: $b); $d isa! $k;
                 fetch {{ "code": $b.code, "name": $b.name, "kind": $k, "lag": $d.lag-days }}; ],
  "limited by": [ match deadline-limit (limited-item: $x, deadline: $dl);
                  fetch {{ "code": $dl.code, "name": $dl.name, "due": $dl.due-date }}; ],
  "limits": [ match deadline-limit (limited-item: $y, deadline: $x); fetch {{ "code": $y.code, "name": $y.name }}; ],
  "bound": [ match bound-by (obligor: $o, obligation: $x); fetch {{ "code": $o.code, "name": $o.name }}; ],
  "consent from": [ match consent-right (obligation: $x, grantor: $g); fetch {{ "code": $g.code, "name": $g.name }}; ],
  "events": [ match $e isa deal-event (subject: $x, actor: $who), has occurred-at $at; $e isa! $et;
              fetch {{ "code": $e.code, "when": $at, "type": $et, "summary": $e.summary,
                       "actor": {{ "code": $who.code, "name": $who.name }} }}; ],
  "discussed at": [ match $m isa meeting (agenda-item: $x, actor: $who, subject: $s), has occurred-at $at;
                    fetch {{ "code": $m.code, "when": $at, "summary": $m.summary, "actor": {{ "code": $who.code, "name": $who.name }},
                             "subject": {{ "code": $s.code, "name": $s.name }} }}; ],
  "risks": [ match risk-exposure (risk: $k, exposed: $x); $k isa risk;
             fetch {{ "code": $k.code, "name": $k.name, "severity": $k.severity, "likelihood": $k.likelihood,
                      "status": $k.risk-status }}; ]
}};
"""


def item_detail(tx, code):
    """One item's detail (see item_detail_query), or None if there is no item with that code."""
    found = fetch(tx, item_detail_query(code))
    if not found:
        return None
    d = found[0]
    x = d["item"]

    def deps(rows_):
        return sorted(({"code": r["code"], "name": r["name"], "kind": _label(r["kind"]), "lag": r.get("lag")}
                       for r in rows_), key=lambda r: r["code"])

    raci = {}
    for r in d["raci"]:          # the same holder can't hold one letter twice, but sort for a stable answer
        entry = {"code": r["holder"]["code"], "name": r["holder"]["name"], "type": _label(r["holder type"]),
                 "employer": r["employer"][0]["name"] if r["employer"] else None,
                 "via": r["via"][0]["code"] if r["via"] else None}
        raci.setdefault(RACI_LETTERS[_label(r["letter"])], []).append(entry)
    events = sorted(({"code": e["code"], "when": _when(e["when"]), "type": _label(e["type"]), "summary": e["summary"],
                      "actor": e["actor"]} for e in d["events"]), key=lambda e: (e["when"], e["code"]), reverse=True)
    return {
        "code": x["code"], "name": x.get("name"), "type": _label(d["type"]), "status": x.get("status"),
        "criticality": x.get("criticality"), "description": x.get("description"),
        "planned_start": x.get("planned-start"), "actual_start": x.get("actual-start-date"),
        "due": x.get("due-date"), "baseline_due": x.get("baseline-due-date"), "completed": x.get("completed-date"),
        "progress": x.get("progress"), "legal_basis": x.get("legal-basis"), "consequence": x.get("consequence"),
        "workstream": d["workstream"][0] if d["workstream"] else None,
        "raci": {letter: sorted(holders, key=lambda h: h["code"]) for letter, holders in raci.items()},
        "depends_on": deps(d["depends on"]), "needed_by": deps(d["needed by"]),
        "limited_by": sorted(d["limited by"], key=lambda r: r["code"]),
        "limits": sorted(d["limits"], key=lambda r: r["code"]),
        "bound": sorted(d["bound"], key=lambda r: r["code"]),
        "consent_from": sorted(d["consent from"], key=lambda r: r["code"]),
        "events": events, "risks": sorted(d["risks"], key=lambda r: r["code"]),
        "discussed_at": sorted(({"code": m["code"], "when": _when(m["when"]), "summary": m["summary"], "actor": m["actor"],
                                 "subject": m["subject"]} for m in d["discussed at"]), key=lambda m: m["when"], reverse=True),
    }


# ---- Workstreams (Phase 6C) ----------------------------------------------------------------

def _in_workstream(code):
    """Restrict a pattern binding $ws to one workstream, or leave it open for all of them."""
    return f"$ws has code {string(code)};" if code else ""


_EVENT_FETCH = """
fetch { "workstream": $ws.code, "event": { $e.* }, "type": $et,
  "actor": { "code": $who.code, "name": $who.name }, "actor type": $wt,
  "employer": [ match employment (employee: $who, employer: $o); fetch { "name": $o.name }; ],
  "item": { "code": $i.code, "name": $i.name },
  "recipients": [ match $e links (recipient: $r); fetch { "code": $r.code, "name": $r.name }; ],
  "attendees": [ match $e links (attendee: $r); fetch { "code": $r.code, "name": $r.name }; ],
  "agenda": [ match $e links (agenda-item: $a); fetch { "code": $a.code, "name": $a.name }; ],
  "replies to": [ match reply (answer: $e, original: $q); fetch { "code": $q.code }; ],
  "answered by": [ match reply (original: $e, answer: $a); fetch { "code": $a.code, "when": $a.occurred-at }; ] };
"""


def workstream_events_query(code=None):
    """Every recorded event on a workstream's items, of every kind (polymorphic deal-event), with who
    it was sent to, who attended, what a meeting discussed (agenda-item), and the request/response chain
    (reply relates two relations)."""
    return f"""
match $ws isa workstream; {_in_workstream(code)} work-breakdown (whole: $ws, part: $i);
      $e isa deal-event (subject: $i, actor: $who), has occurred-at $t; $e isa! $et; $who isa! $wt;
{_EVENT_FETCH}"""


def workstream_oversight_query(code=None):
    """Events recorded against a workstream itself (steering meetings: oversight). A separate query rather
    than `{ $i is $ws; } or { work-breakdown ... }`, which hits TypeDB 3.14.1's planner bug QPL1."""
    return f"""
match $ws isa workstream; {_in_workstream(code)} $i is $ws;
      $e isa deal-event (subject: $i, actor: $who), has occurred-at $t; $e isa! $et; $who isa! $wt;
{_EVENT_FETCH}"""


def workstream_events(tx, code=None):
    """[{workstream, code, when, type, kind, summary, actor, item, recipients, attendees, replies_to,
    answered_by, response_due, previous_status, new_status, automated}], newest first."""
    out = []
    for d in fetch(tx, workstream_events_query(code)) + fetch(tx, workstream_oversight_query(code)):
        e = d["event"]
        out.append({
            "workstream": d["workstream"], "code": e["code"], "when": _when(e["occurred-at"]), "type": _label(d["type"]),
            "kind": e.get("event-kind"), "summary": e.get("summary"),
            "actor": {**d["actor"], "type": _label(d["actor type"]),
                      "employer": d["employer"][0]["name"] if d["employer"] else None},
            "item": d["item"],
            "recipients": sorted(d["recipients"], key=lambda r: r["code"]),
            "attendees": sorted(d["attendees"], key=lambda r: r["code"]),
            "agenda": sorted(d["agenda"], key=lambda r: r["code"]),
            "replies_to": sorted(r["code"] for r in d["replies to"]),
            "answered_by": sorted(({"code": a["code"], "when": _when(a["when"])} for a in d["answered by"]),
                                  key=lambda a: a["when"]),
            "response_due": e.get("response-due-date"), "automated": e.get("automated"),
            "previous_status": e.get("previous-status"), "new_status": e.get("new-status"),
        })
    return sorted(out, key=lambda e: (e["when"], e["code"]), reverse=True)


WORKSTREAM_RISKS = """
match $k isa risk; risk-exposure (risk: $k, exposed: $x); work-breakdown (whole: $ws, part: $x);
fetch { "workstream": $ws.code, "risk": { $k.* }, "item": $x.code,
  "owner": [ match risk-ownership (owned-risk: $k, risk-owner: $o); fetch { "code": $o.code, "name": $o.name }; ] };
"""


def workstream_risks(tx):
    """{workstream: [{code, name, description, likelihood, severity, status, mitigation, owner, items}]}."""
    found = {}
    for d in fetch(tx, WORKSTREAM_RISKS):
        k = d["risk"]
        risk = found.setdefault(d["workstream"], {}).setdefault(k["code"], {
            "code": k["code"], "name": k.get("name"), "description": k.get("description"),
            "likelihood": k.get("likelihood"), "severity": k.get("severity"), "status": k.get("risk-status"),
            "mitigation": k.get("mitigation"), "owner": d["owner"][0] if d["owner"] else None, "items": []})
        risk["items"] = sorted({*risk["items"], d["item"]})
    return {w: sorted(r.values(), key=lambda r: r["code"]) for w, r in found.items()}


def deadlines_in_reach_query(code=None):
    """Contractual deadlines a workstream's open items can still affect: a deadline limiting an open
    item, or anything downstream of it (the recursive downstream function)."""
    return f"""
match $ws isa workstream, has code $w; {_in_workstream(code)} work-breakdown (whole: $ws, part: $i);
      not {{ $i has status "complete"; }};
      {{ $x is $i; }} or {{ let $x in downstream($i); }};
      deadline-limit (limited-item: $x, deadline: $dl);
fetch {{ "workstream": $w, "deadline": $dl.code, "via": $x.code, "from": $i.code }};
"""


def deadlines_in_reach(tx, code=None):
    """{workstream: {deadline code: {"via": [limited items], "from": [open items in the workstream]}}},
    de-duplicated in Python (`or` branches can repeat rows)."""
    out = {}
    for d in fetch(tx, deadlines_in_reach_query(code)):
        entry = out.setdefault(d["workstream"], {}).setdefault(d["deadline"], {"via": set(), "from": set()})
        entry["via"].add(d["via"])
        entry["from"].add(d["from"])
    return {w: {dl: {k: sorted(v) for k, v in e.items()} for dl, e in dls.items()} for w, dls in out.items()}


# Written with the RACI pattern inside each branch: `{ $i is $ws; } or { ... }` followed by the RACI
# pattern makes TypeDB 3.14.1's query planner fail (QPL1; see docs/queries.md).
WORKSTREAM_HOLDERS = """
match $ws isa workstream, has code $w;
      { raci-assignment (holder: $h, item: $ws); } or
      { work-breakdown (whole: $ws, part: $i); raci-assignment (holder: $h, item: $i); };
      $h isa! $ht;
fetch { "workstream": $w, "holder": { "code": $h.code, "name": $h.name }, "type": $ht,
  "employer": [ match employment (employee: $h, employer: $o); fetch { "code": $o.code, "name": $o.name }; ] };
"""


def workstream_organisations(tx):
    """{workstream: [organisations involved]}: every organisation holding a RACI letter on the workstream
    or its items, directly or through one of its people."""
    out = {}
    for d in fetch(tx, WORKSTREAM_HOLDERS):
        orgs = out.setdefault(d["workstream"], {})
        if d["employer"]:
            orgs[d["employer"][0]["code"]] = d["employer"][0]["name"]
        elif _label(d["type"]) != "person":
            orgs[d["holder"]["code"]] = d["holder"]["name"]
    return {w: [{"code": c, "name": n} for c, n in sorted(o.items(), key=lambda x: x[1])] for w, o in out.items()}


def open_prerequisites_query(code):
    """For each item in a workstream, its prerequisites at any depth (the recursive upstream function)
    that are not yet complete."""
    return f"""
match $ws isa workstream, has code {string(code)}; work-breakdown (whole: $ws, part: $i);
      let $u in upstream($i); not {{ $u has status "complete"; }};
fetch {{ "item": $i.code, "open prerequisite": $u.code }};
"""


def open_prerequisites(tx, code):
    """{item code: [open prerequisite codes]}, de-duplicated."""
    out = {}
    for d in fetch(tx, open_prerequisites_query(code)):
        out.setdefault(d["item"], set()).add(d["open prerequisite"])
    return {k: sorted(v) for k, v in out.items()}


def workstream_obligations_query(code):
    """A workstream's obligations (conditions, covenants and deadlines: the abstract parent type), with who
    is bound, whose consent is needed and what each deadline limits."""
    return f"""
match $ws isa workstream, has code {string(code)}; work-breakdown (whole: $ws, part: $o); $o isa obligation; $o isa! $t;
fetch {{ "obligation": {{ $o.* }}, "type": $t,
  "bound": [ match bound-by (obligor: $b, obligation: $o); fetch {{ "code": $b.code, "name": $b.name }}; ],
  "consent from": [ match consent-right (obligation: $o, grantor: $g); fetch {{ "code": $g.code, "name": $g.name }}; ],
  "limits": [ match deadline-limit (deadline: $o, limited-item: $x); fetch {{ "code": $x.code, "name": $x.name }}; ] }};
"""


def workstream_obligations(tx, code):
    out = []
    for d in fetch(tx, workstream_obligations_query(code)):
        o = d["obligation"]
        out.append({"code": o["code"], "name": o.get("name"), "type": _label(d["type"]), "status": o.get("status"),
                    "due": o.get("due-date"), "legal_basis": o.get("legal-basis"), "consequence": o.get("consequence"),
                    "bound": sorted(d["bound"], key=lambda r: r["code"]),
                    "consent_from": sorted(d["consent from"], key=lambda r: r["code"]),
                    "limits": sorted(d["limits"], key=lambda r: r["code"])})
    return sorted(out, key=lambda o: (o["due"] or "", o["code"]))


# ---- Parties and engagement (Phase 6D) --------------------------------------------------------

PEOPLE = """
match $p isa person;
fetch { "code": $p.code, "name": $p.name,
  "employment": [ match employment (employee: $p, employer: $o), has job-title $t; $o isa! $ot;
                  fetch { "code": $o.code, "name": $o.name, "type": $ot, "title": $t }; ] };
"""

# Contacts are read here only, and masked in Python before any response is built (decision D8):
# "Show TypeQL" shows this query, never the values.
CONTACTS = """
match $p isa person, has code $c;
fetch { "code": $c, "email": $p.email, "phone": $p.phone };
"""

ORGANISATION_PARTIES = """
match $o isa organisation; $o isa! $t;
fetch { "code": $o.code, "name": $o.name, "type": $t, "jurisdiction": $o.jurisdiction,
  "authority": [ match $o isa authority; fetch { "code": $o.code }; ] };
"""

ALL_RACI = """
match $r isa raci-assignment (holder: $h, item: $i); $r isa! $l;
fetch { "holder": $h.code, "item": $i.code, "letter": $l,
  "via": [ match $r links (via: $m); fetch { "code": $m.code }; ] };
"""

ENGAGEMENT_TYPES = """
match $t sub engagement;
fetch { "type": $t };
"""

OUTREACH_TYPES = """
match $t sub outreach;
fetch { "type": $t };
"""

AUTHORITY_REVIEWS = """
match $a isa authority; authority-review (reviewer: $a, subject: $c);
fetch { "authority": $a.code, "clearance": $c.code };
"""


def people(tx):
    out = []
    for d in fetch(tx, PEOPLE):
        e = d["employment"][0] if d["employment"] else None
        out.append({"code": d["code"], "name": d["name"],
                    "employer": {"code": e["code"], "name": e["name"], "type": _label(e["type"])} if e else None,
                    "title": e["title"] if e else None})
    return sorted(out, key=lambda p: p["name"])


def contacts(tx):
    """{person code: (email, phone)}: full values, for masking on the server only."""
    return {d["code"]: (d.get("email"), d.get("phone")) for d in fetch(tx, CONTACTS)}


def organisation_parties(tx):
    return sorted(({"code": d["code"], "name": d["name"], "type": _label(d["type"]), "jurisdiction": d.get("jurisdiction"),
                    "authority": bool(d["authority"])} for d in fetch(tx, ORGANISATION_PARTIES)), key=lambda o: o["name"])


def all_raci(tx):
    return sorted(({"holder": d["holder"], "item": d["item"], "letter": RACI_LETTERS[_label(d["letter"])],
                    "via": d["via"][0]["code"] if d["via"] else None} for d in fetch(tx, ALL_RACI)),
                  key=lambda r: (r["holder"], r["item"], r["letter"]))


def subtypes(tx, query):
    return {_label(d["type"]) for d in fetch(tx, query)}


def authority_reviews(tx):
    return sorted(({"authority": d["authority"], "clearance": d["clearance"]} for d in fetch(tx, AUTHORITY_REVIEWS)),
                  key=lambda r: (r["authority"], r["clearance"]))


# ---- Connection paths (Phase 6E) ---------------------------------------------------------------

CODE_PATTERN = re.compile(r"^[A-Z0-9-]+$")


def _work_breakdown(include):
    return "" if include else "not { $r isa work-breakdown; };"


def neighbours_query(code, workstreams=False):
    """Everything one relation away from `code` that is an entity (the kind statement `entity $mt;`), with
    the relation and its exact type. Role-less matching ($r links ($a, $m)) finds every kind of relation."""
    return f"""match $a has code {string(code)}; $r links ($a, $m); $m isa $mt, has code $mc; entity $mt; $r isa! $t;
      not {{ $m is $a; }}; {_work_breakdown(workstreams)}
select $r, $t, $mc; distinct;"""


def middle_query(starts, ends, workstreams=False, row_cap=2000):
    """For three-step paths, met in the middle: the relations joining a neighbour of one end ($m) to a
    neighbour of the other ($n). Activity records (deal-event and its subtypes) are not used for three steps."""
    for code in (*starts, *ends):
        if not CODE_PATTERN.match(code):
            raise ValueError(f"not a code: {code}")
    return f"""match $m has code $mc; $mc like "^({'|'.join(sorted(starts))})$";
      $r links ($m, $n); $n has code $nc; $nc like "^({'|'.join(sorted(ends))})$"; $r isa! $t;
      not {{ $r isa deal-event; }}; {_work_breakdown(workstreams)}
select $r, $t, $mc, $nc; distinct; limit {int(row_cap)};"""


def _iid_patterns(iids):
    return " or ".join(f"{{ $r iid {i}; }}" for i in iids)


def relation_players_query(iids):
    """Every player of the given relations, with the role it plays and its code."""
    return f"match {_iid_patterns(iids)}; $r links ($role: $p); $p has code $c;\nselect $r, $role, $c;"


def relation_attributes_query(iids):
    return f"match {_iid_patterns(iids)}; $r has $attr;\nselect $r, $attr;"


ENTITY_LINKS = """
match $x isa $t, has code $c; entity $t; $r links ($x);
reduce $links = count groupby $c;
"""

CONNECTABLES = """
match $x isa! $t, has code $c, has name $n; entity $t;
select $c, $n, $t;
"""


def connectables(tx):
    """{code: {"code", "name", "type"}} for every entity that has a code: the things a path can join."""
    out = {}
    for r in rows(tx, CONNECTABLES):
        code = r.get("c").as_attribute().get_value()
        out[code] = {"code": code, "name": r.get("n").as_attribute().get_value(), "type": r.get("t").get_label()}
    return out


def entity_links(tx):
    return {r.get("c").as_attribute().get_value(): r.get("links").get() for r in rows(tx, ENTITY_LINKS)}
