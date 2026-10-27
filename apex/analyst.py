"""AI Analyst: a concept preview (/api/analyst). No AI model, chatbot, free text or agent runtime.

Each investigation is a fixed sequence of steps. "Executed" steps call Apex's existing query modules in one READ
transaction and record exactly which queries they ran; "hypothetical" steps describe what an AI agent could do
next with those results. They are written in advance, filled in from the executed results, and labelled as such.
"""

from apex import impact, parties, queries

INVESTIGATIONS = {
    "closing-risks": ("What could stop this deal closing?",
                      "Open conditions, the deadlines they put at risk, the risks recorded against them, and how their owners are engaging."),
    "who-acts": ("Who needs to act now?",
                 "Overdue and imminent responses, near-term responsibilities and deal-risk flags, each with its supporting records."),
    "regulatory-delay": ("What if the CMA review runs long?",
                         "The consequences of delaying the at-risk clearance, from the recursive dependency walk, and the work that could shorten it."),
}


class Steps:
    def __init__(self, tx, as_of):
        self.tx, self.as_of, self.out = tx, as_of, []

    def executed(self, title, how, work):
        start = len(self.tx.queries)
        findings = work()
        self.out.append({"kind": "executed", "title": title, "how": how, "findings": findings,
                         "queries": list(range(start, len(self.tx.queries)))})
        return findings

    def hypothetical(self, title, how, findings):
        self.out.append({"kind": "hypothetical", "title": title, "how": how, "findings": findings, "queries": []})


def _item_link(code, items):
    ws = items[code]["workstream"]
    return {"label": items[code]["name"], "to": f"/workstreams/{ws}?item={code}" if ws else f"/workstreams/{code}"}


def _person(code, names):
    return {"label": names.get(code, code), "to": f"/parties/{code}"}


def run(tx, key):
    if key not in INVESTIGATIONS:
        raise KeyError(key)
    title, question = INVESTIGATIONS[key]
    deal = queries.deal(tx)
    steps = Steps(tx, deal["as_of"])
    {"closing-risks": _closing_risks, "who-acts": _who_acts, "regulatory-delay": _regulatory_delay}[key](tx, steps)
    return {"key": key, "title": title, "question": question, "as_of": deal["as_of"], "steps": steps.out,
            "investigations": [{"key": k, "title": t} for k, (t, _) in INVESTIGATIONS.items()]}


def _floats(tx):
    items = queries.all_items(tx)
    floats = impact.schedule_float(items, queries.all_dependencies(tx), queries.all_deadline_limits(tx),
                                   set(queries.hard_dependency_kinds(tx)))
    return items, floats, impact.critical_paths(floats)


def _names(tx):
    return {c: t["name"] for c, t in queries.connectables(tx).items()}


