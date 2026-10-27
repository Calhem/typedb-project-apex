"""Rebuild the "apex-demo" database from the seed data, after a live demonstration.

    uv run python -m scripts.reset_demo

Only ever touches apex-demo; the pristine "apex" database is never changed.
"""

import sys

from scripts.create_database import main

if __name__ == "__main__":
    sys.exit(main(["--name", "apex-demo", "--replace"]))
