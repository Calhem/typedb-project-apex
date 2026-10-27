"""Creating and loading the database, and checking the loaded data in TypeDB itself."""

import copy
from datetime import date, timedelta

import pytest

from apex import database, queries
from apex import seed as seed_module
from apex.db import READ, WRITE, fetch, rows
from apex.seed import SeedError, expected_counts, read_seed


@pytest.fixture(scope="module")
def seed():
    return read_seed()


def test_every_expected_instance_arrived(driver, seeded, seed):
    expected = expected_counts(seed)
    with driver.transaction(seeded, READ) as tx:
        assert queries.counts_by_type(tx, sorted(expected)) == expected
    assert sum(expected.values()) == 1058          # 6A.2: 471 -> 1051; 6E: + 7 steering meetings


def test_each_item_has_exactly_one_accountable_in_typedb(driver, seeded):
    q = """match $x isa deal-item, has code $c; accountability (holder: $h, accountable-item: $x);
           reduce $n = count groupby $c;"""
    with driver.transaction(seeded, READ) as tx:
        result = {r.get("c").as_attribute().get_value(): r.get("n").get() for r in rows(tx, q)}
        total = queries.count_exact(tx, "workstream") + sum(
            queries.count_exact(tx, t) for t in ("activity", "milestone", "regulatory-clearance", "shareholder-approval",
                                                  "court-sanction", "covenant", "deadline"))
    assert len(result) == total == 85              # re-baselined in Phase 6A.2 (was 41)
    assert set(result.values()) == {1}


def test_hard_dependencies_have_no_cycles(driver, seeded):
    q = "match $x isa deal-item, has code $c; let $y in downstream($x); $y is $x; select $c;"
    with driver.transaction(seeded, READ) as tx:
        assert rows(tx, q) == []


def test_base_plan_dates_respect_every_hard_dependency(driver, seeded):
    q = """match $d isa hard-dependency (prerequisite: $a, dependent: $b), has lag-days $lag;
                 $a has code $ac, has due-date $ad; $b has code $bc, has due-date $bd;
           fetch { "from": $ac, "to": $bc, "from due": $ad, "to due": $bd, "lag": $lag };"""
    with driver.transaction(seeded, READ) as tx:
        edges = fetch(tx, q)
    assert len(edges) == 67                        # re-baselined in Phase 6A.2 (was 25)
    late = [e for e in edges
            if date.fromisoformat(e["to due"]) < date.fromisoformat(e["from due"]) + timedelta(days=e["lag"])]
    assert late == []


