"""Check that Apex can reach its TypeDB server, and list Apex's databases.

    uv run python -m scripts.check_connection
"""

from apex.db import connect
from apex.settings import is_apex_database, load_settings

if __name__ == "__main__":
    settings = load_settings()
    driver = connect(settings)
    names = sorted(db.name for db in driver.databases.all() if is_apex_database(db.name))
    print(f"Connected to TypeDB at {settings.typedb_address}. Apex databases: {', '.join(names) or 'none yet'}.")
    driver.close()
