"""The frontend holds no business data: no seed names, codes, dates, amounts or text in its source
or in the built bundle. Everything on screen has to come from TypeDB through the API.

(The product itself is called "Project Apex", the same as the fictional deal, so that one name is
allowed in the page title. The app's header takes the deal name from the API.)
"""

import re
from pathlib import Path

import pytest

from oracle import raw_seed

WEB = Path(__file__).resolve().parent.parent / "web"
SOURCE_SUFFIXES = {".ts", ".tsx", ".css", ".html", ".json", ".svg"}


def _seed_values():
    seed = raw_seed()
    values = set()
    for section in ("organisations", "people", "mandates", "financing", "workstreams", "items"):
        for record in seed[section]:
            values.add(record["code"])
            for field in ("name", "description", "consequence", "job_title"):
                if isinstance(record.get(field), str) and len(record[field]) > 6:
                    values.add(record[field])
            for field in ("due", "planned_start"):
                if record.get(field):
                    values.add(record[field].isoformat())
            if record.get("value"):
                values.add(record["value"])
    for event in seed["events"]:
        values.add(event["summary"])
    deal = seed["deal"]
    values |= {deal["code"], deal["description"], deal["value"], deal["as_of"].isoformat(), deal["structure"]}
    # Generic words that legitimately appear in UI chrome are not business data.
    return {v for v in values if v not in {"company", "Regulatory", "Financing", "milestone"}}


def _files(root, suffixes=None):
    for path in root.rglob("*"):
        if path.is_file() and (suffixes is None or path.suffix in suffixes):
            yield path


def _offending(paths, values):
    """Seed values appearing as whole words. (Library code contains fragments such as the hex
    constant 0x800000000000 or the name RoutingDirector, which are not business data.)"""
    patterns = {v: re.compile(r"(?<![\w])" + re.escape(v) + r"(?![\w])") for v in values}
    found = []
    for path in paths:
        text = path.read_text(errors="ignore")
        found += [f"{path.relative_to(WEB)}: {v!r}" for v, pat in patterns.items() if pat.search(text)]
    return found


def test_frontend_source_has_no_seed_data():
    src = [p for p in _files(WEB / "src", SOURCE_SUFFIXES)] + [WEB / "index.html"]
    assert len(src) > 10
    assert _offending(src, _seed_values()) == []


def test_built_bundle_has_no_seed_data():
    dist = WEB / "dist"
    if not dist.exists():
        pytest.skip("frontend not built (scripts/npm-docker.sh npm run build)")
    bundle = [p for p in _files(dist) if p.suffix in {".js", ".css", ".html"}]
    assert bundle
    assert _offending(bundle, _seed_values()) == []


def test_impact_explorer_has_no_scenario_outcomes():
    """The thresholds (61, 120, 136 days...), the presets and the scenario's outcomes all come from
    /api/impact. None of them, and none of the delay figures, may be written into the screen's code."""
    files = list((WEB / "src" / "components" / "impact").glob("*.tsx")) + [WEB / "src" / "pages" / "Impact.tsx"]
    assert len(files) >= 5
    numbers = re.compile(r"(?<![\w.#/-])(28|60|61|119|120|135|136|149|150|168)(?![\w%])")
    words = ("Helix", "long-stop", "Long-stop", "certain funds", "CMA", "CP-CMA", "Phase 2")
    found = []
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("//")[0]
            if numbers.search(code) or any(w in code for w in words):
                found.append(f"{path.name}:{n}: {line.strip()[:80]}")
    assert found == []


def test_schedule_never_uses_the_real_date():
    """Every "due in", "overdue" and the date line come from the scenario date stored in TypeDB
    (docs/phase6-plan.md §4); the Gantt chart and the item panel never read the browser's clock."""
    files = [*(WEB / "src" / "components" / "gantt").glob("*.ts*"), *(WEB / "src" / "components" / "parties").glob("*.ts*"),
             *(WEB / "src" / "components" / "workstreams").glob("*.ts*"),
             *[WEB / "src" / "pages" / f for f in ("Parties.tsx", "Party.tsx", "Workstreams.tsx", "Workstream.tsx")]]
    assert len(files) >= 9
    clock = re.compile(r"Date\.now\(|new Date\(\s*\)|performance\.now\(")
    assert [f.name for f in files if clock.search(f.read_text())] == []
    assert "as_of" in (WEB / "src" / "components" / "gantt" / "gantt.tsx").read_text()


def test_no_contact_value_in_the_frontend_source_or_bundle():
    """Contacts are masked on the server (decision D8): no full address or number may be in the frontend."""
    people = raw_seed()["people"]
    secrets = {p["email"] for p in people} | {p["phone"] for p in people}
    paths = [p for p in _files(WEB / "src", SOURCE_SUFFIXES)]
    if (WEB / "dist").exists():
        paths += [p for p in _files(WEB / "dist") if p.suffix in {".js", ".css", ".html"}]
    found = [f"{p.relative_to(WEB)}: {s}" for p in paths for s in secrets if s in p.read_text(errors="ignore")]
    assert found == []
