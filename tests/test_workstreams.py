"""The Workstreams API (/api/workstreams, /api/workstreams/{code}) against answers recomputed from the raw
YAML. Recursion is re-done here as plain graph searches over the dependencies (no apex code is used)."""

from collections import Counter, defaultdict

import pytest
from fastapi.testclient import TestClient

from apex import api
from apex.settings import load_settings
from oracle import derived_progress, raw_seed

SEED = raw_seed()
ITEMS = {i["code"]: i for i in SEED["items"]}
WS = {w["code"]: w for w in SEED["workstreams"]}
AS_OF = SEED["deal"]["as_of"]
HARD = {"sequencing", "gating"}
CONDITIONS = {"regulatory-clearance", "shareholder-approval", "court-sanction"}
NEXT = defaultdict(set)
PREV = defaultdict(set)
for d in SEED["dependencies"]:
    if d["kind"] in HARD:
        NEXT[d["from"]].add(d["to"])
        PREV[d["to"]].add(d["from"])


def reach(start, graph):
    seen, todo = set(), list(graph[start])
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo += graph[x]
    return seen


def own(ws):
    return [i for i in ITEMS.values() if i["workstream"] == ws]


def iso(d):
    return d.isoformat() if d else None


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


@pytest.fixture(scope="module")
def listing(client):
    r = client.get("/api/workstreams")
    assert r.status_code == 200, r.text
    return {w["code"]: w for w in r.json()["data"]["workstreams"]}


@pytest.fixture(scope="module")
def details(client):
    out = {}
    for code in WS:
        r = client.get(f"/api/workstreams/{code}")
        assert r.status_code == 200, r.text
        out[code] = r.json()["data"]
    return out


def test_every_workstream_once_in_date_order(listing):
    assert list(listing) == [w["code"] for w in sorted(WS.values(), key=lambda w: (w["planned_start"], w["due"], w["code"]))]


def test_three_measures_kept_apart(listing):
    for code, w in listing.items():
        mine = own(code)
        expected = derived_progress(mine)
        conditions = [i for i in mine if i["type"] in CONDITIONS]
        assert w["progress"] == expected["percent"], code                                  # activity progress
        assert (w["milestones_achieved"], w["milestones_total"]) == (expected["achieved"], expected["points"])
        assert (w["conditions_satisfied"], w["conditions_total"]) == (                    # readiness to close
            sum(i["status"] == "complete" for i in conditions), len(conditions))
    assert listing["WS-REG"]["conditions_total"] == 4 and listing["WS-DD"]["conditions_total"] == 0


def test_counts_lead_and_next_milestone(listing):
    for code, w in listing.items():
        mine = own(code)
        assert w["items"] == len(mine) and w["item_statuses"] == dict(Counter(i["status"] for i in mine))
        assert w["lead"]["code"] == SEED["raci"][code]["A"]
        points = sorted((i for i in mine if i["type"] != "deadline" and not i.get("planned_start") and i["status"] != "complete"),
                        key=lambda i: (i["due"], i["code"]))
        if points:
            assert w["next_milestone"]["code"] == points[0]["code"]
            assert w["next_milestone"]["days_remaining"] == (points[0]["due"] - AS_OF).days
        else:
            assert w["next_milestone"] is None


def test_deadlines_the_open_work_can_affect(listing):
    limits = defaultdict(set)
    for l in SEED["deadline_limits"]:
        limits[l["item"]].add(l["deadline"])
    for code, w in listing.items():
        expected = {i["code"] for i in own(code) if i["type"] == "deadline"}
        for i in own(code):
            if i["status"] != "complete":
                for x in {i["code"]} | reach(i["code"], NEXT):
                    expected |= limits[x]
        assert {d["code"] for d in w["deadlines"]} == expected, code
        for d in w["deadlines"]:
            assert d["days_remaining"] == (ITEMS[d["code"]]["due"] - AS_OF).days
    assert {d["code"] for d in listing["WS-REG"]["deadlines"]} == {"DL-HELIX-WAIVER", "DL-LONGSTOP", "DL-FUNDS"}


def test_risks_and_organisations(listing):
    for code, w in listing.items():
        expected = {r["code"] for r in SEED["risks"] if any(ITEMS.get(x, {}).get("workstream") == code for x in r["exposed"])}
        assert {r["code"] for r in w["risks"]} == expected, code
        for r in w["risks"]:
            src = next(s for s in SEED["risks"] if s["code"] == r["code"])
            assert r["owner"]["code"] == src["owner"] and r["severity"] == src["severity"]
        people = {p["code"]: p for p in SEED["people"]}
        orgs = set()
        for item in [code] + [i["code"] for i in own(code)]:
            for holders in SEED["raci"][item].values():
                for h in [holders] if isinstance(holders, str) else holders:
                    orgs.add(people[h]["employer"] if h in people else h)
        assert {o["code"] for o in w["organisations"]} == orgs, code


