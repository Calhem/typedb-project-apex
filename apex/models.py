"""The shapes of the API's answers (Pydantic models).

FastAPI publishes these as an OpenAPI description, and the frontend's TypeScript types are
generated from it (scripts.export_openapi, then openapi-typescript), so the two can't drift.
Money and stakes are exact decimals sent as text ("2400000000", "0.9"); dates are ISO text.
"""

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["not-started", "in-progress", "at-risk", "delayed", "complete"]
Criticality = Literal["low", "medium", "high", "critical"]
Letter = Literal["R", "A", "C", "I"]


class Ref(BaseModel):
    code: str
    name: str | None = None


class Organisation(BaseModel):
    code: str
    name: str | None = None
    description: str | None = None
    jurisdiction: str | None = None
    sector: str | None = None


class Item(BaseModel):
    code: str
    name: str | None = None
    type: str | None = Field(None, description="exact TypeDB type, e.g. regulatory-clearance")
    status: Status | None = None
    criticality: Criticality | None = None
    due: str | None = Field(None, description="due date, ISO")
    completed: str | None = None
    progress: int | None = None


class Envelope(BaseModel):
    """Every response: the answer, plus the exact TypeQL that produced it."""

    typeql: list[str] = Field(description="every query run for this answer, in order, parameters included")
    parameters: dict[str, str | int | list[str]] = Field(default_factory=dict,
                                                       description="the request parameters used in the queries")
    database: str
    elapsed_ms: float
    capabilities: list[str] = Field(default_factory=list,
                                    description="TypeDB capabilities these queries use, detected from the TypeQL (apex/capabilities.py)")
    query_capabilities: list[list[str]] = Field(default_factory=list,
                                                description="the same, query by query (aligned with typeql)")


# ---- /api/deal ----

class AtRisk(BaseModel):
    code: str
    name: str | None = None
    status: Status
    due: str | None = None


class DealStatus(BaseModel):
    state: Literal["on-track", "at-risk", "conditions-satisfied"]
    headline: str
    conditions_total: int
    conditions_satisfied: int
    at_risk: list[AtRisk]


class Money(BaseModel):
    currency: str
    total: str


class NextDeadline(BaseModel):
    code: str
    name: str | None = None
    due: str
    days_remaining: int
    headroom_days: int | None = None


class Metrics(BaseModel):
    progress_percent: int = Field(description="duration-weighted completion of activities (apex/progress.py)")
    progress_basis: str = Field(description="how progress_percent was calculated, in words")
    progress_items: int = Field(description="how many duration-bearing items the percentage covers")
    milestones_achieved: int
    milestones_total: int
    committed_financing: list[Money]
    next_deadline: NextDeadline | None


class Deal(BaseModel):
    code: str
    name: str | None = None
    description: str | None = None
    value: str
    currency: str | None = None
    structure: str | None = None
    as_of: str
    acquirer: Organisation
    target: Organisation
    status: DealStatus
    metrics: Metrics


class DealResponse(Envelope):
    data: Deal


# ---- /api/milestones ----

class TimelineItem(Item):
    workstream: Ref | None = None
    accountable: Ref | None = None


class LimitedItem(Ref):
    due: str | None = None
    status: Status | None = None


class Deadline(Item):
    consequence: str | None = None
    legal_basis: str | None = None
    limits: list[LimitedItem]
    bound: list[Ref]
    consent_from: list[Ref]
    days_remaining: int
    headroom_days: int | None = Field(None, description="deadline minus the planned date of the latest item it limits")


class Milestones(BaseModel):
    as_of: str
    items: list[TimelineItem]
    deadlines: list[Deadline]


class MilestonesResponse(Envelope):
    data: Milestones


# ---- /api/approvals ----

class Review(BaseModel):
    code: str
    name: str
    jurisdiction: str | None = None
    legal_basis: str | None = None
    phase: str | None = None
    applicant: str | None = None


class Event(BaseModel):
    when: str
    summary: str


class Approval(Item):
    description: str | None = None
    legal_basis: str | None = None
    reviews: list[Review]
    accountable: Ref | None = None
    latest: Event | None = None


class ApprovalsResponse(Envelope):
    data: list[Approval]


# ---- /api/activity ----

class Actor(BaseModel):
    code: str
    name: str | None = None
    type: str
    employer: str | None = None


