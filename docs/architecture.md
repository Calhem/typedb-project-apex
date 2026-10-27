# Project Apex: technical architecture

A concise overview for technical readers. The detailed design is in [schema-design.md](schema-design.md)
(the TypeDB schema), [queries.md](queries.md) (the TypeQL behind each screen) and [plan.md](plan.md)
(decisions, risks and test results per phase).

## The shape of it

```
 Browser ──► web (nginx, 127.0.0.1:8091) ──► /api/* ──► api (FastAPI, Python 3.13)
             React + TypeScript                           │  apex/queries.py: all TypeQL
             Tailwind CSS + shadcn/ui                     │  READ transactions only
             React Flow + ELK (graph layout)              ▼
                                                 typedb (TypeDB CE 3.14.1, 127.0.0.1:1732)
                                                 the only database: schema, data, functions
```

Four Docker Compose services, all ports bound to 127.0.0.1:

| Service | What it does |
|---|---|
| `typedb` | TypeDB 3.14.1. The only place business data lives. Studio on 8003. |
| `seed` | One-off: creates the `apex` database (schema, functions, checked fictional data) if it is missing, then exits. Never replaces existing data. |
| `api` | Read-only FastAPI service. No published port; reached through `web`. |
| `web` | nginx serving the built React app and forwarding `/api` to `api`. |

## Data and rules live in TypeDB

- **Schema** (`schema/schema.tql`, then each file in `schema/migrations/` in number order; applied files
  are never edited, changes are new files): typed entities and relations with inheritance, for example
  `law-firm` < `professional-services-firm` < `organisation` < `party`, and `gating`/`sequencing` <
  `hard-dependency` < `dependency`. Role names are part of the model (`mandate` relates `client`,
  `adviser` and `engagement`; `authority-review` relates `reviewer`, `subject` and `applicant`).
- **Constraints** enforced by the database: at most one Accountable per item (`@card`), stakes 0-100%
  (`@range`), allowed status words (`@values`), unique codes (`@key`), and who may play which role.
- **Functions** (`schema/functions.tql`), saved in the schema and called by queries:
  - `downstream` and `impact_edges`: recursive walks along hard dependencies (the Impact Explorer).
  - `group_members`: recursive ownership, carrying the stake multiplied along each route.
  - `owners_above` and `ownership_cycles`: the ownership guards.
- **Data** (`data/seed/*.yaml`, the source of truth; `data/seed.tql` is generated from it): loaded in
  **one write transaction** that checks counts, ownership guards and data-quality rules before it commits.
  If anything is wrong, nothing is saved.

## The API (`apex/`)