def _closing_risks(tx, steps):
    open_ = steps.executed(
        "Find the conditions to completion that are not yet satisfied",
        "The parent type `condition` returns every clearance, approval and court sanction in one query.",
        lambda: [{"text": f"{c['name']}: {c['status'].replace('-', ' ')}, due {c['due']}", "links": [], "code": c["code"]}
                 for c in queries.conditions(tx) if c["status"] != "complete"])
    state = {}

    def exposure():
        items, floats, critical = _floats(tx)
        state.update(items=items, critical=critical)
        return [{"text": f"{items[dl]['name']} ({items[dl]['due']}) can absorb {cp['float']} days of delay; its deadline-critical chain: "
                         + " → ".join(items[c]["name"] for c in cp["items"]),
                 "links": [{"label": "Show on the Gantt", "to": f"/?cp={dl}"}]}
                for dl, cp in sorted(critical.items(), key=lambda kv: kv[1]["float"])]
    steps.executed("Measure each contractual deadline's exposure and deadline-critical chain",
                   "Schedule float from the Impact Explorer's projection over every hard dependency (one calculation for all screens).",
                   exposure)
    items = state["items"]
    on_path = {c for cp in state["critical"].values() for c in cp["items"]} | {f["code"] for f in open_}

    def risks():
        found = {}
        for ws_risks in queries.workstream_risks(tx).values():
            for r in ws_risks:
                if r["status"] != "closed" and set(r["items"]) & on_path:
                    found[r["code"]] = r
        return [{"text": f"{r['name']} ({r['severity']} severity, {r['likelihood']} likelihood), owned by {r['owner']['name']}",
                 "links": [_item_link(c, items) for c in r["items"] if c in on_path][:2]} for r in found.values()]
    found_risks = steps.executed("Find the open risks recorded against those items",
                                 "risk-exposure links each risk to the items it threatens; risk-ownership gives its one owner.", risks)

    def owners():
        ctx = parties._context(tx)
        names = {p["code"]: p["name"] for p in ctx["people"]}
        holders = sorted({r["holder"] for r in ctx["raci"] if r["item"] in on_path and r["letter"] in "AR" and r["holder"] in names})
        out, quiet = [], 0
        for h in holders:
            party, staff = parties._party_of(ctx, h)
            a = parties._assess(ctx, party, staff)
            if a["level"] in ("red", "amber"):
                out.append({"text": f"{names[h]}: {a['level']} ({a['category'].replace('-', ' ')}). {a['headline']}",
                            "links": [_person(h, names)]})
            else:
                quiet += 1
        out.append({"text": f"{quiet} other Accountable or Responsible people on these items are green, not yet active, "
                            "or have nothing to assess.", "links": []})
        return out
    steps.executed("Check how the people accountable and responsible for them are engaging",
                   "The engagement policy (config/engagement-policy.yaml) over requests, meetings and other events, item by item.",
                   owners)
    tightest = min(state["critical"].items(), key=lambda kv: kv[1]["float"])
    steps.hypothetical(
        "Brief the deal team",
        "An AI agent could turn the findings above into a short briefing and suggested next steps, citing each record. "
        "Here the sentence is a fixed template filled with the executed results.",
        [{"text": f"The tightest exposure is {items[tightest[0]]['name']}: {tightest[1]['float']} days of float, driven by "
                  f"{items[tightest[1]['items'][0]]['name']}. {len(found_risks)} open risks are recorded against the items that "
                  "decide closing. Suggested next step: confirm the remedy plan and the Accountable sign-off on the CMA response.",
          "links": [{"label": "Open the Impact Explorer", "to": f"/impact?item={tightest[1]['items'][0]}"}]}])


def _who_acts(tx, steps):
    state = {}

    def read():
        ctx = parties._context(tx)
        names = {p["code"]: p["name"] for p in ctx["people"]} | {o["code"]: o["name"] for o in ctx["organisations"]}
        found = []
        for code in names:
            party, staff = parties._party_of(ctx, code)
            for a in parties._assess(ctx, party, staff)["assessments"]:
                found.append((code, a))
        state.update(ctx=ctx, names=names, found=found)
        return [{"text": f"{sum(1 for _, a in found if a['level'] in ('red', 'amber'))} red or amber findings across "
                         f"{len({c for c, a in found if a['level'] in ('red', 'amber')})} people and organisations.", "links": []}]
    steps.executed("Read every request, reply, event and responsibility, and apply the engagement policy",
                   "One READ transaction: people and organisations, RACI on the parent type, every event with its replies, the "
                   "schema's engagement and outreach types, authority reviews, and the dependency data for schedule float.", read)
    names, found = state["names"], state["found"]

    def of(category):
        return [{"text": f"{names[c]}: {a['reason']}", "links": [_person(c, names)]} for c, a in found if a["category"] == category]
    for title, category, how in (
            ("Find responses that are overdue", "overdue-response",
             "Requests whose reply-by date has passed with no reply relation recorded: a fact, not an inference."),
            ("Find responses due in the next two days", "response-due", "The same requests, with reply-by dates inside the policy's warning window."),
            ("Find deal-risk flags", "deal-risk", "Inactivity on an item that is schedule-critical or recorded as critical, and due soon or at risk.")):
        steps.out.append({"kind": "executed", "title": title, "how": how + " Uses the records read in step 1.",
                          "findings": of(category), "queries": []})

    def due_soon():
        items = queries.all_items(tx)
        raci = state["ctx"]["raci"]
        out = []
        for code, it in sorted(items.items(), key=lambda kv: kv[1]["due"] or ""):
            if it["status"] == "complete" or it["type"] in ("deadline", "workstream") or not it["due"]:
                continue
            days = (_date(it["due"]) - _date(steps.as_of)).days
            if 0 <= days <= 14:
                who = sorted(r["holder"] for r in raci if r["item"] == code and r["letter"] in "AR" and r["holder"] in names)
                out.append({"text": f"{it['name']}: due in {days} days; " + ", ".join(names[h] for h in who),
                            "links": [_item_link(code, items)] + [_person(h, names) for h in who][:2]})
        return out
    steps.executed("Find Accountable and Responsible people on items due within 14 days",
                   "Each item's due date, joined to the RACI letters read in step 1.", due_soon)
    overdue = [(c, a) for c, a in found if a["category"] == "overdue-response"]
    steps.hypothetical(
        "Draft reminders for a person to review",
        "An AI agent could draft one reminder per overdue response, citing the request. Apex sends nothing: it is read-only "
        "and has no messaging. Shown: the subject line a draft would carry, filled from the request record.",
        [{"text": f"Draft subject for {names[c]}: “Response overdue: {a['evidence'][0]['label']}”", "links": [_person(c, names)]}
         for c, a in overdue])


