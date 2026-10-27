"""The seed loader: reads data/seed/*.yaml, checks it, and loads it into TypeDB in ONE transaction.

Steps, all or nothing:
1. validate(): the YAML is complete and consistent (every reference exists and has the right
   kind, codes are unique, values are allowed, every item has exactly one Accountable).
2. render(): turn it into a single TypeQL `insert` (also saved as data/seed.tql for Studio users).
3. load(): inside one WRITE transaction, run the insert, then check in TypeDB that every expected
   entity and relation arrived (a relation with no players, or an insert that matched nothing,
   would otherwise vanish silently), that the ownership guards and the data-quality checks find
   nothing. Only then commit. If anything is wrong the transaction is abandoned: nothing is saved.
"""

import re
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from apex import queries
from apex.db import WRITE, decimal, string

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data" / "seed"
SEED_TQL = ROOT / "data" / "seed.tql"
# Shared files, then one file per workstream (its items, the dependencies into them, RACI and history).
SEED_FILES = ("parties.yaml", "deal.yaml", "links.yaml", "risks.yaml")
WORKSTREAM_DIR = "workstreams"

CODE = re.compile(r"^[A-Z]{2,5}-[A-Z0-9]+(-[A-Z0-9]+)*$")
COMPANY = {"company"}
ADVISER_TYPES = {"investment-bank", "law-firm", "accounting-firm", "consultancy", "communications-firm", "registrar"}
LENDER_TYPES = {"investment-bank", "commercial-bank"}
AUTHORITY_TYPES = {"regulator", "court"}
ORG_TYPES = COMPANY | ADVISER_TYPES | LENDER_TYPES | AUTHORITY_TYPES
OBLIGATION_TYPES = {"regulatory-clearance", "shareholder-approval", "court-sanction", "covenant", "deadline"}
ITEM_TYPES = {"milestone", "activity"} | OBLIGATION_TYPES
REVIEWABLE_TYPES = {"regulatory-clearance", "court-sanction"}
DEPENDENCY_KINDS = {"gating", "sequencing", "informing"}
STATUSES = {"not-started", "in-progress", "at-risk", "delayed", "complete"}
CRITICALITIES = {"low", "medium", "high", "critical"}
RACI_TYPES = {"R": "responsibility", "A": "accountability", "C": "consultation", "I": "notification"}
# Activity history (migration 001). engagement and outreach group these and are never inserted directly.
ENGAGEMENT_TYPES = {"response", "meeting", "delivery", "decision"}
OUTREACH_TYPES = {"request", "notice"}
EVENT_TYPES = ENGAGEMENT_TYPES | OUTREACH_TYPES | {"status-change"}
LIKELIHOODS = {"low", "medium", "high"}
SEVERITIES = {"low", "medium", "high", "critical"}
RISK_STATUSES = {"open", "mitigating", "closed"}
EMAIL = re.compile(r"^[a-z][a-z.-]*@[a-z0-9-]+\.example$")          # reserved domain (RFC 2606) only
PHONE = re.compile(r"^07700 900\d{3}$")                             # Ofcom's drama range only