| Module | Role |
|---|---|
| `queries.py` | Every TypeQL query the app runs (the loader's inserts are in `seed.py`). |
| `overview.py`, `explorer.py`, `impact.py`, `schedule.py`, `workstreams.py`, `parties.py`, `engagement.py`, `connections.py` | Compose each screen's answer from query results. The only calculations outside TypeQL are counting, date arithmetic (TypeQL has no `date + duration`) and listing ownership routes, which must reproduce TypeDB's own recursive figures exactly or nothing is returned. |
| `api.py` | GET endpoints; one READ transaction per request. Every response carries the exact TypeQL that ran, recorded as it ran (`db.RecordingTransaction`), with its parameters: this is what "Show TypeQL" displays. Each response also lists the capabilities those queries use (`capabilities.py`: recursive functions, n-ary relationships, polymorphism, negation, aggregation, typed relationships, nested queries), detected from the recorded TypeQL against the schema files. |
| `advantage.py`, `analyst.py`, `guide.py` | The TypeDB advantage panel (schema excerpts read from the files that built the database, plus live figures), the AI Analyst concept preview (fixed investigations, no AI model) and the guided demonstration's steps. |
| `database.py`, `seed.py` | Create a database (schema, migrations, functions) and load the checked seed data. Used by the `seed` service and `scripts/`, never by the API. |
| `settings.py`, `db.py` | Settings from the environment or `.env` (the password is kept secret in logs); the TypeDB connection and the read-only, recording transaction. |
| `config/*.yaml` | Policy, not data: how engagement is assessed and which relations count as connections. |
| `models.py` | Response models. TypeScript types are generated from them (`web/openapi.json` → `web/src/api/schema.d.ts`), so frontend and backend can't drift apart. |

Endpoints: `/api/deal`, `/milestones`, `/approvals`, `/activity`, `/parties`, `/progress`, `/raci`,
`/checks`, `/graph`, `/graph/node/{code}`, `/ownership`, `/impact`, `/schedule` (the Gantt chart, `apex/schedule.py`),
`/items/{code}` (the shared item panel), `/workstreams` and `/workstreams/{code}` (`apex/workstreams.py`), `/connections`, `/connectables`, `/advantage`, `/analyst/{key}` (concept preview, no AI model), `/guide`,
`/register`, `/parties/{code}` and `/engagement-policy`
(`apex/parties.py`, `apex/engagement.py`, `config/engagement-policy.yaml`), `/health`. OpenAPI docs are served by
the API at `/api/docs` (port 8000 when run on its own with `uv run python -m apex.serve`).

**Safety and robustness:** no writes (tested: no write transactions in the API code, and the database
fingerprint is unchanged after a simulation run); inputs validated against fixed patterns; ownership
figures withheld (HTTP 409) if the guards find a cycle or a company over 100% owned; a lost TypeDB
connection gives a plain 503 and is reopened automatically; `/api/health` asks TypeDB on every call.

## The frontend (`web/`)

- React 19 + TypeScript, built by Vite inside the official Node image (`scripts/npm-docker.sh`), so
  Node.js isn't needed on the host.
- Tailwind CSS 4 and shadcn/ui; one set of colour tokens (`src/index.css`) for every screen.
- TanStack Query for data; the Explorer and Impact screens load on demand (they include the layout engine).
- Graphs: React Flow, laid out by ELK. ELK was chosen over dagre after measuring both on the real graph
  (`web/scripts/compare-layouts.ts`; results in plan.md §9); `?layout=dagre` still shows the alternative.
  The group tree and impact chain use a small tree layout of their own.
- **No business data or rules in the frontend.** Tests scan the source and the built bundle for every seed
  name, code, date and amount, and the Impact screen's code for any threshold or outcome.
- While TypeDB is unreachable, one banner says so and the screens refresh by themselves when it returns.

## Databases

| Name | Use |
|---|---|
| `apex` | The pristine demonstration data the app reads (set by `APEX_DATABASE`). |
| `apex-demo` | A copy for the optional live steps (schema extension, constraint violations). `scripts.reset_demo` rebuilds it. |
| `apex-test-*` | Throwaway databases created and deleted by the tests. |

## Running, seeding and resetting

`docker compose up -d --build` starts `typedb`, waits for it to answer, runs `seed` (which creates `apex` only
if it is missing), then starts `api` and finally `web`, each waiting for the previous one to report healthy.
Data lives in the named volume `apex-typedb-data`, so `docker compose down` keeps it and `down -v` removes it.
`scripts.create_database --replace` and `scripts.reset_demo` rebuild a database from `data/seed/`; both can
run inside the backend image (`docker compose run --rm seed python -m scripts...`) or on the host with uv.
Tested from a fresh clone with no `.env` and an empty volume (plan, Phase 6H).

## Tests

`uv run pytest` (TypeDB running): about 290 tests in ~35 seconds, against real throwaway databases.
Expected answers come from `tests/oracle.py`, which reads the YAML directly and uses different algorithms
from the app (for example Bellman-Ford instead of the app's dependency-order walk), so a wrong query
can't pass by agreeing with itself. Deliberately broken versions of key functions were used to confirm
the tests fail when they should (plan.md §7-§11).

`uv run python -m scripts.e2e_check` checks a running stack end to end: the record counts against the seed
files, the Impact answer key (61, 120 and 136 days), and every main view and guided-demonstration step in
headless Chrome at 1440 and 1280 px (no console errors, one page heading, every control named, no duplicate
ids, no sideways scrolling). It uses only the Python standard library and visits only 127.0.0.1.