def _regulatory_delay(tx, steps):
    state = {}

    def subject():
        code = impact.default_subject(tx)
        items = queries.all_items(tx)
        state.update(code=code, items=items)
        return [{"text": f"{items[code]['name']}: {items[code]['status'].replace('-', ' ')}, due {items[code]['due']}",
                 "links": [_item_link(code, items)]}]
    steps.executed("Pick the condition recorded as at risk",
                   "The first open condition whose status is at risk or delayed, by due date.", subject)
    code, items = state["code"], state["items"]

    def first_break():
        base = impact.scenario(tx, code, 0)
        breaks = sorted(d["breach_from_days"] for d in base["deadlines"] if d.get("breach_from_days"))
        days = breaks[0]
        result = impact.scenario(tx, code, days)
        state.update(days=days, breaks=breaks)
        broken = [d["name"] for d in result["deadlines"] if d["breached"]]
        later = {d["name"]: d["breach_from_days"] for d in base["deadlines"] if d.get("breach_from_days") and d["name"] not in broken}
        return [{"text": f"The first deadline breaks at {days} days of delay: {', '.join(broken)}. "
                         f"{result['summary']['moved']} items move, and {len(result['summary']['people'])} people hold a RACI letter on affected items.",
                 "links": [{"label": f"Open the {days}-day scenario", "to": f"/impact?item={code}&days={days}"}]},
                {"text": "Later breaking points: " + "; ".join(f"{n} at {v} days" for n, v in sorted(later.items(), key=lambda kv: kv[1])) + ".",
                 "links": [{"label": f"Open the {breaks[-1]}-day scenario", "to": f"/impact?item={code}&days={breaks[-1]}"}]}]
    steps.executed("Find where the delay breaks each contractual deadline",
                   "The recursive impact_edges function walks every hard dependency to any depth; each breaking point is found by search.",
                   first_break)

    def mitigations():
        deps = queries.all_dependencies(tx)
        raci = queries.all_raci(tx)
        names = _names(tx)
        out = []
        for d in deps:
            if d["to"] == code and d["kind"] == "informing" and items[d["from"]]["status"] != "complete":
                src = d["from"]
                owners = sorted(r["holder"] for r in raci if r["item"] == src and r["letter"] in "AR")
                out.append({"text": f"{items[src]['name']} informs {items[code]['name']}; led by "
                                    + ", ".join(names.get(o, o) for o in owners),
                            "links": [_item_link(src, items)] + [_person(o, names) for o in owners][:2]})
        return out
    found = steps.executed("Find open work that feeds the clearance",
                           "Dependencies into the clearance of the informing kind: work whose result shapes it without fixing its date.",
                           mitigations)
    steps.hypothetical(
        "Compare options",
        "An AI agent could weigh the options against the breaking points, for example whether an undertakings-in-lieu offer "
        "could land inside the first threshold, and cite the records it relied on. Nothing here is generated.",
        [{"text": f"Question it would test: can the work above settle the CMA's concern within {state['days'] - 1} days of extra time?",
          "links": found[0]["links"][:1] if found else []}])


def _date(value):
    from datetime import date
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
