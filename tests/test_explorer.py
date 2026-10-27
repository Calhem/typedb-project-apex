"""Relationship Explorer API, against a real seeded TypeDB database.

Expected graphs are rebuilt from data/seed/*.yaml and the type hierarchy from schema/schema.tql
text, sharing no code with apex/, so a wrong query can't pass by agreeing with itself.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apex import api
from apex.db import WRITE
from apex.settings import load_settings
from oracle import schema_text, effective_stakes, ownership_routes, raw_seed

SEED = raw_seed()
ORGS = {o["code"]: o for o in SEED["organisations"]}
DEAL = SEED["deal"]
SCHEMA = schema_text()   # base schema plus migrations, as TypeDB loads it


def schema_parents():
    """{type: parent} for the party types (people and organisations), parsed from the schema text."""
    parents = dict(re.findall(r"entity ([\w-]+)(?: @abstract,)? sub ([\w-]+)", SCHEMA))

    def is_party(t):
        while t in parents:
            t = parents[t]
        return t == "party"
    return {t: p for t, p in parents.items() if is_party(t)}


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


def get(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def relation_set(relations):
    return {(r["type"], frozenset((p["role"], p["code"]) for p in r["players"])) for r in relations}


def expected_relations():
    """Every relation in the seed linking two or more organisations, with all players and roles."""
    out = {("acquisition", frozenset({("acquirer", DEAL["acquirer"]), ("target", DEAL["target"])}))}
    for m in SEED["mandates"]:
        out.add(("mandate", frozenset({("client", m["client"]), ("adviser", m["adviser"]), ("engagement", DEAL["code"])})))
    for f in SEED["financing"]:
        out.add(("financing", frozenset({("borrower", f["borrower"]), ("lender", f["lender"]), ("purpose", DEAL["code"])})))
    for rv in SEED["reviews"]:
        out.add(("authority-review", frozenset({("reviewer", rv["reviewer"]), ("subject", rv["subject"]),
                                                ("applicant", rv["applicant"])})))
    for o in SEED["ownership"]:
        out.add(("ownership", frozenset({("owner", o["owner"]), ("owned", o["owned"])})))
    return out


# ---- the graph -------------------------------------------------------------------------------

def test_graph_nodes_are_every_organisation_with_its_type_path(client):
    data = get(client, "/api/graph")["data"]
    parents = schema_parents()
    assert {n["code"] for n in data["nodes"]} == set(ORGS)
    for n in data["nodes"]:
        assert n["type"] == ORGS[n["code"]]["type"] and n["name"] == ORGS[n["code"]]["name"]
        path = [n["type"]]
        while path[-1] in parents:
            path.append(parents[path[-1]])
        assert n["type_path"] == path
        assert n["people"] == sum(p["employer"] == n["code"] for p in SEED["people"])
    assert data["types"]["parties"] == parents


def test_graph_relations_keep_every_player_and_role(client):
    """n-ary relations come back whole: a mandate has its client, adviser AND the deal it is for."""
    data = get(client, "/api/graph")["data"]
    assert relation_set(data["relations"]) == expected_relations()
    review = next(r for r in data["relations"] if r["type"] == "authority-review"
                  and any(p["code"] == "ORG-CMA" for p in r["players"]))
    assert {p["role"]: p["code"] for p in review["players"]} == {"reviewer": "ORG-CMA", "subject": "CP-CMA",
                                                                  "applicant": "ORG-NOV"}
    mandate = next(r for r in data["relations"] if r["key"] == "MAN-BWM")
    assert {p["role"]: p["kind"] for p in mandate["players"]}["engagement"] == "relation"   # the deal is a relation
    assert mandate["attributes"]["mandate-type"] == "Legal and competition counsel"
    stakes = {(next(p["code"] for p in r["players"] if p["role"] == "owner"),
               next(p["code"] for p in r["players"] if p["role"] == "owned")): Decimal(r["attributes"]["stake"])
              for r in data["relations"] if r["type"] == "ownership"}
    assert stakes == {(o["owner"], o["owned"]): Decimal(o["stake"]) for o in SEED["ownership"]}
    assert data["types"]["relations"]["mandate"]["roles"] == ["adviser", "client", "engagement"]
    assert data["types"]["relations"]["acquisition"] == {"parent": "transaction", "roles": ["acquirer", "target"]}


def test_graph_clusters(client):
    nodes = {n["code"]: n["cluster"] for n in get(client, "/api/graph")["data"]["nodes"]}
    group = set(effective_stakes(SEED, DEAL["target"]))
    assert nodes[DEAL["acquirer"]] == "acquirer" and nodes[DEAL["target"]] == "target"
    assert {c for c, k in nodes.items() if k == "target-group"} == group
    assert {c for c, k in nodes.items() if k == "outside-shareholders"} == {
        o["owner"] for o in SEED["ownership"] if o["owned"] in group and o["owner"] not in group | {DEAL["target"]}}
    for m in SEED["mandates"]:
        assert nodes[m["adviser"]] == f"advisers:{m['client']}"
    assert {c for c, k in nodes.items() if k == "lenders"} == {f["lender"] for f in SEED["financing"]}
    assert {c for c, k in nodes.items() if k == "authorities"} == {c for c, o in ORGS.items() if o["type"] in ("regulator", "court")}
    assert "other" not in nodes.values()


# ---- one node --------------------------------------------------------------------------------

def _letters(holder):
    out = set()
    for item, letters in SEED["raci"].items():
        for letter, hs in letters.items():
            if holder in ([hs] if isinstance(hs, str) else hs):
                out.add((letter, item))
    return out


def test_node_detail_for_an_adviser(client):
    body = get(client, "/api/graph/node/ORG-BWM")
    d = body["data"]
    assert (d["name"], d["type"]) == ("Blackwood Marsh LLP", "law-firm")
    mandate = next(r for r in d["relations"] if r["type"] == "mandate")
    assert mandate["role"] == "adviser"
    assert {(o["role"], o["code"]) for o in mandate["others"]} == {("client", "ORG-NOV"), ("engagement", "DEAL-APEX")}
    staff = [p for p in SEED["people"] if p["employer"] == "ORG-BWM"]
    assert sorted(p["code"] for p in d["people"]) == sorted(p["code"] for p in staff)
    for p in d["people"]:
        assert {(r["letter"], r["item"]["code"]) for r in p["raci"]} == _letters(p["code"])
        assert {r["via"] for r in p["raci"]} == {"MAN-BWM"}
    assert body["parameters"] == {"code": "ORG-BWM"} and all('"ORG-BWM"' in q for q in body["typeql"])


def test_node_detail_for_a_person_and_an_authority(client):
    shaw = get(client, "/api/graph/node/PER-SHAW")["data"]
    assert shaw["type"] == "person" and shaw["people"] == []
    assert {(r["letter"], r["item"]["code"]) for r in shaw["raci"]} == _letters("PER-SHAW")
    event_types = {"deal-event", "response", "meeting", "delivery", "decision", "request", "notice", "status-change"}
    assert not {r["type"] for r in shaw["relations"]} & event_types     # history belongs to the Parties page, not here
    cma = get(client, "/api/graph/node/ORG-CMA")["data"]
    review = next(r for r in cma["relations"] if r["type"] == "authority-review")
    assert review["role"] == "reviewer"
    assert {(o["role"], o["code"]) for o in review["others"]} == {("subject", "CP-CMA"), ("applicant", "ORG-NOV")}
    helix = get(client, "/api/graph/node/ORG-HELIX")["data"]
    assert {r["type"] for r in helix["relations"]} >= {"ownership", "consent-right"}
    assert client.get("/api/graph/node/ORG-NOPE").status_code == 404
    assert client.get("/api/graph/node/nope").status_code == 422


# ---- group ownership -------------------------------------------------------------------------

def test_ownership_defaults_to_the_target_and_matches_the_oracle(client):
    body = get(client, "/api/ownership")
    data = body["data"]
    assert data["parent"] == DEAL["target"] and body["parameters"] == {}
    stakes = effective_stakes(SEED, DEAL["target"])
    routes = ownership_routes(SEED, DEAL["target"])
    assert {c["code"]: (Decimal(c["stake"]), c["routes"]) for c in data["companies"]} == stakes
    for c in data["companies"]:
        assert sorted(Decimal(p["stake"]) for p in c["paths"]) == sorted(routes[c["code"]])
        for p in c["paths"]:
            assert p["steps"][0]["owner"] == DEAL["target"] and p["steps"][-1]["owned"] == c["code"]
            product = Decimal(1)
            for s in p["steps"]:
                product *= Decimal(s["stake"])
            assert product == Decimal(p["stake"])


def test_ownership_headline_figures(client):
    """The two figures the demo relies on: 90% of Atlas IP Holdings via two routes, 60% of the JV."""
    companies = {c["code"]: c for c in get(client, "/api/ownership")["data"]["companies"]}
    ip = companies["ORG-ATL-IP"]
    assert (Decimal(ip["stake"]), ip["routes"]) == (Decimal("0.9"), 2)
    assert sorted(Decimal(p["stake"]) for p in ip["paths"]) == [Decimal("0.2"), Decimal("0.7")]
    assert sorted(len(p["steps"]) for p in ip["paths"]) == [2, 3]
    jv = companies["ORG-ATL-JV"]
    assert (Decimal(jv["stake"]), jv["routes"]) == (Decimal("0.6"), 1)
    assert [s["owned"] for s in jv["paths"][0]["steps"]] == ["ORG-ATL-UK", "ORG-ATL-IE", "ORG-ATL-JV"]


def test_ownership_direct_stakes_and_outside_shareholders(client):
    data = get(client, "/api/ownership")["data"]
    group = set(effective_stakes(SEED, DEAL["target"]))
    expected = {(o["owner"], o["owned"], Decimal(o["stake"])) for o in SEED["ownership"] if o["owned"] in group}
    assert {(d["owner"], d["owned"], Decimal(d["stake"])) for d in data["direct"]} == expected
    assert [o["code"] for o in data["outside_shareholders"]] == ["ORG-CALDER", "ORG-HELIX"]
    assert {d["owner"] for d in data["direct"] if not d["inside_group"]} == {"ORG-CALDER", "ORG-HELIX"}
    typeql = get(client, "/api/ownership")["typeql"]
    guards = [i for i, q in enumerate(typeql) if "ownership_cycles()" in q or "> 1.0dec" in q]
    figures = [i for i, q in enumerate(typeql) if "group_members(" in q]
    assert len(guards) >= 2 and guards[1] < min(figures)   # both guards run before any figure


# ---- guards stay active ------------------------------------------------------------------------

def _write(driver, db, query):
    with driver.transaction(db, WRITE) as tx:
        tx.query(query).resolve()
        tx.commit()


def test_cycle_blocks_ownership_figures_but_not_the_graph(driver, fresh_seeded):
    _write(driver, fresh_seeded, """
        match $a isa company, has code "ORG-ATL-JV"; $b isa company, has code "ORG-ATL-UK";
        insert ownership (owner: $a, owned: $b), has stake 0.05dec;""")
    with TestClient(api.create_app(load_settings(apex_database=fresh_seeded))) as c:
        own = c.get("/api/ownership")
        graph = c.get("/api/graph")
    assert own.status_code == 409 and "data" not in own.json() and own.json()["cycles"]
    assert graph.status_code == 200    # the network itself has no percentages to get wrong
    assert len([r for r in graph.json()["data"]["relations"] if r["type"] == "ownership"]) == len(SEED["ownership"]) + 1


def test_over_owned_company_blocks_ownership_figures(driver, fresh_seeded):
    _write(driver, fresh_seeded, """
        match $a isa company, has code "ORG-HELIX"; $b isa company, has code "ORG-ATL-IP";
        insert ownership (owner: $a, owned: $b), has stake 0.3dec;""")
    with TestClient(api.create_app(load_settings(apex_database=fresh_seeded))) as c:
        r = c.get("/api/ownership")
    assert r.status_code == 409 and r.json()["over_owned"] == {"ORG-ATL-IP": "1.3"}
