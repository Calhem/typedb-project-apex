"""The Apex HTTP API (FastAPI). Read-only: every endpoint is a GET and opens only READ transactions.

The React frontend talks only to this. Each answer carries the exact TypeQL that produced it
(recorded as it ran, parameters included), so the "Show TypeQL" drawer is always the truth.

The database connection is opened on first use, so the API starts (and /api/health explains the
problem) even while TypeDB is still starting or unreachable; a broken connection is dropped and
reopened on the next request.
"""

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from apex import advantage, analyst, capabilities, connections, engagement, explorer, guide, impact, models, overview, queries, schedule, workstreams
from apex import parties as party_pages      # `parties` is also the /api/parties endpoint below
from apex.db import READ, RecordingTransaction, connect, rows, string
from apex.settings import load_settings

log = logging.getLogger("apex.api")

# The same pattern the seed validator enforces for every code.
CODE = r"^[A-Z]{2,5}-[A-Z0-9]+(-[A-Z0-9]+)*$"

UNAVAILABLE = "The database is not reachable. Check that TypeDB is running (docker compose ps)."
LOGIN_REFUSED = "TypeDB refused the login. Check TYPEDB_USERNAME and TYPEDB_PASSWORD in .env."
QUERY_FAILED = "The database could not answer this question. The server log has the details."


class DatabaseMissing(Exception):
    pass


def explain(error):
    """A safe, plain explanation of a connection failure (never the raw driver message)."""
    if isinstance(error, DatabaseMissing):
        return str(error)
    return LOGIN_REFUSED if "CXN16" in str(error) else UNAVAILABLE


class Database:
    """One shared TypeDB connection, opened on first use. The driver is thread-safe, and FastAPI
    runs these plain (non-async) endpoints in a thread pool."""

    def __init__(self, settings):
        self.settings = settings
        self.name = settings.apex_database
        self._driver = None
        self._lock = threading.Lock()

    def driver(self):
        with self._lock:
            if self._driver is None:
                driver = connect(self.settings)
                try:
                    if not driver.databases.contains(self.name):
                        raise DatabaseMissing(f"The database {self.name!r} doesn't exist yet. Create it with: "
                                              "uv run python -m scripts.create_database")
                except Exception:
                    driver.close()
                    raise
                self._driver = driver
            return self._driver

    def reset(self):
        """Forget a broken connection so the next request reconnects."""
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass
            self._driver = None


