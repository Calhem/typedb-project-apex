"""Connecting to TypeDB and running queries. Everything else in Apex uses this.

Adapted from NewsLens's newslens/db.py (same author).
"""

import textwrap
from decimal import Decimal
from pathlib import Path

from typedb.driver import Credentials, DriverOptions, DriverTlsConfig, TransactionType, TypeDB

READ, WRITE, SCHEMA = TransactionType.READ, TransactionType.WRITE, TransactionType.SCHEMA


def connect(settings):
    """Open a connection ("driver") to the TypeDB server described in the settings."""
    return TypeDB.driver(
        settings.typedb_address,
        Credentials(settings.typedb_username, settings.typedb_password.get_secret_value()),
        # The local server has TLS (encryption) switched off; see compose.yaml.
        DriverOptions(DriverTlsConfig.disabled()),
    )


def string(value):
    """Write a Python string as a TypeQL string, escaping characters that would break it.

    Always use this when putting text into a query, never plain quotes.
    """
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"')
        .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def decimal(value):
    """Write a Decimal as a TypeQL decimal literal, e.g. Decimal("0.7") -> 0.7dec."""
    value = Decimal(value)
    if not value.is_finite():
        raise ValueError(f"not a finite decimal: {value}")
    text = format(value, "f")
    return (text if "." in text else text + ".0") + "dec"


def fetch(tx, query):
    """Run a fetch query inside an open transaction; returns a list of JSON-like documents."""
    return list(tx.query(query).resolve().as_concept_documents())


def rows(tx, query):
    """Run a match/reduce query inside an open transaction; returns a list of answer rows."""
    return list(tx.query(query).resolve().as_concept_rows())


class RecordingTransaction:
    """Wraps an open transaction and remembers every query run through it, in order.

    The API uses it so each answer carries the exact TypeQL that produced it ("Show TypeQL"),
    parameters and guard queries included, rather than a hand-kept copy that could drift.
    """

    def __init__(self, tx):
        self._tx = tx
        self.queries = []

    def query(self, text):
        self.queries.append(textwrap.dedent(text).strip())
        return self._tx.query(text)


def read(driver, db, query):
    """Run a fetch query in a READ transaction and return the results as a list.

    A READ transaction can only look at data, never change it.
    """
    with driver.transaction(db, READ) as tx:
        return fetch(tx, query)


def run_file(driver, db, path, kind=SCHEMA):
    """Run a whole .tql file as one query and commit (used for the schema)."""
    with driver.transaction(db, kind) as tx:
        tx.query(Path(path).read_text()).resolve()
        tx.commit()


def to_decimal(value):
    """Turn a decimal as the driver returns it (e.g. "0.6dec", 0.6, "1") into a Decimal, exactly."""
    text = str(value)
    return Decimal(text[:-3] if text.endswith("dec") else text)
