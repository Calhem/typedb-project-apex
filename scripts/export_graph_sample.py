"""Save the current /api/graph answer to web/.layout-sample.json (git-ignored), the input for
web/scripts/compare-layouts.ts, which compares graph layout engines on the real data."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apex.api import create_app

OUT = Path(__file__).resolve().parent.parent / "web" / ".layout-sample.json"


def main():
    with TestClient(create_app()) as client:
        OUT.write_text(json.dumps(client.get("/api/graph").json()["data"]))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