def test_base_plan_meets_every_deadline(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        items = queries.all_items(tx)
        limits = queries.all_deadline_limits(tx)
    assert len(limits) == 4
    for limit in limits:
        assert items[limit["item"]]["due"] <= limit["due"], limit


def test_data_quality_checks_are_clean_on_the_seed(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        assert queries.data_quality_checks(tx) == {
            "no_accountable": [], "overdue": [], "clearance_without_authority": []}


def test_responsibility_is_recorded_via_the_advisers_mandate(driver, seeded):
    q = """match $p isa person, has code "PER-AINSWORTH"; $r isa responsibility (holder: $p, via: $m);
           $m has code $mc; select $mc; distinct;"""
    with driver.transaction(seeded, READ) as tx:
        assert [r.get("mc").as_attribute().get_value() for r in rows(tx, q)] == ["MAN-BWM"]


def test_saved_functions_answer_on_the_seed(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        blockers = fetch(tx, """match $x isa milestone, has code "MS-COMPLETION"; let $y in upstream($x);
                                $y has code $c; fetch { "c": $c };""")
        owner = fetch(tx, """match $x isa regulatory-clearance, has code "CP-CMA"; let $p = accountable_for($x);
                             $p has name $n; fetch { "n": $n };""")
    assert {"CP-CMA", "CP-EC", "CP-HSR", "CP-NSIA", "CP-SHAREHOLDERS", "CP-COURT", "MS-ANNOUNCE"} <= {b["c"] for b in blockers}
    assert owner == [{"n": "Daniel Okafor"}]


def test_generated_seed_tql_loads_on_its_own_as_in_studio(driver, fresh_name, seed):
    database.create_schema(driver, fresh_name)
    with driver.transaction(fresh_name, WRITE) as tx:
        tx.query(seed_module.SEED_TQL.read_text()).resolve()
        tx.commit()
    expected = expected_counts(seed)
    with driver.transaction(fresh_name, READ) as tx:
        assert queries.counts_by_type(tx, sorted(expected)) == expected


def test_create_refuses_an_existing_database(driver, seeded):
    with pytest.raises(database.DatabaseExists):
        database.create(driver, seeded)


@pytest.mark.parametrize("name", ["newslens", "charcoal", "cti", "apex-production"])
def test_create_and_drop_refuse_other_projects_databases(driver, name):
    with pytest.raises(ValueError, match="refusing"):
        database.create(driver, name, replace=True)
    with pytest.raises(ValueError, match="refusing"):
        database.drop(driver, name)


def test_seed_failing_a_typedb_check_saves_nothing(driver, fresh_name, seed):
    """An overdue item passes the YAML validator but fails the data-quality check inside TypeDB."""
    broken = copy.deepcopy(seed)
    next(i for i in broken["items"] if i["code"] == "CP-CMA")["due"] = date(2026, 10, 1)
    with pytest.raises(SeedError, match="overdue failed for CP-CMA"):
        database.create(driver, fresh_name, seed=broken)
    assert not driver.databases.contains(fresh_name)


def test_count_mismatch_saves_nothing(driver, fresh_name, monkeypatch):
    """If anything failed to arrive (e.g. a relation silently dropped), the load is refused."""
    real = seed_module.expected_counts
    monkeypatch.setattr(seed_module, "expected_counts", lambda s: {**real(s), "person": real(s)["person"] + 1})
    with pytest.raises(SeedError, match="person: expected 46 in TypeDB, found 45"):   # re-baselined in 6A.2
        database.create(driver, fresh_name)
    assert not driver.databases.contains(fresh_name)


# ---- Phase 6A.2: the activity history and risk register, checked in TypeDB ----

def test_migrations_are_part_of_a_new_database(driver, seeded):
    with driver.transaction(seeded, READ) as tx:
        for t in ("activity", "request", "reply", "risk", "risk-exposure", "registrar"):
            assert rows(tx, f"match $t label {t};"), t


def test_event_types_arrive_exactly(driver, seeded, seed):
    types = ("request", "response", "meeting", "delivery", "decision", "notice", "status-change")
    with driver.transaction(seeded, READ) as tx:
        found = {t: queries.count_exact(tx, t) for t in types}
        total = len(rows(tx, "match $e isa deal-event;"))
    assert found == {t: sum(e["type"] == t for e in seed["events"]) for t in types}
    assert total == len(seed["events"]) == 118          # 6E: + 7 steering meetings


def test_every_reply_links_a_response_to_its_request(driver, seeded, seed):
    q = """match $q isa request, has code $qc; $a isa response, has code $ac;
           reply (original: $q, answer: $a); select $qc, $ac;"""
    with driver.transaction(seeded, READ) as tx:
        found = {(r.get("qc").as_attribute().get_value(), r.get("ac").as_attribute().get_value()) for r in rows(tx, q)}
    assert found == {(o, e["code"]) for e in seed["events"] for o in e.get("reply_to", [])}


def test_unanswered_requests_are_found_by_negation(driver, seeded, seed):
    q = "match $q isa request, has code $c; not { reply (original: $q); }; select $c;"
    with driver.transaction(seeded, READ) as tx:
        found = {r.get("c").as_attribute().get_value() for r in rows(tx, q)}
    answered = {o for e in seed["events"] for o in e.get("reply_to", [])}
    assert found == {e["code"] for e in seed["events"] if e["type"] == "request"} - answered
    assert {"EV-INT-05", "EV-PPL-04"} <= found               # the two deliberate overdue responses


def test_every_risk_has_one_owner_and_its_exposures(driver, seeded, seed):
    q = """match $r isa risk, has code $c; risk-ownership (risk-owner: $o, owned-risk: $r); $o has code $oc;
           select $c, $oc;"""
    with driver.transaction(seeded, READ) as tx:
        owners = {r.get("c").as_attribute().get_value(): r.get("oc").as_attribute().get_value() for r in rows(tx, q)}
        exposures = len(rows(tx, "match risk-exposure (risk: $r, exposed: $x);"))
    assert owners == {r["code"]: r["owner"] for r in seed["risks"]}
    assert exposures == sum(len(r["exposed"]) for r in seed["risks"])
