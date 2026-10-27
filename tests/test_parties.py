"""The Parties API (/api/register, /api/parties/{code}, /api/engagement-policy) against answers recomputed
from the raw YAML with independent code: schedule float by brute force, and every party's engagement level."""

import re
from collections import defaultdict
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from apex import api
from apex.settings import load_settings
from oracle import raw_seed

SEED = raw_seed()
AS_OF = SEED["deal"]["as_of"]
ITEMS = {i["code"]: i for i in SEED["items"]} | {w["code"]: {**w, "type": "workstream", "workstream": None} for w in SEED["workstreams"]}
PEOPLE = {p["code"]: p for p in SEED["people"]}
ORGS = {o["code"]: o for o in SEED["organisations"]}
AUTHORITY_TYPES = {"regulator", "court"}
POLICY = yaml.safe_load(open("config/engagement-policy.yaml"))
HARD = {"sequencing", "gating"}
ENGAGEMENT = {"response", "meeting", "delivery", "decision"}
OUTREACH = {"request", "notice"}
RACI = [(h, item, letter) for item, letters in SEED["raci"].items() for letter, hs in letters.items()
        for h in ([hs] if isinstance(hs, str) else hs)]
LEVEL_RANK = {"red": 0, "amber": 1, "green": 2}


# ---- Oracles -------------------------------------------------------------------------------

def brute_force_float(code):
    """Try every delay, day by day, until a limited item lands after its deadline."""
    return brute_force_float_to(code, None)


def brute_force_float_to(code, only_deadline):
    """As brute_force_float, for one deadline (or any, when only_deadline is None)."""
    out = defaultdict(list)
    for d in SEED["dependencies"]:
        if d["kind"] in HARD:
            out[d["from"]].append(d)
    reach, todo = {code}, [code]
    while todo:
        for d in out[todo.pop()]:
            if d["to"] not in reach:
                reach.add(d["to"]); todo.append(d["to"])
    limits = [l for l in SEED["deadline_limits"] if l["item"] in reach and only_deadline in (None, l["deadline"])]
    if not limits:
        return None
    order, seen = [], set()
    def visit(x):                       # topological order over the reachable sub-graph
        if x in seen:
            return
        seen.add(x)
        for d in out[x]:
            if d["to"] in reach:
                visit(d["to"])
        order.append(x)
    visit(code)
    order.reverse()
    for delay in range(0, 366):
        when = {}
        for x in order:
            base = ITEMS[x]["due"]
            if ITEMS[x]["status"] == "complete":
                when[x] = base
                continue
            when[x] = base + timedelta(days=delay) if x == code else base
            for d in SEED["dependencies"]:
                if d["kind"] in HARD and d["to"] == x and d["from"] in when:
                    when[x] = max(when[x], when[d["from"]] + timedelta(days=d.get("lag") or 0))
        if any(when[l["item"]] > ITEMS[l["deadline"]]["due"] for l in limits):
            return delay - 1
    return None


