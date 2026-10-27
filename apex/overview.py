"""The Deal Overview's answers, composed from the TypeQL in apex/queries.py.

The only things worked out here, rather than in TypeQL, are counting, the headline status rule
and date arithmetic (TypeQL can't subtract dates). Every input comes from TypeDB in the same
READ transaction; nothing is hard-coded about the deal itself.
"""

from datetime import date

from apex import progress, queries

OPEN_PROBLEM = ("at-risk", "delayed")


def _days(later, earlier):
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def deadline_position(deadline, as_of):
    """Add days_remaining (from the as-of date) and headroom_days (the deadline's date minus the
    planned date of the latest item it limits) to a deadline from queries.deadlines()."""
    planned = [i["due"] for i in deadline["limits"] if i.get("due")]
    return {**deadline, "days_remaining": _days(deadline["due"], as_of),
            "headroom_days": _days(deadline["due"], max(planned)) if planned else None}


def deal_status(conditions):
    """Headline status from the conditions' recorded statuses:
    all complete -> "conditions-satisfied"; any at risk or delayed -> "at-risk"; else "on-track"."""
    satisfied = [c for c in conditions if c["status"] == "complete"]
    problems = [c for c in conditions if c["status"] in OPEN_PROBLEM]
    if conditions and len(satisfied) == len(conditions):
        state = "conditions-satisfied"
    elif problems:
        state = "at-risk"
    else:
        state = "on-track"
    outstanding = len(conditions) - len(satisfied)
    return {"state": state, "conditions_total": len(conditions), "conditions_satisfied": len(satisfied),
            "headline": f"{outstanding} of {len(conditions)} conditions outstanding" if outstanding
            else "All conditions satisfied",
            "at_risk": [{"code": c["code"], "name": c["name"], "status": c["status"], "due": c["due"]} for c in problems]}


def summary(tx):
    deal = queries.deal(tx)
    conditions = queries.conditions(tx)
    derived = progress.summary(list(queries.all_items(tx).values()))
    financing = queries.committed_financing(tx)
    upcoming = [deadline_position(d, deal["as_of"]) for d in queries.deadlines(tx) if d["due"] >= deal["as_of"]]
    nxt = upcoming[0] if upcoming else None
    return {**deal, "status": deal_status(conditions),
            "metrics": {"progress_percent": derived["percent"], "progress_basis": derived["basis"],
                        "progress_items": (derived["activity_completion"] or {}).get("items", 0),
                        "milestones_achieved": derived["milestone_readiness"]["achieved"],
                        "milestones_total": derived["milestone_readiness"]["total"],
                        "committed_financing": financing,
                        "next_deadline": {k: nxt[k] for k in ("code", "name", "due", "days_remaining", "headroom_days")}
                        if nxt else None}}


def milestones(tx, criticalities=None):
    as_of = queries.deal(tx)["as_of"]
    return {"as_of": as_of, "items": queries.timeline(tx, criticalities),
            "deadlines": [deadline_position(d, as_of) for d in queries.deadlines(tx)]}


def checks(tx):
    details = queries.data_quality_details(tx)
    out = []
    for name, (title, explanation) in queries.CHECK_DESCRIPTIONS.items():
        out.append({"check": name, "title": title, "description": explanation, "items": details[name]})
    return {"passed": all(not c["items"] for c in out), "checks": out}


def ownership(tx, parent_code):
    """Effective ownership and group consents, guarded (raises queries.OwnershipDataError)."""
    stakes = queries.effective_ownership(tx, parent_code)
    consents = queries.group_consents(tx, parent_code)
    companies = sorted(({"code": c, "name": v["name"], "stake": format(v["stake"], "f"), "routes": v["routes"]}
                        for c, v in stakes.items()), key=lambda c: (-float(c["stake"]), c["code"]))
    return {"parent": parent_code, "companies": companies,
            "consents": [{**c, "stake": format(c["stake"], "f")} for c in consents]}
