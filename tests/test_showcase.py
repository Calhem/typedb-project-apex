"""Phase 6F showcase: the TypeDB Advantage panel, the AI Analyst concept preview and the guided demonstration.
Every claim must be backed by the schema, the data or a working view; nothing may be invented."""

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apex import api
from apex.settings import load_settings
from oracle import raw_seed

ROOT = Path(__file__).resolve().parent.parent
SEED = raw_seed()
ROUTES = ("/", "/workstreams", "/parties", "/explorer", "/impact", "/analyst", "/guide")
COMPARATIVE = re.compile(r"\b(faster|slower|better than|worse than|unlike|compared with|outperform|times (faster|quicker)|only TypeDB)\b", re.I)


@pytest.fixture(scope="module")
def client(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield c


def links_of(obj):
    if isinstance(obj, dict):
        return [obj["to"]] if set(obj) == {"label", "to"} else [x for v in obj.values() for x in links_of(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in links_of(v)]
    return []


def valid_route(to):
    path = to.split("?")[0].split("#")[0]
    return path == "/" or any(path == r or path.startswith(r + "/") for r in ROUTES[1:])


def test_advantage_has_six_sections_with_real_evidence(client):
    body = client.get("/api/advantage").json()
    sections = body["data"]["sections"]
    assert [s["key"] for s in sections] == ["typed-relationships", "recursive-reasoning", "schema-constraints",
                                            "polymorphism", "application-complexity", "ai-context"]
    for s in sections:
        assert s["schema"] and s["links"] and s["statement"]
        for ex in s["schema"]:                                   # quoted verbatim from the file, at that line
            lines = (ROOT / ex["file"]).read_text().splitlines()
            assert "\n".join(lines[ex["line"] - 1: ex["line"] - 1 + len(ex["text"].splitlines())]) == ex["text"]
        for text in [s["statement"], s.get("note") or "", *[f["label"] for f in s["live"]]]:
            assert not COMPARATIVE.search(text), (s["key"], text)
        assert all(valid_route(to) for to in links_of(s["links"]))
    facts = {f["label"]: f["value"] for s in sections for f in s["live"]}
    assert facts["Mandates stored, each relating client, adviser and the deal"] == str(len(SEED["mandates"]))
    assert facts["Saved functions in the schema"] == str(len(re.findall(r"^fun ", (ROOT / "schema" / "functions.tql").read_text(), re.M)))
    reach = next(v for k, v in facts.items() if k.startswith("Items a delay to"))
    assert reach == "17"                                         # the Impact Explorer's downstream count for CP-CMA
    assert body["typeql"] and not any(w in q for q in body["typeql"] for w in ("insert", "delete", "define"))


@pytest.mark.parametrize("key", ["closing-risks", "who-acts", "regulatory-delay"])
def test_each_investigation_separates_executed_from_hypothetical(client, key):
    body = client.get(f"/api/analyst/{key}").json()
    steps = body["data"]["steps"]
    kinds = [s["kind"] for s in steps]
    assert kinds[-1] == "hypothetical" and kinds.count("hypothetical") == 1 and "executed" in kinds
    assert any(s["queries"] for s in steps if s["kind"] == "executed")
    for s in steps:
        assert all(0 <= i < len(body["typeql"]) for i in s["queries"])
        if s["kind"] == "hypothetical":
            assert s["queries"] == [] and ("AI agent" in s["how"])
        assert all(valid_route(to) for to in links_of(s["findings"]))
    assert [i["key"] for i in body["data"]["investigations"]] == ["closing-risks", "who-acts", "regulatory-delay"]


def test_investigation_findings_match_the_records(client):
    who = client.get("/api/analyst/who-acts").json()["data"]["steps"]
    overdue = next(s for s in who if s["title"] == "Find responses that are overdue")
    assert {l["to"] for f in overdue["findings"] for l in f["links"]} == {"/parties/PER-DONNELLY", "/parties/PER-OSULLIVAN"}
    delay = client.get("/api/analyst/regulatory-delay").json()["data"]["steps"]
    text = " ".join(f["text"] for s in delay for f in s["findings"])
    assert "breaks at 61 days" in text and "120 days" in text and "136 days" in text
    risks = client.get("/api/analyst/closing-risks").json()["data"]["steps"]
    assert "can absorb 60 days" in risks[1]["findings"][0]["text"]
    assert client.get("/api/analyst/nope").status_code == 404


def test_no_ai_model_or_external_api_anywhere():
    code = "\n".join(p.read_text() for p in (ROOT / "apex").glob("*.py"))
    deps = (ROOT / "pyproject.toml").read_text() + (ROOT / "web" / "package.json").read_text()
    for word in ("anthropic", "openai", "langchain", "requests.post", "httpx.post", "urllib.request"):
        assert word not in code.lower() and word not in deps.lower(), word


def test_guide_figures_match_the_live_answers(client):
    steps = client.get("/api/guide").json()["data"]["steps"]
    assert len(steps) == 8 and all(valid_route(s["to"]) for s in steps)
    text = " ".join(s["say"] + " " + " ".join(s["show"]) for s in steps)
    chart = client.get("/api/schedule").json()["data"]
    helix = next(d for d in chart["deadlines"] if d["code"] == "DL-HELIX-WAIVER")
    assert f"absorb {helix['critical_path']['float']} days" in text and len(helix["critical_path"]["items"]) == 3
    breaks = sorted(d["breach_from_days"] for d in client.get("/api/impact?item=CP-CMA").json()["data"]["deadlines"])
    assert f"{breaks[0]}, {breaks[1]} and {breaks[2]} days" in text
    moved = client.get("/api/impact?item=CP-CMA&delay_days=61").json()["data"]["summary"]["moved"]
    assert f"{moved} items move" in text
    group = client.get("/api/ownership").json()["data"]
    ip = next(c for c in group["companies"] if c["code"] == "ORG-ATL-IP")
    assert Decimal(ip["stake"]) == Decimal("0.9") and ip["routes"] == 2 and "90% effective ownership over two routes" in text
    status = client.get("/api/deal").json()["data"]["status"]
    outstanding = status["conditions_total"] - status["conditions_satisfied"]
    assert f"{['zero', 'one', 'two', 'three'][outstanding]} of {['', '', '', '', '', '', 'six'][status['conditions_total']]} conditions outstanding" in text


def test_show_typeql_uses_the_typedb_purple():
    css = (ROOT / "web" / "src" / "index.css").read_text().lower()
    assert all(h in css for h in ("#f5f1fc", "#ddd1f2", "#65459a"))
    drawer = (ROOT / "web" / "src" / "components" / "typeql.tsx").read_text()
    assert "bg-typedb-tint" in drawer and "border-typedb-line" in drawer and "text-typedb" in drawer


def test_deadline_chains_are_not_called_critical_paths(client):
    """The chain is measured against a contractual deadline, so screens and scripts say "deadline-critical chain";
    "critical path" may appear only to explain that it is not a conventional one."""
    allowed = re.compile(r"not a conventional (\(CPM\) )?critical path", re.I)
    texts = [client.get(u).text for u in ("/api/guide", "/api/advantage", "/api/analyst/closing-risks")]
    texts += [p.read_text() for p in (ROOT / "web" / "src").rglob("*.tsx")]
    found = [m for t in texts for m in re.findall(r"[^\n]{0,40}critical path[^\n]{0,40}", allowed.sub("", t), re.I)]
    assert found == []
