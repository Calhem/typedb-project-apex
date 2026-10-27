"""Creating Apex databases: schema, functions and seed data, from scratch.

Only Apex's own databases can be created or replaced (apex, apex-demo, apex-test-*); anything
else on the server is refused, so a typo can never delete someone else's data.
"""

from pathlib import Path

from apex import seed as seed_module
from apex.db import SCHEMA, run_file
from apex.settings import is_apex_database

ROOT = Path(__file__).resolve().parent.parent
BASE_SCHEMA = (ROOT / "schema" / "schema.tql", ROOT / "schema" / "functions.tql")
MIGRATIONS_DIR = ROOT / "schema" / "migrations"


def migration_files():
    """schema/migrations/NNN-*.tql in number order. Approved schema files are never edited: changes are
    new migrations, applied after the base schema whenever a database is built."""
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]-*.tql"))
    numbers = [f.name[:3] for f in files]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"two migrations share a number: {numbers}")
    return tuple(files)


SCHEMA_FILES = BASE_SCHEMA + migration_files()


class DatabaseExists(Exception):
    pass


def _check_name(name):
    if not is_apex_database(name):
        raise ValueError(f"refusing to touch database {name!r}: Apex only manages apex, apex-demo and apex-test-*")


def drop(driver, name):
    _check_name(name)
    if driver.databases.contains(name):
        driver.databases.get(name).delete()


def create_schema(driver, name):
    """Create an empty database with the schema and functions (no data)."""
    _check_name(name)
    if driver.databases.contains(name):
        raise DatabaseExists(f"database {name!r} already exists (use --replace to rebuild it)")
    driver.databases.create(name)
    try:
        for path in SCHEMA_FILES:
            run_file(driver, name, path, SCHEMA)
    except Exception:
        drop(driver, name)
        raise


def create(driver, name, replace=False, seed=None):
    """Create database `name` with schema, functions and seed data. Returns the load report.

    If the seed is refused, the half-made database is removed again.
    """
    _check_name(name)
    if replace:
        drop(driver, name)
    create_schema(driver, name)
    try:
        return seed_module.load(driver, name, seed)
    except Exception:
        drop(driver, name)
        raise
