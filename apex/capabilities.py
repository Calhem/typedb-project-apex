"""Which TypeDB capabilities an answer's queries actually use ("Show TypeQL" line 1).

Labels are detected from the exact TypeQL that ran (RecordingTransaction), checked against the schema
files themselves, so a panel is never labelled with something its queries don't do:

- Recursive functions: a query calls a saved function whose definition recurses (a cycle in the schema's
  call graph, or a call into one). Calling an ordinary function is not enough.
- N-ary relationships: a relation pattern names three or more role players.
- Polymorphism: a query matches a type that has subtypes (one pattern finds every kind), or walks the
  type hierarchy (`sub`).
- Negation: `not { ... }`.
- Aggregation: `reduce` (count, sum, ...).
- Typed relationships: a relation pattern with named roles.
- Nested queries: a fetch with a subquery.
"""

import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORDER = ("Recursive functions", "N-ary relationships", "Polymorphism", "Negation", "Aggregation",
         "Typed relationships", "Nested queries")

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"#[^\n]*")
_RELATION = re.compile(r"\b([a-z][\w-]*)\s*\(([^()]*)\)")
_ROLE = re.compile(r"\b[a-z][\w-]*\s*:\s*\$")
_ISA = re.compile(r"\bisa\s+([a-z][\w-]*)")


def _clean(text):
    return _COMMENT.sub("", _STRING.sub('""', text))


def _schema_text():
    files = [ROOT / "schema" / "schema.tql", *sorted((ROOT / "schema" / "migrations").glob("[0-9][0-9][0-9]-*.tql"))]
    return "\n".join(f.read_text() for f in files)


@cache
def parent_types():
    """Types that have at least one subtype, read from the schema files."""
    return frozenset(re.findall(r"^\s*(?:entity|relation)\s+[\w-]+\s+sub\s+([\w-]+)", _clean(_schema_text()), re.M))


def function_calls(text):
    """{function: {functions its body calls}} for every `fun` defined in the given TypeQL."""
    text = _clean(text)
    bodies = dict(re.findall(r"^\s*fun\s+([\w-]+)\s*\((.*?)(?=^\s*fun\s|^\s*(?:re)?define\b|\Z)", text, re.M | re.S))
    return {name: {other for other in bodies if re.search(rf"\b{re.escape(other)}\s*\(", body)}
            for name, body in bodies.items()}


def recursive_in(text):
    """Functions whose evaluation recurses: a cycle in the call graph (a function calling itself, directly
    or through others), or a call into such a function. An ordinary function is never included."""
    calls = function_calls(text)

    def reaches(start):
        seen, todo = set(), list(calls[start])
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                todo += calls.get(name, ())
        return seen

    on_cycle = {name for name in calls if name in reaches(name)}
    return frozenset(name for name in calls if name in on_cycle or reaches(name) & on_cycle)


@cache
def recursive_functions():
    """recursive_in() over every function the schema defines (functions.tql and any migration)."""
    files = [ROOT / "schema" / "functions.tql", *sorted((ROOT / "schema" / "migrations").glob("[0-9][0-9][0-9]-*.tql"))]
    return recursive_in("\n".join(f.read_text() for f in files))


def detect(queries):
    """The capability labels the given queries genuinely use, in a fixed order."""
    found = set()
    parents = parent_types()
    recursive = recursive_functions()
    for raw in queries:
        q = _clean(raw)
        if any(re.search(rf"\b{re.escape(f)}\s*\(", q) for f in recursive):
            found.add("Recursive functions")
        for rel_type, inside in _RELATION.findall(q):
            roles = len(_ROLE.findall(inside))
            if roles >= 1:
                found.add("Typed relationships")
                if rel_type in parents:
                    found.add("Polymorphism")
            if roles >= 3:
                found.add("N-ary relationships")
        if any(t in parents for t in _ISA.findall(q)) or re.search(r"\$[\w-]+\s+sub!?\s+", q):
            found.add("Polymorphism")
        if re.search(r"\bnot\s*\{", q):
            found.add("Negation")
        if re.search(r"\breduce\b", q):
            found.add("Aggregation")
        if re.search(r"\bfetch\b.*[\[(]\s*match\b", q, re.S):
            found.add("Nested queries")
    return [label for label in ORDER if label in found]
