"""Connection paths (/api/connections) against a breadth-first search over the raw YAML (no apex code):
every simple path of up to three relations through entities, under the same rules."""

import time
from itertools import product

import pytest
import yaml
from fastapi.testclient import TestClient

from apex import api
from apex.settings import load_settings
from oracle import raw_seed

SEED = raw_seed()
POLICY = yaml.safe_load(open("config/connection-policy.yaml"))
EVENTS = {"request", "response", "meeting", "delivery", "decision", "notice", "status-change"}
LETTER = {"A": "accountability", "R": "responsibility", "C": "consultation", "I": "notification"}
ITEMS = {i["code"]: i for i in SEED["items"]}


def relations(workstreams=False):
    """Every relation instance with at least two entity players: (type, {entity codes}). The deal itself,
    mandates and events are relations, so they are never players here."""
    out = [("acquisition", {SEED["deal"]["acquirer"], SEED["deal"]["target"]})]
    out += [("employment", {p["code"], p["employer"]}) for p in SEED["people"]]
    out += [("mandate", {m["client"], m["adviser"]}) for m in SEED["mandates"]]
    out += [("financing", {f["lender"], f["borrower"]}) for f in SEED["financing"]]
    out += [("authority-review", {r["reviewer"], r["subject"], r["applicant"]}) for r in SEED["reviews"]]
    out += [("ownership", {o["owner"], o["owned"]}) for o in SEED["ownership"]]
    out += [("bound-by", {b["obligor"], b["obligation"]}) for b in SEED["bound_by"]]
    out += [("consent-right", {c["grantor"], c["obligation"]}) for c in SEED["consents"]]
    out += [("deadline-limit", {l["item"], l["deadline"]}) for l in SEED["deadline_limits"]]
    out += [(d["kind"], {d["from"], d["to"]}) for d in SEED["dependencies"]]
    for item, letters in SEED["raci"].items():
        for letter, hs in letters.items():
            out += [(LETTER[letter], {h, item}) for h in ([hs] if isinstance(hs, str) else hs)]
    for r in SEED["risks"]:
        out.append(("risk-ownership", {r["owner"], r["code"]}))
        out += [("risk-exposure", {r["code"], x}) for x in r["exposed"]]
    for e in SEED["events"]:
        out.append((e["type"], {e["actor"], e["subject"], *e.get("recipients", []), *e.get("attendees", []), *e.get("agenda", [])}))
    if workstreams:
        out += [("work-breakdown", {i["code"], i["workstream"]}) for i in SEED["items"]]
    return [(t, players) for t, players in out if len(players) >= 2]


def oracle_paths(a, b, max_hops=3, activity=True, workstreams=False):
    """{(types, stones)} for every simple path a → b."""
    rels = list(enumerate(relations(workstreams)))
    touching = {}
    for i, (t, players) in rels:
        for x in players:
            touching.setdefault(x, []).append((i, t, players))
    found = []

    def allowed(t, hops):
        return t not in EVENTS or (activity and hops <= POLICY["event_max_hops"])

    def walk(node, used, seen, types, stones, hops):
        for i, t, players in touching.get(node, []):
            if i in used or not allowed(t, hops):
                continue
            for nxt in players - {node}:
                if nxt == b and len(types) + 1 == hops:
                    found.append((tuple(types + [t]), tuple(stones)))
                elif nxt not in seen and nxt != b and len(types) + 1 < hops:
                    walk(nxt, used | {i}, seen | {nxt}, types + [t], stones + [nxt], hops)
    for hops in range(1, max_hops + 1):
        walk(a, frozenset(), {a}, [], [], hops)
    return found


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


def get(client, a, b, **kw):
    r = client.get("/api/connections", params={"from": a, "to": b, "results": 50, **kw})
    assert r.status_code == 200, r.text
    return r.json()


PAIRS = [("PER-AINSWORTH", "ORG-CMA"), ("ORG-HELIX", "PER-OKAFOR"), ("PER-GRANT", "DL-LONGSTOP"), ("RK-HELIX", "ORG-ALB"),
         ("PER-HARTLEY", "ORG-HCJ"), ("PER-MENSAH", "CP-CMA"), ("ORG-CALDER", "PER-LINDQVIST"), ("AC-INT-INTERIM", "PER-DONNELLY")]


@pytest.mark.parametrize("a, b", PAIRS)
def test_every_path_matches_a_breadth_first_search(client, a, b):
    body = get(client, a, b)["data"]
    expected = oracle_paths(a, b)
    assert body["total"] == len(expected), (a, b)
    got = [(tuple(s["type"] for s in p["steps"]), tuple(n["code"] for n in p["nodes"][1:-1])) for p in body["paths"]]
    assert set(got) <= set(expected)
    assert [p["cost"] for p in body["paths"]] == sorted(p["cost"] for p in body["paths"])        # best first


