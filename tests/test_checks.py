"""The three Overview data-quality checks fire on planted problems (never committed)."""

from apex import queries
from apex.db import WRITE

PLANTED = """
insert
  $m isa milestone, has code "MS-PLANTED", has name "Planted problem", has status "in-progress",
     has due-date 2026-10-01;
  $c isa regulatory-clearance, has code "CP-PLANTED", has name "Planted clearance", has status "not-started",
     has due-date 2026-12-31;
"""


def test_each_check_finds_exactly_its_planted_problem(driver, seeded):
    with driver.transaction(seeded, WRITE) as tx:   # never committed
        tx.query(PLANTED).resolve()
        found = queries.data_quality_checks(tx)
    assert found == {
        "no_accountable": ["CP-PLANTED", "MS-PLANTED"],
        "overdue": ["MS-PLANTED"],                     # due before 26 Oct 2026 and not complete
        "clearance_without_authority": ["CP-PLANTED"],
    }


def test_planted_problems_were_not_saved(driver, seeded):
    from apex.db import READ
    with driver.transaction(seeded, READ) as tx:
        assert all(codes == [] for codes in queries.data_quality_checks(tx).values())
