"""The engagement rules on small made-up records (no TypeDB): every threshold boundary, each escalation
trigger on its own, the authority profile, Unknown, not-yet-active, ties and the requests-only letters."""

from datetime import date, timedelta

import pytest

from apex import engagement

AS_OF = date(2026, 10, 26)
P = engagement.policy()
ETYPES = {"engagement", "response", "meeting", "delivery", "decision"}
OTYPES = {"outreach", "request", "notice"}
PERSON = {"code": "P1", "name": "Pat", "kind": "person", "employer": "O1", "authority": False}


def day(n):
    return (AS_OF + timedelta(days=n)).isoformat()


def item(code="X1", **kw):
    base = {"name": f"Item {code}", "type": "activity", "status": "in-progress", "due": day(30), "planned_start": day(-60),
            "actual_start": None, "criticality": "medium", "workstream": "WS1"}
    return {code: {**base, **kw}}


def event(code, kind, actor="P1", subject="X1", when=-1, **kw):
    return {"code": code, "type": kind, "when": f"{day(when)}T10:00:00", "summary": f"{kind} {code}",
            "actor": {"code": actor, "name": actor}, "item": {"code": subject, "name": subject},
            "recipients": [{"code": c, "name": c} for c in kw.pop("to", [])],
            "attendees": [{"code": c, "name": c} for c in kw.pop("attended", [])],
            "answered_by": kw.pop("answered_by", []), "response_due": kw.pop("due", None),
            "automated": kw.pop("automated", None), **kw}


def run(items, events, letter="R", raci=None, party=PERSON, reviews=(), floats=None, employees=()):
    raci = raci if raci is not None else [{"holder": party["code"], "item": c, "letter": letter} for c in items]
    return engagement.assess(party, AS_OF, items, raci, events, ETYPES, list(reviews), floats or {}, employees,
                             outreach_types=OTYPES)


def top(result):
    return result["assessments"][0]


@pytest.mark.parametrize("letter", ["A", "R"])
@pytest.mark.parametrize("days, level", [(3, "green"), (4, "amber"), (7, "amber"), (8, "red")])
def test_inactivity_thresholds_for_accountable_and_responsible(letter, days, level):
    other = event("E0", "delivery", subject="Y1", when=-1)        # engaged elsewhere: never improves this item
    r = run({**item(), **item("Y1")}, [event("E1", "meeting", when=-days), other], letter,
            raci=[{"holder": "P1", "item": "X1", "letter": letter}])
    assert top(r)["level"] == level and top(r)["days"] == days


@pytest.mark.parametrize("letter", ["C", "I"])
def test_consulted_and_informed_are_assessed_on_requests_only(letter):
    r = run(item(), [event("E1", "meeting", when=-200, attended=["P1"])], letter)
    assert top(r)["category"] == "requests-only" and r["level"] == "none"


def test_no_engagement_on_the_item_counts_from_when_it_became_active():
    items = {**item(planned_start=day(-9)), **item("Y1")}
    r = run(items, [event("E9", "delivery", subject="Y1", when=-1)], raci=[{"holder": "P1", "item": "X1", "letter": "R"}])
    a = top(r)
    assert a["level"] == "red" and a["days"] == 9 and "since it became active" in a["reason"]
    assert a["context"] and "not counted" in a["context"]                      # elsewhere: context only


@pytest.mark.parametrize("start, active", [(1, False), (0, True)])
def test_not_yet_active_boundary(start, active):
    r = run(item(planned_start=day(start)), [event("E1", "delivery", subject="Y1")])
    assert (top(r)["level"] != "not-yet-active") == active


@pytest.mark.parametrize("due, active", [(P["milestone_active_window_days"] + 1, False), (P["milestone_active_window_days"], True)])
def test_point_items_become_active_a_window_before_they_are_due(due, active):
    r = run(item(type="milestone", planned_start=None, due=day(due)), [event("E1", "delivery", subject="Y1")])
    assert (top(r)["level"] != "not-yet-active") == active


def test_no_engagement_anywhere_is_unknown_never_red():
    r = run(item(), [])
    assert r["level"] == "unknown" and top(r)["category"] == "insufficient-information" and r["no_engagement_recorded"]


