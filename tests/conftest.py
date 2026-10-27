"""Shared test setup. Tests use throwaway apex-test-* databases only: never `apex` or `apex-demo`.

Needs Apex's TypeDB server running (docker compose up -d).
"""

import secrets

import pytest

from apex import database
from apex.db import connect
from apex.settings import load_settings


@pytest.fixture(scope="session")
def driver():
    d = connect(load_settings())
    yield d
    d.close()


def _new_name():
    return f"apex-test-{secrets.token_hex(4)}"


@pytest.fixture(scope="session")
def seeded(driver):
    """One fully seeded database shared by tests that only READ (or that never commit)."""
    name = _new_name()
    database.create(driver, name)
    yield name
    database.drop(driver, name)


@pytest.fixture
def fresh_name(driver):
    """A database name for a test to create itself; removed afterwards if it exists."""
    name = _new_name()
    yield name
    database.drop(driver, name)


@pytest.fixture
def fresh_seeded(driver, fresh_name):
    """A seeded database of the test's own, for tests that commit changes."""
    database.create(driver, fresh_name)
    return fresh_name