def test_latest_event_and_event_counts(listing, details):
    for code, w in listing.items():
        events = sorted((e for e in SEED["events"] if e["subject"] == code or ITEMS.get(e["subject"], {}).get("workstream") == code),
                        key=lambda e: (e["at"], e["code"]), reverse=True)
        assert w["events"] == len(events) and w["latest_event"]["code"] == events[0]["code"]
        assert [e["code"] for e in details[code]["events"]] == [e["code"] for e in events]     # newest first
    assert sum(w["events"] for w in listing.values()) == len(SEED["events"]) == 118
    oversight = [e for d in details.values() for e in d["events"] if e["item"]["code"].startswith("WS-")]
    assert len(oversight) == 7 and all(e["type"] == "meeting" and e["agenda"] for e in oversight)


def test_feed_carries_the_event_model(details):
    feed = {e["code"]: e for d in details.values() for e in d["events"]}
    for e in SEED["events"]:
        got = feed[e["code"]]
        assert got["type"] == e["type"] and got["actor"]["code"] == e["actor"] and got["item"]["code"] == e["subject"]
        assert [r["code"] for r in got["recipients"]] == sorted(e.get("recipients", []))
        assert [r["code"] for r in got["attendees"]] == sorted(e.get("attendees", []))
        assert [r["code"] for r in got["agenda"]] == sorted(e.get("agenda", []))
        assert got["replies_to"] == sorted(e.get("reply_to", []))
        assert got["response_due"] == iso(e.get("response_due"))
        assert (got["previous_status"], got["new_status"]) == (e.get("previous_status"), e.get("new_status"))
    answered = defaultdict(list)
    for e in SEED["events"]:
        for q in e.get("reply_to", []):
            answered[q].append(e["code"])
    for code, got in feed.items():
        assert sorted(a["code"] for a in got["answered_by"]) == sorted(answered[code])
    assert feed["EV-INT-05"]["answered_by"] == []                 # the deliberate unanswered request


def test_items_and_open_prerequisites_at_any_depth(details):
    for code, d in details.items():
        assert [i["code"] for i in d["items"]] == [i["code"] for i in sorted(
            own(code), key=lambda i: (i.get("planned_start") or i["due"], i["due"], i["code"]))]
        for it in d["items"]:
            expected = {u for u in reach(it["code"], PREV) if ITEMS[u]["status"] != "complete"}
            assert {u["code"] for u in it["open_prerequisites"]} == expected, it["code"]
    court = next(i for i in details["WS-LEG"]["items"] if i["code"] == "CP-COURT")
    assert {u["workstream"] for u in court["open_prerequisites"]} >= {"WS-REG"}     # blocked from another workstream


def test_dependencies_and_crossings(details):
    for code, d in details.items():
        expected = {(x["from"], x["to"]): ITEMS[x["from"]]["workstream"] != ITEMS[x["to"]]["workstream"]
                    for x in SEED["dependencies"] if code in (ITEMS[x["from"]]["workstream"], ITEMS[x["to"]]["workstream"])}
        assert {(x["from"], x["to"]): x["crosses"] for x in d["dependencies"]} == expected, code
        assert all(x["hard"] == (x["kind"] in HARD) for x in d["dependencies"])


def test_open_conditions_offered_for_simulation(details):
    for code, d in details.items():
        assert {c["code"] for c in d["open_conditions"]} == {
            i["code"] for i in own(code) if i["type"] in CONDITIONS and i["status"] != "complete"}
    assert {c["code"] for c in details["WS-REG"]["open_conditions"]} == {"CP-CMA"}


def test_obligations(details):
    for code, d in details.items():
        expected = {i["code"] for i in own(code) if i["type"] in CONDITIONS | {"covenant", "deadline"}}
        assert {o["code"] for o in d["obligations"]} == expected, code
    longstop = next(o for o in details["WS-LEG"]["obligations"] if o["code"] == "DL-LONGSTOP")
    assert longstop["consequence"] and {x["code"] for x in longstop["limits"]} == {"MS-COMPLETION"}


def test_raci_is_contextual_per_item(client):
    """One person, three different letters on different items of one workstream (plan §5.1)."""
    cells = client.get("/api/raci?workstream=WS-REG").json()["data"]["cells"]
    letters = {c["letter"] for c in cells if c["holder"] == "PER-OKAFOR"}
    expected = {letter for i in own("WS-REG") for letter, h in SEED["raci"][i["code"]].items()
                if "PER-OKAFOR" in ([h] if isinstance(h, str) else h)}
    assert letters == expected and len(letters) >= 3


def test_unknown_workstream_and_read_only(client):
    assert client.get("/api/workstreams/WS-NOPE").status_code == 404
    assert client.get("/api/workstreams/not a code").status_code == 422
    for url in ("/api/workstreams", "/api/workstreams/WS-REG"):
        body = client.get(url).json()
        assert not any(w in q for q in body["typeql"] for w in ("insert", "delete", "define", "update"))
        assert "Recursive functions" in body["capabilities"]          # downstream and upstream are really called