class Activity(BaseModel):
    when: str
    summary: str
    kind: str | None = None
    actor: Actor
    item: Ref


class ActivityResponse(Envelope):
    data: list[Activity]


# ---- /api/parties ----

class Person(BaseModel):
    code: str
    name: str
    title: str


class Principal(BaseModel):
    role: str
    organisation: Organisation
    team: list[Person]


class Adviser(BaseModel):
    mandate: str
    mandate_type: str | None = None
    client: Ref
    organisation: Organisation
    type: str
    team: list[Person]


class Facility(BaseModel):
    code: str
    name: str | None = None
    value: str
    currency: str | None = None


class Lender(BaseModel):
    facility: Facility
    borrower: Ref
    organisation: Organisation
    type: str
    team: list[Person]


class ReviewedItem(Ref):
    status: Status | None = None


class RaciRef(Ref):
    letter: Letter


class Authority(BaseModel):
    organisation: Organisation
    type: str
    reviews: list[ReviewedItem]
    raci: list[RaciRef]


class Parties(BaseModel):
    principals: list[Principal]
    advisers: list[Adviser]
    lenders: list[Lender]
    authorities: list[Authority]


class PartiesResponse(Envelope):
    data: Parties


# ---- /api/progress ----

class Workstream(Item):
    planned_start: str | None = None
    description: str | None = None
    accountable: Ref | None = None
    item_statuses: dict[str, int]
    progress_basis: str = Field(description="how the derived progress was calculated")
    milestones_achieved: int
    milestones_total: int


class ProgressResponse(Envelope):
    data: list[Workstream]


# ---- /api/raci ----

class Holder(BaseModel):
    code: str
    name: str | None = None
    type: str
    employer: str | None = None


class Cell(BaseModel):
    item: str
    holder: str
    letter: Letter
    via: str | None = Field(None, description="the mandate the holder acts under, if any")


class Raci(BaseModel):
    workstream: Ref
    items: list[Item]
    holders: list[Holder]
    cells: list[Cell]


class RaciResponse(Envelope):
    data: Raci


# ---- /api/checks ----

class Check(BaseModel):
    check: str
    title: str
    description: str
    items: list[Ref]


class Checks(BaseModel):
    passed: bool
    checks: list[Check]


class ChecksResponse(Envelope):
    data: Checks


# ---- /api/ownership ----

class RouteStep(BaseModel):
    owner: str
    owned: str
    stake: str = Field(description="direct stake recorded in TypeDB (an ownership relation)")


class Route(BaseModel):
    stake: str = Field(description="product of the direct stakes along this route")
    steps: list[RouteStep]


class GroupCompany(BaseModel):
    code: str
    name: str | None = None
    stake: str = Field(description="effective economic interest from TypeDB's recursive group_members (0.9 = 90%)")
    routes: int = Field(description="how many ownership chains lead to this company (from TypeDB)")
    paths: list[Route] = Field(description="each route, listed from the direct holdings; they add up to `stake`")


class DirectStake(BaseModel):
    owner: str
    owned: str
    stake: str
    inside_group: bool = Field(description="false for outside shareholders' holdings")
    description: str | None = Field(None, description="why the holding exists, and any minority rights attached")


class Consent(BaseModel):
    obligation: str
    obligation_name: str | None = None
    owed_by: str
    consent_from: str
    stake: str


class Ownership(BaseModel):
    parent: str
    parent_name: str | None = None
    companies: list[GroupCompany]
    direct: list[DirectStake]
    outside_shareholders: list[Ref]
    names: dict[str, str | None]
    consents: list[Consent]


class OwnershipResponse(Envelope):
    data: Ownership


# ---- /api/graph ----

class Player(BaseModel):
    role: str = Field(description="the role this player has in the relation, e.g. adviser")
    code: str | None = None
    name: str | None = None
    type: str
    kind: str = Field(description="entity or relation (a relation can play a role, e.g. the deal)")


class Relation(BaseModel):
    key: str
    type: str = Field(description="exact relation type, e.g. mandate")
    attributes: dict[str, str | int | float | None]
    players: list[Player]


class GraphNode(Organisation):
    type: str
    type_path: list[str] = Field(description="the type and its supertypes, e.g. law-firm ... party")
    cluster: str
    people: int


class Cluster(BaseModel):
    key: str
    label: str
    order: int


class RelationType(BaseModel):
    parent: str | None = None
    roles: list[str]


