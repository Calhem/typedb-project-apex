"""The Impact Explorer's projections, checked against an independent calculation and the answer
key in docs/dataset.md."""

from datetime import date

import pytest

import oracle
from apex import impact
from apex.db import READ

ANSWER_KEY_DELAYS = [0, 1, 28, 60, 61, 119, 120, 135, 136, 149, 150, 168, 365]


@pytest.fixture(scope="module")
def projections(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        return {d: impact.project(tx, "CP-CMA", d) for d in ANSWER_KEY_DELAYS}


def breaches(result):
    return {(d["deadline"], d["item"]): d["days_over"] for d in result["deadlines"] if d["breached"]}


@pytest.mark.parametrize("delay", ANSWER_KEY_DELAYS)
def test_every_projected_date_matches_the_independent_calculation(projections, delay):
    seed = oracle.raw_seed()
    expected = oracle.projected_dates(seed, "CP-CMA", delay)
    result = projections[delay]
    for code, item in result["items"].items():
        assert item["projected"] == expected[code], code
    planned = {i["code"]: i["due"] for i in seed["items"] + seed["workstreams"]}
    moved_by_oracle = {c for c in expected if expected[c] != planned[c]}
    assert moved_by_oracle | {"CP-CMA"} == set(result["items"])
    assert breaches(result) == oracle.breached_deadlines(seed, expected)


def test_no_delay_moves_nothing(projections):
    assert set(projections[0]["items"]) == {"CP-CMA"}
    assert breaches(projections[0]) == {}


def test_28_days(projections):
    r = projections[28]
    assert r["items"]["MS-COMPLETION"]["projected"] == date(2026, 12, 30)
    assert r["items"]["OB-SETTLE"]["projected"] == date(2027, 1, 13)
    assert len(r["items"]) - 1 == 14                         # 14 downstream items move
    assert {i["workstream"] for c, i in r["items"].items() if c != "CP-CMA"} == {
        "WS-LEG", "WS-FIN", "WS-INT", "WS-PPL", "WS-COM"}   # in 5 other workstreams
    assert breaches(r) == {}


def test_helix_waiver_lapses_from_61_days(projections):
    assert ("DL-HELIX-WAIVER", "MS-COMPLETION") not in breaches(projections[60])
    assert breaches(projections[61]) == {("DL-HELIX-WAIVER", "MS-COMPLETION"): 1}


def test_long_stop_breached_from_120_days(projections):
    assert ("DL-LONGSTOP", "MS-COMPLETION") not in breaches(projections[119])
    assert breaches(projections[120])[("DL-LONGSTOP", "MS-COMPLETION")] == 1


def test_certain_funds_breached_by_settlement_from_136_days(projections):
    assert ("DL-FUNDS", "OB-SETTLE") not in breaches(projections[135])
    assert breaches(projections[136])[("DL-FUNDS", "OB-SETTLE")] == 1
    assert ("DL-FUNDS", "MS-FUNDS") not in breaches(projections[149])
    assert breaches(projections[150])[("DL-FUNDS", "MS-FUNDS")] == 1


def test_phase_2_reference_168_days(projections):
    r = projections[168]
    assert r["items"]["MS-COMPLETION"]["projected"] == date(2027, 5, 19)
    assert r["items"]["CV-RETENTION"]["projected"] == date(2027, 11, 17)
    assert breaches(r) == {("DL-HELIX-WAIVER", "MS-COMPLETION"): 108, ("DL-LONGSTOP", "MS-COMPLETION"): 49,
                           ("DL-FUNDS", "MS-FUNDS"): 19, ("DL-FUNDS", "OB-SETTLE"): 33}


def test_breached_deadline_carries_its_consequence_and_who_is_bound(projections):
    helix = next(d for d in projections[61]["deadlines"] if d["deadline"] == "DL-HELIX-WAIVER")
    assert helix["bound"] == ["ORG-ATL-IE"] and "call option revives" in helix["consequence"]


def test_cause_chain_is_reported(projections):
    items = projections[28]["items"]
    assert items["CP-COURT"]["caused_by"] == ["CP-CMA"]
    assert items["MS-COMPLETION"]["caused_by"] == ["CP-COURT"]
    assert items["OB-SETTLE"]["caused_by"] == ["MS-COMPLETION"]   # 14 days after completion beats drawdown + 0


def test_soft_links_never_move_dates(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        r = impact.project(tx, "HR-CONSULT", 60)       # only "informs" Day 1
    assert set(r["items"]) == {"HR-CONSULT"}


def test_a_completed_item_cannot_be_delayed(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        r = impact.project(tx, "CP-EC", 30)
    assert r["items"]["CP-EC"]["shift_days"] == 0 and set(r["items"]) == {"CP-EC"}


def test_unknown_item_and_bad_delay_are_refused(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        with pytest.raises(KeyError):
            impact.project(tx, "CP-NOPE", 10)
        with pytest.raises(ValueError):
            impact.project(tx, "CP-CMA", 400)


def test_propagate_takes_the_latest_of_several_prerequisites():
    planned = {"A": date(2027, 1, 1), "B": date(2027, 1, 1), "C": date(2027, 1, 10)}
    edges = [{"from": "A", "to": "C", "lag": 5}, {"from": "B", "to": "C", "lag": 9}]
    assert impact.propagate("A", 10, edges, planned)["C"] == (date(2027, 1, 16), ["A"])
    assert impact.propagate("A", 2, edges, planned)["C"] == (date(2027, 1, 10), [])   # slack absorbs it


def test_propagate_refuses_a_dependency_cycle():
    planned = {"A": date(2027, 1, 1), "B": date(2027, 1, 2)}
    edges = [{"from": "A", "to": "B", "lag": 1}, {"from": "B", "to": "A", "lag": 1}]
    with pytest.raises(ValueError, match="cycle"):
        impact.propagate("A", 1, edges, planned)