@pytest.mark.parametrize("a, b", PAIRS[:4])
def test_options_change_the_search_exactly_as_stated(client, a, b):
    assert get(client, a, b, activity="false")["data"]["total"] == len(oracle_paths(a, b, activity=False))
    assert get(client, a, b, workstreams="true")["data"]["total"] == len(oracle_paths(a, b, workstreams=True))
    for hops in (1, 2):
        assert get(client, a, b, max_hops=hops)["data"]["total"] == len(oracle_paths(a, b, hops))


def test_paths_never_go_through_the_transaction_or_repeat_anything(client):
    for a, b in PAIRS:
        for p in get(client, a, b, workstreams="true")["data"]["paths"]:
            codes = [n["code"] for n in p["nodes"]]
            assert len(set(codes)) == len(codes)
            assert not any(n["code"].startswith(("DEAL-", "MAN-", "EV-")) for n in p["nodes"])       # relations, never stones
            assert all(s["type"] != "deal-scope" for s in p["steps"])
            if p["hops"] == 3:
                assert not any(s["type"] in EVENTS for s in p["steps"])                           # activity: two steps at most


def test_direct_and_indirect_are_distinguished_and_explained(client):
    body = get(client, "ORG-NOV", "PER-OKAFOR")["data"]
    first = body["paths"][0]
    assert first["direct"] and first["hops"] == 1 and first["steps"][0]["type"] == "employment"
    assert first["steps"][0]["sentence"] == "Daniel Okafor works for Novacore plc as Director, Corporate Development"
    assert all(not p["direct"] for p in body["paths"] if p["hops"] > 1)
    meaningful = get(client, "PER-AINSWORTH", "ORG-CMA")["data"]["paths"][0]
    assert [s["type"] for s in meaningful["steps"]] == ["employment", "mandate", "authority-review"]   # a standing chain first
    assert "applied for by Novacore plc" in meaningful["steps"][2]["sentence"]
    assert any("hub" in n for n in meaningful["notes"])                                            # through Novacore


def test_n_ary_relations_stay_whole(client):
    review = next(s for p in get(client, "PER-AINSWORTH", "ORG-CMA")["data"]["paths"] for s in p["steps"] if s["type"] == "authority-review")
    assert {o["role"] for o in review["others"]} | {review["from_role"], review["to_role"]} == {"reviewer", "subject", "applicant"}


def test_activity_records_are_labelled(client):
    body = get(client, "ORG-HELIX", "PER-OKAFOR")["data"]
    event = next(p for p in body["paths"] if any(s["type"] == "request" for s in p["steps"]))
    assert any("activity record" in n for n in event["notes"]) and "sent" in event["steps"][0]["sentence"]


def test_results_are_bounded_grouped_and_stable(client):
    small = client.get("/api/connections", params={"from": "ORG-NOV", "to": "PER-OKAFOR", "results": 5}).json()["data"]
    assert len(small["paths"]) == 5 and sum(g["count"] for g in small["more"]) == small["total"] - 5
    again = client.get("/api/connections", params={"from": "ORG-NOV", "to": "PER-OKAFOR", "results": 5}).json()["data"]
    assert [p["text"] for p in again["paths"]] == [p["text"] for p in small["paths"]]
    assert [d["hops"] for d in small["depths"]] == [1, 2, 3]


def test_fast_enough_on_the_full_dataset(client):
    for a, b in PAIRS + [("ORG-NOV", "PER-OKAFOR")]:
        t = time.perf_counter()
        get(client, a, b)
        assert (time.perf_counter() - t) * 1000 < 1000, (a, b)          # in practice 20–180 ms; generous for CI


def test_bad_requests_and_read_only(client):
    assert client.get("/api/connections", params={"from": "PER-NOPE", "to": "ORG-CMA"}).status_code == 404
    assert client.get("/api/connections", params={"from": "ORG-CMA", "to": "ORG-CMA"}).status_code == 422
    assert client.get("/api/connections", params={"from": "ORG-CMA", "to": "PER-GRANT", "max_hops": 4}).status_code == 422
    body = client.get("/api/connections", params={"from": "ORG-CMA", "to": "PER-GRANT"}).json()
    assert body["typeql"] and not any(w in q for q in body["typeql"] for w in ("insert", "delete", "define", "update"))
    assert any("entity $mt" in q for q in body["typeql"])
    nodes = client.get("/api/connectables").json()["data"]
    assert {n["kind"] for n in nodes} == {"person", "organisation", "item", "workstream", "risk"}
    assert not any(n["code"].startswith(("DEAL-", "MAN-", "EV-")) for n in nodes)