class TypeHierarchy(BaseModel):
    parties: dict[str, str] = Field(description="each party type and its parent type, from the schema")
    relations: dict[str, RelationType]


class GraphDeal(BaseModel):
    code: str
    name: str | None = None
    value: str
    currency: str | None = None


class Graph(BaseModel):
    deal: GraphDeal
    nodes: list[GraphNode]
    relations: list[Relation]
    clusters: list[Cluster]
    types: TypeHierarchy


class GraphResponse(Envelope):
    data: Graph


class Membership(BaseModel):
    key: str
    type: str
    role: str = Field(description="this party's role in the relation")
    attributes: dict[str, str | int | float | None]
    others: list[Player]


class RaciEntry(BaseModel):
    letter: Letter
    item: dict[str, str | None]
    via: str | None = None


class PartyRaci(RaciEntry):
    item_type: str


class Employee(BaseModel):
    code: str
    name: str | None = None
    title: str
    raci: list[RaciEntry]


class PartyDetail(BaseModel):
    code: str
    name: str | None = None
    type: str
    description: str | None = None
    jurisdiction: str | None = None
    sector: str | None = None
    relations: list[Membership]
    raci: list[PartyRaci]
    people: list[Employee]


class PartyResponse(Envelope):
    data: PartyDetail


# ---- /api/impact ----

class ImpactHolder(BaseModel):
    letter: Letter
    code: str
    name: str | None = None
    type: str
    employer: Ref | None = None
    title: str | None = None
    via: dict[str, str | None] | None = Field(None, description="the mandate the person acts under, if any")


class Cause(BaseModel):
    code: str
    kind: str = Field(description="dependency type, e.g. gating or sequencing")
    lag: int | None = None


class ImpactItem(BaseModel):
    code: str
    name: str | None = None
    type: str
    status: Status | None = None
    role: Literal["start", "downstream", "deadline", "context"]
    workstream: Ref | None = None
    planned: str | None = None
    projected: str | None = Field(None, description="PROJECTED date for this scenario (never stored)")
    shift_days: int
    completed: bool
    caused_by: list[Cause] = Field(description="the prerequisite(s) that set the projected date")
    path: list[str] = Field(description="the chain of items from the delayed item to this one")
    raci: list[ImpactHolder]


class LimitedProjection(BaseModel):
    code: str
    name: str | None = None
    planned: str
    projected: str
    breached: bool
    days_over: int
    headroom_days: int
    breach_from_days: int | None = Field(None, description="shortest delay at which this item misses the deadline")


class GroupObligation(BaseModel):
    owed_by: str
    consent_from: str
    effective_stake: str


class ImpactDeadline(ImpactItem):
    due: str
    consequence: str | None = None
    legal_basis: str | None = None
    bound: list[Ref]
    consent_from: list[Ref]
    group_obligation: GroupObligation | None = None
    limits: list[LimitedProjection]
    breached: bool
    days_over: int
    headroom_days: int
    breach_from_days: int | None = None


class ImpactEdge(BaseModel):
    model_config = {"populate_by_name": True}
    source: str = Field(alias="from")
    to: str
    kind: str
    lag: int | None = None
    hard: bool = Field(description="true for hard dependencies (the schema's hard-dependency and its subtypes)")
    moved: bool = Field(description="this link moved the dependent's date (or, for a deadline, breached it)")


class Dependency(BaseModel):
    model_config = {"populate_by_name": True}
    source: str = Field(alias="from")
    to: str
    kind: str
    lag: int | None = None


class PersonItem(BaseModel):
    code: str
    name: str | None = None
    letter: Letter
    via: dict[str, str | None] | None = None
    kind: Literal["item", "deadline"]
    breached: bool
    shift_days: int


class PersonImpact(BaseModel):
    code: str
    name: str | None = None
    type: str
    employer: Ref | None = None
    title: str | None = None
    items: list[PersonItem]


class ImpactSummary(BaseModel):
    moved: int
    workstreams: list[str]
    breached: list[str]
    people: list[PersonImpact]


class Scenario(BaseModel):
    days: int
    label: str
    detail: str
    kind: Literal["baseline", "reference", "threshold"]
    deadline: str | None = None


class FunctionDefinition(BaseModel):
    name: str
    definition: str


class Subject(BaseModel):
    code: str
    name: str | None = None
    status: Status | None = None
    type: str | None = None


