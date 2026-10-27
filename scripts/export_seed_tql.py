"""Regenerate data/seed.tql (the seed data as one TypeQL insert, for loading in TypeDB Studio).

    uv run python -m scripts.export_seed_tql

A test fails if data/seed.tql is out of date with data/seed/*.yaml.
"""

from apex.seed import write_seed_tql

if __name__ == "__main__":
    print(f"Wrote {write_seed_tql()}")