def oracle_level(code):
    """The party's overall level, recomputed from the raw YAML."""
    person = PEOPLE.get(code)
    employer = person["employer"] if person else None
    authority = (ORGS.get(code, {}).get("type") in AUTHORITY_TYPES) or (employer and ORGS[employer]["type"] in AUTHORITY_TYPES)
    members = {code} | ({p for p, x in PEOPLE.items() if x["employer"] == code} if code in ORGS else set())
    answered = {q for e in SEED["events"] for q in e.get("reply_to", [])}
    mine = [(i, l) for h, i, l in RACI if h == code]
    assigned = {i for i, _ in mine}
    levels = []
    for e in SEED["events"]:
        if e["type"] != "request" or not e.get("response_due") or e["code"] in answered:
            continue
        to = set(e.get("recipients", []))
        if code in to or (employer in to and e["subject"] in assigned):
            days = (e["response_due"] - AS_OF).days
            levels.append("red" if days < 0 else "amber" if days <= POLICY["response_warning_days"] else "green")
    if authority:
        for rv in SEED["reviews"]:
            if rv["reviewer"] == code and ITEMS[rv["subject"]]["status"] != "complete":
                days = (ITEMS[rv["subject"]]["due"] - AS_OF).days
                levels.append("red" if days < 0 else "amber" if days <= POLICY["statutory_warning_days"] else "green")
        return min(levels, key=LEVEL_RANK.get) if levels else "none"
    engaged = [e for e in SEED["events"] if not e.get("automated") and (
        (e["type"] in ENGAGEMENT and (e["actor"] in members or members & set(e.get("attendees", []))))
        or (e["type"] in OUTREACH and e["actor"] in members))]
    other = set()
    for i, letter in mine:
        it = ITEMS[i]
        if it["status"] == "complete" or it["type"] == "deadline" or letter not in POLICY["inactivity"]:
            continue
        start = it.get("actual_start") or it.get("planned_start")
        active = start or it["due"] - timedelta(days=POLICY["milestone_active_window_days"])
        if active > AS_OF:
            other.add("not-yet-active"); continue
        if not engaged:
            other.add("unknown"); continue
        if it["type"] == "workstream":                       # oversight: the workstream or any of its items
            on = [e for e in engaged if e["subject"] == i or ITEMS[e["subject"]]["workstream"] == i]
        else:                                                # this item, or a meeting that discussed it
            on = [e for e in engaged if e["subject"] == i or i in e.get("agenda", [])]
        last = max((e["at"].date() for e in on), default=None)
        days = (AS_OF - (last if last and last >= active else active)).days
        th = POLICY["inactivity"][letter]
        level = "red" if days >= th["red_from"] else "amber" if days >= th["amber_from"] else "green"
        fl = brute_force_float(i) if it["type"] != "workstream" else None
        critical = (fl is not None and fl <= POLICY["critical_float_days"]) or it.get("criticality") == "critical"
        urgent = 0 <= (it["due"] - AS_OF).days <= POLICY["urgent_window_days"] or it["status"] in ("at-risk", "delayed")
        levels.append("red" if level == "amber" and critical and urgent else level)
    if levels:
        return min(levels, key=LEVEL_RANK.get)
    return "unknown" if "unknown" in other else "not-yet-active" if "not-yet-active" in other else "none"


# ---- Fixtures --------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


@pytest.fixture(scope="module")
def register(client):
    r = client.get("/api/register")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module")
def profiles(client):
    out = {}
    for code in [*PEOPLE, *ORGS]:
        r = client.get(f"/api/parties/{code}")
        assert r.status_code == 200, (code, r.text)
        out[code] = r.json()
    return out


# ---- Tests -----------------------------------------------------------------------------------

def test_float_matches_a_brute_force_search_and_the_impact_explorer(profiles):
    floats = {a["item"]: a["float"] for p in profiles.values() for a in p["data"]["assignments"]
              if ITEMS[a["item"]]["status"] != "complete" and ITEMS[a["item"]]["type"] != "workstream"}
    assert floats["CP-CMA"] == 60                                        # Impact: Helix breaks at 61 days
    for code, value in floats.items():
        assert value == brute_force_float(code), code


def test_every_partys_level_matches_the_oracle(register, profiles):
    people = {p["code"]: p for p in register["data"]["people"]}
    orgs = {o["code"]: o for o in register["data"]["organisations"]}
    assert set(people) == set(PEOPLE) and set(orgs) == set(ORGS)
    for code in [*PEOPLE, *ORGS]:
        row = people.get(code) or orgs[code]
        assert row["engagement"]["level"] == oracle_level(code) == profiles[code]["data"]["engagement"]["level"], code


def test_deliberate_scenarios(profiles):
    def top(code):
        return profiles[code]["data"]["assessments"][0]
    assert (top("PER-DONNELLY")["category"], top("PER-DONNELLY")["evidence"][0]["code"]) == ("overdue-response", "EV-INT-05")
    assert (top("PER-OSULLIVAN")["category"], top("PER-OSULLIVAN")["evidence"][0]["code"]) == ("overdue-response", "EV-PPL-04")
    grant = next(a for a in profiles["PER-GRANT"]["data"]["assessments"] if a["item"] == "AC-COM-DAY1")
    assert (grant["level"], grant["days"]) == ("red", 13)
    menon = next(a for a in profiles["PER-MENON"]["data"]["assessments"] if a["item"] == "CV-CONDUCT")
    assert menon["category"] == "inactivity" and menon["days"] == 10
    assert profiles["PER-MENSAH"]["data"]["engagement"]["no_engagement_recorded"] is True