class Impact(BaseModel):
    start: ImpactItem
    delay_days: int
    as_of: str
    max_delay_days: int
    level: Literal["on-plan", "delayed", "breach"]
    hard_kinds: list[str]
    items: list[ImpactItem]
    deadlines: list[ImpactDeadline]
    context: list[ImpactItem] = Field(description="items linked by soft dependencies only; never moved")
    edges: list[ImpactEdge]
    dependencies: list[Dependency]
    names: dict[str, str | None] = Field(description="names of every item the dependencies mention")
    statuses: dict[str, Status | None]
    summary: ImpactSummary
    scenarios: list[Scenario]
    ownership_note: str | None = None
    functions: list[FunctionDefinition]
    subjects: list[Subject] = Field(description="conditions not yet complete that can be delayed")


class ImpactResponse(Envelope):
    data: Impact


# ---- /api/schedule (the Gantt chart) ----

class ScheduleItem(Item):
    shape: Literal["bar", "milestone", "deadline"] = Field(
        description="bar: has a planned start; milestone: a point in time; deadline: a contractual limit")
    float: int | None = Field(None, description="schedule float: days of delay before any contractual deadline breaks (impact.schedule_float)")
    float_deadline: str | None = Field(None, description="the deadline that float is measured to")
    critical_for: list[str] = Field(description="deadlines whose deadline-critical chain this item is on")
    planned_start: str | None = None
    actual_start: str | None = None
    workstream: str | None = None


class ScheduleWorkstream(Workstream):
    items: list[ScheduleItem]


class ScheduleDeadline(BaseModel):
    code: str
    name: str | None = None
    due: str
    consequence: str | None = None
    workstream: str | None = None
    limits: list[str] = Field(description="codes of the items this deadline limits")
    critical_path: "CriticalPath | None" = Field(None, description="the deadline's exposure and its deadline-critical chain (not a conventional CPM critical path)")


class CriticalPath(BaseModel):
    float: int = Field(description="deadline exposure: the delay it can absorb before it breaks (smallest float into it)")
    items: list[str] = Field(description="the deadline-critical chain: open items sharing that smallest float")


class ScheduleDependency(BaseModel):
    model_config = {"populate_by_name": True}
    source: str = Field(alias="from")
    to: str
    kind: str
    lag: int | None = None
    hard: bool = Field(description="a hard dependency moves dates (hard-dependency and its subtypes)")


class Schedule(BaseModel):
    as_of: str = Field(description="the scenario date stored in TypeDB; never the real date")
    workstreams: list[ScheduleWorkstream]
    deadlines: list[ScheduleDeadline]
    dependencies: list[ScheduleDependency]


class ScheduleResponse(Envelope):
    data: Schedule


# ---- /api/items/{code} (shared item detail) ----

class RaciHolder(BaseModel):
    code: str
    name: str | None = None
    type: str
    employer: str | None = None
    via: str | None = Field(None, description="the mandate the holder acts under, if any")


class ItemLink(BaseModel):
    code: str
    name: str | None = None
    kind: str
    lag: int | None = None


class LimitRef(Ref):
    due: str | None = None


class ItemEvent(BaseModel):
    code: str
    when: str
    type: str
    summary: str | None = None
    actor: Ref


class ItemRisk(BaseModel):
    code: str
    name: str | None = None
    severity: str | None = None
    likelihood: str | None = None
    status: str | None = None


class DiscussedAt(BaseModel):
    code: str
    when: str
    summary: str | None = None
    actor: Ref
    subject: Ref


class ItemDetail(Item):
    description: str | None = None
    planned_start: str | None = None
    actual_start: str | None = None
    baseline_due: str | None = None
    legal_basis: str | None = None
    consequence: str | None = None
    workstream: Ref | None = None
    raci: dict[Letter, list[RaciHolder]]
    depends_on: list[ItemLink]
    needed_by: list[ItemLink]
    limited_by: list[LimitRef] = Field(description="contractual deadlines that limit this item")
    limits: list[Ref] = Field(description="for a deadline: the items it limits")
    bound: list[Ref]
    consent_from: list[Ref]
    events: list[ItemEvent] = Field(description="newest first")
    discussed_at: list["DiscussedAt"] = Field(description="meetings that had this item on their agenda")
    risks: list[ItemRisk]
    simulate: bool = Field(description="an open condition the Impact Explorer can delay")
    float: int | None = Field(None, description="schedule float in days (impact.schedule_float)")
    float_deadline: str | None = None
    critical_for: list[str] = Field(description="deadlines whose deadline-critical chain this item is on")


