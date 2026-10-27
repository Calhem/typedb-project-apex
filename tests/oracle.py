"""Independent answer keys, computed straight from data/seed/*.yaml with plain Python.

Deliberately shares NO code with apex/ (it reads the YAML itself and uses different
algorithms), so a bug in the app's queries or calculations can't hide in both places.
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


def raw_seed():
    """Every YAML file under data/seed, merged: lists concatenated, mappings combined."""
    seed = {}
    for path in sorted(SEED_DIR.rglob("*.yaml")):
        for key, value in (yaml.safe_load(path.read_text()) or {}).items():
            if isinstance(value, list):
                seed[key] = seed.get(key, []) + value
            elif isinstance(value, dict) and key != "deal":
                seed[key] = {**seed.get(key, {}), **value}
            else:
                seed[key] = value
    return seed


def schema_text():
    """The schema as TypeDB loads it: the base file, then the numbered migrations."""
    root = SEED_DIR.parent.parent / "schema"
    return "\n".join(p.read_text() for p in [root / "schema.tql"] + sorted((root / "migrations").glob("[0-9]*.tql")))


def ownership_routes(seed, parent):
    """Every route down from `parent`: {company: [product of stakes along each route]}.

    Depth-first enumeration of every path; raises if a company is revisited on one path (a cycle).
    """
    children = {}
    for own in seed["ownership"]:
        children.setdefault(own["owner"], []).append((own["owned"], Decimal(own["stake"])))
    routes = {}

    def walk(company, product, path):
        for child, stake in children.get(company, []):
            if child in path:
                raise ValueError(f"cycle: {' -> '.join(path + [child])}")
            routes.setdefault(child, []).append(product * stake)
            walk(child, product * stake, path + [child])

    walk(parent, Decimal(1), [parent])
    return routes


def effective_stakes(seed, parent):
    return {c: (sum(products, Decimal(0)), len(products)) for c, products in ownership_routes(seed, parent).items()}


def projected_dates(seed, start, delay_days):
    """Relax every hard dependency until nothing changes (Bellman-Ford style)."""
    items = {i["code"]: i for i in seed["items"] + seed["workstreams"]}
    dates = {code: it["due"] for code, it in items.items()}
    done = {code for code, it in items.items() if it["status"] == "complete"}
    if start not in done:
        dates[start] = dates[start] + timedelta(days=delay_days)
    hard = [d for d in seed["dependencies"] if d["kind"] in ("gating", "sequencing")]
    for _ in range(len(items) + 1):
        changed = False
        for d in hard:
            if d["to"] in done:
                continue
            candidate = dates[d["from"]] + timedelta(days=d["lag"])
            if candidate > dates[d["to"]]:
                dates[d["to"]] = candidate
                changed = True
        if not changed:
            return dates
    raise ValueError("dates never settled: hard dependencies contain a cycle")


def breached_deadlines(seed, dates):
    """{(deadline, item): days over} for every deadline an item would miss."""
    due = {i["code"]: i["due"] for i in seed["items"]}
    return {(dl["deadline"], dl["item"]): (dates[dl["item"]] - due[dl["deadline"]]).days
            for dl in seed["deadline_limits"] if dates[dl["item"]] > due[dl["deadline"]]}


def derived_progress(items):
    """Duration-weighted completion and milestone readiness, recomputed from raw YAML items (D5)."""
    spans = [(i, (i["due"] - i["planned_start"]).days + 1) for i in items
             if i.get("type") != "deadline" and i.get("planned_start")]
    points = [i for i in items if i.get("type") != "deadline" and not i.get("planned_start")]
    weight = sum(days for _, days in spans)
    done = sum(days * (100 if i["status"] == "complete" else i.get("progress", 0)) for i, days in spans)
    return {"percent": round(done / weight) if weight else None, "activities": len(spans),
            "achieved": sum(i["status"] == "complete" for i in points), "points": len(points)}
