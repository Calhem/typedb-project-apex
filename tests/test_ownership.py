"""Effective ownership of the Atlas group, checked independently, and its guards.

The calculation (group_members) is only correct without ownership cycles. These tests prove the
guards catch cycles and over-100% holdings, and that Apex then refuses to give figures at all,
rather than returning plausible-looking wrong totals.
"""

import copy
from decimal import Decimal

import pytest

import oracle
from apex import database, queries
from apex.db import READ, WRITE, fetch
from apex.queries import OwnershipDataError
from apex.seed import SeedError, read_seed

CYCLE = """match $us isa company, has code "ORG-ATL-US"; $atl isa company, has code "ORG-ATL";
           insert ownership (owner: $us, owned: $atl), has stake 0.05dec;"""
OVER = """match $cal isa company, has code "ORG-CALDER"; $de isa company, has code "ORG-ATL-DE";
          insert ownership (owner: $cal, owned: $de), has stake 0.2dec;"""


def test_effective_stakes_match_an_independent_route_by_route_calculation(driver, seeded):
    expected = oracle.effective_stakes(oracle.raw_seed(), "ORG-ATL")
    with driver.transaction(seeded, READ) as tx:
        actual = queries.effective_ownership(tx, "ORG-ATL")
    assert {c: (v["stake"], v["routes"]) for c, v in actual.items()} == expected


def test_answer_key_including_two_routes(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        actual = queries.effective_ownership(tx, "ORG-ATL")
    assert actual["ORG-ATL-IP"]["stake"] == Decimal("0.9") and actual["ORG-ATL-IP"]["routes"] == 2
    assert actual["ORG-ATL-JV"]["stake"] == Decimal("0.6") and actual["ORG-ATL-JV"]["routes"] == 1
    for code in ("ORG-ATL-UK", "ORG-ATL-US", "ORG-ATL-IE", "ORG-ATL-DE"):
        assert actual[code]["stake"] == Decimal("1") and actual[code]["routes"] == 1
    assert set(actual) == {"ORG-ATL-UK", "ORG-ATL-US", "ORG-ATL-IE", "ORG-ATL-DE", "ORG-ATL-IP", "ORG-ATL-JV"}


def test_a_single_route_would_give_the_wrong_answer():
    """Guards the test above: 90% only comes out if BOTH routes are added."""
    routes = oracle.ownership_routes(oracle.raw_seed(), "ORG-ATL")["ORG-ATL-IP"]
    assert sorted(routes) == [Decimal("0.2"), Decimal("0.7")]


def test_guards_find_nothing_on_the_seed(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        assert queries.ownership_problems(tx) == {"cycles": [], "over_owned": {}}


def test_a_cycle_is_detected_and_no_figures_are_returned(driver, seeded):
    with driver.transaction(seeded, WRITE) as tx:   # never committed
        tx.query(CYCLE).resolve()
        assert queries.ownership_problems(tx)["cycles"] == ["ORG-ATL", "ORG-ATL-UK", "ORG-ATL-US"]
        with pytest.raises(OwnershipDataError, match="ownership cycle"):
            queries.effective_ownership(tx, "ORG-ATL")
        with pytest.raises(OwnershipDataError):
            queries.group_consents(tx, "ORG-ATL")


def test_why_the_guard_exists_unguarded_totals_are_misleading(driver, seeded):
    """Without the guard, a 5% cross-holding makes the recursion report more than 100%."""
    with driver.transaction(seeded, WRITE) as tx:   # never committed
        tx.query(CYCLE).resolve()
        raw = {d["company"]: d for d in fetch(tx, queries.effective_ownership_query("ORG-ATL"))}
    assert Decimal(str(raw["ORG-ATL-UK"]["effective stake"]).removesuffix("dec")) > 1


def test_over_100_percent_is_detected_and_no_figures_are_returned(driver, seeded):
    with driver.transaction(seeded, WRITE) as tx:   # never committed
        tx.query(OVER).resolve()
        assert queries.ownership_problems(tx)["over_owned"] == {"ORG-ATL-DE": Decimal("1.2")}
        with pytest.raises(OwnershipDataError, match="more than 100% owned"):
            queries.effective_ownership(tx, "ORG-ATL")


def test_loader_refuses_a_cross_shareholding(driver, fresh_name):
    seed = copy.deepcopy(read_seed())
    seed["ownership"].append({"owner": "ORG-ATL-US", "owned": "ORG-ATL", "stake": "0.05"})
    with pytest.raises(SeedError, match="cross-shareholdings are rejected"):
        database.create(driver, fresh_name, seed=seed)
    assert not driver.databases.contains(fresh_name)


def test_loader_refuses_over_100_percent(driver, fresh_name):
    seed = copy.deepcopy(read_seed())
    seed["ownership"].append({"owner": "ORG-CALDER", "owned": "ORG-ATL-DE", "stake": "0.2"})
    with pytest.raises(SeedError, match="ORG-ATL-DE is more than 100% owned"):
        database.create(driver, fresh_name, seed=seed)
    assert not driver.databases.contains(fresh_name)


def test_oracle_itself_notices_cycles():
    seed = copy.deepcopy(oracle.raw_seed())
    seed["ownership"].append({"owner": "ORG-ATL-US", "owned": "ORG-ATL", "stake": "0.05"})
    with pytest.raises(ValueError, match="cycle"):
        oracle.ownership_routes(seed, "ORG-ATL")


def test_change_of_control_consents_inside_the_atlas_group(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        consents = queries.group_consents(tx, "ORG-ATL")
    assert consents == [
        {"obligation": "CV-COC-CALDER", "owed_by": "ORG-ATL-IP", "consent_from": "ORG-CALDER", "stake": Decimal("0.9")},
        {"obligation": "CV-COC-HELIX", "owed_by": "ORG-ATL-IE", "consent_from": "ORG-HELIX", "stake": Decimal("1")},
        {"obligation": "DL-HELIX-WAIVER", "owed_by": "ORG-ATL-IE", "consent_from": "ORG-HELIX", "stake": Decimal("1")},
    ]