class ItemResponse(Envelope):
    data: ItemDetail


# ---- /api/workstreams and /api/workstreams/{code} ----

class EventActor(Ref):
    type: str
    employer: str | None = None


class Answer(BaseModel):
    code: str
    when: str


class WorkstreamEvent(BaseModel):
    workstream: str
    code: str
    when: str
    type: str = Field(description="exact event type: request, response, meeting, delivery, decision, notice, status-change")
    kind: str | None = None
    summary: str | None = None
    actor: EventActor
    item: Ref
    recipients: list[Ref]
    attendees: list[Ref]
    agenda: list[Ref] = Field(description="for a meeting: the items it discussed (agenda-item)")
    replies_to: list[str] = Field(description="for a response: the request(s) it answers")
    answered_by: list[Answer] = Field(description="for a request: the responses recorded against it")
    response_due: str | None = None
    automated: bool | None = None
    previous_status: Status | None = None
    new_status: Status | None = None


class NextMilestone(Ref):
    due: str
    status: Status | None = None
    days_remaining: int = Field(description="from the scenario date")


class ReachDeadline(Ref):
    due: str
    days_remaining: int
    owned: bool = Field(description="the deadline belongs to this workstream")
    via: list[str] = Field(description="the limited items, which this workstream's open work leads to")
    from_: list[str] = Field(alias="from", description="this workstream's open items whose delay could reach it")
    model_config = {"populate_by_name": True}


class WorkstreamRisk(BaseModel):
    code: str
    name: str | None = None
    description: str | None = None
    likelihood: str | None = None
    severity: str | None = None
    status: str | None = None
    mitigation: str | None = None
    owner: Ref | None = None
    items: list[str]


class WorkstreamSummary(BaseModel):
    code: str
    name: str | None = None
    description: str | None = None
    status: Status | None = None
    planned_start: str | None = None
    due: str | None = None
    progress: int | None = Field(None, description="activity progress, duration-weighted (not readiness to close)")
    progress_basis: str
    milestones_achieved: int
    milestones_total: int
    conditions_satisfied: int = Field(description="readiness to close: conditions in this workstream that are satisfied")
    conditions_total: int
    item_statuses: dict[str, int]
    lead: Ref | None = Field(None, description="the workstream's Accountable")
    items: int
    activities: int
    organisations: list[Ref]
    next_milestone: NextMilestone | None = None
    deadlines: list[ReachDeadline] = Field(description="contractual deadlines this workstream's open work can affect")
    risks: list[WorkstreamRisk]
    latest_event: WorkstreamEvent | None = None
    events: int


class WorkstreamList(BaseModel):
    as_of: str
    workstreams: list[WorkstreamSummary]


class WorkstreamsResponse(Envelope):
    data: WorkstreamList


class Prerequisite(Ref):
    workstream: str | None = None


class WorkstreamItem(ScheduleItem):
    open_prerequisites: list[Prerequisite] = Field(description="prerequisites at any depth (recursive upstream) not yet complete")


class WorkstreamDependency(BaseModel):
    model_config = {"populate_by_name": True}
    source: str = Field(alias="from")
    to: str
    kind: str
    lag: int | None = None
    hard: bool
    from_name: str | None = None
    to_name: str | None = None
    from_workstream: Ref
    to_workstream: Ref
    crosses: bool = Field(description="the two items are in different workstreams")


class WorkstreamObligation(BaseModel):
    code: str
    name: str | None = None
    type: str
    status: Status | None = None
    due: str | None = None
    legal_basis: str | None = None
    consequence: str | None = None
    bound: list[Ref]
    consent_from: list[Ref]
    limits: list[Ref]


class WorkstreamDetail(BaseModel):
    as_of: str
    summary: WorkstreamSummary
    items: list[WorkstreamItem]
    dependencies: list[WorkstreamDependency]
    obligations: list[WorkstreamObligation]
    open_conditions: list[Ref] = Field(description="conditions to completion here not yet satisfied: the delays the Impact Explorer can simulate")
    events: list[WorkstreamEvent] = Field(description="newest first")


class WorkstreamResponse(Envelope):
    data: WorkstreamDetail