def test_authorities_follow_their_statutory_timetable(profiles):
    cma = profiles["ORG-CMA"]["data"]
    assert cma["authority"] and cma["engagement"]["profile"] == "Statutory timetable"
    assert cma["engagement"]["level"] == "amber"                       # Novacore's request EV-REG-13, reply due 27 Oct
    assert {a["category"] for a in cma["assessments"]} >= {"statutory-timetable", "response-due"}
    assert profiles["ORG-HCJ"]["data"]["engagement"]["level"] == "green"
    assert not any(p["employer"] in {c for c, o in ORGS.items() if o["type"] in AUTHORITY_TYPES} for p in SEED["people"])  # D7
    assert profiles["ORG-CMA"]["data"]["people"] == []


def test_outstanding_responsibilities_letters_and_delivery_risk(register):
    for row in register["data"]["people"]:
        code = row["code"]
        open_ = [(i, l) for h, i, l in RACI if h == code and ITEMS[i]["status"] != "complete"]
        assert row["letters"] == {l: sum(x == l for _, x in open_) for l in "ARCI"}, code
        assert row["outstanding"] == sum(l in "AR" for _, l in open_)
        risky = {i for i, l in open_ if l in "AR" and ITEMS[i]["type"] != "deadline"
                 and (ITEMS[i]["status"] in ("at-risk", "delayed") or ITEMS[i]["due"] < AS_OF)}
        assert {d["code"] for d in row["delivery_risk"]} == risky, code


def test_profile_assignments_requests_and_history(profiles):
    for code, body in profiles.items():
        p = body["data"]
        assert {(a["item"], a["letter"]) for a in p["assignments"]} == {(i, l) for h, i, l in RACI if h == code}, code
        members = {code} | ({x for x, y in PEOPLE.items() if y["employer"] == code} if code in ORGS else set())
        addressed = members | ({PEOPLE[code]["employer"]} if code in PEOPLE else set())
        expected = {e["code"] for e in SEED["events"] if e["actor"] in members or members & set(e.get("attendees", []))
                    or addressed & set(e.get("recipients", []))}
        assert {e["code"] for e in p["history"]} == expected, code
        whens = [e["when"] for e in p["history"]]
        assert whens == sorted(whens, reverse=True)
        assert {e["code"] for e in p["requests_to"]} == {e["code"] for e in SEED["events"] if e["type"] == "request"
                                                          and addressed & set(e.get("recipients", []))}


def test_every_finding_is_explained_with_records(profiles):
    known = set(ITEMS) | {e["code"] for e in SEED["events"]}
    for code, body in profiles.items():
        for a in body["data"]["assessments"]:
            assert a["reason"] and a["evidence"], (code, a)
            assert all(ev["code"] in known for ev in a["evidence"]), (code, a)


def test_contacts_are_masked_everywhere(client, register, profiles):
    secrets = [p["email"] for p in SEED["people"]] + [p["phone"] for p in SEED["people"]]
    bodies = [client.get("/api/register").text] + [client.get(f"/api/parties/{c}").text for c in [*PEOPLE, *ORGS]]
    for text in bodies:
        assert not [s for s in secrets if s in text]
    for row in register["data"]["people"]:
        src = PEOPLE[row["code"]]
        assert row["contact"]["email"].endswith("@" + src["email"].split("@")[1]) and "•" in row["contact"]["email"]
        assert re.fullmatch(r"07700 900•••", row["contact"]["phone"])


def test_policy_is_served_as_configured(client):
    body = client.get("/api/engagement-policy").json()
    assert body["data"] == POLICY and body["source"] == "config/engagement-policy.yaml"


def test_unknown_party_and_read_only(client):
    assert client.get("/api/parties/ORG-NOPE").status_code == 404
    assert client.get("/api/parties/not a code").status_code == 422
    for url in ("/api/register", "/api/parties/PER-OKAFOR"):
        body = client.get(url).json()
        assert not any(w in q for q in body["typeql"] for w in ("insert", "delete", "define", "update"))
        assert any("sub engagement" in q for q in body["typeql"])          # what counts comes from the schema


def test_the_api_image_contains_the_engagement_policy():
    """The policy file is read at runtime, so the API's Docker image must include config/."""
    assert "COPY config ./config" in open("Dockerfile").read()