def create_app(settings=None):
    settings = settings or load_settings()
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        database.reset()

    app = FastAPI(title="Project Apex API", version="0.2.0", lifespan=lifespan,
                  description="Read-only API over the fictional Project Apex deal in TypeDB.",
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.database = database

    def answer(work, **parameters):
        """Run work(tx) in one READ transaction and wrap the result with its TypeQL.

        A connection problem gives 503 (after one reconnect attempt); any other database error
        gives 500 with a plain message (details go to the server log, never to the browser)."""
        for attempt in (1, 2):
            try:
                driver = database.driver()
            except Exception as error:
                log.warning("TypeDB unavailable: %s", explain(error))
                database.reset()
                raise HTTPException(503, explain(error)) from None
            started = time.perf_counter()
            try:
                with driver.transaction(database.name, READ) as raw:
                    tx = RecordingTransaction(raw)
                    data = work(tx)
            except (HTTPException, queries.OwnershipDataError, queries.NoDealError, explorer.OwnershipRoutesMismatch):
                raise
            except Exception as error:
                if attempt == 1 and not _reachable(driver, database.name):
                    log.warning("TypeDB connection lost, reconnecting: %s", type(error).__name__)
                    database.reset()
                    continue
                log.exception("query failed")
                raise HTTPException(500, QUERY_FAILED) from None
            return {"data": data, "typeql": tx.queries, "parameters": parameters, "database": database.name,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "capabilities": capabilities.detect(tx.queries),
                    "query_capabilities": [capabilities.detect([q]) for q in tx.queries]}

    @app.exception_handler(queries.NoDealError)
    def no_deal(request, error):
        return JSONResponse({"detail": f"{error}. Rebuild it with: uv run python -m scripts.create_database --replace"},
                            status_code=503)

    @app.exception_handler(explorer.OwnershipRoutesMismatch)
    def routes_mismatch(request, error):
        body = models.OwnershipProblem(detail=f"Ownership routes don't match TypeDB's figures: {error}",
                                       cycles=[], over_owned={}, typeql=getattr(error, "typeql", []))
        return JSONResponse(body.model_dump(), status_code=409)

    @app.exception_handler(queries.OwnershipDataError)
    def ownership_problem(request, error):
        # Effective stakes would be misleading, so none are returned: only the problem, and the
        # guard queries that found it.
        p = error.problems
        body = models.OwnershipProblem(detail=str(error), cycles=p["cycles"],
                                       over_owned={c: format(t, "f") for c, t in p["over_owned"].items()},
                                       typeql=getattr(error, "typeql", []))
        return JSONResponse(body.model_dump(), status_code=409)

    @app.get("/api/health", response_model=models.Health)
    def health():
        try:
            if not _reachable(database.driver(), database.name):     # ask the server, not just the cache
                raise ConnectionError("TypeDB did not answer")
        except Exception as error:
            database.reset()
            return JSONResponse(models.Health(status="unavailable", database=database.name,
                                              detail=explain(error)).model_dump(), status_code=503)
        return models.Health(status="ok", database=database.name)

    @app.get("/api/deal", response_model=models.DealResponse)
    def deal():
        """Deal summary, headline status and key metrics."""
        return answer(overview.summary)

    @app.get("/api/milestones", response_model=models.MilestonesResponse)
    def milestones(criticality: list[models.Criticality] | None = Query(None)):
        """Milestones and conditions by date (optionally only some criticalities), and deadlines."""
        return answer(lambda tx: overview.milestones(tx, criticality), **({"criticality": criticality} if criticality else {}))

    @app.get("/api/approvals", response_model=models.ApprovalsResponse)
    def approvals():
        return answer(queries.approvals)

    @app.get("/api/activity", response_model=models.ActivityResponse)
    def activity(limit: int = Query(10, ge=1, le=50)):
        return answer(lambda tx: queries.activity(tx, limit), limit=limit)

    @app.get("/api/parties", response_model=models.PartiesResponse)
    def parties():
        return answer(queries.parties)

    @app.get("/api/progress", response_model=models.ProgressResponse)
    def progress():
        return answer(queries.workstreams)

    @app.get("/api/raci", response_model=models.RaciResponse)
    def raci(workstream: str = Query(..., pattern=CODE)):
        def work(tx):
            found = queries.raci(tx, workstream)
            if found is None:
                raise HTTPException(404, f"No workstream with code {workstream}.")
            return found
        return answer(work, workstream=workstream)

    @app.get("/api/checks", response_model=models.ChecksResponse)
    def checks():
        """The three data-quality checks behind "Attention needed"."""
        return answer(overview.checks)

    @app.get("/api/ownership", response_model=models.OwnershipResponse,
             responses={409: {"model": models.OwnershipProblem}})
    def ownership(parent: str | None = Query(None, pattern=CODE)):
        """Group structure below a company (default: the deal's target): direct stakes, effective
        stakes with every route, and change-of-control consents. Refuses (409) if the ownership data
        has a cycle or a company over 100% owned."""
        def work(tx):
            if parent and not rows(tx, f"match $p isa company, has code {string(parent)};"):
                raise HTTPException(404, f"No company with code {parent}.")
            try:
                return explorer.ownership(tx, parent)
            except (queries.OwnershipDataError, explorer.OwnershipRoutesMismatch) as error:
                error.typeql = list(tx.queries)
                raise
        return answer(work, **({"parent": parent} if parent else {}))

    @app.get("/api/graph", response_model=models.GraphResponse)
    def graph():
        """Every organisation and every relation linking two or more of them, with role names."""
        return answer(explorer.graph)

    @app.get("/api/graph/node/{code}", response_model=models.PartyResponse)
    def graph_node(code: str = Path(..., pattern=CODE)):
        """One party: its relations (with roles and all other players), people and RACI letters."""
        def work(tx):
            found = explorer.node(tx, code)
            if found is None:
                raise HTTPException(404, f"No organisation or person with code {code}.")
            return found
        return answer(work, code=code)

    @app.get("/api/schedule", response_model=models.ScheduleResponse, response_model_by_alias=True)
    def schedule_chart():
        """The Gantt chart: workstreams and their items (bars, milestones, deadlines) with TypeDB's dates,
        every dependency, the contractual deadlines and the scenario date."""
        return answer(schedule.schedule)

    @app.get("/api/items/{code}", response_model=models.ItemResponse)
    def item_detail(code: str = Path(..., pattern=CODE)):
        """One work item: dates, status, RACI, dependencies both ways, deadlines, events and risks."""
        def work(tx):
            found = schedule.item(tx, code)
            if found is None:
                raise HTTPException(404, f"No work item with code {code}.")
            return found
        return answer(work, code=code)

    @app.get("/api/workstreams", response_model=models.WorkstreamsResponse, response_model_by_alias=True)
    def workstream_list():
        """Every workstream: status, activity progress, milestones, conditions, lead, organisations, next
        milestone, deadlines its open work can affect, risks and latest event."""
        return answer(workstreams.summaries)

    @app.get("/api/workstreams/{code}", response_model=models.WorkstreamResponse, response_model_by_alias=True)
    def workstream_detail(code: str = Path(..., pattern=CODE)):
        """One workstream: summary, items with open prerequisites, dependencies, obligations and events."""
        def work(tx):
            found = workstreams.detail(tx, code)
            if found is None:
                raise HTTPException(404, f"No workstream with code {code}.")
            return found
        return answer(work, code=code)

    @app.get("/api/register", response_model=models.RegisterResponse, response_model_by_alias=True)
    def party_register():
        """Every person and organisation, with masked contacts, outstanding responsibilities, delivery risk
        and an explainable engagement assessment (config/engagement-policy.yaml)."""
        return answer(party_pages.register)

    @app.get("/api/parties/{code}", response_model=models.PartyProfileResponse, response_model_by_alias=True)
    def party_profile(code: str = Path(..., pattern=CODE)):
        """One person or organisation: engagement assessments with their evidence, assignments, requests,
        history and relationships."""
        def work(tx):
            found = party_pages.profile(tx, code)
            if found is None:
                raise HTTPException(404, f"No person or organisation with code {code}.")
            return found
        return answer(work, code=code)

    @app.get("/api/engagement-policy", response_model=models.EngagementPolicyResponse)
    def engagement_policy():
        """The engagement thresholds in force (configuration, not business data; no database query)."""
        return {"data": engagement.policy(), "source": "config/engagement-policy.yaml"}

    @app.get("/api/connectables", response_model=models.ConnectablesResponse)
    def connectables():
        """Everything a connection path can join: people, organisations, work items, workstreams and risks."""
        def work(tx):
            return sorted((connections._node(things, c) for things in [queries.connectables(tx)] for c in things),
                          key=lambda n: (n["kind"], n["name"] or ""))
        return answer(work)

    @app.get("/api/connections", response_model=models.ConnectionsResponse, response_model_by_alias=True)
    def connection_paths(source: str = Query(..., alias="from", pattern=CODE), to: str = Query(..., pattern=CODE),
                         max_hops: int = Query(3, ge=1, le=3), results: int = Query(20, ge=1, le=50),
                         activity: bool = Query(True), workstreams: bool = Query(False)):
        """How two things are connected: every simple path of up to three relations through people,
        organisations, work items and risks (never through the transaction itself), best first, each step
        explained in words. Activity records count only in paths of up to two steps."""
        def work(tx):
            try:
                return connections.find(tx, source, to, max_hops, results, activity, workstreams)
            except KeyError as error:
                raise HTTPException(404, f"Nothing with code {error.args[0]} can be connected.") from None
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        return answer(work, **{"from": source, "to": to, "max_hops": max_hops, "results": results,
                               "activity": str(activity).lower(), "workstreams": str(workstreams).lower()})

    @app.get("/api/advantage", response_model=models.AdvantageResponse, response_model_by_alias=True)
    def typedb_advantage():
        """The TypeDB Advantage panel: six sections, each with schema excerpts, live figures and links."""
        return answer(advantage.advantage)

    @app.get("/api/analyst/{key}", response_model=models.InvestigationResponse)
    def analyst_investigation(key: str = Path(..., pattern=r"^[a-z-]{3,40}$")):
        """AI Analyst concept preview: one predefined investigation. Executed steps run here against TypeDB;
        hypothetical steps are written in advance. No AI model is used."""
        def work(tx):
            try:
                return analyst.run(tx, key)
            except KeyError:
                raise HTTPException(404, f"No investigation called {key}.") from None
        return answer(work, key=key)

    @app.get("/api/guide", response_model=models.GuideResponse)
    def guided_demo():
        """The guided demonstration's steps (content only; no database query)."""
        return {"data": guide.guide()}

    @app.get("/api/impact", response_model=models.ImpactResponse, response_model_by_alias=True)
    def impact_scenario(item: str | None = Query(None, pattern=CODE),
                        delay_days: int = Query(0, ge=0, le=impact.MAX_DELAY_DAYS)):
        """What happens if one condition is delayed: projected dates, deadlines, who is involved.
        Read-only: nothing is written; the dates are projections. With no item, simulates the
        condition TypeDB records as at risk."""
        def work(tx):
            code = item or impact.default_subject(tx)
            if code is None:
                raise HTTPException(404, "No open condition to simulate.")
            try:
                result = impact.scenario(tx, code, delay_days)
            except KeyError:
                raise HTTPException(404, f"No work item with code {code}.") from None
            result["subjects"] = [{"code": c["code"], "name": c["name"], "status": c["status"], "type": c["type"]}
                                  for c in queries.conditions(tx) if c["status"] != "complete"]
            return result
        return answer(work, **({"item": item} if item else {}), delay_days=delay_days)

    return app


def _reachable(driver, name):
    try:
        return driver.databases.contains(name)
    except Exception:
        return False