# ---- /api/register, /api/parties/{code}, /api/engagement-policy (Parties) ----

EngagementLevel = Literal["red", "amber", "green", "unknown", "not-yet-active", "none"]


class Contact(BaseModel):
    """Masked on the server (decision D8): no full value is ever returned."""
    email: str | None = Field(None, description="masked, e.g. e•••••y@novacore.example")
    phone: str | None = Field(None, description="masked, e.g. 07700 900•••")


class Evidence(BaseModel):
    kind: Literal["event", "request", "item", "deadline"]
    code: str
    label: str | None = None
    date: str | None = None


class Assessment(BaseModel):
    kind: Literal["assignment", "request", "review"]
    item: str
    level: Literal["red", "amber", "green", "unknown", "not-yet-active", "not-assessed"]
    category: str = Field(description="overdue-response, response-due, response-awaited, inactivity, deal-risk, engaged, "
                                      "insufficient-information, not-yet-active, statutory-timetable, decision-recorded, "
                                      "requests-only, deadline or authority")
    reason: str
    letter: Letter | None = None
    days: int | None = None
    due: str | None = None
    evidence: list[Evidence]
    context: str | None = Field(None, description="shown for context only; never changes the level")


class Flags(BaseModel):
    overdue_response: int = Field(alias="overdue-response")
    response_due: int = Field(alias="response-due")
    inactivity: int
    deal_risk: int = Field(alias="deal-risk")
    insufficient_information: int = Field(alias="insufficient-information")
    not_yet_active: int = Field(alias="not-yet-active")
    model_config = {"populate_by_name": True}


class EngagementSummary(BaseModel):
    level: EngagementLevel
    category: str = Field(description="the category of the finding that sets the level (see Assessment.category)")
    profile: Literal["Deal participant", "Statutory timetable"]
    headline: str
    flags: Flags
    no_engagement_recorded: bool


class DeliveryRisk(Ref):
    status: Status | None = None
    due: str | None = None
    reason: str


class PartySummary(BaseModel):
    engagement: EngagementSummary
    letters: dict[Letter, int] = Field(description="open assignments by RACI letter")
    outstanding: int = Field(description="open items they are Accountable or Responsible for")
    delivery_risk: list[DeliveryRisk] = Field(description="their A/R items at risk, delayed or overdue (not engagement)")
    workstreams: list[str]
    last_activity: str | None = None


class EmployerRef(Ref):
    type: str


class RegisterPerson(PartySummary):
    code: str
    name: str | None = None
    title: str | None = None
    employer: EmployerRef | None = None
    authority: bool
    contact: Contact


class RegisterOrganisation(PartySummary):
    code: str
    name: str | None = None
    type: str
    jurisdiction: str | None = None
    authority: bool
    people: int


class Register(BaseModel):
    as_of: str
    people: list[RegisterPerson]
    organisations: list[RegisterOrganisation]
    workstream_names: dict[str, str | None]


class RegisterResponse(Envelope):
    data: Register


class Assignment(BaseModel):
    item: str
    name: str | None = None
    type: str | None = None
    letter: Letter
    via: str | None = None
    status: Status | None = None
    due: str | None = None
    criticality: Criticality | None = None
    workstream: Ref
    float: int | None = Field(None, description="schedule float in days before a contractual deadline breaks; null if none in range")
    float_deadline: str | None = None
    simulate: bool


class PersonRef(Ref):
    title: str | None = None
    employer: EmployerRef | None = None


class PartyProfile(PartySummary):
    as_of: str
    code: str
    name: str | None = None
    kind: Literal["person", "organisation"]
    authority: bool
    type: str
    jurisdiction: str | None = None
    title: str | None = None
    employer: EmployerRef | None = None
    contact: Contact | None = None
    assessments: list[Assessment]
    assignments: list[Assignment]
    requests_to: list[WorkstreamEvent]
    requests_from: list[WorkstreamEvent]
    history: list[WorkstreamEvent] = Field(description="newest first")
    people: list[PersonRef]
    relations: list[Membership] = Field(description="every relation the party plays a role in (from the Relationship Explorer)")
    workstream_names: dict[str, str | None]


class PartyProfileResponse(Envelope):
    data: PartyProfile


class EngagementPolicy(BaseModel):
    inactivity: dict[str, dict[str, int]]
    response_warning_days: int
    milestone_active_window_days: int
    urgent_window_days: int
    critical_float_days: int
    near_critical_float_days: int
    statutory_warning_days: int