@pytest.mark.parametrize("due_in, level, category", [
    (-1, "red", "overdue-response"), (0, "amber", "response-due"), (2, "amber", "response-due"), (3, "green", "response-awaited")])
def test_request_reply_by_boundaries(due_in, level, category):
    r = run(item(status="complete"), [event("Q1", "request", actor="P9", to=["P1"], due=day(due_in), when=-5)])
    assert (top(r)["level"], top(r)["category"]) == (level, category)


def test_answered_requests_and_unrelated_org_requests_are_not_findings():
    answered = event("Q1", "request", actor="P9", to=["P1"], due=day(-3), when=-5, answered_by=[{"code": "R1", "when": day(-4)}])
    org_other_item = event("Q2", "request", actor="P9", to=["O1"], due=day(-3), when=-5, subject="Z9")
    r = run(item(status="complete"), [answered, org_other_item])
    assert r["level"] == "none"


def test_org_request_counts_for_a_person_assigned_to_that_item():
    q = event("Q1", "request", actor="P9", to=["O1"], due=day(-2), when=-5, subject="X1")
    assert top(run(item(), [q, event("E1", "delivery", when=-1)]))["category"] == "overdue-response"


def test_overdue_response_outranks_inactivity_and_recent_engagement():
    q = event("Q1", "request", actor="P9", to=["P1"], due=day(-1), when=-5, subject="Y1")
    r = run({**item(), **item("Y1")}, [q, event("E1", "meeting", when=-20, attended=["P1"]), event("E2", "delivery", subject="Y1", when=0)],
            raci=[{"holder": "P1", "item": "X1", "letter": "A"}])
    assert [a["category"] for a in r["assessments"][:2]] == ["overdue-response", "inactivity"]


def test_outreach_counts_for_its_sender_not_its_recipients_and_automation_never():
    sent = event("E1", "request", when=-2)
    received = event("E2", "request", actor="P9", to=["P1"], when=-1)
    automated = event("E3", "notice", when=-1, automated=True)
    assert top(run(item(), [sent]))["days"] == 2
    assert top(run(item(), [received, event("E9", "delivery", subject="Y1", when=-1)]))["days"] == 60      # since active
    assert run(item(), [automated])["level"] == "unknown"


@pytest.mark.parametrize("trigger", ["float", "critical"])
@pytest.mark.parametrize("urgency", ["due-soon", "at-risk"])
def test_escalation_amber_to_red_names_its_reasons(trigger, urgency):
    kw = {"due": day(10)} if urgency == "due-soon" else {"status": "at-risk"}
    if trigger == "critical":
        kw["criticality"] = "critical"
    floats = {"X1": {"float": 30, "deadline": "D1"}} if trigger == "float" else {}
    items = {**item(**kw), **item("D1", type="deadline", name="The deadline")}
    r = run(items, [event("E1", "delivery", when=-5)], raci=[{"holder": "P1", "item": "X1", "letter": "R"}], floats=floats)
    a = top(r)
    assert (a["level"], a["category"]) == ("red", "deal-risk")
    assert ("schedule-critical" in a["reason"]) == (trigger == "float") and "Escalated" in a["reason"]


@pytest.mark.parametrize("kw, floats", [
    ({"due": day(15), "criticality": "critical"}, {}),                       # not urgent enough
    ({"due": day(10)}, {"X1": {"float": 31, "deadline": "D1"}}),            # float above the critical line
])
def test_no_escalation_without_both_conditions(kw, floats):
    items = {**item(**kw), **item("D1", type="deadline")}
    r = run(items, [event("E1", "delivery", when=-5)], raci=[{"holder": "P1", "item": "X1", "letter": "R"}], floats=floats)
    assert (top(r)["level"], top(r)["category"]) == ("amber", "inactivity")


def test_an_engaged_person_is_never_escalated():
    items = {**item(criticality="critical", due=day(3), status="at-risk")}
    assert top(run(items, [event("E1", "delivery", when=-1)]))["level"] == "green"


def test_workstream_letters_count_engagement_on_any_item_in_it():
    items = {**item("WS1", type="workstream", workstream=None), **item("X1")}
    r = run(items, [event("E1", "delivery", subject="X1", when=-2)], raci=[{"holder": "P1", "item": "WS1", "letter": "A"}])
    assert top(r)["days"] == 2 and "or any of its items" in top(r)["reason"]