class SeedError(Exception):
    """The seed data is wrong. The message lists every problem found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("Seed data problems:\n  - " + "\n  - ".join(self.problems))


# ---- 1. read and validate ------------------------------------------------------------------

def read_seed(seed_dir=SEED_DIR):
    """Merge the YAML files into one dictionary: list sections are concatenated across files and mapping
    sections (raci) merged; a code defined twice is reported by validate()."""
    seed_dir = Path(seed_dir)
    paths = [seed_dir / name for name in SEED_FILES] + sorted((seed_dir / WORKSTREAM_DIR).glob("*.yaml"))
    seed, problems = {}, []
    for path in paths:
        part = yaml.safe_load(path.read_text()) or {}
        for key, value in part.items():
            if key not in seed:
                seed[key] = value
            elif isinstance(value, list) and isinstance(seed[key], list):
                seed[key] = seed[key] + value
            elif isinstance(value, dict) and isinstance(seed[key], dict) and key == "raci":
                for code in value.keys() & seed[key].keys():
                    problems.append(f"{path.name}: RACI for {code} is also given in another file")
                seed[key] = {**seed[key], **value}
            else:
                problems.append(f"{path.name}: section {key!r} already defined in another file")
    if problems:
        raise SeedError(problems)
    return seed


def validate(seed):
    """Check the seed data; raise SeedError listing every problem, or return None."""
    problems = []
    add = problems.append

    orgs = {o["code"]: o for o in seed.get("organisations", [])}
    people = {p["code"]: p for p in seed.get("people", [])}
    deal = seed.get("deal") or {}
    mandates = {m["code"]: m for m in seed.get("mandates", [])}
    financing = {f["code"]: f for f in seed.get("financing", [])}
    workstreams = {w["code"]: w for w in seed.get("workstreams", [])}
    items = {i["code"]: i for i in seed.get("items", [])}
    all_items = {**workstreams, **items}

    # An unquoted comma inside a YAML {...} entry silently starts a new, empty field and cuts the text short.
    for section, records in seed.items():
        for record in records if isinstance(records, list) else []:
            if isinstance(record, dict):
                for key, value in record.items():
                    if value is None:
                        add(f"{section} {record.get('code', '')}: field {key!r} is empty "
                            "(quote any text that contains a comma)")
    parties = {**orgs, **people}

    codes = ([o["code"] for o in seed.get("organisations", [])] + [p["code"] for p in seed.get("people", [])]
             + [deal.get("code")] + [m["code"] for m in seed.get("mandates", [])]
             + [f["code"] for f in seed.get("financing", [])]
             + [w["code"] for w in seed.get("workstreams", [])] + [i["code"] for i in seed.get("items", [])]
             + [e.get("code") for e in seed.get("events", [])] + [r.get("code") for r in seed.get("risks", [])])
    for code, n in Counter(codes).items():
        if n > 1:
            add(f"code {code} is used {n} times (codes must be unique across the whole dataset)")
    for code in codes:
        if not isinstance(code, str) or not CODE.match(code):
            add(f"code {code!r} doesn't match the pattern PREFIX-PART (capital letters, digits, hyphens)")

    def org_of(code, allowed, where):
        org = orgs.get(code)
        if org is None:
            add(f"{where}: unknown organisation {code}")
        elif org["type"] not in allowed:
            add(f"{where}: {code} is a {org['type']}, expected one of {sorted(allowed)}")

    def need(record, fields, where):
        for f in fields:
            if record.get(f) in (None, ""):
                add(f"{where}: missing {f}")

    for o in orgs.values():
        need(o, ["name", "type", "jurisdiction"], o["code"])
        if o.get("type") not in ORG_TYPES:
            add(f"{o['code']}: unknown organisation type {o.get('type')!r}")
    for p in people.values():
        need(p, ["name", "employer", "job_title"], p["code"])
        if p.get("employer") not in orgs:
            add(f"{p['code']}: unknown employer {p.get('employer')}")

    need(deal, ["code", "name", "value", "currency", "structure", "as_of", "acquirer", "target"], "deal")
    org_of(deal.get("acquirer"), COMPANY, "deal acquirer")
    org_of(deal.get("target"), COMPANY, "deal target")
    _money(deal.get("value"), "deal value", add)
    if not isinstance(deal.get("as_of"), date):
        add("deal as_of must be a date like 2026-10-26")

    for m in mandates.values():
        need(m, ["client", "adviser", "type"], m["code"])
        org_of(m.get("client"), COMPANY, f"{m['code']} client")
        org_of(m.get("adviser"), ADVISER_TYPES, f"{m['code']} adviser")
    for f in financing.values():
        need(f, ["name", "borrower", "lender", "value", "currency"], f["code"])
        org_of(f.get("borrower"), COMPANY, f"{f['code']} borrower")
        org_of(f.get("lender"), LENDER_TYPES, f"{f['code']} lender")
        _money(f.get("value"), f"{f['code']} value", add)

    for i, own in enumerate(seed.get("ownership", [])):
        where = f"ownership #{i + 1} ({own.get('owner')} -> {own.get('owned')})"
        org_of(own.get("owner"), COMPANY, where)
        org_of(own.get("owned"), COMPANY, where)
        if not isinstance(own.get("stake"), str):
            add(f"{where}: stake must be a quoted string such as \"0.6\", so it stays exact")
        else:
            try:
                Decimal(own["stake"])
            except InvalidOperation:
                add(f"{where}: stake {own['stake']!r} is not a number")
        # Range (0-1), cycles and over-100% totals are deliberately left to TypeDB: the schema's
        # @range and the guard queries run during load().

    for code, it in all_items.items():
        need(it, ["name", "status", "due"], code)
        if it.get("status") not in STATUSES:
            add(f"{code}: status {it.get('status')!r} is not one of {sorted(STATUSES)}")
        if it.get("criticality", "medium") not in CRITICALITIES:
            add(f"{code}: criticality {it.get('criticality')!r} is not allowed")
        for f in ("due", "planned_start", "baseline_due", "completed"):
            if f in it and not isinstance(it[f], date):
                add(f"{code}: {f} must be a date like 2026-11-30")
        progress = it.get("progress")
        if progress is not None and not (isinstance(progress, int) and 0 <= progress <= 100):
            add(f"{code}: progress must be a whole number from 0 to 100")
    for code, it in items.items():
        if it.get("type") not in ITEM_TYPES:
            add(f"{code}: unknown item type {it.get('type')!r}")
        if it.get("workstream") not in workstreams:
            add(f"{code}: unknown workstream {it.get('workstream')}")
        if it.get("type") == "deadline" and not it.get("consequence"):
            add(f"{code}: a deadline needs a consequence (what happens if it is missed)")

    for i, dep in enumerate(seed.get("dependencies", [])):
        where = f"dependency #{i + 1} ({dep.get('from')} -> {dep.get('to')})"
        if dep.get("kind") not in DEPENDENCY_KINDS:
            add(f"{where}: kind must be one of {sorted(DEPENDENCY_KINDS)}")
        for end in ("from", "to"):
            if dep.get(end) not in all_items:
                add(f"{where}: unknown item {dep.get(end)}")
        if not (isinstance(dep.get("lag"), int) and 0 <= dep["lag"] <= 365):
            add(f"{where}: lag must be a whole number of days from 0 to 365")

    for dl in seed.get("deadline_limits", []):
        if items.get(dl.get("deadline"), {}).get("type") != "deadline":
            add(f"deadline limit: {dl.get('deadline')} is not a deadline item")
        if dl.get("item") not in all_items:
            add(f"deadline limit: unknown item {dl.get('item')}")
    for rv in seed.get("reviews", []):
        if items.get(rv.get("subject"), {}).get("type") not in REVIEWABLE_TYPES:
            add(f"review: {rv.get('subject')} can't be reviewed by an authority")
        org_of(rv.get("reviewer"), AUTHORITY_TYPES, f"review of {rv.get('subject')}")
        org_of(rv.get("applicant"), COMPANY, f"review of {rv.get('subject')}")
        need(rv, ["legal_basis", "phase"], f"review of {rv.get('subject')}")
    for section, role in (("bound_by", "obligor"), ("consents", "grantor")):
        for rec in seed.get(section, []):
            if items.get(rec.get("obligation"), {}).get("type") not in OBLIGATION_TYPES:
                add(f"{section}: {rec.get('obligation')} is not an obligation")
            org_of(rec.get(role), ORG_TYPES, f"{section} {rec.get('obligation')}")

    raci = seed.get("raci", {})
    for code in all_items:
        if code not in raci:
            add(f"{code}: no RACI entry (every item needs exactly one Accountable)")
    for code, letters in raci.items():
        if code not in all_items:
            add(f"RACI: unknown item {code}")
            continue
        if not isinstance(letters.get("A"), str):
            add(f"RACI {code}: needs exactly one Accountable (A: CODE)")
        holders = [letters.get("A")] + [h for k in "RCI" for h in letters.get(k, [])]
        for h in holders:
            if h not in parties:
                add(f"RACI {code}: unknown holder {h}")
        for h, n in Counter(holders).items():
            if n > 1:
                add(f"RACI {code}: {h} appears {n} times; give each holder one letter")
        for k in letters:
            if k not in RACI_TYPES:
                add(f"RACI {code}: unknown letter {k!r}")

    for i, ev in enumerate(seed.get("events", [])):
        where = f"event #{i + 1}"
        if not isinstance(ev.get("at"), datetime):
            add(f"{where}: at must be a date-time like 2026-10-21T11:00:00")
        if ev.get("actor") not in parties:
            add(f"{where}: unknown actor {ev.get('actor')}")
        if ev.get("subject") not in all_items:
            add(f"{where}: unknown subject {ev.get('subject')}")
        need(ev, ["kind", "summary"], where)

    _validate_phase6(seed, add, orgs, people, parties, workstreams, items, all_items, raci)

    if problems:
        raise SeedError(problems)


def _validate_phase6(seed, add, orgs, people, parties, workstreams, items, all_items, raci):
    """Rules for the Phase 6 data (migration 001): dates that agree with the scenario date, contact
    details that can only be fictional, a coherent activity history and an owned risk register."""
    as_of = (seed.get("deal") or {}).get("as_of")
    if not isinstance(as_of, date):
        return

    # ---- people: fictional contact details only ----
    for field, pattern, what in (("email", EMAIL, "an address on a reserved .example domain"),
                                 ("phone", PHONE, "a number in Ofcom's drama range, 07700 900xxx")):
        seen = Counter(p.get(field) for p in people.values())
        for p in people.values():
            if not isinstance(p.get(field), str) or not pattern.match(p[field]):
                add(f"{p['code']}: {field} must be {what}")
            elif seen[p[field]] > 1:
                add(f"{p['code']}: {field} {p[field]} is used by more than one person")
    holders = {h for letters in raci.values() for hs in letters.values() for h in ([hs] if isinstance(hs, str) else hs)}
    for p in people.values():
        if p["code"] not in holders:
            add(f"{p['code']}: has no RACI letter anywhere (every person needs a role)")

    # ---- work: dates agree with each other and with the scenario date ----
    for w in workstreams.values():
        if "progress" in w:
            add(f"{w['code']}: workstream progress is derived from its items, so it must not be stored")
    for code, it in items.items():
        start, due, status = it.get("planned_start"), it.get("due"), it.get("status")
        if it.get("type") == "activity" and not start:
            add(f"{code}: an activity needs a planned_start (milestones are the items without one)")
        if isinstance(start, date) and isinstance(due, date) and start > due:
            add(f"{code}: planned_start {start} is after its due date {due}")
        actual = it.get("actual_start")
        if actual is not None and (not isinstance(actual, date) or actual > as_of):
            add(f"{code}: actual_start must be a date on or before the scenario date ({as_of})")
        if it.get("type") == "deadline" or not isinstance(due, date):
            continue
        if status == "complete" and due > as_of:
            add(f"{code}: complete, but due after the scenario date ({as_of})")
        if status in ("in-progress", "at-risk", "delayed") and isinstance(start, date) and start > as_of:
            add(f"{code}: {status}, but its planned start is after the scenario date")
        if status == "not-started" and isinstance(start, date) and start <= as_of:
            add(f"{code}: not started, though its planned start ({start}) has passed")
        ws = workstreams.get(it.get("workstream"))
        if ws and isinstance(ws.get("planned_start"), date) and isinstance(ws.get("due"), date):
            if (start or due) < ws["planned_start"] or due > ws["due"]:
                add(f"{code}: dates fall outside its workstream ({ws['planned_start']} to {ws['due']})")

    # ---- activity history ----
    events = {e.get("code"): e for e in seed.get("events", [])}
    employer = {p["code"]: p.get("employer") for p in people.values()}
    ws_of = {c: it.get("workstream", c) for c, it in all_items.items()}
    involved = {}                                    # workstream -> holders and their employers
    for code, letters in raci.items():
        for hs in letters.values():
            for h in [hs] if isinstance(hs, str) else hs:
                involved.setdefault(ws_of.get(code), set()).update({h, employer.get(h)})
    latest_change = {}
    for code, e in events.items():
        where = f"event {code}"
        typ, at = e.get("type"), e.get("at")
        if typ not in EVENT_TYPES:
            add(f"{where}: type must be one of {sorted(EVENT_TYPES)}")
            continue
        if isinstance(at, datetime) and at.date() > as_of:
            add(f"{where}: happens after the scenario date ({as_of})")
        actor, subject = e.get("actor"), e.get("subject")
        actor_type = orgs.get(actor, {}).get("type")
        if actor in parties and subject in all_items and actor_type not in AUTHORITY_TYPES \
                and actor not in involved.get(ws_of[subject], set()):
            add(f"{where}: {actor} has no role in {ws_of[subject]}, so can't act on {subject}")
        for role in ("recipients", "attendees"):
            for party in e.get(role, []):
                if party not in parties:
                    add(f"{where}: unknown {role[:-1]} {party}")
        if (typ in OUTREACH_TYPES) != bool(e.get("recipients")):
            add(f"{where}: requests and notices have recipients; other events don't")
        if (typ == "meeting") != bool(e.get("attendees")):
            add(f"{where}: meetings have attendees; other events don't")
        if e.get("agenda"):
            if typ != "meeting":
                add(f"{where}: only meetings have an agenda")
            for code_ in e["agenda"]:
                if code_ not in all_items:
                    add(f"{where}: unknown agenda item {code_}")
                elif subject in all_items and ws_of.get(code_) != ws_of.get(subject):
                    add(f"{where}: agenda item {code_} is not in {ws_of.get(subject)}, the meeting's workstream")
        if "response_due" in e and (typ != "request" or not isinstance(e["response_due"], date)
                                    or (isinstance(at, datetime) and e["response_due"] < at.date())):
            add(f"{where}: response_due is for requests, on or after the request")
        if "automated" in e and (typ != "notice" or not isinstance(e["automated"], bool)):
            add(f"{where}: automated is a true/false flag for notices")
        for original in e.get("reply_to", []):
            req = events.get(original)
            if typ != "response":
                add(f"{where}: only responses reply to requests")
            elif not req or req.get("type") != "request":
                add(f"{where}: replies to {original}, which is not a request")
            else:
                if isinstance(at, datetime) and isinstance(req.get("at"), datetime) and at < req["at"]:
                    add(f"{where}: answers {original} before it was asked")
                answerable = set(req.get("recipients", []))
                if actor not in answerable and employer.get(actor) not in answerable:
                    add(f"{where}: {actor} answers {original} but was not one of its recipients")
        if typ == "status-change":
            for f in ("previous_status", "new_status"):
                if e.get(f) not in STATUSES:
                    add(f"{where}: {f} must be one of {sorted(STATUSES)}")
            if isinstance(at, datetime) and (subject not in latest_change or at > latest_change[subject]["at"]):
                latest_change[subject] = e
    for subject, e in latest_change.items():
        if all_items.get(subject, {}).get("status") != e.get("new_status"):
            add(f"event {e['code']}: the latest status change on {subject} says {e.get('new_status')!r}, "
                f"but the item's status is {all_items.get(subject, {}).get('status')!r}")

    # ---- risk register ----
    for r in seed.get("risks", []):
        where = f"risk {r.get('code')}"
        for field, allowed in (("likelihood", LIKELIHOODS), ("severity", SEVERITIES), ("status", RISK_STATUSES)):
            if r.get(field) not in allowed:
                add(f"{where}: {field} must be one of {sorted(allowed)}")
        if r.get("owner") not in parties:
            add(f"{where}: unknown owner {r.get('owner')}")
        if not r.get("exposed") or any(c not in all_items for c in r.get("exposed", [])):
            add(f"{where}: must threaten at least one known work item")
        for field in ("name", "description", "mitigation"):
            if not r.get(field):
                add(f"{where}: missing {field}")


def _money(value, where, add):
    if not isinstance(value, str):
        add(f"{where}: must be a quoted string such as \"2400000000\", so it stays exact")
        return
    try:
        if Decimal(value) <= 0:
            add(f"{where}: must be positive")
    except InvalidOperation:
        add(f"{where}: {value!r} is not a number")


# ---- 2. render TypeQL ----------------------------------------------------------------------

def var(code):
    """TypeQL variable for a code: ORG-ATL-UK -> $org-atl-uk."""
    return "$" + code.lower()


def _has(**attrs):
    parts = []
    for name, value in attrs.items():
        if value is None:
            continue
        label = name.replace("_", "-")
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, Decimal):
            literal = decimal(value)
        elif isinstance(value, datetime):
            literal = value.isoformat(timespec="seconds")
        elif isinstance(value, date):
            literal = value.isoformat()
        elif isinstance(value, int):
            literal = str(value)
        else:
            literal = string(str(value))
        parts.append(f"has {label} {literal}")
    return (", " + ", ".join(parts)) if parts else ""


def _mandate_by_adviser(seed):
    by_adviser = defaultdict(list)
    for m in seed.get("mandates", []):
        by_adviser[m["adviser"]].append(m["code"])
    return by_adviser


def via_mandate(seed, holder):
    """The mandate a holder acts under: a person employed by an adviser with exactly one mandate."""
    employer = {p["code"]: p["employer"] for p in seed.get("people", [])}.get(holder, holder)
    found = _mandate_by_adviser(seed).get(employer, [])
    return found[0] if len(found) == 1 else None


def render(seed):
    """The whole dataset as one TypeQL insert query."""
    deal = seed["deal"]
    d = var(deal["code"])
    lines = ["# GENERATED by apex/seed.py from data/seed/*.yaml. Do not edit by hand.",
             "# FICTIONAL DEMONSTRATION DATA. Load in a WRITE transaction after schema/*.tql.",
             "insert"]
    out = lines.append

    out("  # ---- organisations and people ----")
    for o in seed["organisations"]:
        out(f"  {var(o['code'])} isa {o['type']}" + _has(code=o["code"], name=o["name"], description=o.get("description"),
                                                         jurisdiction=o.get("jurisdiction"), sector=o.get("sector")) + ";")
    for p in seed["people"]:
        out(f"  {var(p['code'])} isa person" + _has(code=p["code"], name=p["name"], email=p.get("email"), phone=p.get("phone")) + ";")
        out(f"  employment (employee: {var(p['code'])}, employer: {var(p['employer'])})" + _has(job_title=p["job_title"]) + ";")

    out("  # ---- the deal, mandates, financing, ownership ----")
    out(f"  {d} isa acquisition (acquirer: {var(deal['acquirer'])}, target: {var(deal['target'])})"
        + _has(code=deal["code"], name=deal["name"], description=deal.get("description"),
               deal_value=Decimal(deal["value"]), currency=deal["currency"], deal_structure=deal["structure"],
               as_of_date=deal["as_of"]) + ";")
    for m in seed["mandates"]:
        out(f"  {var(m['code'])} isa mandate (client: {var(m['client'])}, adviser: {var(m['adviser'])}, engagement: {d})"
            + _has(code=m["code"], mandate_type=m["type"]) + ";")
    for f in seed["financing"]:
        out(f"  {var(f['code'])} isa financing (borrower: {var(f['borrower'])}, lender: {var(f['lender'])}, purpose: {d})"
            + _has(code=f["code"], name=f["name"], deal_value=Decimal(f["value"]), currency=f["currency"]) + ";")
    for own in seed["ownership"]:
        out(f"  ownership (owner: {var(own['owner'])}, owned: {var(own['owned'])})"
            + _has(stake=Decimal(own["stake"]), description=own.get("description")) + ";")

    out("  # ---- work ----")
    for w in seed["workstreams"]:
        out(f"  {var(w['code'])} isa workstream" + _item_attrs(w) + ";")
        out(f"  deal-scope (deal: {d}, item: {var(w['code'])});")
    for it in seed["items"]:
        out(f"  {var(it['code'])} isa {it['type']}" + _item_attrs(it) + ";")
        out(f"  deal-scope (deal: {d}, item: {var(it['code'])});")
        out(f"  work-breakdown (whole: {var(it['workstream'])}, part: {var(it['code'])});")
    for dep in seed["dependencies"]:
        out(f"  {dep['kind']} (prerequisite: {var(dep['from'])}, dependent: {var(dep['to'])})" + _has(lag_days=dep["lag"]) + ";")
    for dl in seed["deadline_limits"]:
        out(f"  deadline-limit (limited-item: {var(dl['item'])}, deadline: {var(dl['deadline'])});")
    for rv in seed["reviews"]:
        out(f"  authority-review (reviewer: {var(rv['reviewer'])}, subject: {var(rv['subject'])}, applicant: {var(rv['applicant'])})"
            + _has(legal_basis=rv["legal_basis"], review_phase=rv["phase"]) + ";")
    for b in seed["bound_by"]:
        out(f"  bound-by (obligor: {var(b['obligor'])}, obligation: {var(b['obligation'])});")
    for c in seed["consents"]:
        out(f"  consent-right (grantor: {var(c['grantor'])}, obligation: {var(c['obligation'])});")

    out("  # ---- RACI ----")
    for code, letters in seed["raci"].items():
        for letter, holders in letters.items():
            for h in [holders] if isinstance(holders, str) else holders:
                item_role = "accountable-item" if letter == "A" else "item"
                via = via_mandate(seed, h)
                via_part = f", via: {var(via)}" if via else ""
                out(f"  {RACI_TYPES[letter]} (holder: {var(h)}, {item_role}: {var(code)}{via_part});")

    out("  # ---- activity history ----")
    for ev in seed["events"]:
        roles = [f"actor: {var(ev['actor'])}", f"subject: {var(ev['subject'])}"]
        roles += [f"recipient: {var(r)}" for r in ev.get("recipients", [])]
        roles += [f"attendee: {var(a)}" for a in ev.get("attendees", [])]
        roles += [f"agenda-item: {var(a)}" for a in ev.get("agenda", [])]
        out(f"  {var(ev['code'])} isa {ev['type']} ({', '.join(roles)})"
            + _has(code=ev["code"], occurred_at=ev["at"], summary=ev["summary"], event_kind=ev["kind"],
                   response_due_date=ev.get("response_due"), automated=ev.get("automated"),
                   previous_status=ev.get("previous_status"), new_status=ev.get("new_status")) + ";")
    for ev in seed["events"]:
        for original in ev.get("reply_to", []):
            out(f"  reply (original: {var(original)}, answer: {var(ev['code'])});")

    out("  # ---- risk register ----")
    for r in seed.get("risks", []):
        out(f"  {var(r['code'])} isa risk" + _has(code=r["code"], name=r["name"], description=r["description"],
                                                  likelihood=r["likelihood"], severity=r["severity"],
                                                  risk_status=r["status"], mitigation=r["mitigation"]) + ";")
        out(f"  risk-ownership (risk-owner: {var(r['owner'])}, owned-risk: {var(r['code'])});")
        for item in r["exposed"]:
            out(f"  risk-exposure (risk: {var(r['code'])}, exposed: {var(item)});")
    return "\n".join(lines) + "\n"


def _item_attrs(it):
    completed = it.get("completed", it["due"] if it["status"] == "complete" else None)
    return _has(code=it["code"], name=it["name"], description=it.get("description"), status=it["status"],
                criticality=it.get("criticality", "medium"), planned_start=it.get("planned_start"),
                due_date=it["due"], baseline_due_date=it.get("baseline_due", it["due"]),
                completed_date=completed, progress=it.get("progress"), actual_start_date=it.get("actual_start"),
                legal_basis=it.get("legal_basis"), consequence=it.get("consequence"))


# ---- 3. expected counts and loading ----------------------------------------------------------

def expected_counts(seed):
    """How many instances of each exact type the seed should create."""
    c = Counter()
    for o in seed["organisations"]:
        c[o["type"]] += 1
    c["person"] += len(seed["people"])
    c["employment"] += len(seed["people"])
    c["acquisition"] += 1
    c["mandate"] += len(seed["mandates"])
    c["financing"] += len(seed["financing"])
    c["ownership"] += len(seed["ownership"])
    c["workstream"] += len(seed["workstreams"])
    for it in seed["items"]:
        c[it["type"]] += 1
    c["deal-scope"] += len(seed["workstreams"]) + len(seed["items"])
    c["work-breakdown"] += len(seed["items"])
    for dep in seed["dependencies"]:
        c[dep["kind"]] += 1
    c["deadline-limit"] += len(seed["deadline_limits"])
    c["authority-review"] += len(seed["reviews"])
    c["bound-by"] += len(seed["bound_by"])
    c["consent-right"] += len(seed["consents"])
    for letters in seed["raci"].values():
        for letter, holders in letters.items():
            c[RACI_TYPES[letter]] += 1 if isinstance(holders, str) else len(holders)
    for ev in seed["events"]:
        c[ev["type"]] += 1
    c["reply"] += sum(len(ev.get("reply_to", [])) for ev in seed["events"])
    c["risk"] += len(seed.get("risks", []))
    c["risk-ownership"] += len(seed.get("risks", []))
    c["risk-exposure"] += sum(len(r["exposed"]) for r in seed.get("risks", []))
    return dict(c)


def load(driver, db, seed=None):
    """Validate and load the seed into an existing database that already has the schema.

    Returns a report {"counts": {...}}. Raises SeedError (nothing saved) if anything is wrong.
    """
    seed = seed if seed is not None else read_seed()
    validate(seed)
    insert = render(seed)
    expected = expected_counts(seed)
    with driver.transaction(db, WRITE) as tx:
        tx.query(insert).resolve()
        actual = queries.counts_by_type(tx, sorted(expected))
        problems = [f"{label}: expected {n} in TypeDB, found {actual[label]}"
                    for label, n in sorted(expected.items()) if actual[label] != n]
        own = queries.ownership_problems(tx)
        if own["cycles"]:
            problems.append("ownership cycle through " + ", ".join(own["cycles"])
                            + " (cross-shareholdings are rejected: see docs/schema-design.md §4)")
        for code, total in own["over_owned"].items():
            problems.append(f"{code} is more than 100% owned (direct stakes add up to {total})")
        for check, codes in queries.data_quality_checks(tx).items():
            if codes:
                problems.append(f"data-quality check {check} failed for {', '.join(codes)}")
        if problems:
            raise SeedError(problems)          # leaving the block without commit discards everything
        tx.commit()
    return {"counts": actual}


def write_seed_tql(seed=None, path=SEED_TQL):
    """Save the generated insert for people who want to load the data in TypeDB Studio."""
    seed = seed if seed is not None else read_seed()
    validate(seed)
    Path(path).write_text(render(seed))
    return path
