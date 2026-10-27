"""The TypeDB Advantage panel (/api/advantage): six short sections, each backed by evidence from Apex itself.

Evidence is never written by hand: schema excerpts are read from the schema files that built the database,
live figures come from TypeDB in the same READ transaction (shown in "Show TypeQL"), and every section links
to a working example in the app. Statements describe what Apex does; they make no comparison with other
databases and no performance claim.
"""

import re
from pathlib import Path

from apex import impact, queries
from apex.db import fetch, rows

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILES = [ROOT / "schema" / "schema.tql", ROOT / "schema" / "functions.tql",
                *sorted((ROOT / "schema" / "migrations").glob("[0-9][0-9][0-9]-*.tql"))]


def excerpt(pattern, file=None):
    """The schema statement starting at the first line matching `pattern` (up to its closing ';' or, for a
    function, its return), with its file and line number."""
    for path in ([ROOT / file] if file else SCHEMA_FILES):
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines):
            if re.search(pattern, line):
                while n > 0 and lines[n - 1].strip() and not lines[n - 1].split("#", 1)[0].rstrip().endswith(";") \
                        and not lines[n - 1].lstrip().startswith("#"):
                    n -= 1                               # back to the start of the statement
                out = []
                for text in lines[n:]:
                    out.append(text)
                    code = text.split("#", 1)[0].rstrip()
                    if (code.endswith(";") and not out[0].startswith("fun ")) or code.strip().startswith("return"):
                        break
                return {"file": str(path.relative_to(ROOT)), "line": n + 1, "text": "\n".join(out)}
    raise LookupError(pattern)


def _labels(tx, query):
    return sorted(d["t"]["label"] for d in fetch(tx, query))


def _count(tx, query):
    r = rows(tx, query)
    return r[0].get("n").get() if r else 0


def _schema_rules():
    text = "\n".join(p.read_text() for p in SCHEMA_FILES)
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return {k: len(re.findall(p, code, re.M)) for k, p in
            (("keys", r"@key"), ("cardinalities", r"@card\("), ("value lists", r"@values\("), ("ranges", r"@range\("),
             ("functions", r"^\s*fun\s"))}


