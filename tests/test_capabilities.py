"""The "Show TypeQL" capability labels: detected from the queries that actually ran, never claimed."""

import re

import pytest
from fastapi.testclient import TestClient

from apex import api, capabilities
from apex.settings import load_settings

# Read by hand from schema/functions.tql: these call themselves, or (ownership_cycles) call one that does.
RECURSIVE = {"downstream", "impact_edges", "upstream", "group_members", "owners_above", "ownership_cycles"}

URLS = ["/api/deal", "/api/approvals", "/api/activity", "/api/parties", "/api/progress", "/api/raci?workstream=WS-REG",
        "/api/checks", "/api/ownership", "/api/graph", "/api/graph/node/PER-SHAW", "/api/impact?delay_days=61",
        "/api/schedule", "/api/items/CP-CMA"]


@pytest.fixture(scope="module")
def answers(seeded):
    with TestClient(api.create_app(load_settings(apex_database=seeded))) as c:
        yield {u: c.get(u).json() for u in URLS}


def test_recursive_functions_are_read_from_the_schema():
    assert capabilities.recursive_functions() == RECURSIVE           # accountable_for is not recursive


def test_recursion_comes_from_the_call_graph_not_from_any_call():
    defs = """
    define
    fun plain($x: company) -> { company }: match ownership (owner: $x, owned: $y); return { $y };
    fun twice($x: company) -> { company }: match let $y in plain($x); return { $y };
    fun loop_a($x: company) -> { company }: match { let $y in loop_b($x); } or { ownership (owner: $x, owned: $y); }; return { $y };
    fun loop_b($x: company) -> { company }: match let $y in loop_a($x); return { $y };
    fun uses_loop($x: company) -> { company }: match let $y in loop_a($x); return { $y };
    """
    assert capabilities.recursive_in(defs) == {"loop_a", "loop_b", "uses_loop"}     # plain and twice are ordinary
    assert capabilities.function_calls(defs)["twice"] == {"plain"}


def test_calling_an_ordinary_function_is_not_labelled_recursive():
    q = 'match $i isa deal-item, has code "CP-CMA"; let $p = accountable_for($i); $p has name $n; fetch { "n": $n };'
    assert "Recursive functions" not in capabilities.detect([q])
    assert "Recursive functions" in capabilities.detect([q.replace("let $p = accountable_for($i)", "let $p in downstream($i)")])


def test_detection_on_small_examples():
    d = capabilities.detect
    assert d(["match $x isa deal-item; not { accountability (accountable-item: $x); };"]) == [
        "Polymorphism", "Negation", "Typed relationships"]
    assert d(['match $m isa mandate (client: $c, adviser: $a, engagement: $d);']) == ["N-ary relationships", "Typed relationships"]
    assert d(['match $s isa company, has code "X"; let $c in owners_above($s);']) == ["Recursive functions"]
    # Words inside strings and comments are not query features.
    assert d(['match $x isa company, has name "not { downstream(x) }"; # reduce count']) == []


def test_every_answer_carries_its_labels(answers):
    for url, body in answers.items():
        assert body["capabilities"] == capabilities.detect(body["typeql"]), url
        assert body["query_capabilities"] == [capabilities.detect([q]) for q in body["typeql"]], url


@pytest.mark.parametrize("url", URLS)
def test_recursion_is_claimed_only_when_a_recursive_function_is_called(answers, url):
    body = answers[url]
    calls = any(re.search(rf"\b{f}\s*\(", q) for q in body["typeql"] for f in RECURSIVE)
    assert ("Recursive functions" in body["capabilities"]) == calls


def test_expected_labels_on_key_panels(answers):
    labels = {u: set(b["capabilities"]) for u, b in answers.items()}
    assert "Recursive functions" in labels["/api/impact?delay_days=61"] and "Recursive functions" in labels["/api/ownership"]
    assert "Recursive functions" not in labels["/api/deal"] | labels["/api/schedule"] | labels["/api/checks"]
    assert "Negation" in labels["/api/checks"] and "Negation" not in labels["/api/schedule"]
    assert "N-ary relationships" in labels["/api/parties"]              # mandate: client, adviser, engagement
    assert "Aggregation" in labels["/api/progress"]                      # count groupby
    assert "Nested queries" in labels["/api/items/CP-CMA"]
