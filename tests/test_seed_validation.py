"""The seed data and its validator, without TypeDB."""

import copy
from decimal import Decimal

import pytest

from apex import seed as seed_module
from apex.seed import SeedError, read_seed, render, validate
from apex.settings import is_apex_database


@pytest.fixture(scope="module")
def seed():
    return read_seed()


def test_seed_is_valid(seed):
    validate(seed)


def test_generated_seed_tql_is_up_to_date(seed):
    assert seed_module.SEED_TQL.read_text() == render(seed), (
        "data/seed.tql is out of date: run uv run python -m scripts.export_seed_tql")


def test_every_item_and_workstream_has_exactly_one_accountable(seed):
    codes = {i["code"] for i in seed["items"]} | {w["code"] for w in seed["workstreams"]}
    assert set(seed["raci"]) == codes
    assert all(isinstance(letters["A"], str) for letters in seed["raci"].values())


def _broken(seed, change):
    s = copy.deepcopy(seed)
    change(s)
    with pytest.raises(SeedError) as error:
        validate(s)
    return str(error.value)


def test_missing_accountable_is_refused(seed):
    assert "exactly one Accountable" in _broken(seed, lambda s: s["raci"]["CP-CMA"].pop("A"))


def test_unknown_reference_is_refused(seed):
    assert "unknown item MS-NOPE" in _broken(
        seed, lambda s: s["dependencies"].append({"kind": "gating", "from": "CP-CMA", "to": "MS-NOPE", "lag": 1}))


def test_duplicate_code_is_refused(seed):
    assert "is used 2 times" in _broken(seed, lambda s: s["people"].append(dict(s["people"][0])))


def test_stake_must_be_an_exact_string(seed):
    def change(s):
        s["ownership"][0]["stake"] = 0.7
    assert "quoted string" in _broken(seed, change)


def test_wrong_kind_of_party_is_refused(seed):
    def change(s):
        s["mandates"][0]["adviser"] = "ORG-CMA"
    assert "is a regulator" in _broken(seed, change)


def test_deadline_needs_a_consequence(seed):
    def change(s):
        next(i for i in s["items"] if i["code"] == "DL-LONGSTOP").pop("consequence")
    assert "needs a consequence" in _broken(seed, change)


def test_decimal_literals_are_exact():
    from apex.db import decimal
    assert decimal(Decimal("0.7")) == "0.7dec"
    assert decimal(Decimal("2400000000")) == "2400000000.0dec"


def test_only_apex_databases_can_be_managed():
    assert is_apex_database("apex") and is_apex_database("apex-demo") and is_apex_database("apex-test-1a2b")
    for other in ("newslens", "charcoal", "cti", "apex-prod", "apex-test-", "APEX"):
        assert not is_apex_database(other)


def test_advisers_people_hold_letters_via_their_mandate(seed):
    assert seed_module.via_mandate(seed, "PER-AINSWORTH") == "MAN-BWM"
    assert seed_module.via_mandate(seed, "PER-OKAFOR") is None        # works for the client
    assert seed_module.via_mandate(seed, "PER-HOLT") is None          # lender: financing, not a mandate


# ---- Phase 6A.2: one test per new validation rule ----

def _item(s, code):
    return next(i for i in s["items"] if i["code"] == code)


def _event(s, code):
    return next(e for e in s["events"] if e["code"] == code)


def test_every_person_has_fictional_contacts(seed):
    emails = [p["email"] for p in seed["people"]]
    phones = [p["phone"] for p in seed["people"]]
    assert len(set(emails)) == len(set(phones)) == len(seed["people"]) == 45
    assert all(e.endswith(".example") for e in emails)                # reserved domain: never deliverable
    assert all(p.startswith("07700 900") for p in phones)              # Ofcom's range reserved for drama


def test_bad_email_is_refused(seed):
    assert "email must be" in _broken(seed, lambda s: s["people"][0].update(email="e.hartley@novacore.com"))


