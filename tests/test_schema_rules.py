"""The schema itself refuses bad data, whatever program writes it. Nothing here is ever saved."""

import pytest
from typedb.driver import TypeDBDriverException

from apex.db import WRITE


def _refused(driver, db, query):
    with pytest.raises(TypeDBDriverException) as error:
        with driver.transaction(db, WRITE) as tx:
            tx.query(query).resolve()
            tx.commit()
    return str(error.value)


def test_second_accountable_owner_is_refused(driver, seeded):
    message = _refused(driver, seeded, """
        match $i isa regulatory-clearance, has code "CP-CMA"; $p isa person, has code "PER-SHAW";
        insert accountability (holder: $p, accountable-item: $i);""")
    assert "CNT5" in message and "accountability:accountable-item" in message


def test_stake_over_100_percent_is_refused(driver, seeded):
    message = _refused(driver, seeded, """
        match $a isa company, has code "ORG-ATL"; $b isa company, has code "ORG-HELIX";
        insert ownership (owner: $a, owned: $b), has stake 1.5dec;""")
    assert "CNT7" in message


def test_unknown_status_word_is_refused(driver, seeded):
    message = _refused(driver, seeded, 'insert $m isa milestone, has code "MS-BAD", has status "nearly-done";')
    assert "@values" in message or "CNT" in message


def test_progress_over_100_is_refused(driver, seeded):
    assert "CNT7" in _refused(driver, seeded, 'insert $m isa milestone, has code "MS-BAD", has progress 150;')


def test_regulator_cannot_be_an_adviser(driver, seeded):
    message = _refused(driver, seeded, """
        match $c isa company, has code "ORG-NOV"; $r isa regulator, has code "ORG-CMA"; $d isa acquisition;
        insert mandate (client: $c, adviser: $r, engagement: $d);""")
    assert "INF" in message      # rejected by type checking, before any data is touched


def test_item_cannot_sit_in_two_workstreams(driver, seeded):
    message = _refused(driver, seeded, """
        match $i isa regulatory-clearance, has code "CP-CMA"; $w isa workstream, has code "WS-LEG";
        insert work-breakdown (whole: $w, part: $i);""")
    assert "CNT5" in message and "work-breakdown:part" in message


def test_codes_are_unique(driver, seeded):
    assert "CNT9" in _refused(driver, seeded, 'insert $m isa milestone, has code "CP-CMA";')


def test_attribute_on_the_wrong_type_is_refused(driver, seeded):
    """A person can't have a stake: only ownership relations own one."""
    assert "INF" in _refused(driver, seeded, 'insert $p isa person, has code "PER-X", has stake 0.5dec;')
