"""Write the API's OpenAPI description to web/openapi.json, the input for the frontend's
generated TypeScript types. Run after changing apex/models.py or apex/api.py:

    uv run python -m scripts.export_openapi
    scripts/npm-docker.sh npm run gen:api

A test fails if the file is out of date. No database is needed: the description comes from the code.
"""

import json
from pathlib import Path

from apex.api import create_app
from apex.settings import load_settings

OPENAPI_JSON = Path(__file__).resolve().parent.parent / "web" / "openapi.json"


def openapi():
    return create_app(load_settings()).openapi()


def main():
    OPENAPI_JSON.parent.mkdir(exist_ok=True)
    OPENAPI_JSON.write_text(json.dumps(openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {OPENAPI_JSON}")


if __name__ == "__main__":
    main()