def test_bad_phone_is_refused(seed):
    assert "phone must be" in _broken(seed, lambda s: s["people"][0].update(phone="020 7946 0000"))


def test_shared_phone_is_refused(seed):
    assert "used by more than one person" in _broken(
        seed, lambda s: s["people"][1].update(phone=s["people"][0]["phone"]))


def test_stored_workstream_progress_is_refused(seed):
    assert "must not be stored" in _broken(seed, lambda s: s["workstreams"][0].update(progress=50))


def test_activity_needs_a_planned_start(seed):
    assert "needs a planned_start" in _broken(seed, lambda s: _item(s, "AC-DD-VDR").pop("planned_start"))


def test_start_after_due_is_refused(seed):
    from datetime import timedelta

    def change(s):
        it = _item(s, "AC-DD-VDR")
        it["planned_start"] = it["due"] + timedelta(days=1)
    assert "is after its due date" in _broken(seed, change)


def test_event_after_the_scenario_date_is_refused(seed):
    from datetime import datetime
    assert "after the scenario date" in _broken(
        seed, lambda s: _event(s, "EV-DD-03").update(at=datetime(2027, 1, 5, 9, 0)))


def test_actor_without_a_role_is_refused(seed):
    assert "has no role in" in _broken(seed, lambda s: _event(s, "EV-DD-03").update(actor="PER-MENSAH"))


def test_response_before_its_request_is_refused(seed):
    from datetime import datetime
    assert "before it was asked" in _broken(
        seed, lambda s: _event(s, "EV-DD-04").update(at=datetime(2026, 5, 20, 9, 0)))


def test_responder_who_was_not_asked_is_refused(seed):
    assert "was not one of its recipients" in _broken(
        seed, lambda s: _event(s, "EV-DD-03").update(recipients=["PER-AINSWORTH"]))


def test_reply_to_something_other_than_a_request_is_refused(seed):
    assert "which is not a request" in _broken(seed, lambda s: _event(s, "EV-DD-04").update(reply_to=["EV-DD-02"]))


def test_response_due_only_on_requests(seed):
    assert "response_due is for requests" in _broken(
        seed, lambda s: _event(s, "EV-DD-02").update(response_due=_event(s, "EV-DD-03")["response_due"]))


def test_meeting_without_attendees_is_refused(seed):
    assert "meetings have attendees" in _broken(seed, lambda s: _event(s, "EV-DD-02").pop("attendees"))


def test_status_change_contradicting_the_item_is_refused(seed):
    assert "the latest status change on AC-REG-CMA-RESP" in _broken(
        seed, lambda s: _event(s, "EV-REG-16").update(new_status="complete"))


def test_risk_needs_valid_fields_and_a_known_owner(seed):
    assert "severity must be one of" in _broken(seed, lambda s: s["risks"][0].update(severity="extreme"))
    assert "unknown owner" in _broken(seed, lambda s: s["risks"][0].update(owner="PER-NOBODY"))
    assert "at least one known work item" in _broken(seed, lambda s: s["risks"][0].update(exposed=[]))


def test_migrations_are_numbered_once_and_in_order():
    from apex.database import migration_files
    names = [f.name for f in migration_files()]
    assert names == sorted(names) and names[0].startswith("001-")


def test_text_split_by_an_unquoted_comma_is_refused(seed):
    def change(s):
        _item(s, "CV-COC-CALDER")["received unconditionally."] = None     # what YAML makes of an unquoted comma
    assert "quote any text that contains a comma" in _broken(seed, change)


def test_agenda_rules(seed):
    assert "only meetings have an agenda" in _broken(seed, lambda s: _event(s, "EV-DD-04").update(agenda=["MS-DD"]))
    assert "unknown agenda item" in _broken(seed, lambda s: _event(s, "EV-LEG-22").update(agenda=["AC-NOPE"]))
    assert "is not in WS-LEG" in _broken(seed, lambda s: _event(s, "EV-LEG-22").update(agenda=["CP-CMA"]))