def steering(when, attended=("P1",), agenda=()):
    return event("M1", "meeting", actor="P9", subject="WS1", when=when, attended=list(attended), agenda=[{"code": a, "name": a} for a in agenda])


def test_workstream_meeting_is_oversight_for_workstream_letters():
    items = {**item("WS1", type="workstream", workstream=None), **item("X1")}
    r = run(items, [steering(-2)], raci=[{"holder": "P1", "item": "WS1", "letter": "A"}])
    assert top(r)["days"] == 2 and top(r)["level"] == "green"


def test_attending_a_workstream_meeting_does_not_reset_items_it_did_not_discuss():
    items = {**item("WS1", type="workstream", workstream=None), **item("X1"), **item("X2")}
    raci = [{"holder": "P1", "item": "X1", "letter": "R"}]
    r = run(items, [steering(-1, agenda=["X2"]), event("E0", "delivery", subject="X1", when=-20)], raci=raci)
    assert (top(r)["level"], top(r)["days"]) == ("red", 20)                     # still measured from 20 days ago


def test_an_agenda_item_counts_for_attendees_only():
    items = {**item("WS1", type="workstream", workstream=None), **item("X1")}
    raci = [{"holder": "P1", "item": "X1", "letter": "R"}]
    attended = run(items, [steering(-1, agenda=["X1"])], raci=raci)
    assert top(attended)["days"] == 1 and "discussed at" in top(attended)["reason"]
    absent = run(items, [steering(-1, attended=("P7",), agenda=["X1"]), event("E9", "delivery", subject="Y1", when=-1)], raci=raci)
    assert top(absent)["days"] == 60                                            # since the item became active


AUTH = {"code": "AU", "name": "Authority", "kind": "organisation", "employer": None, "authority": True}


@pytest.mark.parametrize("due_in, level", [(11, "green"), (10, "amber"), (0, "amber"), (-1, "red")])
def test_authority_statutory_timetable_boundaries(due_in, level):
    items = item("C1", type="regulatory-clearance", due=day(due_in), planned_start=None)
    r = run(items, [], party=AUTH, raci=[], reviews=[{"authority": "AU", "clearance": "C1"}])
    assert (r["level"], r["profile"], top(r)["category"]) == (level, "Statutory timetable", "statutory-timetable")


def test_authorities_are_never_assessed_on_silence():
    items = {**item("C1", type="regulatory-clearance", due=day(30), planned_start=None), **item()}
    r = run(items, [], party=AUTH, raci=[{"holder": "AU", "item": "X1", "letter": "I"}], reviews=[{"authority": "AU", "clearance": "C1"}])
    assert r["level"] == "green" and {a["category"] for a in r["assessments"]} == {"statutory-timetable", "authority"}
    q = event("Q1", "request", actor="P9", to=["AU"], due=day(1), when=-3, subject="C1")
    assert run(items, [q], party=AUTH, raci=[], reviews=[{"authority": "AU", "clearance": "C1"}])["level"] == "amber"


def test_deadlines_are_limits_not_work():
    r = run(item("D1", type="deadline", planned_start=None), [event("E1", "delivery", subject="Y1")], "A")
    assert top(r)["category"] == "deadline" and r["level"] == "none"


def test_ties_go_to_the_stronger_letter_then_the_earlier_due_date():
    items = {**item("X1", due=day(40)), **item("X2", due=day(20)), **item("X3", due=day(10))}
    raci = [{"holder": "P1", "item": "X1", "letter": "A"}, {"holder": "P1", "item": "X2", "letter": "R"},
            {"holder": "P1", "item": "X3", "letter": "R"}]
    r = run(items, [event("E9", "delivery", subject="Y1", when=-1)], raci=raci)
    assert [a["item"] for a in r["assessments"][:3]] == ["X1", "X3", "X2"]


def test_every_finding_carries_its_evidence():
    q = event("Q1", "request", actor="P9", to=["P1"], due=day(-1), when=-5)
    r = run(item(), [q, event("E1", "meeting", when=-9, attended=["P1"])])
    kinds = {e["kind"] for a in r["assessments"] for e in a["evidence"]}
    assert {"request", "item", "event"} <= kinds
