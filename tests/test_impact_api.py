"""The Impact Explorer API, against a real seeded TypeDB database.

Every expected date, breach and threshold is recomputed from data/seed/*.yaml by tests/oracle.py
(a different algorithm from apex/impact.py), and the dependency graph from the YAML and the schema
text, so the app can't pass by agreeing with itself.
"""

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apex import api, impact, queries
from apex.db import READ, fetch
from apex.settings import load_settings
from oracle import schema_text, breached_deadlines, projected_dates, raw_seed

SEED = raw_seed()
ITEMS = {i["code"]: i for i in SEED["items"] + SEED["workstreams"]}
PLANNED = {c: i["due"] for c, i in ITEMS.items()}
SCHEMA = schema_text()   # base schema plus migrations, as TypeDB loads it
PRESETS = [0, 28, 61, 120, 136, 168]
# Either side of every contractual deadline (from docs/dataset.md's answer key).
BOUNDARIES = [60, 61, 119, 120, 135, 136, 149, 150]


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


def run(client, days, item=None):
    r = client.get("/api/impact", params={"delay_days": days, **({"item": item} if item else {})})
    assert r.status_code == 200, r.text
    return r.json()


def hard_kinds_from_schema():
    kinds = {"hard-dependency"}
    for child, parent in re.findall(r"relation ([\w-]+)(?: @abstract,)? sub ([\w-]+)", SCHEMA):
        if parent in kinds:
            kinds.add(child)
    return kinds


HARD = hard_kinds_from_schema()


def reachable(start):
    """Items reachable from start through hard dependencies, by depth-first search over the YAML."""
    out, todo = {start}, [start]
    while todo:
        x = todo.pop()
        for d in SEED["dependencies"]:
            if d["from"] == x and d["kind"] in HARD and d["to"] not in out:
                out.add(d["to"])
                todo.append(d["to"])
    return out


def raci_of(code):
    letters = SEED["raci"][code]
    return {(l, h) for l, hs in letters.items() for h in ([hs] if isinstance(hs, str) else hs)}


# ---- projections -------------------------------------------------------------------------------

@pytest.mark.parametrize("days", sorted(set(PRESETS + BOUNDARIES)))
def test_projected_dates_and_breaches_match_the_oracle(client, days):
    data = run(client, days)["data"]
    expected = projected_dates(SEED, "CP-CMA", days)
    for it in data["items"]:
        assert date.fromisoformat(it["projected"]) == expected[it["code"]], it["code"]
        assert it["shift_days"] == (expected[it["code"]] - PLANNED[it["code"]]).days
    got = {(d["code"], x["code"]): x["days_over"] for d in data["deadlines"] for x in d["limits"] if x["breached"]}
    assert got == breached_deadlines(SEED, expected)
    assert data["level"] == ("breach" if got else "delayed" if days else "on-plan")
    assert {c for c in expected if expected[c] != PLANNED[c]} == {i["code"] for i in data["items"] if i["shift_days"] > 0}


def test_the_four_business_scenarios(client):
    def breached(days):
        return set(run(client, days)["data"]["summary"]["breached"])
    assert breached(0) == breached(28) == breached(60) == set()
    assert breached(61) == {"DL-HELIX-WAIVER"}                                   # Helix waiver lapses
    assert breached(119) == {"DL-HELIX-WAIVER"}
    assert breached(120) == {"DL-HELIX-WAIVER", "DL-LONGSTOP"}                   # long-stop exceeded
    assert breached(135) == {"DL-HELIX-WAIVER", "DL-LONGSTOP"}
    assert breached(136) == {"DL-HELIX-WAIVER", "DL-LONGSTOP", "DL-FUNDS"}       # funds before payment
    full = {d["code"]: {x["code"]: x["days_over"] for x in d["limits"]} for d in run(client, 168)["data"]["deadlines"]}
    assert full == {"DL-HELIX-WAIVER": {"MS-COMPLETION": 108}, "DL-LONGSTOP": {"MS-COMPLETION": 49},
                    "DL-FUNDS": {"MS-FUNDS": 19, "OB-SETTLE": 33}}