class EngagementPolicyResponse(BaseModel):
    data: EngagementPolicy
    source: str


# ---- /api/connections and /api/connectables ----

class PathNode(BaseModel):
    code: str
    name: str | None = None
    type: str
    kind: Literal["person", "organisation", "item", "workstream", "risk"]


class PathOther(BaseModel):
    role: str
    code: str
    name: str | None = None


class PathStep(BaseModel):
    model_config = {"populate_by_name": True}
    source: str = Field(alias="from")
    to: str
    type: str = Field(description="the relation's exact type")
    code: str | None = None
    from_role: str
    to_role: str
    sentence: str = Field(description="what this record states, in words")
    others: list[PathOther] = Field(description="the relation's other players, kept for context (n-ary relations stay whole)")
    attributes: dict[str, str | int | float | bool | None]


class Path_(BaseModel):
    rank: int
    hops: int
    cost: float = Field(description="ranking cost (config/connection-policy.yaml): lower is more specific")
    direct: bool = Field(description="one relation joins the two: a direct relationship")
    nodes: list[PathNode]
    steps: list[PathStep]
    notes: list[str] = Field(description="cautions: hubs, activity records, shared workstreams")
    text: str


class PathGroup(BaseModel):
    through: PathNode | None = Field(None, description="the first stepping stone shared by these paths; null for direct ones")
    count: int


class PathDepth(BaseModel):
    hops: int
    paths: int
    capped: bool
    skipped: bool
    note: str | None = None


class Connections(BaseModel):
    model_config = {"populate_by_name": True}
    source: PathNode = Field(alias="from")
    to: PathNode
    max_hops: int
    activity: bool
    workstreams: bool
    total: int = Field(description="every path found within the bounds")
    paths: list[Path_] = Field(description="the best paths, lowest cost first")
    more: list[PathGroup] = Field(description="the remaining paths, grouped by their first stepping stone")
    depths: list[PathDepth]
    elapsed_ms: float
    disclaimer: str


class ConnectionsResponse(Envelope):
    data: Connections


class ConnectablesResponse(Envelope):
    data: list[PathNode]


# ---- /api/advantage, /api/analyst, /api/guide (Phase 6F showcase) ----

class SchemaExcerpt(BaseModel):
    file: str
    line: int
    text: str


class LiveFact(BaseModel):
    label: str
    value: str


class NavLink(BaseModel):
    label: str
    to: str


class AdvantageSection(BaseModel):
    key: Literal["typed-relationships", "recursive-reasoning", "schema-constraints", "polymorphism",
                 "application-complexity", "ai-context"]
    title: str
    statement: str
    schema_: list[SchemaExcerpt] = Field(alias="schema", description="excerpts read from the schema files that built the database")
    live: list[LiveFact] = Field(description="figures read from TypeDB in this request")
    note: str | None = None
    links: list[NavLink]
    model_config = {"populate_by_name": True}


class Advantage(BaseModel):
    sections: list[AdvantageSection]


class AdvantageResponse(Envelope):
    data: Advantage


class Finding(BaseModel):
    text: str
    links: list[NavLink]


class InvestigationStep(BaseModel):
    kind: Literal["executed", "hypothetical"] = Field(description="executed against TypeDB now, or what an AI agent could do (not run)")
    title: str
    how: str
    findings: list[Finding]
    queries: list[int] = Field(description="indexes into typeql of the queries this step ran")


class InvestigationRef(BaseModel):
    key: str
    title: str


class Investigation(BaseModel):
    key: str
    title: str
    question: str
    as_of: str
    steps: list[InvestigationStep]
    investigations: list[InvestigationRef]


class InvestigationResponse(Envelope):
    data: Investigation


class GuideStep(BaseModel):
    n: int
    title: str
    time: str
    to: str
    show: list[str]
    say: str
    typedb: str


class Guide(BaseModel):
    steps: list[GuideStep]
    notes: list[str]


class GuideResponse(BaseModel):
    data: Guide


# ---- errors and health ----

class OwnershipProblem(BaseModel):
    detail: str
    cycles: list[str]
    over_owned: dict[str, str]
    typeql: list[str]


class Health(BaseModel):
    status: Literal["ok", "unavailable"]
    database: str
    detail: str | None = None
