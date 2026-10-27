"""The Gantt chart's API (/api/schedule) and the shared item detail (/api/items/{code}), checked
against answers recomputed from the raw YAML (tests/oracle.py shares no code with apex/)."""

import pytest
from fastapi.testclient import TestClient

from apex import api
from apex.settings import load_settings
from oracle import derived_progress, raw_seed

SEED = raw_seed()
ITEMS = {i["code"]: i for i in SEED["items"]}
CONDITIONS = {c for c, i in ITEMS.items() if i["type"] in ("regulatory-clearance", "shareholder-approval", "court-sanction")}
HARD = {"sequencing", "gating"}          # the schema's hard-dependency subtypes; informing is soft
LETTERS = {"A", "R", "C", "I"}


def iso(value):
    return value.isoformat() if value else None


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


@pytest.fixture(scope="module")
def chart(client):
    r = client.get("/api/schedule")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_scenario_date_is_the_stored_one(chart):
    assert chart["as_of"] == iso(SEED["deal"]["as_of"]) == "2026-10-26"


def test_every_bar_and_diamond_has_typedb_dates(chart):
    seen = {}
    for ws in chart["workstreams"]:
        for it in ws["items"]:
            assert it["workstream"] == ws["code"]
            seen[it["code"]] = it
    assert set(seen) == set(ITEMS)                                   # every item exactly once
    for code, src in ITEMS.items():
        it = seen[code]
        expected_shape = "deadline" if src["type"] == "deadline" else "bar" if src.get("planned_start") else "milestone"
        assert (it["shape"], it["type"], it["status"], it["due"], it["planned_start"], it["actual_start"]) == (
            expected_shape, src["type"], src["status"], iso(src["due"]), iso(src.get("planned_start")),
            iso(src.get("actual_start"))), code
        assert it["workstream"] == src["workstream"]
    shapes = [it["shape"] for it in seen.values()]
    assert (shapes.count("bar"), shapes.count("milestone"), shapes.count("deadline")) == (47, 28, 3)


def test_workstreams_carry_their_window_and_both_progress_measures(chart):
    assert [w["code"] for w in chart["workstreams"]] == [
        w["code"] for w in sorted(SEED["workstreams"], key=lambda w: (w["planned_start"], w["due"], w["code"]))]
    for w in chart["workstreams"]:
        src = next(s for s in SEED["workstreams"] if s["code"] == w["code"])
        expected = derived_progress([i for i in ITEMS.values() if i["workstream"] == w["code"]])
        assert (w["planned_start"], w["due"]) == (iso(src["planned_start"]), iso(src["due"]))
        assert w["progress"] == expected["percent"]
        assert (w["milestones_achieved"], w["milestones_total"]) == (expected["achieved"], expected["points"])
        starts = [it["planned_start"] or it["due"] for it in w["items"]]
        assert starts == sorted(starts)                              # rows in date order


def test_deadlines_and_what_they_limit(chart):
    expected = {}
    for limit in SEED["deadline_limits"]:
        expected.setdefault(limit["deadline"], set()).add(limit["item"])
    assert {d["code"]: set(d["limits"]) for d in chart["deadlines"]} == expected
    for d in chart["deadlines"]:
        assert d["due"] == iso(ITEMS[d["code"]]["due"]) and d["consequence"]


def test_every_dependency_with_its_kind(chart):
    found = {(d["from"], d["to"]): (d["kind"], d["lag"], d["hard"]) for d in chart["dependencies"]}
    assert found == {(d["from"], d["to"]): (d["kind"], d.get("lag"), d["kind"] in HARD) for d in SEED["dependencies"]}
    assert sum(d["hard"] for d in chart["dependencies"]) == 67


# ---- /api/items/{code} ----

def detail(client, code):
    r = client.get(f"/api/items/{code}")
    assert r.status_code == 200, r.text
    return r.json()["data"]


@pytest.mark.parametrize("code", ["CP-CMA", "DL-HELIX-WAIVER", "AC-REG-CMA-RESP", "CV-COC-CALDER", "MS-COMPLETION",
                                  "AC-COM-DAY1"])