def test_28_days_moves_14_downstream_items_in_5_workstreams(client):
    data = run(client, 28)["data"]
    moved = [i for i in data["items"] if i["shift_days"] > 0 and i["role"] == "downstream"]
    assert len(moved) == 14
    assert len({i["workstream"]["code"] for i in moved}) == 5
    assert data["summary"]["moved"] == 15        # including the delayed clearance itself


def test_presets_are_found_from_the_data(client):
    """The thresholds are the smallest delays at which the oracle sees each deadline breached."""
    data = run(client, 0)["data"]
    first = {}
    for days in range(0, 366):
        for (dl, _), _ in breached_deadlines(SEED, projected_dates(SEED, "CP-CMA", days)).items():
            first.setdefault(dl, days)
    thresholds = {s["deadline"]: s["days"] for s in data["scenarios"] if s["kind"] == "threshold"}
    assert thresholds == first == {"DL-HELIX-WAIVER": 61, "DL-LONGSTOP": 120, "DL-FUNDS": 136}
    assert [s["days"] for s in data["scenarios"]] == PRESETS
    for d in data["deadlines"]:
        assert d["breach_from_days"] == first[d["code"]]


def test_completed_items_never_move_and_other_subjects_work(client):
    data = run(client, 90, item="CP-COURT")["data"]
    assert data["start"]["code"] == "CP-COURT"
    expected = projected_dates(SEED, "CP-COURT", 90)
    assert all(date.fromisoformat(i["projected"]) == expected[i["code"]] for i in data["items"])
    assert {s["code"] for s in data["subjects"]} == {c for c, i in ITEMS.items()
                                                    if i.get("type") in ("regulatory-clearance", "shareholder-approval", "court-sanction")
                                                    and i["status"] != "complete"}
    assert client.get("/api/impact", params={"item": "XX-NONE"}).status_code == 404
    assert client.get("/api/impact", params={"delay_days": 366}).status_code == 422
    assert client.get("/api/impact", params={"delay_days": -1}).status_code == 422


# ---- the dependency graph ------------------------------------------------------------------------

def test_graph_edges_match_the_yaml_and_the_schema(client):
    data = run(client, 61)["data"]
    assert set(data["hard_kinds"]) == HARD
    reach = reachable("CP-CMA")
    assert {i["code"] for i in data["items"]} == reach
    hard = {(e["from"], e["to"], e["kind"], e["lag"]) for e in data["edges"] if e["hard"]}
    assert hard == {(d["from"], d["to"], d["kind"], d["lag"]) for d in SEED["dependencies"]
                    if d["kind"] in HARD and d["from"] in reach}
    soft = {(e["from"], e["to"]) for e in data["edges"] if not e["hard"] and e["kind"] != "deadline-limit"}
    assert soft == {(d["from"], d["to"]) for d in SEED["dependencies"]
                    if d["kind"] not in HARD and (d["from"] in reach or d["to"] in reach)}
    limits = {(e["from"], e["to"]) for e in data["edges"] if e["kind"] == "deadline-limit"}
    assert limits == {(dl["item"], dl["deadline"]) for dl in SEED["deadline_limits"] if dl["item"] in reach}
    assert {e["to"] for e in data["edges"] if e["kind"] == "deadline-limit" and e["moved"]} == {"DL-HELIX-WAIVER"}


def test_paths_and_causes_follow_real_dependencies(client):
    data = run(client, 168)["data"]
    hard = {(d["from"], d["to"]): d for d in SEED["dependencies"] if d["kind"] in HARD}
    for it in data["items"]:
        assert it["path"][0] == "CP-CMA" and it["path"][-1] == it["code"]
        for a, b in zip(it["path"], it["path"][1:]):
            assert (a, b) in hard
        for c in it["caused_by"]:
            assert (c["code"], it["code"]) in hard and hard[(c["code"], it["code"])]["lag"] == c["lag"]
    settle = next(i for i in data["items"] if i["code"] == "OB-SETTLE")
    assert settle["path"] == ["CP-CMA", "CP-COURT", "MS-COMPLETION", "OB-SETTLE"]


