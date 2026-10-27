"""The HTTP API, against a real seeded TypeDB database (a throwaway apex-test-* one).

Expected answers are recomputed from data/seed/*.yaml with plain Python (tests/oracle.py style),
never taken from the app's own code, so a wrong query can't pass by agreeing with itself.
"""

import inspect
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apex import api, overview
from apex.db import READ, WRITE, fetch
from apex.settings import load_settings
from oracle import derived_progress, effective_stakes, raw_seed

SEED = raw_seed()
ITEMS = {i["code"]: i for i in SEED["items"]}
CONDITIONS = {c for c, i in ITEMS.items() if i["type"] in ("regulatory-clearance", "shareholder-approval", "court-sanction")}
LETTER_ORDER = "ARCI"


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


def get(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


# ---- every endpoint, checked against the YAML ----------------------------------------------

def test_deal_summary_matches_seed(client):
    d = get(client, "/api/deal")["data"]
    deal = SEED["deal"]
    orgs = {o["code"]: o for o in SEED["organisations"]}
    assert d["code"] == deal["code"] and d["name"] == deal["name"]
    assert Decimal(d["value"]) == Decimal(deal["value"]) and d["currency"] == deal["currency"]
    assert d["as_of"] == deal["as_of"].isoformat() and d["structure"] == deal["structure"]
    assert d["acquirer"]["name"] == orgs[deal["acquirer"]]["name"]
    assert d["target"]["name"] == orgs[deal["target"]]["name"]


def test_deal_status_and_metrics_match_seed(client):
    d = get(client, "/api/deal")["data"]
    statuses = {c: ITEMS[c]["status"] for c in CONDITIONS}
    done = sum(s == "complete" for s in statuses.values())
    risky = sorted(c for c, s in statuses.items() if s in ("at-risk", "delayed"))
    assert d["status"]["conditions_total"] == len(CONDITIONS) == 6
    assert d["status"]["conditions_satisfied"] == done == 4
    assert sorted(a["code"] for a in d["status"]["at_risk"]) == risky == ["CP-CMA"]
    assert d["status"]["state"] == "at-risk"

    expected = derived_progress(list(ITEMS.values()))        # D5: duration-weighted, never a plain mean
    m = d["metrics"]
    assert m["progress_percent"] == expected["percent"]
    assert m["progress_items"] == expected["activities"]
    assert (m["milestones_achieved"], m["milestones_total"]) == (expected["achieved"], expected["points"])
    assert "Duration-weighted" in m["progress_basis"]
    assert m["committed_financing"] == [{"currency": "GBP",
                                         "total": str(sum(Decimal(f["value"]) for f in SEED["financing"]))}]

    as_of = SEED["deal"]["as_of"]
    deadlines = sorted((i for i in ITEMS.values() if i["type"] == "deadline" and i["due"] >= as_of),
                       key=lambda i: i["due"])
    first = deadlines[0]
    limited = [ITEMS[dl["item"]]["due"] for dl in SEED["deadline_limits"] if dl["deadline"] == first["code"]]
    assert m["next_deadline"] == {"code": first["code"], "name": first["name"], "due": first["due"].isoformat(),
                                  "days_remaining": (first["due"] - as_of).days,
                                  "headroom_days": (first["due"] - max(limited)).days}


def test_deadline_headroom_matches_impact_answer_key(client):
    """The Overview's headroom is one day short of the Impact Explorer's breach thresholds
    (dataset.md: Helix from 61 days' delay, long-stop from 120, certain funds from 136)."""
    dls = {d["code"]: d for d in get(client, "/api/milestones")["data"]["deadlines"]}
    assert {c: d["headroom_days"] for c, d in dls.items()} == {"DL-HELIX-WAIVER": 60, "DL-LONGSTOP": 119, "DL-FUNDS": 135}
    assert dls["DL-HELIX-WAIVER"]["consent_from"] == [{"code": "ORG-HELIX", "name": "Helix Contract Services Ltd"}]
    assert dls["DL-HELIX-WAIVER"]["bound"][0]["code"] == "ORG-ATL-IE"
    assert all(d["consequence"] == ITEMS[c]["consequence"] for c, d in dls.items())


def test_milestones_timeline_matches_seed(client):
    data = get(client, "/api/milestones")["data"]
    expected = [c for c, i in ITEMS.items() if i["type"] == "milestone" or c in CONDITIONS]
    got = [i["code"] for i in data["items"]]
    assert sorted(got) == sorted(expected)
    assert [i["due"] for i in data["items"]] == sorted(ITEMS[c]["due"].isoformat() for c in expected)
    accountable = {c: v["A"] for c, v in SEED["raci"].items()}
    for i in data["items"]:
        assert i["accountable"]["code"] == accountable[i["code"]]
        assert i["workstream"]["code"] == ITEMS[i["code"]]["workstream"]


def test_milestones_criticality_filter(client):
    body = get(client, "/api/milestones?criticality=critical&criticality=high")
    expected = {c for c, i in ITEMS.items() if (i["type"] == "milestone" or c in CONDITIONS)
                and i.get("criticality", "medium") in ("critical", "high")}
    assert {i["code"] for i in body["data"]["items"]} == expected
    assert body["parameters"] == {"criticality": ["critical", "high"]}
    assert 'like "^(critical|high)$"' in body["typeql"][1]
    assert client.get("/api/milestones?criticality=urgent").status_code == 422


def test_approvals_match_seed(client):
    data = get(client, "/api/approvals")["data"]
    clearances = sorted((c for c, i in ITEMS.items() if i["type"] == "regulatory-clearance"), key=lambda c: ITEMS[c]["due"])
    assert [a["code"] for a in data] == clearances
    reviewers = {}
    for rv in SEED["reviews"]:
        reviewers.setdefault(rv["subject"], set()).add(rv["reviewer"])
    for a in data:
        assert a["status"] == ITEMS[a["code"]]["status"]
        assert {r["code"] for r in a["reviews"]} == reviewers[a["code"]]
        assert all(r["applicant"] == "Novacore plc" for r in a["reviews"])
    by_code = {a["code"]: a for a in data}
    assert {r["code"] for r in by_code["CP-HSR"]["reviews"]} == {"ORG-FTC", "ORG-DOJ"}
    latest_cma = max((e for e in SEED["events"] if e["subject"] == "CP-CMA"), key=lambda e: e["at"])
    assert by_code["CP-CMA"]["latest"]["summary"] == latest_cma["summary"]


def test_activity_newest_first(client):
    body = get(client, "/api/activity?limit=5")
    expected = sorted(SEED["events"], key=lambda e: e["at"], reverse=True)[:5]
    assert [a["summary"] for a in body["data"]] == [e["summary"] for e in expected]
    assert [a["when"] for a in body["data"]] == [e["at"].isoformat() for e in expected]
    assert body["parameters"] == {"limit": 5} and "limit 5;" in body["typeql"][0]
    assert len(get(client, "/api/activity")["data"]) == 10
    assert client.get("/api/activity?limit=0").status_code == 422
    assert client.get("/api/activity?limit=51").status_code == 422


def _team(org):
    holders = {h for v in SEED["raci"].values() for k, hs in v.items() for h in ([hs] if isinstance(hs, str) else hs)}
    return sorted(p["code"] for p in SEED["people"] if p["employer"] == org and p["code"] in holders)


def test_parties_match_seed(client):
    data = get(client, "/api/parties")["data"]
    deal = SEED["deal"]
    assert [(p["role"], p["organisation"]["code"]) for p in data["principals"]] == [
        ("Acquirer", deal["acquirer"]), ("Target", deal["target"])]
    for p in data["principals"]:
        assert [m["code"] for m in p["team"]] == _team(p["organisation"]["code"])
    assert {(a["mandate"], a["client"]["code"], a["organisation"]["code"], a["mandate_type"]) for a in data["advisers"]} == {
        (m["code"], m["client"], m["adviser"], m["type"]) for m in SEED["mandates"]}
    for a in data["advisers"]:
        assert [m["code"] for m in a["team"]] == _team(a["organisation"]["code"])
    assert {(l["facility"]["code"], l["organisation"]["code"], l["facility"]["value"]) for l in data["lenders"]} == {
        (f["code"], f["lender"], f["value"]) for f in SEED["financing"]}
    authorities = {o["code"] for o in SEED["organisations"] if o["type"] in ("regulator", "court")}
    assert {a["organisation"]["code"] for a in data["authorities"]} == authorities
    panel = next(a for a in data["authorities"] if a["organisation"]["code"] == "ORG-PANEL")
    assert panel["reviews"] == [] and {r["letter"] for r in panel["raci"]} == {"I"}


def test_progress_matches_seed(client):
    data = get(client, "/api/progress")["data"]
    assert [w["code"] for w in data] == [w["code"] for w in sorted(SEED["workstreams"], key=lambda w: w["planned_start"])]
    for w in data:
        src = next(s for s in SEED["workstreams"] if s["code"] == w["code"])
        own = [i for i in ITEMS.values() if i["workstream"] == w["code"]]
        expected = derived_progress(own)
        assert w["status"] == src["status"] and w["description"] == src["description"]
        assert w["progress"] == expected["percent"]                     # derived from items, not stored
        assert (w["milestones_achieved"], w["milestones_total"]) == (expected["achieved"], expected["points"])
        assert w["item_statuses"] == dict(Counter(i["status"] for i in own))


def test_raci_matrix_matches_seed(client):
    body = get(client, "/api/raci?workstream=WS-REG")
    data = body["data"]
    items = [c for c, i in ITEMS.items() if i["workstream"] == "WS-REG"]
    assert sorted(i["code"] for i in data["items"]) == sorted(items)
    expected = set()
    for code in items:
        for letter, hs in SEED["raci"][code].items():
            for h in [hs] if isinstance(hs, str) else hs:
                expected.add((code, h, letter))
    assert {(c["item"], c["holder"], c["letter"]) for c in data["cells"]} == expected
    # Benedict Shaw (Blackwood Marsh) acts under Blackwood Marsh's mandate.
    assert {c["via"] for c in data["cells"] if c["holder"] == "PER-SHAW"} == {"MAN-BWM"}
    # Columns: strongest letter first.
    strongest = [min(LETTER_ORDER.index(c["letter"]) for c in data["cells"] if c["holder"] == h["code"])
                 for h in data["holders"]]
    assert strongest == sorted(strongest)
    assert '"WS-REG"' in body["typeql"][0] and body["parameters"] == {"workstream": "WS-REG"}
    assert client.get("/api/raci?workstream=WS-NONE").status_code == 404
    assert client.get('/api/raci?workstream=WS-REG";').status_code == 422


def test_checks_pass_on_seed(client):
    data = get(client, "/api/checks")["data"]
    assert data["passed"] is True
    assert [c["check"] for c in data["checks"]] == ["no_accountable", "overdue", "clearance_without_authority"]
    assert all(c["items"] == [] for c in data["checks"])


def test_ownership_matches_independent_oracle(client):
    data = get(client, "/api/ownership?parent=ORG-ATL")["data"]
    expected = effective_stakes(SEED, "ORG-ATL")
    assert {c["code"]: (Decimal(c["stake"]), c["routes"]) for c in data["companies"]} == expected
    assert {c["obligation"] for c in data["consents"]} == {"CV-COC-CALDER", "CV-COC-HELIX", "DL-HELIX-WAIVER"}
    assert client.get("/api/ownership?parent=ORG-NOPE").status_code == 404


# ---- planted problems (each test commits to its own throwaway database) --------------------

def _client_for(name):
    return TestClient(api.create_app(load_settings(apex_database=name)))


def _write(driver, db, query):
    with driver.transaction(db, WRITE) as tx:
        tx.query(query).resolve()
        tx.commit()


def test_checks_report_planted_problems(driver, fresh_seeded):
    _write(driver, fresh_seeded, """
        match $x isa milestone, has code "IN-SYNERGY"; $a isa accountability (accountable-item: $x); delete $a;""")
    _write(driver, fresh_seeded, """
        match $x isa milestone, has code "MS-COMMS-PLAN", has due-date $d; delete has $d of $x;
        insert $x has due-date 2026-10-01;""")
    _write(driver, fresh_seeded, """
        match $ws isa workstream, has code "WS-REG"; $o isa person, has code "PER-OKAFOR";
        insert $c isa regulatory-clearance, has code "CP-IE-SCREEN", has name "Irish investment screening",
               has status "in-progress", has due-date 2026-11-20;
               work-breakdown (whole: $ws, part: $c); accountability (holder: $o, accountable-item: $c);""")
    with _client_for(fresh_seeded) as c:
        data = get(c, "/api/checks")["data"]
        approvals = get(c, "/api/approvals")["data"]
    found = {ch["check"]: [i["code"] for i in ch["items"]] for ch in data["checks"]}
    assert data["passed"] is False
    assert found == {"no_accountable": ["IN-SYNERGY"], "overdue": ["MS-COMMS-PLAN"],
                     "clearance_without_authority": ["CP-IE-SCREEN"]}
    # The new kind of clearance shows up in the approvals panel with no query changed.
    assert next(a for a in approvals if a["code"] == "CP-IE-SCREEN")["reviews"] == []


def test_ownership_refuses_figures_on_a_cycle(driver, fresh_seeded):
    _write(driver, fresh_seeded, """
        match $a isa company, has code "ORG-ATL-DE"; $b isa company, has code "ORG-ATL";
        insert ownership (owner: $a, owned: $b), has stake 0.05dec;""")
    with _client_for(fresh_seeded) as c:
        r = c.get("/api/ownership?parent=ORG-ATL")
    assert r.status_code == 409
    body = r.json()
    assert "ORG-ATL" in body["cycles"] and "companies" not in body and "data" not in body
    assert any("ownership_cycles()" in q for q in body["typeql"])   # the guard that fired is shown


# ---- the TypeQL drawer shows exactly what ran ------------------------------------------------

ENDPOINTS = ["/api/deal", "/api/impact?delay_days=61", "/api/milestones", "/api/approvals", "/api/activity", "/api/parties",
             "/api/progress", "/api/raci?workstream=WS-REG", "/api/checks", "/api/ownership?parent=ORG-ATL",
             "/api/ownership", "/api/graph", "/api/graph/node/ORG-BWM", "/api/graph/node/PER-SHAW"]


def test_recorded_typeql_reruns_to_the_same_answer(client, driver, seeded):
    """Every query in a response's typeql list runs as-is in a READ transaction (as it would in
    Studio). The guards and checks are included, so the drawer hides nothing."""
    for url in ENDPOINTS:
        body = get(client, url)
        assert body["typeql"], url
        assert body["database"] == seeded
        with driver.transaction(seeded, READ) as tx:
            for q in body["typeql"]:
                tx.query(q).resolve()
    ownership = get(client, "/api/ownership?parent=ORG-ATL")["typeql"]
    assert any("ownership_cycles()" in q for q in ownership) and any("group_members" in q for q in ownership)
    approvals = get(client, "/api/approvals")["typeql"][0]
    with driver.transaction(seeded, READ) as tx:
        assert [d["clearance"]["code"] for d in fetch(tx, approvals)] == [a["code"] for a in get(client, "/api/approvals")["data"]]


# ---- read-only, robust ----------------------------------------------------------------------

def test_api_is_read_only(client):
    for route in client.app.routes:
        if route.path.startswith("/api/") and hasattr(route, "methods") and route.path not in (
                "/api/openapi.json", "/api/docs", "/api/docs/oauth2-redirect"):
            assert route.methods <= {"GET", "HEAD"}, route.path
    assert client.post("/api/deal").status_code == 405
    assert client.delete("/api/checks").status_code == 405
    for module in (api, overview):
        source = inspect.getsource(module)
        assert "WRITE" not in source and "SCHEMA" not in source and ".commit(" not in source


def test_concurrent_requests(client):
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, ENDPOINTS * 3))
    assert codes == [200] * len(codes)


