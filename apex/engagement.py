"""Engagement risk (decision D9, docs/phase6-plan.md §5.3, §5.4 and §12): deterministic, explainable,
and computed in Python over records read from TypeDB. Thresholds come from config/engagement-policy.yaml.

Four things are kept apart, each with its own category:
- **Overdue response** (a fact): a request addressed to the party, with a reply-by date that has passed and
  no response recorded. Red, whatever else they have done ("response due soon" is amber).
- **Inactivity**: days without engagement on an assigned, active item, against the letter's thresholds.
  Only engagement on that item counts; activity elsewhere is shown as context and never improves a rating.
- **Deal risk**: inactivity on an item that matters now (schedule-critical or recorded as critical, and due
  soon or at risk): amber is escalated one step to red, and the reason says which condition applied.
- **Insufficient information**: no engagement recorded for the party at all. Shown as Unknown, never red.

Authorities (regulators, courts) are assessed against their statutory timetable and the requests sent to
them, never against silence. Delivery risk (an item at risk, delayed or overdue) is reported separately.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cache
from pathlib import Path

import yaml

from apex import impact

POLICY_FILE = Path(__file__).resolve().parent.parent / "config" / "engagement-policy.yaml"
LEVELS = ("red", "amber", "green")            # assessed levels, strongest first
# Within a level, a fact outranks an inference: an overdue response first, then deal risk, then inactivity.
CATEGORY_ORDER = ("overdue-response", "statutory-timetable", "deal-risk", "response-due", "inactivity")
LETTER_ORDER = "ARCI"
CONDITIONS = ("regulatory-clearance", "shareholder-approval", "court-sanction")
PROBLEMS = ("at-risk", "delayed")


@cache
def policy():
    return yaml.safe_load(POLICY_FILE.read_text())


def _d(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


# Schedule float lives with the projection it uses (apex/impact.py), so every screen shares one calculation.
schedule_float = impact.schedule_float


# ---- Assessments ------------------------------------------------------------------------------

@dataclass
class Assessment:
    kind: str                     # "assignment", "request" or "review"
    item: str
    level: str                    # red, amber, green, unknown, not-yet-active, not-assessed
    category: str                 # overdue-response, response-due, inactivity, deal-risk, insufficient-information, ...
    reason: str
    letter: str | None = None
    days: int | None = None
    due: str | None = None
    evidence: list = field(default_factory=list)   # [{"kind", "code", "label", "date"}]
    context: str | None = None

    def rank(self):
        lv = LEVELS.index(self.level) if self.level in LEVELS else len(LEVELS)
        cat = CATEGORY_ORDER.index(self.category) if self.category in CATEGORY_ORDER else len(CATEGORY_ORDER)
        lt = LETTER_ORDER.index(self.letter) if self.letter else len(LETTER_ORDER)
        return (lv, cat, lt, self.due or "9999")


def _request_assessment(e, as_of, items, p):
    due = _d(e["response_due"])
    days = (due - as_of).days
    sent = f"sent by {e['actor']['name']} on {_d(e['when']):%d %b}"
    evidence = [{"kind": "request", "code": e["code"], "label": e["summary"], "date": e["when"][:10]},
                {"kind": "item", "code": e["item"]["code"], "label": e["item"]["name"], "date": None}]
    if days < 0:
        return Assessment("request", e["item"]["code"], "red", "overdue-response",
                          f"Response to “{e['summary']}” ({sent}) was due {due:%d %b %Y}: overdue by "
                          f"{_plural(-days, 'day')}, and no response is recorded.", days=-days, due=str(due), evidence=evidence)
    if days <= p["response_warning_days"]:
        return Assessment("request", e["item"]["code"], "amber", "response-due",
                          f"Response to “{e['summary']}” ({sent}) is due {due:%d %b %Y}, in {_plural(days, 'day')}; "
                          "none recorded yet.", days=days, due=str(due), evidence=evidence)
    return Assessment("request", e["item"]["code"], "green", "response-awaited",
                      f"Response to “{e['summary']}” is due {due:%d %b %Y}, in {_plural(days, 'day')}.",
                      days=days, due=str(due), evidence=evidence)


def assess(party, as_of, items, raci, events, engagement_types, reviews, floats, employees=(), p=None, outreach_types=()):
    """Every assessment for one party, its overall level and a one-line explanation.

    party: {"code", "name", "kind": "person"|"organisation", "authority": bool, "employer": code|None}
    items: {code: {"name", "type", "status", "due", "planned_start", "actual_start", "criticality", "workstream"}}
    raci: [{"holder", "item", "letter"}]; events: workstream_events() rows; reviews: [{"authority", "clearance"}]
    floats: schedule_float(); employees: codes of the organisation's people (for an organisation).
    """
    p = p or policy()
    as_of = _d(as_of)
    code = party["code"]
    members = {code, *employees}
    addressed = {code} | ({party["employer"]} if party.get("employer") else set())
    mine = [r for r in raci if r["holder"] == code]
    assigned_items = {r["item"] for r in mine}

    def engaged_by(e):
        # The schema's engagement subtypes (response, meeting, delivery, decision) by the party, or a meeting
        # they attended; outreach (requests, notices) counts for its sender, never for its recipients, and an
        # automated notice never counts.
        if e.get("automated"):
            return False
        if e["type"] in engagement_types:
            return e["actor"]["code"] in members or bool(members & {a["code"] for a in e["attendees"]})
        return e["type"] in outreach_types and e["actor"]["code"] in members

    engagements = [e for e in events if engaged_by(e)]
    out = []

    # 1. Requests awaiting this party's response (always assessed: they are facts, not inferences).
    for e in events:
        if e["type"] != "request" or not e.get("response_due") or e["answered_by"]:
            continue
        to = {r["code"] for r in e["recipients"]}
        direct = code in to
        via_org = bool(addressed - {code} & to) and e["item"]["code"] in assigned_items
        if direct or via_org:
            out.append(_request_assessment(e, as_of, items, p))

    if party.get("authority"):
        # 2B. Authorities: their statutory timetable, never silence.
        for rv in reviews:
            if rv["authority"] != code:
                continue
            c = items[rv["clearance"]]
            ev = [{"kind": "item", "code": rv["clearance"], "label": c["name"], "date": c["due"]}]
            if c["status"] == "complete":
                out.append(Assessment("review", rv["clearance"], "not-assessed", "decision-recorded",
                                      f"Decision recorded on {c['name']}.", due=c["due"], evidence=ev))
                continue
            days = (_d(c["due"]) - as_of).days
            if days < 0:
                level, why = "red", f"the statutory decision date ({_d(c['due']):%d %b %Y}) passed {_plural(-days, 'day')} ago with no decision recorded"
            elif days <= p["statutory_warning_days"]:
                level, why = "amber", f"a decision is due within {_plural(days, 'day')} ({_d(c['due']):%d %b %Y}) and none is recorded yet"
            else:
                level, why = "green", f"its decision is due {_d(c['due']):%d %b %Y}, in {_plural(days, 'day')}: normal progress"
            out.append(Assessment("review", rv["clearance"], level, "statutory-timetable",
                                  f"Statutory timetable for {c['name']}: {why}.", days=days, due=c["due"], evidence=ev))
        for r in mine:
            it = items[r["item"]]
            out.append(Assessment("assignment", r["item"], "not-assessed", "authority", letter=r["letter"], due=it["due"],
                                  reason=f"{r['letter']} on {it['name']}: authorities are not assessed on inactivity.",
                                  evidence=[{"kind": "item", "code": r["item"], "label": it["name"], "date": it["due"]}]))
        return _combine(party, out, p, profile="Statutory timetable")

    # 2A. Deal participants: inactivity on each open, active assignment.
    no_engagement_at_all = not engagements
    last_any = max((e["when"] for e in engagements), default=None)
    for r in mine:
        it = items[r["item"]]
        letter = r["letter"]
        if it["status"] == "complete":
            continue
        ev_item = {"kind": "item", "code": r["item"], "label": it["name"], "date": it["due"]}
        if letter not in p["inactivity"]:
            out.append(Assessment("assignment", r["item"], "not-assessed", "requests-only", letter=letter, due=it["due"],
                                  reason=f"{letter} on {it['name']}: {'Consulted' if letter == 'C' else 'Informed'} people act when "
                                         "asked, so they are assessed on the requests sent to them, not on silence.",
                                  evidence=[ev_item]))
            continue
        if it["type"] == "deadline":
            out.append(Assessment("assignment", r["item"], "not-assessed", "deadline", letter=letter, due=it["due"],
                                  reason=f"{letter} on {it['name']}: a contractual deadline is a limit, not work, so it is "
                                         "not assessed for inactivity.", evidence=[ev_item]))
            continue
        start = it.get("actual_start") or it.get("planned_start")
        active = _d(start) if start else _d(it["due"]) - timedelta(days=p["milestone_active_window_days"])
        if active > as_of:
            out.append(Assessment("assignment", r["item"], "not-yet-active", "not-yet-active", letter=letter, due=it["due"],
                                  reason=f"{letter} on {it['name']}: not active until {active:%d %b %Y}, so not assessed yet.",
                                  evidence=[ev_item]))
            continue
        if no_engagement_at_all:
            out.append(Assessment("assignment", r["item"], "unknown", "insufficient-information", letter=letter, due=it["due"],
                                  reason=f"{letter} on {it['name']}: no engagement by {party['name']} is recorded anywhere on the "
                                         "deal, so there is not enough information to assess it.", evidence=[ev_item]))
            continue
        if it["type"] == "workstream":
            # Oversight: a steering meeting on the workstream itself, or engagement on any of its items.
            relevant = [e for e in engagements if e["item"]["code"] == r["item"]
                        or items.get(e["item"]["code"], {}).get("workstream") == r["item"]]
            scope = f"{it['name']} or any of its items"
        else:
            # Activity level: an event on this item, or a meeting that had it on its agenda. Attending a
            # workstream meeting that did not discuss it is not engagement on it.
            relevant = [e for e in engagements if e["item"]["code"] == r["item"]
                        or r["item"] in {a["code"] for a in e.get("agenda", [])}]
            scope = "this item"
        last = max(relevant, key=lambda e: (e["when"], e["code"]), default=None)
        if last and _d(last["when"]) >= active:
            days = (as_of - _d(last["when"])).days
            discussed = last["item"]["code"] != r["item"] and r["item"] in {a["code"] for a in last.get("agenda", [])}
            how = f"discussed at “{last['summary']}”" if discussed else last["summary"]
            basis = f"last engaged on {scope} {_plural(days, 'day')} ago ({how}, {_d(last['when']):%d %b})"
            evidence = [ev_item, {"kind": "event", "code": last["code"], "label": last["summary"], "date": last["when"][:10]}]
        else:
            days = (as_of - active).days
            basis = f"no engagement on {scope} since it became active on {active:%d %b %Y} ({_plural(days, 'day')})"
            evidence = [ev_item]
        th = p["inactivity"][letter]
        level = "red" if days >= th["red_from"] else "amber" if days >= th["amber_from"] else "green"
        threshold = f"threshold for {letter}: amber from {th['amber_from']} days, red from {th['red_from']}"
        category = "inactivity" if level != "green" else "engaged"
        reason = f"{letter} on {it['name']}: {basis}; {threshold}."
        # Escalation: inactivity on an item that matters now (amber to red only).
        fl = floats.get(r["item"], {})
        critical = []
        if fl.get("float") is not None and fl["float"] <= p["critical_float_days"]:
            critical.append(f"schedule-critical (float {_plural(fl['float'], 'day')} to {items[fl['deadline']]['name']})")
        if it.get("criticality") == "critical":
            critical.append("recorded as critical")
        urgent = []
        due_in = (_d(it["due"]) - as_of).days
        if 0 <= due_in <= p["urgent_window_days"]:
            urgent.append(f"due in {_plural(due_in, 'day')}")
        if it["status"] in PROBLEMS:
            urgent.append(f"recorded as {it['status'].replace('-', ' ')}")
        if level == "amber" and critical and urgent:
            level, category = "red", "deal-risk"
            reason += f" Escalated to red: the item is {' and '.join(critical)}, and {' and '.join(urgent)}."
        if fl.get("deadline"):
            evidence.append({"kind": "deadline", "code": fl["deadline"], "label": items[fl["deadline"]]["name"],
                             "date": items[fl["deadline"]]["due"]})
        context = None
        if category != "engaged" and last_any and (not last or last_any > last["when"]):
            context = f"Context only, not counted: last engagement anywhere on the deal {_plural((as_of - _d(last_any)).days, 'day')} ago."
        out.append(Assessment("assignment", r["item"], level, category, reason, letter=letter, days=days,
                              due=it["due"], evidence=evidence, context=context))
    return _combine(party, out, p, profile="Deal participant", no_engagement=no_engagement_at_all)


def _combine(party, out, p, profile, no_engagement=False):
    assessed = sorted((a for a in out if a.level in LEVELS), key=Assessment.rank)
    if assessed:
        top = assessed[0]
        level, category, headline = top.level, top.category, top.reason
    elif any(a.level == "unknown" for a in out):
        level, category, headline = "unknown", "insufficient-information", (
            f"No engagement by {party['name']} is recorded anywhere on the deal: insufficient information, not a finding.")
    elif any(a.level == "not-yet-active" for a in out):
        level, category, headline = "not-yet-active", "not-yet-active", "Their open assignments have not started yet."
    else:
        level, category, headline = "none", "none", "No open Accountable or Responsible work, and no requests awaiting them."
    counts = {c: sum(a.category == c for a in out) for c in
              ("overdue-response", "response-due", "inactivity", "deal-risk", "insufficient-information", "not-yet-active")}
    return {"level": level, "category": category, "profile": profile, "headline": headline, "flags": counts,
            "no_engagement_recorded": no_engagement,
            "assessments": [a.__dict__ for a in sorted(out, key=Assessment.rank)]}