# ---- who needs to act -----------------------------------------------------------------------------

def test_raci_for_every_affected_item_and_deadline(client):
    data = run(client, 168)["data"]
    for n in data["items"] + data["deadlines"]:
        assert {(r["letter"], r["code"]) for r in n["raci"]} == raci_of(n["code"]), n["code"]
    people = {p["code"]: p for p in data["summary"]["people"]}
    okafor = people["PER-OKAFOR"]
    assert ("A", "CP-CMA") in {(i["letter"], i["code"]) for i in okafor["items"]}
    shaw = people["PER-SHAW"]
    assert {i["via"]["code"] for i in shaw["items"] if i["via"]} == {"MAN-BWM"}
    assert shaw["employer"]["code"] == "ORG-BWM"


def test_deadlines_carry_obligations_and_consent_rights(client):
    data = run(client, 61)["data"]
    dls = {d["code"]: d for d in data["deadlines"]}
    helix = dls["DL-HELIX-WAIVER"]
    src = next(i for i in SEED["items"] if i["code"] == "DL-HELIX-WAIVER")
    assert helix["consequence"] == src["consequence"] and helix["breached"]
    assert [c["code"] for c in helix["consent_from"]] == ["ORG-HELIX"]
    assert [b["code"] for b in helix["bound"]] == ["ORG-ATL-IE"]
    assert helix["group_obligation"] == {"owed_by": "ORG-ATL-IE", "consent_from": "ORG-HELIX", "effective_stake": "1"}
    assert {b["code"] for b in dls["DL-LONGSTOP"]["bound"]} == {o["obligor"] for o in SEED["bound_by"] if o["obligation"] == "DL-LONGSTOP"}


# ---- TypeQL and read-only ---------------------------------------------------------------------------

def test_typeql_shows_the_recursive_queries(client, driver, seeded):
    body = run(client, 61)
    typeql = body["typeql"]
    assert any("impact_edges($start)" in q and '"CP-CMA"' in q for q in typeql)
    assert any("downstream($start)" in q for q in typeql)
    assert body["parameters"] == {"delay_days": 61}
    names = {f["name"]: f["definition"] for f in body["data"]["functions"]}
    # Each definition is the function's own comment and body, exactly as in schema/functions.tql.
    tql = (Path(__file__).resolve().parent.parent / "schema" / "functions.tql").read_text()
    for name, text in names.items():
        assert text in tql and f"fun {name}(" in text and text.rstrip().endswith(";")
        assert text.count("\nfun ") + text.startswith("fun ") == 1      # one function, not two
    assert "impact_edges($start)" in names["impact_edges"]
    with driver.transaction(seeded, READ) as tx:
        for q in typeql:
            tx.query(q).resolve()


def _snapshot(driver, db):
    with driver.transaction(db, READ) as tx:
        counts = queries.counts_by_type(tx, ["acquisition", "milestone", "regulatory-clearance", "deadline", "covenant",
                                             "gating", "sequencing", "informing", "deadline-limit", "responsibility",
                                             "accountability", "consultation", "notification", "deal-event"])
        dates = sorted((d["code"], d["due"], d.get("status")) for d in
                       fetch(tx, 'match $x isa deal-item, has code $c, has due-date $d; fetch { "code": $c, "due": $d, "status": $x.status };'))
    return counts, dates


def test_simulation_never_changes_the_database(client, driver, seeded):
    before = _snapshot(driver, seeded)
    for days in range(0, 169, 7):
        run(client, days)
    run(client, 300, item="CP-COURT")
    assert _snapshot(driver, seeded) == before
    import inspect
    assert "WRITE" not in inspect.getsource(impact) and ".commit(" not in inspect.getsource(impact)