def test_item_detail_matches_the_seed(client, code):
    d = detail(client, code)
    src = ITEMS[code]
    assert (d["name"], d["type"], d["status"], d["due"], d["planned_start"]) == (
        src["name"], src["type"], src["status"], iso(src["due"]), iso(src.get("planned_start")))
    assert d["description"] == src.get("description") and d["workstream"]["code"] == src["workstream"]

    raci = SEED["raci"][code]
    expected = {letter: sorted([holders] if isinstance(holders, str) else holders) for letter, holders in raci.items()}
    assert {letter: sorted(h["code"] for h in hs) for letter, hs in d["raci"].items()} == expected
    assert len(d["raci"]["A"]) == 1

    assert {(x["code"], x["kind"], x["lag"]) for x in d["depends_on"]} == {
        (e["from"], e["kind"], e.get("lag")) for e in SEED["dependencies"] if e["to"] == code}
    assert {(x["code"], x["kind"]) for x in d["needed_by"]} == {
        (e["to"], e["kind"]) for e in SEED["dependencies"] if e["from"] == code}
    assert {x["code"] for x in d["limited_by"]} == {l["deadline"] for l in SEED["deadline_limits"] if l["item"] == code}
    assert {x["code"] for x in d["limits"]} == {l["item"] for l in SEED["deadline_limits"] if l["deadline"] == code}
    assert {x["code"] for x in d["bound"]} == {b["obligor"] for b in SEED["bound_by"] if b["obligation"] == code}
    assert {x["code"] for x in d["consent_from"]} == {c["grantor"] for c in SEED["consents"] if c["obligation"] == code}

    events = sorted((e for e in SEED["events"] if e["subject"] == code), key=lambda e: (e["at"], e["code"]), reverse=True)
    assert [e["code"] for e in d["events"]] == [e["code"] for e in events]          # newest first
    assert {r["code"] for r in d["risks"]} == {r["code"] for r in SEED["risks"] if code in r["exposed"]}
    assert d["simulate"] == (code in CONDITIONS and src["status"] != "complete")


def test_simulate_is_offered_only_for_open_conditions(client):
    assert detail(client, "CP-CMA")["simulate"] is True
    assert detail(client, "CP-EC")["simulate"] is False          # complete
    assert detail(client, "AC-REG-CMA-RESP")["simulate"] is False   # an activity, not a condition


def test_advisers_act_via_their_mandate_in_item_detail(client):
    d = detail(client, "DL-HELIX-WAIVER")
    ainsworth = next(h for h in d["raci"]["R"] if h["code"] == "PER-AINSWORTH")
    assert ainsworth["via"] == "MAN-BWM" and ainsworth["employer"] == "Blackwood Marsh LLP"


def test_unknown_item_is_404_and_bad_codes_are_refused(client):
    assert client.get("/api/items/MS-NOPE").status_code == 404
    assert client.get("/api/items/not a code").status_code == 422


def test_schedule_and_item_detail_only_read(client):
    for url in ("/api/schedule", "/api/items/CP-CMA"):
        typeql = client.get(url).json()["typeql"]
        assert typeql and not any(w in q for q in typeql for w in ("insert", "delete", "define", "update"))


def test_item_detail_lists_the_meetings_that_discussed_it(client):
    d = detail(client, "AC-INT-INTERIM")
    expected = {e["code"] for e in SEED["events"] if "AC-INT-INTERIM" in e.get("agenda", [])}
    assert {m["code"] for m in d["discussed_at"]} == expected == {"EV-INT-14"}
    assert d["discussed_at"][0]["subject"]["code"] == "WS-INT"


def test_critical_paths_are_the_impact_explorers_thresholds(client):
    """One calculation: each deadline's exposure is its Impact breaking point minus one, and its critical
    path is the open items sharing that smallest float (recomputed by brute force)."""
    from test_parties import brute_force_float_to
    chart = client.get("/api/schedule").json()["data"]
    paths = {d["code"]: d["critical_path"] for d in chart["deadlines"]}
    assert {k: v["float"] for k, v in paths.items()} == {"DL-HELIX-WAIVER": 60, "DL-LONGSTOP": 119, "DL-FUNDS": 135}
    for deadline, cp in paths.items():
        floats = {c: brute_force_float_to(c, deadline) for c, i in ITEMS.items() if i["status"] != "complete"}
        floats = {c: f for c, f in floats.items() if f is not None}
        assert cp["float"] == min(floats.values())
        assert set(cp["items"]) == {c for c, f in floats.items() if f == cp["float"]}
    on_path = {i["code"] for w in chart["workstreams"] for i in w["items"] if i["critical_for"]}
    assert on_path == {"CP-CMA", "CP-COURT", "MS-COMPLETION", "OB-SETTLE"}


def test_item_panel_float_is_the_same_calculation(client):
    chart = client.get("/api/schedule").json()["data"]
    floats = {i["code"]: i["float"] for w in chart["workstreams"] for i in w["items"]}
    for code in ("CP-CMA", "AC-REG-CMA-RESP", "AC-INT-SYSTEMS", "MS-FUNDS"):
        assert detail(client, code)["float"] == floats[code]