def advantage(tx):
    raci_types = _labels(tx, 'match $t sub raci-assignment; fetch { "t": $t };')
    item_types = _labels(tx, 'match $t sub deal-item; fetch { "t": $t };')
    event_types = _labels(tx, 'match $t sub deal-event; fetch { "t": $t };')
    engagement_types = _labels(tx, 'match $t sub engagement; fetch { "t": $t };')
    items = _count(tx, "match $x isa deal-item; reduce $n = count;")
    mandates = _count(tx, "match $m isa mandate; reduce $n = count;")
    start = impact.default_subject(tx)
    reached = len({r.get("c").as_attribute().get_value() for r in rows(
        tx, f'match $s isa deal-item, has code "{start}"; let $x in downstream($s); $x has code $c; select $c;')})
    checks = queries.data_quality_checks(tx)
    rules = _schema_rules()
    start_name = queries.all_items(tx)[start]["name"]
    return {"sections": [
        {"key": "typed-relationships", "title": "Typed relationships",
         "statement": "Each relationship in Apex is stored as a typed relation with named roles, so an answer says what "
                      "connects two things and in which capacity, and a relation with three players stays one fact.",
         "schema": [excerpt(r"^relation mandate"), excerpt(r"^relation authority-review"), excerpt(r"^relation raci-assignment")],
         "live": [{"label": "Mandates stored, each relating client, adviser and the deal", "value": str(mandates)},
                  {"label": "RACI letters, each its own relation type", "value": ", ".join(t for t in raci_types if t != "raci-assignment")}],
         "links": [{"label": "See a path explained step by step", "to": "/explorer?view=paths&from=PER-AINSWORTH&to=ORG-CMA"},
                   {"label": "Open the transaction network", "to": "/explorer"}]},
        {"key": "recursive-reasoning", "title": "Recursive reasoning",
         "statement": "Dependencies and ownership chains are followed to any depth by functions saved in the schema; "
                      "screens call them like built-ins instead of re-implementing the walk.",
         "schema": [excerpt(r"^fun downstream"), excerpt(r"^fun group_members")],
         "live": [{"label": f"Items a delay to {start_name} reaches, through every hard dependency", "value": str(reached)},
                  {"label": "Saved functions in the schema", "value": str(rules["functions"])}],
         "links": [{"label": "Delay the CMA clearance by 61 days", "to": "/impact?item=CP-CMA&days=61"},
                   {"label": "Effective ownership, route by route", "to": "/explorer?view=group"}]},
        {"key": "schema-constraints", "title": "Schema constraints",
         "statement": "Rules such as one Accountable per item, stakes between 0 and 100% and the allowed status words are "
                      "declared in the schema and enforced by the database on every write.",
         "schema": [excerpt(r"plays accountability:accountable-item @card"), excerpt(r"^attribute stake"), excerpt(r"^attribute status")],
         "live": [{"label": "Constraints declared (keys, cardinalities, value lists, ranges)",
                   "value": f"{rules['keys']} · {rules['cardinalities']} · {rules['value lists']} · {rules['ranges']}"},
                  {"label": "Data-quality checks (negation queries) finding a problem now", "value": f"{sum(1 for found in checks.values() if found)} of {len(checks)}"}],
         "note": "Apex only reads, so a rejected write cannot be shown in the app. The optional Studio steps in "
                 "docs/demo-script.md (T1) show TypeDB refusing five bad writes, and the automated tests check the same.",
         "links": [{"label": "The data checks (Attention needed) on the Overview", "to": "/"},
                   {"label": "Deadline-critical chain to the Helix waiver", "to": "/?cp=DL-HELIX-WAIVER"}]},
        {"key": "polymorphism", "title": "Polymorphism",
         "statement": "Queries ask for a parent type and receive every kind beneath it, so a new kind of item or event "
                      "appears in existing screens without changing their queries.",
         "schema": [excerpt(r"^entity obligation"), excerpt(r"^relation engagement")],
         "live": [{"label": f"Types under deal-item, all answered by one pattern (`$x isa deal-item` finds {items} records)",
                   "value": ", ".join(t for t in item_types if t != "deal-item")},
                  {"label": "Event types; engagement is read from the type hierarchy, not listed in code",
                   "value": f"{', '.join(t for t in event_types if t not in ('deal-event', 'engagement', 'outreach'))}; "
                            f"engagement: {', '.join(t for t in engagement_types if t != 'engagement')}"}],
         "links": [{"label": "A workstream's activity history", "to": "/workstreams/WS-REG"},
                   {"label": "How engagement is assessed", "to": "/parties"}]},
        {"key": "application-complexity", "title": "Application complexity",
         "statement": "In Apex the model's meaning lives in the schema and saved functions; the Python adds dates, ranks "
                      "and formats, and the browser only displays answers.",
         "schema": [excerpt(r"^relation dependency"), excerpt(r"^relation hard-dependency")],
         "live": [{"label": "Rules and saved queries held by the database", "value": f"{rules['keys'] + rules['cardinalities'] + rules['value lists'] + rules['ranges']} constraints, {rules['functions']} functions"},
                  {"label": "Which dependency kinds move dates", "value": "read from the schema (`$k sub hard-dependency`), not configured"}],
         "note": "Calculations done in Python are stated where they appear: date arithmetic for projections, duration-"
                 "weighted progress, schedule float, engagement and path ranking.",
         "links": [{"label": "Open any panel's Show TypeQL", "to": "/"},
                   {"label": "The guided demonstration", "to": "/guide"}]},
        {"key": "ai-context", "title": "Structured context for AI",
         "statement": "Every Apex answer arrives as typed records with stable codes, the exact queries that produced it and, "
                      "for findings, the records that support them: material an AI assistant could cite rather than guess.",
         "schema": [excerpt(r"^relation reply")],
         "live": [{"label": "What each answer carries", "value": "data, the executed TypeQL, capability labels, timing"}],
         "note": "Apex runs no AI model. The AI Analyst page is a concept preview built from deterministic queries.",
         "links": [{"label": "AI Analyst (concept preview)", "to": "/analyst"},
                   {"label": "A finding with its records", "to": "/parties/PER-DONNELLY"}]},
    ]}
