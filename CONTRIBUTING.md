# Contributing to Project Apex

Thank you for your interest. Apex is a reference demonstration of TypeDB 3.x, so changes are judged by
whether they keep it **correct, honest and easy to follow**.

## Getting set up

1. Start the stack: `docker compose up -d --build` (see the README for prerequisites and ports).
2. Install [uv](https://docs.astral.sh/uv/); Node.js is not needed (`scripts/npm-docker.sh` runs npm in Docker).
3. Run the checks:

```bash
uv run pytest                              # ~290 tests on throwaway apex-test-* databases
scripts/npm-docker.sh npm run build        # build the frontend (also enables the bundle scan test)
uv run python -m scripts.e2e_check         # every view in headless Chrome, against the running stack
```

All three must pass before a pull request is merged.

## Ground rules

- **TypeDB 3.x only.** 2.x syntax (rules, `thing`, old driver calls) does not work. Check syntax against
  https://typedb.com/docs and run it against the real server.
- **The app stays read-only.** The API uses READ transactions only; TypeDB is the only database.
- **All TypeQL lives in `apex/queries.py`** (the loader's inserts in `apex/seed.py`), and text goes into a query
  only through `apex.db.string`.
- **No business data in the frontend.** Every figure comes from the API; tests scan the source and bundle.
- **Schema changes are new files** in `schema/migrations/`; never edit one that has been applied.
- **Never weaken a check to make data load or a test pass.** If a check fails, fix the data or the code.
- **Fictional data stays fictional and labelled.** New organisations or people must be invented, checked
  against the Companies House register, and use `.example` email domains and the 07700 900xxx drama number
  range. Real public authorities may appear only in their real roles, in an invented scenario. Propose
  dataset changes in an issue first, and keep `docs/dataset.md` and the tests' answer keys in step.
- After editing `data/seed/*.yaml`, run `uv run python -m scripts.export_seed_tql`; after changing response
  models, `uv run python -m scripts.export_openapi` and `scripts/npm-docker.sh npm run gen:api`.

## Pull requests

Keep each pull request to one change, explain what it does and how you verified it, and add or update tests
(answer keys come from `tests/oracle.py`, which must not share code with `apex/`). By contributing you agree
that your contribution is licensed under the Apache License 2.0, like the rest of the project.

Security problems: please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