def test_unreachable_database_gives_plain_503():
    app = api.create_app(load_settings(typedb_address="127.0.0.1:1"))
    started = time.monotonic()
    with TestClient(app) as c:
        h = c.get("/api/health")
        r = c.get("/api/deal")
    assert h.status_code == 503 and h.json()["status"] == "unavailable"
    assert r.status_code == 503 and r.json()["detail"] == api.UNAVAILABLE
    assert time.monotonic() - started < 30


def test_missing_database_gives_plain_503():
    with _client_for("apex-test-doesnotexist") as c:
        r = c.get("/api/deal")
    assert r.status_code == 503 and "doesn't exist yet" in r.json()["detail"]


def test_openapi_file_is_up_to_date():
    """web/openapi.json (input to the TypeScript types) must match the running API's models."""
    from scripts.export_openapi import OPENAPI_JSON, openapi
    assert json.loads(Path(OPENAPI_JSON).read_text()) == openapi(), \
        "Run: uv run python -m scripts.export_openapi && scripts/npm-docker.sh npm run gen:api"


def test_health_reports_a_database_that_stops_answering(client, monkeypatch):
    """Health must ask the server each time, not trust a connection opened earlier."""
    assert client.get("/api/health").json()["status"] == "ok"
    monkeypatch.setattr(api, "_reachable", lambda driver, name: False)
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"


def test_no_contact_details_leave_the_api(client):
    """Contacts are stored but masked server-side (D7): no full email or phone appears in any response."""
    people = raw_seed()["people"]
    secrets = [p["email"] for p in people] + [p["phone"] for p in people]
    urls = ["/api/deal", "/api/milestones", "/api/approvals", "/api/activity", "/api/parties", "/api/progress",
            "/api/raci", "/api/checks", "/api/ownership", "/api/graph", "/api/impact?item=CP-CMA&delay_days=61"]
    urls += [f"/api/graph/node/{p['code']}" for p in people]
    for url in urls:
        body = client.get(url).text
        assert not [s for s in secrets if s in body], url
