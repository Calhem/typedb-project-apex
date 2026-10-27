"""Create an Apex database with the schema, functions and fictional seed data.

    uv run python -m scripts.create_database                  # creates "apex"
    uv run python -m scripts.create_database --replace        # deletes and rebuilds "apex"
    uv run python -m scripts.create_database --name apex-demo --replace
    uv run python -m scripts.create_database --if-missing     # creates "apex" only if absent (Docker)

Everything is checked before it is saved; if any check fails, nothing is saved.
"""

import argparse
import sys
import time

from apex import database
from apex.db import connect
from apex.seed import SeedError
from apex.settings import DEMO_DATABASE, PRISTINE_DATABASE, load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=PRISTINE_DATABASE, choices=[PRISTINE_DATABASE, DEMO_DATABASE])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replace", action="store_true", help="delete the database first if it exists")
    mode.add_argument("--if-missing", action="store_true", help="do nothing (successfully) if it already exists")
    args = parser.parse_args(argv)

    settings = load_settings()
    driver = connect(settings)
    try:
        started = time.perf_counter()
        report = database.create(driver, args.name, replace=args.replace)
    except database.DatabaseExists as error:
        print(f"Not changed: {error}.")
        return 0 if args.if_missing else 1
    except SeedError as error:
        print(f"Refused, nothing saved.\n{error}")
        return 1
    finally:
        driver.close()
    total = sum(report["counts"].values())
    print(f"Created database {args.name!r}: {total} entities and relations, all checks passed "
          f"({time.perf_counter() - started:.1f}s).")
    for label, n in sorted(report["counts"].items()):
        print(f"  {label:24} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
